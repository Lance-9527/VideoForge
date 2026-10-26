import { readFileSync, writeFileSync } from 'node:fs'

const base = 'D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-vf-brand/src/client'

// ── Panel.tsx：接 onBack，用 props 而不是直接 setVfView ────────────────
let panel = readFileSync(`${base}/Panel.tsx`, 'utf8')

panel = panel.replace(
  'export function VfPanel() {',
  'export function VfPanel(props: { onBack?: () => void } = {}) {',
)
panel = panel.replace(
  `  if (view.section === 'chat') return null`,
  `  const back = props.onBack ?? ((): void => { setVfView({ section: 'chat', projectId: null, projectTitle: null }) })

  if (view.section === 'chat') return null`,
)
panel = panel.replaceAll(
  `          <button type="button" style={primaryButton} onClick={() => { setVfView({ section: 'chat' }) }}>
            回到 AI 对话
          </button>`,
  `          <button type="button" style={primaryButton} onClick={back}>
            回到 AI 对话
          </button>`,
)
panel = panel.replaceAll(
  `        <button type="button" style={ghostButton} onClick={() => { setVfView({ section: 'chat' }) }}>
          ← AI 对话
        </button>`,
  `        <button type="button" style={ghostButton} onClick={back}>
          ← AI 对话（回到对应会话）
        </button>`,
)

writeFileSync(`${base}/Panel.tsx`, panel)
console.log('Panel.tsx 已接 onBack:', !panel.includes("onClick={() => { setVfView({ section: 'chat' }) }}"))

// ── VfCard.tsx：渲染 shots_prompts（让用户过一遍）───────────────────────
const cardPath = 'D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-vf-cards/src/client/VfCard.tsx'
let card = readFileSync(cardPath, 'utf8')

// ① 类型里加字段
if (!card.includes('shots_prompts?:')) {
  card = card.replace(
    '  script_text?: string',
    `  script_text?: string
  /** 分镜提示词（previz/draft/final 提案卡带上，供用户过一遍） */
  shots_prompts?: Array<{ index?: number, video_prompt?: string, negative_prompt?: string, audio_prompt?: string }>`,
  )
}

// ② GateCard 里渲染（放在剧本正文之后）
const anchor = `        {/* ★ 剧本正文（2026-10-21 用户要求：这一步必须把内容摊开给用户看，再看要不要改） */}`
const block = `        {/* ★ 分镜提示词（2026-10-22 用户要求：出片前给用户过一遍，可改） */}
        {Array.isArray(payload.shots_prompts) && payload.shots_prompts.length > 0 ? (
          <div className="gate-prompts" data-vf-prompts={String(payload.shots_prompts.length)}>
            <div className="gate-script-head">
              分镜提示词（共 {payload.shots_prompts.length} 镜 · 要改哪一镜直接说"改第 N 镜…"）
            </div>
            {payload.shots_prompts.map((shot, i) => (
              <details key={i} className="gate-prompt" open={i === 0}>
                <summary>第 {(shot.index ?? i) + 1} 镜</summary>
                <div className="gate-prompt-body">
                  <div className="gate-prompt-label">视频生成提示词</div>
                  <div className="gate-prompt-text">{shot.video_prompt || '（空）'}</div>
                  <div className="gate-prompt-label">音色 / 配音提示词</div>
                  <div className="gate-prompt-text">{shot.audio_prompt || '（空）'}</div>
                </div>
              </details>
            ))}
          </div>
        ) : null}

${anchor}`
if (!card.includes('gate-prompts')) {
  card = card.replace(anchor, block, 1)
}

// ③ CSS
if (!card.includes('.vf-cards .gate-prompt{')) {
  card = card.replace(
    '.vf-cards .agent-gate-tweaks{margin-top:9px;display:flex;gap:6px;flex-wrap:wrap;',
    `.vf-cards .gate-prompts{margin-top:10px;border:1px solid var(--border-1,rgba(127,127,127,.25));
  border-radius:8px;background:var(--bg-2,rgba(127,127,127,.06));overflow:hidden}
.vf-cards .gate-prompt{border-top:1px solid var(--border-1,rgba(127,127,127,.18))}
.vf-cards .gate-prompt:first-of-type{border-top:none}
.vf-cards .gate-prompt>summary{cursor:pointer;padding:6px 10px;font-size:var(--text-xs,12px);color:var(--text-secondary)}
.vf-cards .gate-prompt-body{padding:6px 11px 10px}
.vf-cards .gate-prompt-label{font-size:var(--text-xs,12px);color:var(--text-secondary);margin-top:6px}
.vf-cards .gate-prompt-text{font-size:var(--text-sm,13px);line-height:1.7;color:var(--text-primary);
  white-space:pre-wrap;max-height:180px;overflow:auto;margin-top:2px}
.vf-cards .agent-gate-tweaks{margin-top:9px;display:flex;gap:6px;flex-wrap:wrap;`,
  )
}

writeFileSync(cardPath, card)
console.log('VfCard.tsx 已渲染分镜提示词:', card.includes('gate-prompts'))
