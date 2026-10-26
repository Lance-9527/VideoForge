const B = 'http://127.0.0.1:8798'
const j = async (u, i) => (await (await fetch(B + u, i)).json())
const run = await j('/api/v2/run', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ project_id:'114dcc06-8762-4b6a-8475-5700b717903d', stage:'script', params:{ idea:'雨夜便利店，12 秒，日系冷调' } }) })
console.log('run =', JSON.stringify(run.data ?? run))
const id = (run.data ?? run).job_id
for (let i=0;i<60;i++){
  await new Promise(r=>setTimeout(r,2000))
  const job = await j('/api/v2/jobs/'+id)
  const d = job.data ?? job
  if (!['running','queued','pending'].includes(d.status)) { console.log('FINAL', d.status, '|', d.message, '| err:', d.error); break }
  if (i%5===0) console.log('t=', i*2, d.status, d.progress)
}
