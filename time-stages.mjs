const B = 'http://127.0.0.1:8765'
const t0 = Date.now()
const el = () => ((Date.now() - t0) / 1000).toFixed(1) + 's'

async function waitHealth(timeoutMs = 120000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    try {
      const r = await fetch(`${B}/api/health`)
      if (r.ok) return (Date.now() - start) / 1000
    } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 500))
  }
  return -1
}

console.log('sidecar ready 耗时 =', await waitHealth(), '秒')

// 建一个一次性测试项目，量 LLM 阶段
const brief = await (await fetch(`${B}/api/projects`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ title: '性能测试·雪夜', description: '一个农夫在雪夜小路上走，12 秒。' }),
})).json()
const pid = (brief.data || brief).id
console.log('测试项目', pid)

async function runStage(stage, params = {}) {
  const t = Date.now()
  const run = await (await fetch(`${B}/api/v2/run`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ project_id: pid, stage, params }),
  })).json()
  const id = (run.data || run).job_id
  if (!id) { console.log(stage, 'no job', JSON.stringify(run).slice(0, 200)); return }
  let last = ''
  for (let i = 0; i < 400; i++) {
    await new Promise((r) => setTimeout(r, 1000))
    const d = (await (await fetch(`${B}/api/v2/jobs/${id}`)).json()).data
    const line = `${d.status} ${d.progress} ${String(d.message || '').slice(0, 70)}`
    if (line !== last) { console.log(`  [${el()}] ${stage}: ${line}`); last = line }
    if (!['running', 'queued', 'pending'].includes(d.status)) {
      console.log(`  [${el()}] ${stage} 用时 ${((Date.now() - t) / 1000).toFixed(1)} 秒 -> ${d.status}`)
      return
    }
  }
}

await runStage('script', { idea: '一个农夫在雪夜小路上走，12 秒。' })
await runStage('assets')
await runStage('shots')
