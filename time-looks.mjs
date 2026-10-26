const B = 'http://127.0.0.1:8765'
const PID = '3ad3f169-fd41-4ed1-a034-a013eb066407'
const t0 = Date.now()
const el = () => ((Date.now() - t0) / 1000).toFixed(1) + 's'

const run = await (await fetch(`${B}/api/v2/run`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ project_id: PID, stage: 'looks', params: {} }),
})).json()
const id = (run.data || run).job_id
console.log(`[${el()}] looks job = ${id}`)

let last = ''
for (let i = 0; i < 200; i++) {
  await new Promise((r) => setTimeout(r, 1500))
  const d = (await (await fetch(`${B}/api/v2/jobs/${id}`)).json()).data
  const line = `${d.status} ${d.progress} ${String(d.message || '').slice(0, 90)}`
  if (line !== last) {
    console.log(`[${el()}] ${line}`)
    last = line
  }
  if (!['running', 'queued', 'pending'].includes(d.status)) {
    console.log(`[${el()}] DONE ${d.status} :: ${String(d.message || '').slice(0, 200)}`)
    break
  }
}
