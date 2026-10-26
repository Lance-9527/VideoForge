/**
 * VideoForge 对话卡片（**唯一实现**；寄存器登记见 `./index.tsx`）。
 *
 * 视觉蓝本 = 老 VideoForge 的 `.agent-gate` / `.msg-media`（`D:\VideoForge-dev\frontend\css\chat.css`），
 * 样式规则逐条抄来、收在 `.vf-cards` 作用域下；载荷契约见 `docs/vf-card-payload.md`。
 *
 * 三条铁律：
 *  1. **卡片不编数字**：`cost_yuan` 为 `null` 就渲染「待确认」+ 下界，绝不写 0 或别的 provider 的价。
 *  2. **媒体走 HTTP URL，不内联字节**（`/api/media?path=` 与 `/api/projects/{pid}/final`）。
 *  3. **证据与错误一律原文**，不改写成"已完成 / 出错了"。
 */
import { useMemo, useState } from 'react'
import type { ReactElement } from 'react'

/* ───────────────────────── 载荷类型 ───────────────────────── */

/** 成本档位 → 人话（与 core/stages.py 的 COST_LABEL 同口径）。 */
export const COST_LABEL: Record<string, string> = {
  free: '免费（本地跑，不花钱）',
  cheap: '很少的钱（一次文字模型调用）',
  image: '按张计费（图像模型）',
  video: '按条计费（视频模型，明显花钱）',
  expensive: '最贵的一步（1080P 成片）',
}

/** 产物媒体项；URL 必须是 sidecar 的 HTTP 地址。 */
export interface VfMedia {
  kind?: 'video' | 'image'
  url: string
  name?: string
  tag?: string
}

/** 卡片载荷（契约见 `docs/vf-card-payload.md`）。 */
export interface VfCardPayload {
  card?: 'vf'
  /** 选臂；`approval` 是历史写法，读进来会映射成 `gate` + `spend:true`。 */
  kind: 'gate' | 'progress' | 'artifact' | 'note' | 'approval'
  title?: string
  stage?: string
  why?: string
  /** 成本档位人话（五句之一）。 */
  cost_label?: string
  /** 具体金额；拿不到必须是 `null`。 */
  cost_yuan?: number | null
  /** 只有下界时放这里（**不是报价**）。 */
  cost_floor_yuan?: number | null
  /** 历史驼峰写法，兼容读取。 */
  costYuan?: number | null
  eta?: string
  /** `true` = 这一步真花钱 → 真机上由原生 `ctx.approval` 出按钮。 */
  spend?: boolean
  can_skip?: boolean
  /** 会做什么（数组或单行）。 */
  doing?: string[] | string
  tweaks?: string[]
  /** 上一步证据**原文**。 */
  evidence?: string
  warning?: string
  warn?: string
  already_done?: boolean
  done_detail?: string
  needs_attention?: {
    characters_without_image?: string[]
    scenes_without_image?: string[]
    shots_without_scene?: Array<number | string>
  }
  note?: string
  /** progress 臂 */
  have?: number | null
  total?: number | null
  message?: string
  items?: string[]
  error?: string
  job_id?: string
  cancellable?: boolean
  /** artifact 臂 */
  final_url?: string
  media?: VfMedia[]
  script_text?: string
  /** 分镜提示词（previz/draft/final 提案卡带上，供用户过一遍） */
  shots_prompts?: Array<{
    index?: number
    video_prompt?: string
    negative_prompt?: string
    audio_prompt?: string
    /** 'relay' = 这一镜接着上一镜的尾帧出的（跨镜连贯） */
    chain_mode?: string
    /** 接龙时携带的"上一镜结束状态"快照 */
    continuity_brief?: string
  }>
  body?: string
  shots?: Array<{ index?: number; text?: string }>
}

/** 卡片动作：按钮一律走「发一条用户消息」（与输入框同一条路径）。 */
export interface VfCardActions {
  send: (text: string) => void
}

/* ───────────────────────── 样式（老站逐条抄）───────────────────────── */

const CSS = `
.vf-cards{
  --space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;
  --radius-sm:6px;--radius-md:10px;--radius-lg:16px;
  --text-xs:11px;--text-sm:13px;--text-base:14px;
  --bg-0:#0a0a0c;--bg-1:#111114;--bg-2:#18181c;--bg-3:#1f1f24;--bg-4:#28282e;
  --border-1:#2a2a30;--border-2:#3a3a42;
  --text-primary:#f0f0f3;--text-secondary:#b8b8c0;--text-tertiary:#7a7a85;
  --accent:#7ac130;--accent-hover:#8ed146;--warning:#ffb648;--error:#ff6b6b;--info:#4ea7ff;
  --transition:200ms ease;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  color:var(--text-primary);
}
.vf-cards .dim{color:var(--text-tertiary)}
.vf-cards button{font:inherit;border:none;background:none;cursor:pointer}
.vf-cards .primary-btn{display:inline-flex;align-items:center;gap:var(--space-2);
  padding:var(--space-2) var(--space-4);background:var(--accent);color:#fff;
  border-radius:var(--radius-md);font-weight:600;font-size:var(--text-sm);transition:all var(--transition)}
.vf-cards .primary-btn:hover:not(:disabled){background:var(--accent-hover);transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(122,193,48,.25)}
.vf-cards .primary-btn:disabled{opacity:.5;cursor:not-allowed}
.vf-cards .secondary-btn{display:inline-flex;align-items:center;gap:var(--space-2);
  padding:var(--space-2) var(--space-4);background:var(--bg-3);color:var(--text-secondary);
  border-radius:var(--radius-md);font-weight:500;font-size:var(--text-sm);
  border:1px solid var(--border-1);transition:all var(--transition)}
.vf-cards .secondary-btn:hover:not(:disabled){background:var(--bg-4);color:var(--text-primary);
  border-color:var(--border-2)}
.vf-cards .secondary-btn:disabled{opacity:.5;cursor:not-allowed}
.vf-cards .agent-gate{margin-top:10px;padding:12px;border-radius:var(--radius-md,10px);
  background:linear-gradient(180deg,rgba(80,140,255,.10),rgba(80,140,255,.04));
  border:1px solid rgba(80,140,255,.35)}
.vf-cards .agent-gate-head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;
  font-size:var(--text-sm,14px)}
.vf-cards .agent-gate-step{font-size:var(--text-xs,11px);padding:1px 7px;border-radius:999px;
  background:rgba(80,140,255,.9);color:#fff;font-weight:600}
.vf-cards .agent-gate-cost{font-size:var(--text-xs,12px);padding:1px 7px;border-radius:999px;
  background:var(--bg-2,rgba(127,127,127,.14));border:1px solid var(--border-1,rgba(127,127,127,.25))}
.vf-cards .agent-gate-why,
.vf-cards .agent-gate-doing,
.vf-cards .agent-gate-note{margin-top:7px;font-size:var(--text-sm,13px);line-height:1.65;
  color:var(--text-secondary)}
.vf-cards .agent-gate-doing{color:var(--text-primary)}
.vf-cards .agent-gate-note{font-style:italic}
.vf-cards .gate-warn{margin-top:7px;font-size:var(--text-sm,13px);color:var(--warning,#e6a23c);line-height:1.6}
.vf-cards .agent-gate-actions{margin-top:11px;display:flex;gap:8px;flex-wrap:wrap}
.vf-cards .agent-gate-actions button{font-size:var(--text-sm,13px)}
.vf-cards .gate-prompts{margin-top:10px;border:1px solid var(--border-1,rgba(127,127,127,.25));
  border-radius:8px;background:var(--bg-2,rgba(127,127,127,.06));overflow:hidden}
.vf-cards .gate-prompt{border-top:1px solid var(--border-1,rgba(127,127,127,.18))}
.vf-cards .gate-prompt:first-of-type{border-top:none}
.vf-cards .gate-prompt>summary{cursor:pointer;padding:6px 10px;font-size:var(--text-xs,12px);color:var(--text-secondary)}
.vf-cards .gate-prompt-body{padding:6px 11px 10px}
.vf-cards .gate-prompt-label{font-size:var(--text-xs,12px);color:var(--text-secondary);margin-top:6px}
.vf-cards .gate-prompt-text{font-size:var(--text-sm,13px);line-height:1.7;color:var(--text-primary);
  white-space:pre-wrap;max-height:180px;overflow:auto;margin-top:2px}
.vf-cards .agent-gate-tweaks{margin-top:9px;display:flex;gap:6px;flex-wrap:wrap;
  align-items:center;font-size:var(--text-xs,12px)}
/* 剧本正文：可滚动、等宽感、保留换行 —— 让用户真能看到剧本长什么样 */
.vf-cards .gate-script{margin-top:10px;border:1px solid var(--border-1,rgba(127,127,127,.25));
  border-radius:8px;background:var(--bg-2,rgba(127,127,127,.08));overflow:hidden}
.vf-cards .gate-script-head{padding:6px 10px;font-size:var(--text-xs,12px);
  color:var(--text-secondary);border-bottom:1px solid var(--border-1,rgba(127,127,127,.2))}
.vf-cards .gate-script-body{padding:9px 11px;max-height:260px;overflow:auto;white-space:pre-wrap;
  font-size:var(--text-sm,13px);line-height:1.75;color:var(--text-primary)}
.vf-cards .gate-tweak{font-size:var(--text-xs,12px);padding:3px 9px;border-radius:999px;
  cursor:pointer;background:var(--bg-2,rgba(127,127,127,.1));
  border:1px dashed var(--border-1,rgba(127,127,127,.35));color:var(--text-secondary)}
.vf-cards .gate-tweak:hover{color:var(--text-primary);border-style:solid}
.vf-cards .agent-gate-msg{margin-top:8px;font-size:var(--text-xs,12px)}
.vf-cards .vf-price{margin-top:8px;font-size:var(--text-base,14px);display:flex;gap:8px;
  align-items:baseline;flex-wrap:wrap}
.vf-cards .vf-price b{font-size:20px;color:var(--warning,#ffb648)}
.vf-cards .vf-floor{margin-top:2px;font-size:var(--text-xs,12px);color:var(--text-tertiary)}
.vf-cards .vf-bar{margin-top:8px;height:5px;border-radius:3px;background:var(--bg-3,#1f1f24);overflow:hidden}
.vf-cards .vf-bar>i{display:block;height:100%;background:var(--accent,#7ac130);transition:width .3s}
.vf-cards .vf-script{margin-top:7px;font-size:var(--text-sm,13px);line-height:1.7;
  color:var(--text-secondary);white-space:pre-wrap;max-height:260px;overflow:auto}
.vf-cards .vf-shots{margin-top:7px;max-height:260px;overflow:auto}
.vf-cards .vf-shot{margin-top:5px;font-size:var(--text-sm,13px);line-height:1.6;color:var(--text-secondary)}
.vf-cards .msg-media{margin-top:10px}
.vf-cards .msg-media-hint{font-size:var(--text-xs,12px);color:var(--text-secondary);margin-bottom:6px}
.vf-cards .msg-media-row{display:flex;flex-wrap:wrap;gap:8px}
.vf-cards .msg-media-cell{position:relative;width:132px;height:92px;border-radius:8px;overflow:hidden;
  border:1px solid var(--border-1,rgba(127,127,127,.35));cursor:zoom-in;
  background:var(--bg-2,rgba(127,127,127,.08))}
.vf-cards .msg-media-cell img,
.vf-cards .msg-media-cell video{width:100%;height:100%;object-fit:cover;display:block}
.vf-cards .msg-media-cell:hover{border-color:var(--accent,#4a9eff)}
.vf-cards .msg-media-tag{position:absolute;top:4px;left:4px;font-size:11px;padding:1px 6px;
  border-radius:999px;background:rgba(0,0,0,.55);color:#fff}
.vf-cards .msg-media-name{position:absolute;bottom:0;left:0;right:0;font-size:11px;padding:2px 6px;
  background:linear-gradient(transparent,rgba(0,0,0,.65));color:#fff;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.vf-cards .vf-lightbox{position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.86);
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;
  cursor:zoom-out;padding:24px}
.vf-cards .vf-lightbox img,.vf-cards .vf-lightbox video{max-width:92vw;max-height:82vh;border-radius:8px}
.vf-cards .vf-lightbox-name{color:#cfd6e4;font-size:12px}
`

/* ───────────────────────── 取值与格式化 ───────────────────────── */

/** 金额：数字 → ¥x.xx；`null`/缺失 → 待确认（**绝不写 0**）。 */
export function vfMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || (v as unknown) === '') return '待确认'
  const n = Number(v)
  return Number.isFinite(n) ? `¥${n.toFixed(2)}` : '待确认'
}

/** 读金额：兼容 `cost_yuan` / `costYuan`；`null` 保持 `null`（不当 0）。 */
function readCost(p: VfCardPayload): number | null {
  const raw = p.cost_yuan !== undefined ? p.cost_yuan : p.costYuan
  if (raw === null || raw === undefined) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** 读下界（"至少多少钱"的锚，不当报价）。 */
function readFloor(p: VfCardPayload): number | null {
  const raw = p.cost_floor_yuan
  if (raw === null || raw === undefined) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** `doing` 兼容 string[] 与单行字符串。 */
function readDoing(p: VfCardPayload): string[] {
  if (Array.isArray(p.doing)) return p.doing.map(String).filter(Boolean)
  if (typeof p.doing === 'string' && p.doing.trim()) return [p.doing.trim()]
  return []
}

/**
 * 从工具块里读出卡片载荷。
 *
 * 宿主把 `output.presentationMeta` 的结果记进结果节点的 `meta`；老行没有时会退回
 * `result.meta` / `card` / `call.args`，都没有就返回 null（渲染层退通用行，不白屏）。
 */
export function readPayload(block: unknown): VfCardPayload | null {
  const b = block as Record<string, any> | null | undefined
  if (!b) return null
  const cands: unknown[] = [b.meta, b.result?.meta, b.card, b.call?.args]
  for (const c of cands) {
    if (c && typeof c === 'object' && (c as VfCardPayload).kind !== undefined) {
      const p = c as VfCardPayload
      // 历史写法 approval → gate + spend
      return p.kind === 'approval' ? { ...p, kind: 'gate', spend: true } : p
    }
  }
  return null
}

/* ───────────────────────── 卡片组件 ───────────────────────── */

function Head(props: { payload: VfCardPayload; badge: string; extra?: string | undefined }): ReactElement {
  const { payload, badge, extra } = props
  return (
    <div className="agent-gate-head">
      <span className="agent-gate-step">{badge}</span>
      <b>{payload.title || payload.stage || ''}</b>
      {payload.cost_label ? <span className="agent-gate-cost">{payload.cost_label}</span> : null}
      {payload.eta ? <span className="dim">预计 {payload.eta}</span> : null}
      {extra ? <span className="dim">{extra}</span> : null}
    </div>
  )
}

/** 证据位：没有就照实写「（还没有可展示的证据）」。 */
function evidenceText(payload: VfCardPayload): string {
  const ev = String(payload.evidence || '').trim()
  return ev || '（还没有可展示的证据）'
}

function needsBits(payload: VfCardPayload): string[] {
  const na = payload.needs_attention || {}
  const out: string[] = []
  if (na.characters_without_image?.length) {
    out.push(`角色定妆照 ${na.characters_without_image.length} 张还没出：${na.characters_without_image.slice(0, 8).join('、')}`)
  }
  if (na.scenes_without_image?.length) {
    out.push(`场景图 ${na.scenes_without_image.length} 张还没出：${na.scenes_without_image.slice(0, 6).join('、')}`)
  }
  if (na.shots_without_scene?.length) {
    out.push(`第 ${na.shots_without_scene.join('、')} 个镜头还没有对应的场景卡（资产里新建一个场景、或让它按分镜文字自动匹配）`)
  }
  return out
}

/** 提案 / 审批卡（老 `.agent-gate`）。 */
export function GateCard(props: { payload: VfCardPayload; actions: VfCardActions }): ReactElement {
  const { payload, actions } = props
  const [msg, setMsg] = useState('')
  /** 点过哪个动作：点完立刻置灰 + 给一行「已发送」，别让用户以为没反应。 */
  const [sentAct, setSentAct] = useState<string | null>(null)
  const cost = readCost(payload)
  const floor = readFloor(payload)
  // 花钱 = 显式 spend，或者**真有大金额/下界**。
  // ★ 2026-10-21 真机实测：`cheap` 档 estimate 返回 `cost_yuan: 0.0`（不是 null），
  //   旧写法 `cost !== null` 就把「剧本」也标成「要花钱」，还显示 ¥0.00 —— 自相矛盾。
  //   0 元不是花钱：只有 >0 或后端明确 spend:true 才走审批臂。
  const spend = payload.spend === true || (cost !== null && cost > 0) || (floor !== null && floor > 0)
  const stage = String(payload.stage || '')
  const doing = readDoing(payload)
  const tweaks = Array.isArray(payload.tweaks) ? payload.tweaks : []
  const bits = needsBits(payload)
  const warn = payload.warning ?? payload.warn
  /** 最后一步（后期成片）的确认文案换成「确认完成」。 */
  const isLastStep = stage === 'compose'
  const script = typeof payload.script_text === 'string' ? payload.script_text.trim() : ''

  const act = (kind: string, text: string, note: string): void => {
    setSentAct(kind)
    setMsg(note)
    actions.send(text)
  }

  return (
    <div className="vf-cards">
      <style>{CSS}</style>
      <div className="agent-gate" data-vf-card={spend ? 'approval' : 'gate'} data-gate-stage={stage}>
        <Head payload={payload} badge={spend ? '要花钱' : '下一步'} />
        {payload.why ? <div className="agent-gate-why">{payload.why}</div> : null}
        {doing.length ? <div className="agent-gate-doing">会做的事：{doing.join('；')}</div> : null}

        {/* ★ 分镜提示词（2026-10-22 用户要求：出片前给用户过一遍，可改） */}
        {Array.isArray(payload.shots_prompts) && payload.shots_prompts.length > 0 ? (
          <div className="gate-prompts" data-vf-prompts={String(payload.shots_prompts.length)}>
            <div className="gate-script-head">
              分镜提示词（共 {payload.shots_prompts.length} 镜 · 要改哪一镜直接说"改第 N 镜…"）
            </div>
            {payload.shots_prompts.map((shot, i) => (
              <details key={i} className="gate-prompt" open={i === 0}>
                <summary>
                  第 {(shot.index ?? i) + 1} 镜
                  {shot.chain_mode === 'relay' ? <span style={{ marginLeft: 6, color: 'var(--accent,#7ac130)' }}>🔗 接上一镜</span> : null}
                </summary>
                <div className="gate-prompt-body">
                  {shot.continuity_brief ? (
                    <div className="gate-prompt-label" style={{ color: 'var(--accent,#7ac130)' }}>接龙状态</div>
                  ) : null}
                  {shot.continuity_brief ? (
                    <div className="gate-prompt-text" style={{ fontSize: 12, opacity: 0.85 }}>{shot.continuity_brief}</div>
                  ) : null}
                  <div className="gate-prompt-label">视频生成提示词</div>
                  <div className="gate-prompt-text">{shot.video_prompt || '（空）'}</div>
                  <div className="gate-prompt-label">音色 / 配音提示词</div>
                  <div className="gate-prompt-text">{shot.audio_prompt || '（空）'}</div>
                </div>
              </details>
            ))}
          </div>
        ) : null}

        {/* ★ 剧本正文（2026-10-21 用户要求：这一步必须把内容摊开给用户看，再看要不要改） */}
        {script !== '' ? (
          <div className="gate-script" data-vf-script="1">
            <div className="gate-script-head">剧本内容（确认无误再往下走）</div>
            <div className="gate-script-body">{script}</div>
          </div>
        ) : null}

        {spend ? (
          <div className="vf-price">
            {cost === null ? (
              <>
                <b>金额：待确认</b>
                <span className="dim">（后端没给这一档的价目表 —— 不编数字）</span>
                {floor !== null ? <div className="vf-floor">下界 {vfMoney(floor)}（**只是下界，不是报价**）</div> : null}
              </>
            ) : (
              <>
                <b>{cost === 0 ? '免费' : vfMoney(cost)}</b>
                <span className="dim">（{payload.cost_label || ''}）</span>
              </>
            )}
          </div>
        ) : null}

        <div className="agent-gate-note">{evidenceText(payload)}</div>

        {payload.already_done ? (
          <div className="gate-warn" style={{ color: 'var(--info)' }}>
            ℹ 这一步看起来已经做过了：{payload.done_detail || ''}。再跑一次会覆盖/重复，确认前想清楚。
          </div>
        ) : null}
        {warn ? (
          <div className="gate-warn">
            {/^\s*[⚠ℹ📌]/u.test(String(warn)) ? '' : '⚠ '}
            {String(warn)}
          </div>
        ) : null}
        {bits.length ? (
          <div className="gate-warn" style={{ borderColor: '#7a6a2a' }}>
            📌 现在缺这些：
            {bits.map((b, i) => (
              <div key={i}>· {b}</div>
            ))}
          </div>
        ) : null}

        <div className="agent-gate-actions">
          <button
            className="primary-btn"
            data-vf-gate-act="confirm"
            disabled={sentAct !== null}
            onClick={() => {
              act('confirm', `确认：${stage || payload.title || ''}`,
                spend ? '已发起确认 —— 批准后才会扣费' : '已发送，模型接着往下做，可能要几分钟…')
            }}
          >
            {sentAct === 'confirm'
              ? '已发送 ✓'
              : isLastStep
                ? '✅ 确认完成'
                : spend
                  ? '✅ 确认后开始'
                  : '✅ 确认，开始做'}
          </button>
          <button
            className="secondary-btn"
            data-vf-gate-act="modify"
            disabled={sentAct !== null}
            onClick={() => {
              act('modify', `我要改一下「${payload.title || stage}」：`, '已发送 —— 直接接着说你要改哪一处')
            }}
          >
            ✏️ 我要改一下
          </button>
          {payload.can_skip !== false ? (
            <button
              className="secondary-btn"
              data-vf-gate-act="skip"
              disabled={sentAct !== null}
              onClick={() => {
                act('skip', `跳过这一步：${stage || payload.title || ''}`, '已发送，这一步跳过。')
              }}
            >
              ⏭ 这步跳过
            </button>
          ) : null}
        </div>

        {tweaks.length ? (
          <div className="agent-gate-tweaks">
            <span className="dim">常见改法：</span>
            {tweaks.map((t) => (
              <button
                key={t}
                className="gate-tweak"
                data-vf-gate-tweak={t}
                onClick={() => {
                  setMsg(`已带上「${t}」`)
                  actions.send(t)
                }}
              >
                {t}
              </button>
            ))}
          </div>
        ) : null}

        {spend ? <div className="agent-gate-doing">不确认就不会扣费；执行中也可以取消。</div> : null}
        {payload.note ? <div className="agent-gate-note">{payload.note}</div> : null}
        <div className="agent-gate-msg dim">{msg}</div>
      </div>
    </div>
  )
}

/** 进度卡：几/几 + 进度条 + 失败原因原文 + 取消。 */
export function ProgressCard(props: { payload: VfCardPayload; actions?: VfCardActions }): ReactElement {
  const p = props.payload
  const have = p.have === null || p.have === undefined ? null : Number(p.have)
  const total = p.total === null || p.total === undefined ? null : Number(p.total)
  const pct = have !== null && total ? Math.round((have / total) * 100) : null
  return (
    <div className="vf-cards">
      <style>{CSS}</style>
      <div className="agent-gate" data-vf-card="progress">
        <Head payload={p} badge="正在跑" extra={have !== null && total ? `${have}/${total}` : undefined} />
        {pct !== null ? (
          <div className="vf-bar">
            <i style={{ width: `${pct}%` }} />
          </div>
        ) : null}
        {p.message ? <div className="agent-gate-why">{p.message}</div> : null}
        {Array.isArray(p.items) && p.items.length ? (
          <div className="vf-shots">
            {p.items.map((it, i) => (
              <div key={i} className="vf-shot">
                · {it}
              </div>
            ))}
          </div>
        ) : null}
        {p.error ? <div className="gate-warn">没做成，原因原文：{p.error}</div> : null}
        {p.job_id && p.cancellable !== false && props.actions ? (
          <div className="agent-gate-actions">
            <button
              className="secondary-btn"
              data-vf-progress-act="cancel"
              onClick={() => props.actions?.send(`取消这一步（job ${p.job_id}）`)}
            >
              ⏹ 取消
            </button>
          </div>
        ) : null}
      </div>
    </div>
  )
}

/** 产物卡：`.msg-media` 同款缩略图 + 灯箱 + 剧本/分镜。 */
export function ArtifactCard(props: { payload: VfCardPayload }): ReactElement {
  const p = props.payload
  const media = Array.isArray(p.media) ? p.media : []
  const shots = Array.isArray(p.shots) ? p.shots : []
  const [box, setBox] = useState<VfMedia | null>(null)
  const body = String(p.script_text || p.body || '')

  return (
    <div className="vf-cards">
      <style>{CSS}</style>
      <div data-vf-card="artifact">
        {media.length ? (
          <div className="msg-media">
            <div className="msg-media-hint">点一下放大看（共 {media.length} 个）</div>
            <div className="msg-media-row">
              {media.map((m, i) => (
                <div key={i} className="msg-media-cell" title={m.name || ''} onClick={() => setBox(m)}>
                  {m.kind === 'image' ? (
                    <img src={m.url} alt={m.name || ''} loading="lazy" />
                  ) : (
                    <video src={m.url} muted preload="metadata" />
                  )}
                  <span className="msg-media-tag">{m.tag || (m.kind === 'image' ? '🖼 图片' : '▶ 视频')}</span>
                  {m.name ? <span className="msg-media-name">{m.name}</span> : null}
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {body ? (
          <div className="agent-gate" style={{ marginTop: 10 }}>
            <div className="agent-gate-head">
              <span className="agent-gate-step">剧本</span>
              <b>正文</b>
            </div>
            <div className="vf-script">{body.slice(0, 4000)}</div>
          </div>
        ) : null}

        {shots.length ? (
          <div className="agent-gate" style={{ marginTop: 10 }}>
            <div className="agent-gate-head">
              <span className="agent-gate-step">分镜</span>
              <b>{shots.length} 条</b>
            </div>
            <div className="vf-shots">
              {shots.map((s, i) => (
                <div key={i} className="vf-shot">
                  <span className="dim">#{s.index ?? i + 1}</span> {String(s.text || '').slice(0, 200)}
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      {box ? (
        <div className="vf-lightbox" onClick={() => setBox(null)}>
          {box.kind === 'image' ? <img src={box.url} alt={box.name || ''} /> : <video src={box.url} controls autoPlay />}
          <div className="vf-lightbox-name">{box.name || ''}</div>
        </div>
      ) : null}
    </div>
  )
}

/**
 * keyed 槽 `tool.call.toolview` 的入口组件。
 * @param props.phase - 工具阶段：`start` 出进度卡，`result` 按载荷 kind 出对应卡。
 * @param props.block - 阶段块（载荷在 `meta` 里）。
 * @param props.actions - 注入的动作（client 侧给 `conversation.send`）。
 */
export function VfToolRow(props: {
  phase?: string | undefined
  block?: unknown
  actions?: VfCardActions | undefined
}): ReactElement | null {
  const payload = useMemo(() => readPayload(props.block), [props.block])
  const actions = props.actions ?? { send: () => {} }

  if (props.phase === 'start' && (payload === null || payload.kind === 'progress')) {
    return <ProgressCard payload={payload ?? { kind: 'progress', title: '正在执行…' }} actions={actions} />
  }
  if (payload === null) {
    return (
      <div className="vf-cards">
        <style>{CSS}</style>
        <div className="agent-gate-note">VideoForge：这一步没有可展示的结果。</div>
      </div>
    )
  }
  switch (payload.kind) {
    case 'gate':
      return <GateCard payload={payload} actions={actions} />
    case 'progress':
      return <ProgressCard payload={payload} actions={actions} />
    case 'artifact':
      return <ArtifactCard payload={payload} />
    default:
      return (
        <div className="vf-cards">
          <style>{CSS}</style>
          <div className="agent-gate">
            <Head payload={payload} badge="结果" />
            <div className="agent-gate-why">{payload.script_text || payload.body || payload.title || ''}</div>
          </div>
        </div>
      )
  }
}
