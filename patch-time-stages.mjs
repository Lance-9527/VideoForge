import { readFileSync, writeFileSync } from 'node:fs'

const p = 'D:/VideoForge-dsh/time-stages.mjs'
let t = readFileSync(p, 'utf8')
if (!t.includes("idea: '一个农夫")) {
  t = t.replace(
    "await runStage('script')",
    "await runStage('script', { idea: '一个农夫在雪夜小路上走，12 秒。' })",
  )
  writeFileSync(p, t)
  console.log('已加 idea 参数')
} else {
  console.log('已有 idea 参数')
}
