const fs = require('node:fs')
const zlib = require('node:zlib')
const dir = 'C:/Users/Administrator/AppData/Local/VideoForge/dsh/sessions/--C-Users-Administrator-Documents-deepseek-harness-default-workspace--'
const files = fs.readdirSync(dir).map((n) => ({ n, t: fs.statSync(dir + '/' + n).mtimeMs })).sort((a, b) => b.t - a.t)
const newest = files[0].n
console.log('newest session:', newest)
const buf = fs.readFileSync(dir + '/' + newest)
let text
try {
  text = zlib.zstdDecompressSync ? zlib.zstdDecompressSync(buf).toString('utf8') : ''
} catch (e) {
  console.log('zstd decompress failed:', e.message)
  process.exit(0)
}
console.log('decompressed', text.length, 'chars')
const names = ['vf_project', 'vf_script', 'vf_status', 'vf_shots', 'pwsh', 'glob', 'grep', 'load_workspace_dependencies', 'bash']
for (const n of names) console.log(n.padEnd(28), (text.split(n).length - 1))
const m = text.match(/"(?:name|toolName)":"([a-zA-Z_][a-zA-Z0-9_]*)"/g) || []
console.log('\nsample tool mentions:', [...new Set(m)].slice(0, 25).join(', '))
