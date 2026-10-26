import { readFileSync, writeFileSync } from 'node:fs'

const base = 'D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-vf-brand/src/client'

// ── Panel.tsx：资产面板里显示「关键帧」（用户确认的那张图）──────────────
let panel = readFileSync(`${base}/Panel.tsx`, 'utf8')

if (!panel.includes('keyframe_path')) {
  // ① 类型
  panel = panel.replace(
    `interface OutputItem {`,
    `/** 一镜的写实关键帧（= 出片时的首帧，用户确认过的那张）。 */
interface ShotKeyframe {
  readonly index?: number
  readonly keyframe_path?: string
}

interface OutputItem {`,
  )
  // ② 拉取
  panel = panel.replace(
    `  const [outputs, setOutputs] = useState<readonly OutputItem[]>([])`,
    `  const [outputs, setOutputs] = useState<readonly OutputItem[]>([])
  const [keyframes, setKeyframes] = useState<readonly ShotKeyframe[]>([])`,
  )
  panel = panel.replace(
    `      const out = await fetchJson<{ outputs?: OutputItem[] }>(\`/api/projects/\${encodeURIComponent(projectId)}/outputs\`)`,
    `      const out = await fetchJson<{ outputs?: OutputItem[] }>(\`/api/projects/\${encodeURIComponent(projectId)}/outputs\`)
      const prompts = await fetchJson<{ shots?: ShotKeyframe[] }>(\`/api/projects/\${encodeURIComponent(projectId)}/prompts\`)`,
  )
  panel = panel.replace(
    `      setOutputs(out?.outputs ?? [])`,
    `      setOutputs(out?.outputs ?? [])
      setKeyframes((prompts?.shots ?? []).filter((s) => Boolean(s && s.keyframe_path)))`,
  )
  // ③ 渲染（资产段）
  panel = panel.replace(
    `            {images.length === 0 && !loading && (`,
    `            {keyframes.length > 0 ? (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12, color: C.dim, marginBottom: 8 }}>
                  关键帧（出片就是用这张当首帧 —— 你看到的构图 = 成片的构图）
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
                  {keyframes.map((k) => (
                    <img
                      key={String(k.index)}
                      src={\`\${SIDECAR}/api/media?path=\${encodeURIComponent(String(k.keyframe_path))}\`}
                      alt={\`第 \${Number(k.index ?? 0) + 1} 镜关键帧\`}
                      style={{ width: '100%', borderRadius: 10, border: \`1px solid \${C.line}\`, display: 'block' }}
                    />
                  ))}
                </div>
              </div>
            ) : null}
            {images.length === 0 && !loading && (`,
  )
  writeFileSync(`${base}/Panel.tsx`, panel)
  console.log('Panel.tsx 已显示关键帧')
} else {
  console.log('Panel.tsx 已存在 keyframe_path')
}
