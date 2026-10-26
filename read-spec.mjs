import { existsSync, readFileSync } from 'node:fs'

const roots = [
  'D:/VideoForge-dev/backend/data/outputs/3ad3f169-fd41-4ed1-a034-a013eb066407/previz/spec.json',
  'D:/VideoForge-dev/backend/data/outputs/3ad3f169-fd41-4ed1-a034-a013eb066407/previz_spec.json',
]
for (const p of roots) {
  if (!existsSync(p)) { console.log('缺', p); continue }
  const s = JSON.parse(readFileSync(p, 'utf8'))
  console.log('##', p)
  for (const sh of (s.shots || []).slice(0, 3)) {
    console.log('  镜: set =', sh.set?.kind, `${sh.set?.width}x${sh.set?.depth}`,
      '| cast =', JSON.stringify((sh.cast || []).map((c) => `${c.name}:${c.pose}`)),
      '| purpose =', sh.purpose || '(无)')
  }
  console.log('  techviz:', (s.techviz || []).length, '条', (s.techviz || []).slice(0, 3))
}
