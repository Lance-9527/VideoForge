const B = 'http://127.0.0.1:8765'
const PID = 'bceee486-2dc3-4134-b3ac-5ca7e048c3d5'

const j = async (u, i) => {
  const r = await fetch(B + u, i)
  const t = await r.text()
  try { return JSON.parse(t) } catch { return { raw: t } }
}

async function runStage(stage, params = {}) {
  const run = await j('/api/v2/run', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ project_id: PID, stage, params }),
  })
  const id = (run.data ?? run).job_id
  if (!id) { console.log(stage, 'NO JOB', JSON.stringify(run).slice(0, 300)); return null }
  console.log(`\n[${stage}] job ${id}`)
  for (let i = 0; i < 240; i++) {
    await new Promise((r) => setTimeout(r, 3000))
    const d = (await j(`/api/v2/jobs/${id}`)).data
    if (!['running', 'queued', 'pending'].includes(d.status)) {
      console.log(`[${stage}] -> ${d.status} | ${String(d.message || '').slice(0, 400)}`)
      return d
    }
    if (i % 10 === 0) console.log(`[${stage}] t=${i * 3}s ${d.status} ${d.progress}`)
  }
  console.log(`[${stage}] TIMEOUT`)
  return null
}

const settings = (await j('/api/settings')).data
console.log('video provider =', settings.default_model_provider, '| model =', settings.default_model_name, '| res =', settings.default_video_resolution, '| spend allowed =', settings.allow_video_spend, '| budget =', settings.default_video_budget_yuan)
console.log('image provider =', settings.image_provider, '| model =', settings.image_model)
console.log('llm =', settings.llm_provider, settings.llm_model)

await runStage('assets')
await runStage('shots')
const status = await j(`/api/projects/${PID}/stage/status`)
console.log('\nstage status:', (status.data.stages || []).map((s) => `${s.key}:${s.status}`).join(' '))
