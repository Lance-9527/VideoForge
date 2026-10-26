import { readFileSync, writeFileSync } from 'node:fs'

const path = 'D:/VideoForge-dsh/deepseek-harness-master/apps/desktop/src/locale.ts'
const before = readFileSync(path, 'utf8')
const after = before.replaceAll('DeepSeek Harness', 'VideoForge')
const replaced = (before.match(/DeepSeek Harness/g) ?? []).length
writeFileSync(path, after)
console.log('locale.ts 替换 DeepSeek Harness →', 'VideoForge', '共', replaced, '处')
const left = (after.match(/DeepSeek Harness/g) ?? []).length
console.log('剩余:', left)
for (const line of after.split('\n')) {
  if (line.includes('VideoForge')) console.log('  ', line.trim().slice(0, 110))
}
