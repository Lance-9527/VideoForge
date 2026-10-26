const B='http://127.0.0.1:8797'
const get = async () => (await (await fetch(B+'/api/settings')).json()).data
const cur = await get()
const llmKeys = { ...(cur.llm_api_keys ?? {}) }
// key 从环境变量读取，不能写进源码
llmKeys.deepseek = process.env.DEEPSEEK_API_KEY || ''
const patch = {
  llm_provider: 'deepseek',
  llm_model: 'deepseek-chat',
  llm_base_url: '',
  llm_api_keys: llmKeys,
}
const r = await fetch(B+'/api/settings', { method:'PUT', headers:{'content-type':'application/json'}, body: JSON.stringify(patch) })
console.log('PUT status', r.status, (await r.text()).slice(0,200))
const after = await get()
console.log('after: provider=', after.llm_provider, '| model=', after.llm_model, '| base=', JSON.stringify(after.llm_base_url), '| deepseek key set=', !!after.llm_api_keys?.deepseek)
