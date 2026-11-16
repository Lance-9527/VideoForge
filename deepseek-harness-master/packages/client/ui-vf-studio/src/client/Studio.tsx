/**
 * 创作工坊 · 页面本体。
 *
 * ★ 2026-11-03：**电商商品视频已独立出去**，成为「掌柜 AI」应用
 *   （packages/client/ui-shop-brand + packages/video/shop-agent）。
 *   VideoForge 这一侧只保留「视频复刻」页签 —— 两块业务不再混在一个应用里。
 *
 * 四条纪律：
 *  1. **不编数字**：所有数值来自 sidecar；拿不到就写"未提供"，不填默认值。
 *  2. **长活两步走**：复刻默认"估算"（不花钱），用户点头后才真跑。
 *  3. **AI 标识是选项**：不加 / 只给方案 / 真烧 —— 选"不加"照样放行，但把法律风险显示出来。
 *  4. **诚实的边界**：合规词表不是法律意见；平台规格未实机上传验证。界面上直接写着。
 */
import { useCallback, useEffect, useState } from 'react'
import type { ReactElement } from 'react'
import {
  api,
  sidecarBase,
  setSidecarBase,
  type RemakeResult,
  type ServiceStatus,
} from './api.ts'

/* ───────────────────────── 样式（内联，避免额外 CSS 管线）───────────────────────── */

const S = {
  wrap: { padding: '20px 24px 48px', maxWidth: 1180, margin: '0 auto', fontSize: 13 } as const,
  tabs: { display: 'flex', gap: 8, marginBottom: 14 } as const,
  card: {
    border: '1px solid rgba(255,255,255,0.10)',
    borderRadius: 12,
    padding: 16,
    marginBottom: 14,
    background: 'rgba(255,255,255,0.02)',
  } as const,
  row: { display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' } as const,
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit,minmax(190px,1fr))',
    gap: 10,
    marginBottom: 10,
  } as const,
  label: { fontSize: 12, display: 'block' } as const,
  input: {
    width: '100%',
    padding: '6px 8px',
    borderRadius: 6,
    border: '1px solid rgba(255,255,255,0.16)',
    background: 'rgba(0,0,0,0.25)',
    color: 'inherit',
    fontSize: 12,
    boxSizing: 'border-box',
  } as const,
  btnPrimary: {
    padding: '6px 14px',
    borderRadius: 6,
    border: '1px solid rgba(122,193,48,0.55)',
    background: 'rgba(122,193,48,0.18)',
    color: 'inherit',
    cursor: 'pointer',
    fontSize: 12,
  } as const,
  btn: {
    padding: '6px 14px',
    borderRadius: 6,
    border: '1px solid rgba(255,255,255,0.18)',
    background: 'rgba(255,255,255,0.05)',
    color: 'inherit',
    cursor: 'pointer',
    fontSize: 12,
  } as const,
  dim: { opacity: 0.62, fontSize: 12 } as const,
  err: { color: '#e07a5f', fontSize: 12 } as const,
  ok: { color: '#7ac130', fontSize: 12 } as const,
  warn: { color: '#e0b25f', fontSize: 12 } as const,
  mono: { fontFamily: 'ui-monospace,Consolas,monospace', fontSize: 11 } as const,
  shot: {
    border: '1px solid rgba(255,255,255,0.08)',
    borderRadius: 8,
    padding: '8px 10px',
    marginBottom: 6,
  } as const,
  badge: {
    display: 'inline-block',
    padding: '1px 6px',
    borderRadius: 4,
    border: '1px solid rgba(255,255,255,0.16)',
    fontSize: 11,
    marginRight: 6,
  } as const,
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 } as const,
  td: { padding: '4px 6px', borderBottom: '1px solid rgba(255,255,255,0.07)' } as const,
}

function sec(n: number | undefined): string {
  if (n == null || Number.isNaN(n)) return '—'
  return `${Math.round(n * 10) / 10}s`
}

const GATE_COLOR: Record<string, string> = {
  pass: '#7ac130',
  fail: '#e07a5f',
  unknown: '#8a8a8a',
  skipped: '#8a8a8a',
}


/* ───────────────────────── 共用小件 ───────────────────────── */

function TabButton(props: {
  active: boolean
  onClick: () => void
  children: ReactElement | string
}): ReactElement {
  return (
    <button
      type="button"
      onClick={props.onClick}
      style={props.active ? S.btnPrimary : S.btn}
    >
      {props.children}
    </button>
  )
}

/** 自检条：让用户一眼看到"这台机器能不能干活"。 */
function SelfCheck(): ReactElement {
  const [status, setStatus] = useState<{ remake?: ServiceStatus; error?: string }>({})

  const load = useCallback(async () => {
    try {
      // ★ 电商已独立成「掌柜 AI」应用，这里只看复刻这一侧
      const remake = await api.remakeStatus().catch((e: unknown) => ({ error: String(e) }) as ServiceStatus)
      setStatus({ remake })
    } catch (error) {
      setStatus({ error: String(error) })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const r = status.remake
  return (
    <div style={{ ...S.dim, marginBottom: 14 }}>
      <span>
        复刻：ffmpeg {r?.ffmpeg ? '✓' : '✗'} · ASR Key {r?.asr?.configured ? '✓' : '✗'} · 风格档{' '}
        {r?.style_profiles ? Object.keys(r.style_profiles).length : 0}
      </span>
      {status.error ? <span style={{ ...S.err, marginLeft: 10 }}>自检失败：{status.error}</span> : null}
      <span style={{ ...S.dim, marginLeft: 10 }}>（sidecar {sidecarBase()}）</span>
    </div>
  )
}

/* ───────────────────────── 视频复刻 ───────────────────────── */

function RemakePanel(): ReactElement {
  const [path, setPath] = useState('')
  const [useOcr, setUseOcr] = useState(true)
  const [useWhisper, setUseWhisper] = useState(true)
  const [useSilicon, setUseSilicon] = useState(true)
  const [maxShots, setMaxShots] = useState(24)
  const [busy, setBusy] = useState<'' | 'estimate' | 'run'>('')
  const [result, setResult] = useState<RemakeResult | null>(null)
  const [error, setError] = useState('')

  const run = useCallback(
    async (dryRun: boolean) => {
      if (!path.trim()) {
        setError('先填参考视频的绝对路径')
        return
      }
      setError('')
      setBusy(dryRun ? 'estimate' : 'run')
      try {
        const data = await api.remakeAnalyze({
          video_path: path.trim(),
          dry_run: dryRun,
          use_ocr: useOcr,
          use_whisper: useWhisper,
          use_siliconflow: useSilicon,
          max_shots: maxShots,
        })
        setResult(data)
      } catch (err) {
        setError(String(err))
      } finally {
        setBusy('')
      }
    },
    [path, useOcr, useWhisper, useSilicon, maxShots],
  )

  const gates = result?.pacing?.gates ?? []
  const shots = result?.shots_preview ?? []

  return (
    <div>
      <div style={S.card}>
        <div style={{ ...S.dim, marginBottom: 8 }}>
          给一段参考视频 → 拆成可照着做的施工图（镜头表 / 景别 / 运镜 / 色调 / 构图 / 台词 / 节奏）。
          <b>复刻的是手法，不是内容</b>：人物与剧情要重新设计。
        </div>
        <div style={{ ...S.row, marginBottom: 10 }}>
          <input
            style={{ ...S.input, flex: '1 1 420px', minWidth: 260 }}
            placeholder="参考视频绝对路径，例如 D:\\videos\\ref.mp4"
            value={path}
            onChange={(ev) => setPath(ev.target.value)}
          />
          <button type="button" style={S.btn} disabled={busy !== ''} onClick={() => void run(true)}>
            {busy === 'estimate' ? '估算中…' : '① 估算（不花钱）'}
          </button>
          <button type="button" style={S.btnPrimary} disabled={busy !== ''} onClick={() => void run(false)}>
            {busy === 'run' ? '复刻中（可能几分钟）…' : '② 开始复刻'}
          </button>
        </div>
        <div style={{ ...S.row, ...S.dim }}>
          <label>
            <input type="checkbox" checked={useOcr} onChange={(e) => setUseOcr(e.target.checked)} /> OCR 硬字幕
          </label>
          <label>
            <input type="checkbox" checked={useWhisper} onChange={(e) => setUseWhisper(e.target.checked)} />{' '}
            本地 whisper 出时间戳（免费）
          </label>
          <label>
            <input type="checkbox" checked={useSilicon} onChange={(e) => setUseSilicon(e.target.checked)} />{' '}
            硅基流动校对台词（花钱，约 ¥0.07/3 分钟）
          </label>
          <label>
            最多分析{' '}
            <input
              type="number"
              min={0}
              max={200}
              value={maxShots}
              onChange={(e) => setMaxShots(Number(e.target.value) || 0)}
              style={{ ...S.input, width: 64, display: 'inline-block' }}
            />{' '}
            镜（0=全部）
          </label>
        </div>
        {error ? <div style={{ ...S.err, marginTop: 8 }}>{error}</div> : null}
      </div>

      {result?.dry_run ? (
        <div style={S.card}>
          <b>估算结果</b>
          <span style={S.dim}>（没有动文件、没有花钱）</span>
          <div style={{ ...S.row, marginTop: 8, gap: 18 }}>
            <span>
              <span style={S.dim}>镜头数约 </span>
              <b>{result.estimated_shots ?? '?'}</b>
            </span>
            <span>
              <span style={S.dim}>预计花费 </span>
              <b>¥{result.est?.est_cost_yuan ?? 0}</b>
            </span>
            <span>
              <span style={S.dim}>预计耗时 </span>
              <b>{result.est?.est_seconds ?? '?'}s</b>
            </span>
            {result.source ? (
              <span style={S.dim}>
                {result.source.width}×{result.source.height} · {sec(result.source.duration)} ·{' '}
                {result.source.aspect ?? ''}
              </span>
            ) : null}
          </div>
          <div style={{ ...S.dim, marginTop: 8 }}>确认没问题就点「② 开始复刻」。</div>
        </div>
      ) : null}

      {result && !result.dry_run ? (
        <div style={S.card}>
          <div style={{ ...S.row, gap: 18, marginBottom: 10 }}>
            <span>
              <span style={S.dim}>镜头 </span>
              <b>{result.shots}</b>
            </span>
            <span>
              <span style={S.dim}>平均/中位/最长 </span>
              <b>
                {sec(result.pacing?.mean_shot)} / {sec(result.pacing?.median_shot)} /{' '}
                {sec(result.pacing?.max_shot)}
              </b>
            </span>
            <span>
              <span style={S.dim}>切点/分钟 </span>
              <b>{result.pacing?.cuts_per_min ?? '—'}</b>
            </span>
            <span>
              <span style={S.dim}>节奏 </span>
              <b>{result.pacing?.pace_class ?? '—'}</b>
            </span>
            <span>
              <span style={S.dim}>风格档 </span>
              <b>{result.style_profile_label ?? result.style_profile ?? '—'}</b>
            </span>
          </div>

          <div style={{ marginBottom: 10 }}>
            {gates.map((g) => (
              <span key={g.id} style={{ display: 'inline-block', margin: '0 12px 6px 0', fontSize: 12 }}>
                <span style={{ color: GATE_COLOR[g.state] ?? '#8a8a8a' }}>●</span> {g.title}{' '}
                <b>{g.value}</b> <span style={S.dim}>[{g.state}]</span>
              </span>
            ))}
          </div>

          <div style={{ ...S.dim, marginBottom: 10 }}>
            台词 {result.dialogue_stats?.lines ?? 0} 行 · 字数 {result.dialogue_stats?.chars ?? 0} ·{' '}
            {result.dialogue_stats?.chars_per_sec ?? 0} 字/秒 · 句长变异系数{' '}
            {result.dialogue_stats?.line_len_cv ?? 0}
            {result.asr?.ok
              ? `　ASR ✓（whisper ${result.asr.whisper_model ?? ''}，花费 ¥${result.asr.cost_yuan ?? 0}）`
              : result.asr
                ? `　ASR ✗ ${result.asr.error ?? ''}`
                : ''}
          </div>

          {result.spec_path ? (
            <div style={{ ...S.dim, marginBottom: 10 }}>
              规格文件：<code style={S.mono}>{result.spec_path}</code>
            </div>
          ) : null}

          {(result.warnings ?? []).length ? (
            <div style={{ ...S.warn, marginBottom: 10 }}>
              {(result.warnings ?? []).map((w) => (
                <div key={w}>⚠ {w}</div>
              ))}
            </div>
          ) : null}

          <div style={{ fontWeight: 600, margin: '12px 0 6px' }}>
            分镜预览 <span style={S.dim}>（最多 12 镜）</span>
          </div>
          {shots.map((s) => (
            <div key={s.index} style={S.shot}>
              <div>
                <b>#{s.index}</b>{' '}
                <span style={S.dim}>
                  {sec(s.start)}–{sec(s.end)}（{sec(s.duration)}）
                </span>{' '}
                <span style={S.badge}>{s.shot_size ?? '?'}</span>
                <span style={S.badge}>{s.camera_motion ?? '?'}</span>
                {s.palette ? <span style={S.badge}>{s.palette}</span> : null}
              </div>
              {s.description ? <div style={S.dim}>{s.description}</div> : null}
              {(s.dialogue ?? []).length ? <div>🎙 {(s.dialogue ?? []).join(' / ')}</div> : null}
              {s.prompt ? (
                <div style={{ ...S.dim, ...S.mono, marginTop: 2 }}>prompt: {s.prompt}</div>
              ) : null}
            </div>
          ))}

          {result.compliance ? (
            <div style={{ ...S.dim, borderTop: '1px solid rgba(255,255,255,0.08)', paddingTop: 10 }}>
              <b>复刻边界</b>
              <div>可复刻：{(result.compliance.can_replicate ?? []).join('、')}</div>
              <div>不可复刻：{(result.compliance.must_not_replicate ?? []).join('、')}</div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
type StudioTab = 'remake'

export function VfStudio(): ReactElement {
  const [tab, setTab] = useState<StudioTab>('remake')
  const [showBase, setShowBase] = useState(false)
  const [base, setBase] = useState(sidecarBase())

  return (
    <div style={S.wrap}>
      <div style={{ ...S.row, justifyContent: 'space-between', marginBottom: 14 }}>
        <h2 style={{ margin: 0, fontSize: 18 }}>🧰 创作工坊</h2>
        <div style={S.row}>
          <button type="button" style={S.btn} onClick={() => setShowBase((v) => !v)}>
            连接设置
          </button>
        </div>
      </div>

      {showBase ? (
        <div style={{ ...S.card, ...S.row }}>
          <span style={S.dim}>sidecar 地址</span>
          <input style={{ ...S.input, flex: '1 1 260px' }} value={base} onChange={(e) => setBase(e.target.value)} />
          <button
            type="button"
            style={S.btnPrimary}
            onClick={() => {
              setSidecarBase(base)
              setShowBase(false)
            }}
          >
            保存
          </button>
        </div>
      ) : null}

      <SelfCheck />

      <div style={S.tabs}>
        <TabButton active={tab === 'remake'} onClick={() => setTab('remake')}>
          🎬 视频复刻
        </TabButton>
      </div>

      {tab === 'remake' ? <RemakePanel /> : <>{''}</>}
    </div>
  )
}
