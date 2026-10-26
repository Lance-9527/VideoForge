import { readFileSync, writeFileSync } from 'node:fs'

const path = 'D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-vf-brand/src/client/Rail.tsx'
let text = readFileSync(path, 'utf8')
if (text.includes("from './view.ts'")) {
  console.log('already imported')
} else {
  text = text.replace(
    "import { useEffect, useState } from 'react'",
    "import { useEffect, useState } from 'react'\n\nimport { setVfView, useVfView } from './view.ts'",
  )
  writeFileSync(path, text)
  console.log('import added')
}
console.log(text.split('\n').slice(0, 8).join('\n'))
