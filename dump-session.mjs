import { readFileSync, readdirSync, statSync } from 'node:fs'
import { zstdDecompressSync } from 'node:zlib'

const root = 'C:/Users/Administrator/AppData/Local/VideoForge/dsh/sessions'
const files = []
const walk = (dir, depth = 0) => {
  if (depth > 3) return
  for (const name of readdirSync(dir)) {
    const full = `${dir}/${name}`
    const st = statSync(full)
    if (st.isDirectory()) walk(full, depth + 1)
    else if (name.endsWith('.zstd')) files.push({ full, t: st.mtimeMs, size: st.size })
  }
}
walk(root)
files.sort((a, b) => b.t - a.t)
console.log('session files:', files.length)
const needles = ['vf-video', 'vf_project', 'vf_script', 'fiber', 'activate', 'failed', 'plugin', 'defineTool', 'dsh-tools']
for (const f of files.slice(0, 2)) {
  let text
  try {
    text = zstdDecompressSync(readFileSync(f.full)).toString('utf8')
  } catch (e) {
    console.log('decompress failed', f.full, e.message)
    continue
  }
  const stamp = new Date(f.t).toISOString()
  console.log(`\n=== ${stamp} ${f.size} B -> ${text.length} chars`)
  for (const n of needles) {
    const count = text.split(n).length - 1
    if (count > 0) console.log(`  ${n.padEnd(12)} x${count}`)
  }
  // 打印含关键字的行片段
  const lines = text.split('\n')
  for (const line of lines) {
    if (/vf-video|defineTool|dsh-tools|fiber|activate/.test(line)) {
      console.log('  >', line.slice(0, 300))
    }
  }
}
