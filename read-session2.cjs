const fs = require('node:fs')
const zlib = require('node:zlib')
const root = 'C:/Users/Administrator/AppData/Local/VideoForge/dsh/sessions/--C-Users-Administrator-Documents-deepseek-harness-default-workspace--'
const entries = []
for (const name of fs.readdirSync(root)) {
  const full = root + '/' + name
  if (!fs.statSync(full).isDirectory()) continue
  for (const inner of fs.readdirSync(full)) {
    const f = full + '/' + inner
    if (fs.statSync(f).isFile()) entries.push({ f, t: fs.statSync(f).mtimeMs })
  }
}
entries.sort((a, b) => b.t - a.t)
console.log('newest:', entries[0].f)
const buf = fs.readFileSync(entries[0].f)
const text = zlib.zstdDecompressSync(buf).toString('utf8')
console.log('chars:', text.length)
for (const n of ['vf_project', 'vf_script', 'vf_status', 'vf_shots', 'pwsh', 'glob', 'grep', 'load_workspace_dependencies', 'bash', 'read']) {
  console.log(n.padEnd(28), text.split(n).length - 1)
}
const seen = new Set((text.match(/"name":"([a-zA-Z_][a-zA-Z0-9_]*)"/g) || []).map((s) => s.slice(8, -1)))
console.log('\ntool names seen:', [...seen].slice(0, 30).join(', '))
