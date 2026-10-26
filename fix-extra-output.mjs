import { readFileSync, writeFileSync } from 'node:fs'

const p = 'D:/VideoForge-dsh/deepseek-harness-master/packages/video/vf-video/lib/core.js'
let text = readFileSync(p, 'utf8')

// ① 导出 outputOf（extra 工具要和阶段工具用同一套输出契约）
if (!/export function outputOf/.test(text)) {
  text = text.replace('function outputOf(', 'export function outputOf(')
}

// ② 两个 extra 工具的 output 换成 outputOf(...)
text = text.replace(
  'const { fetchJson } = deps;\n  const nullOut = (v) => v;',
  'const { fetchJson } = deps;\n  // 宿主只认 { schema, render }：直接用阶段工具那套 outputOf\n  const textOut = (summary) => outputOf(summary);',
)

const outBlock = [
  '    output: textOut((v) => (v && v.ok === false',
  '      ? String(v.error || "失败")',
  '      : JSON.stringify(v).slice(0, 1200))),',
].join('\n')

const count = (text.match(/    output: nullOut,/g) || []).length
text = text.replaceAll('    output: nullOut,', outBlock)

writeFileSync(p, text)
console.log('导出 outputOf:', /export function outputOf/.test(text))
console.log('替换 output: nullOut 处数 =', count, '| 残留 nullOut =', (text.match(/nullOut/g) || []).length)
