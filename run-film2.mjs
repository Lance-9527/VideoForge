const B = 'http://127.0.0.1:8765'
const PID = 'bceee486-2dc3-4134-b3ac-5ca7e048c3d5'

const j = async (u, i) => {
  const r = await fetch(B + u, i)
  const t = await r.text()
  try { return JSON.parse(t) } catch { return { raw: t } }
}

async function runStage(stage, params = {}, maxTicks = 300) {
  const run = await j('/api/v2/run', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ project_id: PID, stage, params }),
  })
  const id = (run.data ?? run).job_id
  if (!id) { console.log(stage, 'NO JOB', JSON.stringify(run).slice(0, 400)); return null }
  console.log(`\n[${stage}] job ${id}`)
  let last = ''
  for (let i = 0; i < maxTicks; i++) {
    await new Promise((r) => setTimeout(r, 3000))
    const d = (await j(`/api/v2/jobs/${id}`)).data
    const line = `${d.status} ${d.progress} ${String(d.message || '').slice(0, 160)}`
    if (line !== last) { console.log(`[${stage}] t=${i * 3}s ${line}`); last = line }
    if (!['running', 'queued', 'pending'].includes(d.status)) {
      console.log(`[${stage}] FINAL -> ${d.status} | ${String(d.message || '').slice(0, 500)}`)
      return d
    }
  }
  console.log(`[${stage}] TIMEOUT`)
  return null
}

const stages = process.argv.slice(2)
for (const stage of stages) await runStage(stage)

const status = await j(`/api/projects/${PID}/stage/status`)
console.log('\nstage status:', (status.data.stages || []).map((s) => `${s.key}:${s.status}`).join(' '))
const outs = await j(`/api/projects/${PID}/outputs`)
console.log('outputs:', JSON.stringify(outs.data?.outputs ?? outs).slice(0, 600))
