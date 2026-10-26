const fs = require('node:fs')
const path = require('node:path')
const root = 'D:/VideoForge-dsh/deepseek-harness-master/apps/desktop/.desktop-build/targets/win-x64/unsigned-artifacts/win-unpacked'
const needle = Buffer.from('剧本阶段需要 idea', 'utf8')
const hits = []
function walk(dir, depth) {
  if (depth > 6) return
  let entries = []
  try { entries = fs.readdirSync(dir, { withFileTypes: true }) } catch { return }
  for (const e of entries) {
    const full = path.join(dir, e.name)
    if (e.isDirectory()) walk(full, depth + 1)
    else if (e.isFile() && fs.statSync(full).size < 400 * 1024 * 1024) {
      try {
        const buf = fs.readFileSync(full)
        if (buf.includes(needle)) hits.push(full + '  (' + buf.length + ' B)')
      } catch {}
    }
  }
}
walk(root, 0)
console.log(hits.length ? hits.join('\n') : 'NO FILE contains the new core.js marker')
