import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const root = 'D:/VideoForge-dsh/deepseek-harness-master/packages/client'
const nodes = []
const walk = (dir, depth) => {
  if (depth > 7) return
  for (const name of readdirSync(dir, { withFileTypes: true })) {
    if (['node_modules', 'lib', '.git', 'tests'].includes(name.name)) continue
    const full = join(dir, name.name)
    if (name.isDirectory()) walk(full, depth + 1)
    else if (/\.tsx?$/.test(name.name)) {
      const text = readFileSync(full, 'utf8')
      if (text.includes("'tool.call.toolview'")) nodes.push(full)
    }
  }
}
walk(root, 0)
for (const file of nodes) console.log(file)
if (nodes.length > 0) {
  const text = readFileSync(nodes[0], 'utf8')
  const i = text.indexOf("'tool.call.toolview'")
  console.log('\n=== first registration context ===')
  console.log(text.slice(Math.max(0, i - 900), i + 900))
}
