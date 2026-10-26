const B='http://127.0.0.1:8797'
const get = async () => (await (await fetch(B+'/api/settings')).json()).data
const cur = await get()
const patch = { llm_base_url: 'https://api.deepseek.com', llm_provider: 'deepseek', llm_model: 'deepseek-chat' }
const r = await fetch(B+'/api/settings', { method:'PUT', headers:{'content-type':'application/json'}, body: JSON.stringify(patch) })
console.log('PUT', r.status)
const after = await get()
console.log('provider=', after.llm_provider, '| model=', after.llm_model, '| base=', after.llm_base_url)
// 跑「角色/场景」+「分镜表」两步（免费/便宜），确认链路能推进
for (const stage of ['assets','shots']) {
  const run = await (await fetch(B+'/api/v2/run', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ project_id:'114dcc06-8762-4b6a-8475-5700b717903d', stage, params:{} }) })).json()
  const id = (run.data ?? run).job_id
  console.log(stage, 'job', id)
  for (let i=0;i<90;i++){
    await new Promise(r=>setTimeout(r,2000))
    const d = (await (await fetch(B+'/api/v2/jobs/'+id)).json()).data
    if (!['running','queued','pending'].includes(d.status)) { console.log('  ->', d.status, '|', String(d.message||'').slice(0,160)); break }
    if (i%10===0) console.log('  t=', i*2, d.status, d.progress)
  }
}
