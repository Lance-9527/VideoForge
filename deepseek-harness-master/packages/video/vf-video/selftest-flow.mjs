// vf-video · 「确认 / 出框 / 跳过」自测（2026-10-26）
//
// 为什么要有这个文件：用户实机提了四条（重复询问、选项框在会话没结束时出现、
// 一个流程要点多次确认、流程要能跳跃）。这四条全是**行为**，必须能离线验：
//   ① 花钱步**没拿到用户同意**时：不出 job、给一张提案卡、并且说清"下一步该做什么"；
//   ② 花钱步**确认后**再跑：起 job，且**不再回提案卡**（那张卡确认前已经给过）；
//   ③ 免费步骤（assets/previz/compose）**不出卡** —— 不再一路弹框；
//      只有内容复核步（script/shots）出卡；
//   ④ vf_skip 一条命令 → POST /stage/skip，跳过后不再挡后面的步骤。
//
// 跑法：node selftest-flow.mjs

import assert from 'node:assert/strict'
import { makeToolSpecs, makeSkipStageSpec } from './lib/core.js'

const calls = []

function fakeFetchJson(path, init = {}) {
  calls.push({ path, method: init.method || 'GET', body: init.body })
  if (path === '/api/v2/run') {
    return Promise.resolve({ job_id: 'sidecar-job-1', stream: '/x', poll: '/y' })
  }
  if (path.includes('/cancel')) return Promise.resolve({ cancelled: true })
  if (path.includes('/stage/skip')) {
    return Promise.resolve({ skipped: 'previz', title: '白膜预演', next: { key: 'draft', title: '480P 小样' } })
  }
  if (path.includes('/stage/status')) {
    return Promise.resolve({
      stages: [{ key: 'draft', title: '480P 小样', status: 'current', evidence: { detail: '白膜已确认' } }],
      done_count: 5, total: 9,
    })
  }
  if (path.includes('/estimate')) {
    return Promise.resolve({ cost_yuan: 8.4, cost_floor_yuan: 8.4, cost_label: '按条计费', eta: '每条 2 ~ 8 分钟' })
  }
  return Promise.resolve({})
}

function fakeOpenStream() {
  return (async function* gen() {
    yield { event: 'hello', data: { job_id: 'sidecar-job-1' } }
    yield { event: 'done', data: { ok: true, result: { ok: true, summary: '完成' } } }
  })()
}

function makeStartJob() {
  const jobs = []
  return {
    jobs,
    startJob: (spec) => {
      const id = `job-${jobs.length + 1}`
      const handle = { updateProgress() {}, append() {} }
      const entry = { id, result: null }
      jobs.push(entry)
      Promise.resolve()
        .then(() => spec.run(handle))
        .then((r) => { entry.result = r })
        .catch((e) => { entry.result = { status: 'failed', detail: String(e) } })
      return id
    },
  }
}

const tick = () => new Promise((r) => setTimeout(r, 30))

function specs(stageKeys) {
  const { startJob, jobs } = makeStartJob()
  return {
    jobs,
    startJob,
    all: makeToolSpecs({
      fetchJson: fakeFetchJson,
      baseUrl: 'http://127.0.0.1:8765',
      startJob,
      pollMs: 10,
      openStream: fakeOpenStream,
      stageKeys,
    }),
  }
}

async function main() {
  // ── ① 花钱步没确认：不起 job、出提案卡、说清下一步 ──
  {
    const s = specs(['draft', 'assets', 'previz', 'compose', 'script', 'shots'])
    const draft = s.all.stageTools.find((t) => t.name === 'vf_draft')
    assert.ok(draft, '找不到 vf_draft')
    assert.ok(draft.parameters.confirm, '花钱步必须暴露 confirm 参数')

    calls.length = 0
    const res = await draft.execute({ project_id: 'p1' })
    assert.equal(res.ok, false, '没确认时不能算成功')
    assert.ok(res.card, '没确认时必须给一张提案卡（用户要看到成本与证据）')
    assert.equal(res.card.kind, 'gate')
    assert.equal(res.card.stage, 'draft')
    assert.ok(String(res.error).includes('没有开始'), 'error 要说清"没有开始"')
    assert.ok(String(res.next_action).includes('confirm=true'), 'next_action 要说清确认后怎么调')
    assert.ok(!calls.some((c) => c.path === '/api/v2/run'), '没确认时不该起 job（会花钱）')
    assert.equal(s.jobs.length, 0, '没确认时不该注册 DSH job')
    console.log('① 花钱步未确认：不出 job + 出提案卡 + 有下一步 OK')
  }

  // ── ② 确认后再跑：起 job，且不再回提案卡 ──
  {
    const s = specs(['draft'])
    const draft = s.all.stageTools.find((t) => t.name === 'vf_draft')
    calls.length = 0
    const res = await draft.execute({ project_id: 'p1', confirm: true })
    assert.equal(res.ok, true)
    assert.equal(res.card, null, '已经在跑的花钱步不该再回一张提案卡（确认前那张给过了）')
    const runCall = calls.find((c) => c.path === '/api/v2/run')
    assert.ok(runCall, '确认后必须 POST /api/v2/run')
    assert.equal(runCall.body.params.confirm, true, 'confirm 没带下去（后端硬门要它）')
    await tick()
    assert.ok(s.jobs.length >= 1, '确认后必须起 job')
    console.log('② 花钱步确认后：起 job + 不再回提案卡 OK')
  }

  // ── ③ 免费步骤不出卡；内容复核步出卡 ──
  {
    const s = specs(['assets', 'previz', 'compose', 'script', 'shots'])
    for (const key of ['assets', 'previz', 'compose']) {
      const tool = s.all.stageTools.find((t) => t.name === `vf_${key}`)
      assert.ok(tool, `找不到 vf_${key}`)
      const res = await tool.execute({ project_id: 'p1' })
      assert.equal(res.ok, true, `vf_${key} 应该能跑`)
      assert.equal(res.card, null, `vf_${key} 是免费步骤，不该再弹卡（用户实机：一路点确认）`)
    }
    for (const key of ['script', 'shots']) {
      const tool = s.all.stageTools.find((t) => t.name === `vf_${key}`)
      const res = await tool.execute({ project_id: 'p1' })
      assert.ok(res.card, `vf_${key} 是内容复核步，必须给用户看内容（卡）`)
    }
    console.log('③ 免费步骤不出卡 / 内容复核步出卡 OK')
  }

  // ── ④ vf_skip：一条命令 → POST /stage/skip ──
  {
    const skip = makeSkipStageSpec(fakeFetchJson, ['script', 'previz', 'draft'])
    assert.equal(skip.name, 'vf_skip')
    assert.deepEqual(skip.parameters.stage.enum, ['script', 'previz', 'draft'])

    calls.length = 0
    const res = await skip.execute({ project_id: 'p1', stage: 'previz', reason: '用户说先跳过白膜' })
    assert.equal(res.ok, true)
    assert.equal(res.stage, 'previz')
    assert.equal(res.next && res.next.key, 'draft', '跳过后要告诉调用方下一步是什么')
    const skipCall = calls.find((c) => c.path === '/api/projects/p1/stage/skip')
    assert.ok(skipCall, 'vf_skip 没有 POST /stage/skip')
    assert.equal(skipCall.method, 'POST')
    assert.equal(skipCall.body.stage, 'previz')
    assert.equal(skipCall.body.reason, '用户说先跳过白膜')

    const bad = await skip.execute({ project_id: 'p1', stage: 'nope' })
    assert.equal(bad.ok, false)
    assert.ok(String(bad.next_action).includes('previz'), '非法 stage 要给出可选列表')
    const noPid = await skip.execute({ stage: 'previz' })
    assert.equal(noPid.ok, false)
    assert.ok(String(noPid.next_action).includes('project_id'))
    console.log('④ vf_skip 一条命令 OK（跳过 + 非法 stage / 缺 project_id 的下一步）')
  }

  console.log('\nSELFTEST_OK')
}

main().catch((e) => {
  console.error('SELFTEST_FAIL', e)
  process.exit(1)
})
