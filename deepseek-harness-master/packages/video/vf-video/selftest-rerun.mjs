// vf-video · 「改参重跑」自测（2026-10-26 · T2）
//
// 为什么要有这个文件：core.js 是纯逻辑层（不 import DSH），可以脱离宿主用 node 直接跑。
// 本文件只验证三条真机反复出错的路径：
//   ① 阶段工具传 force / params ⇒ 请求体里必须有 redo/force + 改后的参数；
//   ② vf_rerun_stage 一条命令 ⇒ 走同一个阶段执行体，并带 redo/force；
//   ③ 失败回执必须把 sidecar 的 next_action / do_not_retry 带出来
//      （以前 done 事件没有顶层 error，只剩「sidecar 报告失败」⇒ 模型只能盲重试）。
//
// 跑法：node selftest-rerun.mjs

import assert from 'node:assert/strict'
import {
  makeToolSpecs,
  makeRerunStageSpec,
  stageFailureDetail,
} from './lib/core.js'

const calls = []

function fakeFetchJson(path, init = {}) {
  calls.push({ path, method: init.method || 'GET', body: init.body })
  if (path === '/api/v2/run') {
    return Promise.resolve({ job_id: 'sidecar-job-1', stream: '/x', poll: '/y' })
  }
  if (path.includes('/cancel')) return Promise.resolve({ cancelled: true })
  if (path.startsWith('/api/projects/') && path.endsWith('/stage/status')) {
    return Promise.resolve({ stages: [], done_count: 0, total: 9 })
  }
  return Promise.resolve({})
}

// SSE：先 hello，再 done（失败，且 result 里带真实原因）
function fakeOpenStream() {
  return (async function* gen() {
    yield { event: 'hello', data: { job_id: 'sidecar-job-1' } }
    yield { event: 'progress', data: { seq: 1, message: '第 1 镜' } }
    yield {
      event: 'done',
      data: {
        ok: false,
        result: {
          ok: false,
          error: '「剧本」这一步已经做完了，再跑一遍会覆盖掉现有内容。',
          next_action: '调 rerun_stage(project_id="p1", stage="script", force=true)。',
          retry: { tool: 'rerun_stage' },
          repeat_count: 2,
          do_not_retry: true,
        },
      },
    }
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

async function main() {
  // ── ① 阶段工具：force + params 必须进请求体 ──
  {
    const { startJob } = makeStartJob()
    const specs = makeToolSpecs({
      fetchJson: fakeFetchJson,
      baseUrl: 'http://127.0.0.1:8765',
      startJob,
      pollMs: 10,
      openStream: fakeOpenStream,
      stageKeys: ['script', 'looks'],
    })
    const script = specs.stageTools.find((t) => t.name === 'vf_script')
    assert.ok(script, '找不到 vf_script')
    assert.equal(script.parameters.params.type, 'object')
    assert.equal(script.parameters.params.additionalProperties, true,
      'params 必须是 additionalProperties=true 的对象（宿主 schema 要求）')
    assert.equal(script.parameters.force.type, 'boolean')

    calls.length = 0
    const res = await script.execute({
      project_id: 'p1', force: true, params: { idea: '雨夜便利店' },
    })
    assert.equal(res.ok, true)
    const runCall = calls.find((c) => c.path === '/api/v2/run')
    assert.ok(runCall, '没有 POST /api/v2/run')
    assert.equal(runCall.body.params.redo, true, 'force 没有转成 redo')
    assert.equal(runCall.body.params.force, true, 'force 没有透传')
    assert.equal(runCall.body.params.idea, '雨夜便利店', 'params.idea 丢了')
    await tick()

    // ── ③ 失败回执：next_action / do_not_retry 必须出现在 detail 里 ──
    const { jobs } = makeStartJob() // 仅占位，用下面的断言对象
    void jobs
    const detail = stageFailureDetail(
      { ok: false, error: '被守卫挡下', next_action: '调 rerun_stage', repeat_count: 2, do_not_retry: true },
      '兜底',
    )
    assert.ok(detail.includes('被守卫挡下'), 'detail 丢了 error')
    assert.ok(detail.includes('下一步'), 'detail 丢了 next_action')
    assert.ok(detail.includes('不要'), 'detail 丢了 do_not_retry 提醒')
    assert.ok(stageFailureDetail(null, '兜底').includes('兜底'))
    console.log('① 阶段工具 force/params 透传 OK；③ 失败回执 next_action OK')
  }

  // ── ② vf_rerun_stage：一条命令 → 复用阶段执行体 → 带 redo/force ──
  {
    const { startJob, jobs } = makeStartJob()
    const specs = makeToolSpecs({
      fetchJson: fakeFetchJson,
      baseUrl: 'http://127.0.0.1:8765',
      startJob,
      pollMs: 10,
      openStream: fakeOpenStream,
      stageKeys: ['script', 'looks'],
    })
    const rerun = makeRerunStageSpec(specs.stageTools)
    assert.equal(rerun.name, 'vf_rerun_stage')
    assert.deepEqual(rerun.parameters.stage.enum, ['script', 'looks'])

    calls.length = 0
    const res = await rerun.execute({
      project_id: 'p1', stage: 'script', params: { idea: '改成悬疑' }, reason: '用户不满意',
    })
    assert.equal(res.ok, true)
    assert.equal(res.rerun, true)
    assert.equal(res.stage, 'script')
    const runCall = calls.find((c) => c.path === '/api/v2/run')
    assert.ok(runCall, 'rerun 没有 POST /api/v2/run')
    assert.equal(runCall.body.stage, 'script')
    assert.equal(runCall.body.params.redo, true)
    assert.equal(runCall.body.params.force, true)
    assert.equal(runCall.body.params.idea, '改成悬疑')
    assert.equal(runCall.body.params.reason, '用户不满意')

    // 未知 stage：给"真能执行"的下一步，而不是空报错
    const bad = await rerun.execute({ project_id: 'p1', stage: 'nope' })
    assert.equal(bad.ok, false)
    assert.ok(String(bad.next_action).includes('script'), '未知 stage 没给可选列表')

    // 缺 project_id
    const noPid = await rerun.execute({ stage: 'script' })
    assert.equal(noPid.ok, false)
    assert.ok(String(noPid.next_action).includes('project_id'))

    await tick()
    assert.ok(jobs.length >= 1, '没有起 job')
    console.log('② vf_rerun_stage 一条命令 OK（含未知 stage / 缺 project_id 的下一步）')
  }

  console.log('\nSELFTEST_OK')
}

main().catch((e) => {
  console.error('SELFTEST_FAIL', e)
  process.exit(1)
})
