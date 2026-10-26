import { readFileSync } from 'node:fs'

const files = [
  'D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-settings/src/client/contract/slots.ts',
  'D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-settings/src/client/index.ts',
]
for (const file of files) {
  let text
  try { text = readFileSync(file, 'utf8') } catch { continue }
  const i = text.indexOf("'settings.section'")
  if (i < 0) continue
  console.log('##', file)
  console.log(text.slice(Math.max(0, i - 900), i + 900))
  console.log('-----')
}

const preset = readFileSync('D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-agent-preset/src/client/index.ts', 'utf8')
const j = preset.indexOf("'settings.section'")
console.log('## agent-preset registration')
console.log(preset.slice(Math.max(0, j - 500), j + 700))
