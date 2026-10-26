const B='http://127.0.0.1:8765'
const PID='bceee486-2dc3-4134-b3ac-5ca7e048c3d5'
const j = async (u,i) => { const r = await fetch(B+u,i); const t=await r.text(); try { return JSON.parse(t) } catch { return {raw:t} } }
const est = await j(`/api/v2/stage/final/estimate?project_id=${PID}`)
console.log('final estimate now:', JSON.stringify(est.data ?? est).slice(0, 300))
const run = await j('/api/v2/run', { method:'POST', headers:{'content-type':'application/json'},
  body: JSON.stringify({ project_id: PID, stage: 'final', params: { confirm: true } }) })
console.log('run:', JSON.stringify(run.data ?? run).slice(0, 200))
const id = (run.data ?? run).job_id
if (!id) process.exit(0)
let last=''
for (let i=0;i<400;i++){
  await new Promise(r=>setTimeout(r,3000))
  const d = (await j(`/api/v2/jobs/${id}`)).data
  const line = `${d.status} ${d.progress} ${String(d.message||'').slice(0,180)}`
  if (line!==last){ console.log(`t=${i*3}s ${line}`); last=line }
  if (!['running','queued','pending'].includes(d.status)) { console.log('FINAL', d.status, '|', String(d.message||'').slice(0,500)); break }
}
