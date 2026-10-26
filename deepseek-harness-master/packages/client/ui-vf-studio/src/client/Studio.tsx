/**
 * 创作工坊 · 页面本体。
 *
 * 两个独立功能页签（Lance 2026-10-24 要求"电商+复刻单独做成一个功能"）：
 *   ⬛ 视频复刻 —— 参考视频 → 镜头表/景别/运镜/色调/构图/台词/节奏
 *   ⬛ 电商商品视频 —— 商品 → 方案（平台规格/品类节拍/合规体检/卖点改写/AI 标识）
 *
 * 四条纪律：
 *  1. **不编数字**：所有数值来自 sidecar；拿不到就写"未提供"，不填默认值。
 *  2. **长活两步走**：复刻默认"估算"（不花钱），用户点头后才真跑。
 *  3. **AI 标识是选项**：不加 / 只给方案 / 真烧 —— 选"不加"照样放行，但把法律风险显示出来。
 *  4. **诚实的边界**：合规词表不是法律意见；平台规格未实机上传验证。界面上直接写着。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactElement } from 'react'
import {
  api,
  sidecarBase,
  setSidecarBase,
  type AiLabelPlan,
  type CategoryBrief,
  type EcomPlan,
  type PlatformBrief,
  type RemakeResult,
  type SellingPointRewrite,
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

const KIND_LABEL: Record<string, string> = {
  table: '命中对照表',
  already_measurable: '已有数字（不改）',
  claim_needs_proof: '主张缺数字',
  quality: '质量类',
  value: '划算类',
  effect: '效果类',
  social: '口碑类',
  feel: '触感类',
  unknown: '判断不了',
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
  const [status, setStatus] = useState<{ remake?: ServiceStatus; ecom?: ServiceStatus; error?: string }>({})

  const load = useCallback(async () => {
    try {
      const [remake, ecom] = await Promise.all([
        api.remakeStatus().catch((e: unknown) => ({ error: String(e) }) as ServiceStatus),
        api.ecommerceStatus().catch((e: unknown) => ({ error: String(e) }) as ServiceStatus),
      ])
      setStatus({ remake, ecom })
    } catch (error) {
      setStatus({ error: String(error) })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const r = status.remake
  const e = status.ecom
  return (
    <div style={{ ...S.dim, marginBottom: 14 }}>
      <span>
        复刻：ffmpeg {r?.ffmpeg ? '✓' : '✗'} · ASR Key {r?.asr?.configured ? '✓' : '✗'} · 风格档{' '}
        {r?.style_profiles ? Object.keys(r.style_profiles).length : 0}
      </span>
      <span style={{ margin: '0 10px', opacity: 0.35 }}>|</span>
      <span>
        电商：平台 {e?.platforms?.length ?? 0} · 品类 {e?.categories?.length ?? 0} · 合规组{' '}
        {e?.compliance_lists?.length ?? 0}
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

/* ───────────────────────── 电商商品视频 ───────────────────────── */

function LabelChoice(props: {
  value: 'off' | 'plan' | 'apply'
  onChange: (v: 'off' | 'plan' | 'apply') => void
}): ReactElement {
  return (
    <div style={{ ...S.row, gap: 12 }}>
      <span style={S.dim}>AI 标识：</span>
      {(['off', 'plan', 'apply'] as const).map((v) => (
        <label key={v} style={{ fontSize: 12 }}>
          <input
            type="radio"
            name="vf-studio-ai-label"
            checked={props.value === v}
            onChange={() => props.onChange(v)}
          />{' '}
          {v === 'off' ? '不加' : v === 'plan' ? '只给方案' : '真烧进画面'}
        </label>
      ))}
    </div>
  )
}

function EcomPanel(): ReactElement {
  const [platforms, setPlatforms] = useState<PlatformBrief[]>([])
  const [categories, setCategories] = useState<CategoryBrief[]>([])
  const [product, setProduct] = useState('')
  const [platform, setPlatform] = useState('')
  const [category, setCategory] = useState('')
  const [points, setPoints] = useState('')
  const [duration, setDuration] = useState('')
  const [price, setPrice] = useState('')
  const [aiLabel, setAiLabel] = useState<'off' | 'plan' | 'apply'>('plan')
  const [auditText, setAuditText] = useState('')
  const [plan, setPlan] = useState<EcomPlan | null>(null)
  const [rewrites, setRewrites] = useState<SellingPointRewrite[] | null>(null)
  const [audit, setAudit] = useState<{ errors?: number; warnings?: number; score?: number; hits?: Array<{ term: string; severity: string; category: string; why?: string }> } | null>(null)
  const [labelPlan, setLabelPlan] = useState<AiLabelPlan | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    void (async () => {
      try {
        const data = await api.platforms()
        setPlatforms(data.platforms ?? [])
        setCategories(data.categories ?? [])
        const first = data.platforms?.[0]?.id
        if (first) setPlatform((cur) => cur || first)
      } catch (err) {
        setError(`读平台/品类失败：${String(err)}`)
      }
    })()
  }, [])

  const pointsList = useMemo(
    () =>
      points
        .split(/[\n、,，;；]+/)
        .map((x) => x.trim())
        .filter(Boolean),
    [points],
  )

  const doPlan = useCallback(async () => {
    if (!product.trim()) {
      setError('先填商品名')
      return
    }
    setError('')
    setBusy('plan')
    try {
      const data = await api.ecomPlan({
        product: product.trim(),
        platform,
        category,
        selling_points: pointsList,
        duration_sec: Number(duration) || 0,
        price_line: price,
        ai_label: aiLabel,
      })
      setPlan(data)
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy('')
    }
  }, [product, platform, category, pointsList, duration, price, aiLabel])

  const doRewrite = useCallback(async () => {
    if (!pointsList.length) {
      setError('先填卖点')
      return
    }
    setError('')
    setBusy('points')
    try {
      const data = await api.ecomPoints(pointsList)
      setRewrites(data.items ?? [])
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy('')
    }
  }, [pointsList])

  const doAudit = useCallback(async () => {
    if (!auditText.trim()) {
      setError('先贴要检查的文案')
      return
    }
    setError('')
    setBusy('audit')
    try {
      const data = await api.ecomAudit(auditText)
      setAudit(data)
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy('')
    }
  }, [auditText])

  const doLabelPlan = useCallback(async () => {
    setError('')
    setBusy('label')
    try {
      const data = await api.aiLabel({ mode: 'start_and_badge', start_seconds: 3 })
      setLabelPlan(data)
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy('')
    }
  }, [])

  const beatBar = useMemo(() => {
    const beats = plan?.beats ?? []
    const maxEnd = Math.max(1, ...beats.map((b) => b.end))
    return beats.map((b) => (
      <div
        key={`${b.beat}-${b.start}`}
        title={`${b.beat} ${b.start}–${b.end}s`}
        style={{
          width: `${((b.end - b.start) / maxEnd) * 100}%`,
          background: 'rgba(122,193,48,0.22)',
          borderRight: '1px solid rgba(0,0,0,0.4)',
          fontSize: 10,
          padding: '2px 3px',
          overflow: 'hidden',
          whiteSpace: 'nowrap',
        }}
      >
        {b.beat}
      </div>
    ))
  }, [plan])

  return (
    <div>
      <div style={S.card}>
        <div style={{ ...S.dim, marginBottom: 10 }}>
          商品 → 视频方案。按品类转化漏斗排段（各品类有各自的必须/禁止项），时长精确切到目标秒数
          （能直接过剧本体检）。<b>纯本地、不花钱。</b>
        </div>
        <div style={S.grid}>
          <label style={S.label}>
            商品名
            <input style={S.input} value={product} onChange={(e) => setProduct(e.target.value)} placeholder="轻薄防晒衣" />
          </label>
          <label style={S.label}>
            平台
            <select style={S.input} value={platform} onChange={(e) => setPlatform(e.target.value)}>
              {platforms.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label style={S.label}>
            品类
            <select style={S.input} value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">（不选，用通用四段）</option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label style={S.label}>
            目标时长(s)
            <input style={S.input} value={duration} onChange={(e) => setDuration(e.target.value)} placeholder="不填用平台推荐值" />
          </label>
          <label style={S.label}>
            价格/优惠
            <input style={S.input} value={price} onChange={(e) => setPrice(e.target.value)} placeholder="到手 89 元" />
          </label>
        </div>
        <label style={{ ...S.label, marginBottom: 10 }}>
          卖点（一行一个，或用顿号分隔）
          <textarea
            style={{ ...S.input, minHeight: 60, resize: 'vertical' }}
            value={points}
            onChange={(e) => setPoints(e.target.value)}
            placeholder={'UPF50+\n重量 90g\n可收纳进巴掌'}
          />
        </label>
        <div style={{ ...S.row, marginBottom: 8 }}>
          <button type="button" style={S.btn} disabled={busy !== ''} onClick={() => void doRewrite()}>
            {busy === 'points' ? '改写着…' : '✍ 卖点改成可拍摄'}
          </button>
          <button type="button" style={S.btnPrimary} disabled={busy !== ''} onClick={() => void doPlan()}>
            {busy === 'plan' ? '生成中…' : '生成方案'}
          </button>
          <button type="button" style={S.btn} disabled={busy !== ''} onClick={() => void doLabelPlan()}>
            {busy === 'label' ? '读取中…' : 'ℹ AI 标识要求'}
          </button>
        </div>
        <LabelChoice value={aiLabel} onChange={setAiLabel} />
        {error ? <div style={{ ...S.err, marginTop: 8 }}>{error}</div> : null}
      </div>

      <div style={S.card}>
        <div style={{ ...S.dim, marginBottom: 8 }}>
          文案合规体检（广告法绝对化用语 / 化妆品医疗暗示 / 食品保健 / 三品一械 / 平台红线）
        </div>
        <textarea
          style={{ ...S.input, minHeight: 56, resize: 'vertical', marginBottom: 8 }}
          value={auditText}
          onChange={(e) => setAuditText(e.target.value)}
          placeholder="把要发的口播稿/字幕贴进来…"
        />
        <button type="button" style={S.btn} disabled={busy !== ''} onClick={() => void doAudit()}>
          {busy === 'audit' ? '体检中…' : '体检这段文案'}
        </button>
        {audit ? (
          <div style={{ marginTop: 10 }}>
            <span style={S.dim}>得分 </span>
            <b style={audit.errors ? S.err : S.ok}>{audit.score}</b>
            <span style={S.dim}>
              {' '}
              （{audit.errors ?? 0} 错误 / {audit.warnings ?? 0} 待举证）
            </span>
            {(audit.hits ?? []).map((h, i) => (
              <div key={`${h.term}-${i}`} style={h.severity === 'error' ? S.err : S.warn}>
                {h.severity === 'error' ? '✗' : '⚠'} 「{h.term}」<span style={S.dim}>[{h.category}]</span>{' '}
                {h.why ?? ''}
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {rewrites ? (
        <div style={S.card}>
          <b>卖点改写</b>
          <span style={S.dim}>（方向建议，落地要换成你真能拍/能测的内容）</span>
          <div style={{ marginTop: 8 }}>
            {rewrites.map((x, i) => (
              <div key={`${x.original}-${i}`} style={{ marginBottom: 8 }}>
                <span style={{ color: x.changed ? '#7ac130' : x.shootable ? '#8a8a8a' : '#e0b25f' }}>●</span>{' '}
                <b>{x.original}</b> <span style={S.dim}>[{KIND_LABEL[x.kind] ?? x.kind}]</span>
                <div style={{ marginLeft: 14 }}>→ {x.rewritten}</div>
                {x.why ? <div style={{ ...S.dim, marginLeft: 14 }}>{x.why}</div> : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {labelPlan ? (
        <div style={S.card}>
          <b>AI 内容标识要求</b>
          <span style={S.dim}>（{labelPlan.checklist?.effective_from ?? '2025-09-01'} 起施行）</span>
          <div style={{ ...S.dim, marginTop: 6 }}>{labelPlan.checklist?.note ?? ''}</div>
          <div style={{ marginTop: 8 }}>
            {(labelPlan.plan?.steps ?? []).map((s) => (
              <div key={s.layer}>
                <span style={S.badge}>{s.layer}</span>
                {s.kind} {s.text ? <span style={S.dim}>— {s.text}</span> : null}
              </div>
            ))}
          </div>
          <div style={{ ...S.dim, marginTop: 6 }}>
            字体：{labelPlan.plan?.font ?? '—'}（{labelPlan.plan?.font_available ? '可用' : '不可用'}）
          </div>
        </div>
      ) : null}

      {plan ? (
        <div style={S.card}>
          <div style={{ ...S.row, gap: 18, marginBottom: 10 }}>
            <span>
              <span style={S.dim}>商品 </span>
              <b>{plan.product}</b>
            </span>
            <span>
              <span style={S.dim}>平台 </span>
              <b>{plan.platform?.name_zh}</b>
            </span>
            <span>
              <span style={S.dim}>品类 </span>
              <b>{plan.category?.name_zh ?? '通用四段'}</b>
            </span>
            <span>
              <span style={S.dim}>时长 </span>
              <b>{sec(plan.target_duration)}</b>
            </span>
            <span>
              <span style={S.dim}>合规 </span>
              <b style={plan.compliance?.errors ? S.err : S.ok}>
                {plan.compliance?.errors ?? 0} 错误 / {plan.compliance?.warnings ?? 0} 待举证
              </b>
            </span>
          </div>

          <div style={{ display: 'flex', height: 26, borderRadius: 6, overflow: 'hidden', marginBottom: 10 }}>
            {beatBar}
          </div>

          {(plan.platform_check?.checks ?? []).length ? (
            <div style={{ ...S.dim, marginBottom: 10 }}>
              平台核对：
              {(plan.platform_check?.checks ?? []).map((c) => (
                <span key={c.id} style={{ marginRight: 12 }}>
                  <span style={{ color: GATE_COLOR[c.level] ?? '#8a8a8a' }}>●</span> {c.title}{' '}
                  <span style={S.dim}>{c.detail}</span>
                </span>
              ))}
            </div>
          ) : null}

          <div style={{ ...S.dim, marginBottom: 10 }}>
            <b>AI 内容标识（可选项）</b>：选择{' '}
            <b>{plan.ai_label?.choice === 'off' ? '不加' : plan.ai_label?.choice === 'apply' ? '出片后真烧' : '只给方案'}</b>
            {' — '}
            {plan.ai_label?.text ?? ''}
            {plan.ai_label?.warning ? <div style={S.warn}>{plan.ai_label.warning}</div> : null}
            {plan.ai_label?.next_step ? <div>下一步：{plan.ai_label.next_step}</div> : null}
            {(plan.ai_label?.manual_steps ?? []).length ? (
              <div>⚠ 还需你做：{(plan.ai_label?.manual_steps ?? []).map((m) => m.kind).join('、')}</div>
            ) : null}
          </div>

          {plan.selling_point_rewrite?.changed ? (
            <div style={{ ...S.ok, marginBottom: 10 }}>
              ✍ 已把 {plan.selling_point_rewrite.changed} 个空泛卖点改写成可拍摄表述（原话见 selling_points_raw）
            </div>
          ) : null}

          {(plan.warnings ?? []).length ? (
            <div style={{ ...S.warn, marginBottom: 10 }}>
              {(plan.warnings ?? []).map((w) => (
                <div key={w}>⚠ {w}</div>
              ))}
            </div>
          ) : null}

          <div style={{ fontWeight: 600, margin: '12px 0 6px' }}>
            分镜（{plan.scenes?.length ?? 0} 场）
          </div>
          {(plan.scenes ?? []).map((s) => (
            <div key={s.scene_number} style={S.shot}>
              <div>
                <b>
                  #{s.scene_number} {s.title}
                </b>{' '}
                <span style={S.dim}>{sec(s.duration_seconds)}</span>{' '}
                <span style={S.badge}>{s.beat}</span>
              </div>
              <div style={S.dim}>📷 {(s.actions ?? []).join('；')}</div>
              {(s.dialogues ?? []).length ? (
                <div>
                  🎙 {(s.dialogues ?? []).map((d) => `${d.character ?? ''}：${d.text ?? ''}`).join(' / ')}
                </div>
              ) : null}
              {(s.must_not ?? []).length ? (
                <div style={{ ...S.warn, fontSize: 11 }}>✗ 不许：{(s.must_not ?? []).join('、')}</div>
              ) : null}
            </div>
          ))}

          <div style={{ ...S.dim, marginTop: 10 }}>
            scenes 用的是 VideoForge 剧本 schema，可直接进剧本体检 / 分镜规划。
          </div>
        </div>
      ) : null}

      <div style={{ ...S.dim, marginTop: 10 }}>
        边界（写在这里免得被当成保证）：平台规格未实机上传验证；品类秒数是推导值；合规词表不是法律意见；
        卖点改写只给方向，落地要换成真实可测的内容。
      </div>
    </div>
  )
}

/* ───────────────────────── 页面 ───────────────────────── */

type StudioTab = 'remake' | 'ecommerce'

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
        <TabButton active={tab === 'ecommerce'} onClick={() => setTab('ecommerce')}>
          🛒 电商商品视频
        </TabButton>
      </div>

      {tab === 'remake' ? <RemakePanel /> : <EcomPanel />}
    </div>
  )
}
