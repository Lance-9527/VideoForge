/**
 * 掌柜 AI · 电商工作台。
 *
 * ★ 本文件是**从 VideoForge 创作工坊抽取**出来的电商面板
 *   （原实现：`packages/client/ui-vf-studio/src/client/Studio.tsx` 的 `EcomPanel`）。
 *   2026-11-03 要求「电商 + 棚拍独立出去，做成新的电商 agent 应用」。
 *
 * 抽取时只留电商相关：平台规格 / 品类节拍 / 合规体检 / 卖点改写 / AI 标识；
 * 「视频复刻」页签不属于电商，没有带过来。
 *
 * 四条纪律（沿用，不许破坏）：
 *  1. **不编数字**：所有数值来自 sidecar；拿不到就写"未提供"，不填默认值。
 *  2. **AI 标识是选项**：不加 / 只给方案 / 真烧 —— 选"不加"照样放行，但把法律风险显示出来。
 *  3. **诚实的边界**：合规词表不是法律意见；平台规格未实机上传验证。界面上直接写着。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import {
  api,
  sidecarBase,
  type AiLabelPlan,
  type CategoryBrief,
  type EcomPlan,
  type PlatformBrief,
  type SellingPointRewrite,
  type ServiceStatus,
} from './api.ts'
import { setVfView } from './view.ts'
import { writeShootTask } from './shoot-task.ts'
import { produceVideo, type ProduceProgress, type ProduceResult } from './video-production.ts'

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
function SelfCheck(): ReactElement {
  const [status, setStatus] = useState<{ ecom?: ServiceStatus; error?: string }>({})

  const load = useCallback(async () => {
    try {
      // ★ 独立成电商应用后只查电商这一侧（复刻不是这个应用的业务）
      const ecom = await api.ecommerceStatus().catch((e: unknown) => ({ error: String(e) }) as ServiceStatus)
      setStatus({ ecom })
    } catch (error) {
      setStatus({ error: String(error) })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const e = status.ecom
  return (
    <div style={{ ...S.dim, marginBottom: 14 }}>
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
  /** 每镜的画面素材（按顺序对应镜头）。 */
  const [shotImages, setShotImages] = useState<File[]>([])
  /** 成片进度。 */
  const [produceProgress, setProduceProgress] = useState<ProduceProgress | null>(null)
  /** 成片结果。 */
  const [produceResult, setProduceResult] = useState<ProduceResult | null>(null)
  /** 成片失败原因。 */
  const [produceError, setProduceError] = useState('')
  /** 商品图选择框。 */
  const imageInputRef = useRef<HTMLInputElement | null>(null)

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

  /** 生成成片：方案 → 逐镜素材 → 合成（免费配音 + 字幕）→ AI 标识 → mp4 */
  const doProduce = useCallback(async (): Promise<void> => {
    if (plan === null) { setProduceError('先「生成方案」'); return }
    const scenes = plan.scenes ?? []
    if (scenes.length === 0) { setProduceError('方案里没有镜头'); return }
    if (shotImages.length === 0) {
      setProduceError('先上传商品图（每一镜要有画面，否则成片会像卡住不动）')
      return
    }
    setBusy('produce')
    setProduceError('')
    setProduceResult(null)
    setProduceProgress({ stage: '准备', percent: 0, message: '准备素材…' })
    try {
      const stamp = Date.now()
      const images = await Promise.all(shotImages.map(async (file) => ({
        filename: `shot_${file.name.replace(/\.[^.]+$/u, '').slice(0, 40)}_${stamp}.png`,
        bytes: new Uint8Array(await file.arrayBuffer()),
      })))
      const result = await produceVideo({
        base: sidecarBase(),
        title: `${product || '商品'} · ${platform}`,
        product,
        shots: scenes.map((scene) => ({
          scene_number: scene.scene_number,
          title: scene.title,
          beat: scene.beat,
          duration_seconds: scene.duration_seconds,
          actions: scene.actions,
          // ★ 台词要带过去，否则成片既没字幕也没配音（sidecar 只认 timeline 里的 dialogue）
          dialogues: (scene.dialogues ?? []).map((dialogue) => ({
            character: dialogue.character ?? '',
            text: dialogue.text ?? '',
          })),
        })),
        images,
        aspect: '9:16',
        onProgress: (progress) => { setProduceProgress(progress) },
      })
      setProduceResult(result)
      setProduceProgress(null)
    } catch (err) {
      setProduceError(err instanceof Error ? err.message : String(err))
      setProduceProgress(null)
    } finally {
      setBusy('')
    }
  }, [plan, product, platform, shotImages])

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

          {/* 成片：从方案直接产出可发布的 mp4（免费配音 + 字幕 + 三层 AI 标识，零付费） */}
          <div style={{ marginTop: 14, padding: 12, border: '1px solid rgba(255,255,255,0.10)', borderRadius: 10 }}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>① 上传每镜画面（商品图）</div>
            <div style={{ ...S.dim, marginBottom: 8 }}>
              一镜一张，按镜头顺序；不够的镜头会沿用上一张。没有画面的话成片会像卡住不动
              （sidecar 自己也会这样警告）。
            </div>
            <input
              type="file"
              accept="image/*"
              multiple
              style={{ display: 'none' }}
              ref={imageInputRef}
              onChange={(event) => {
                const files = Array.from(event.target.files ?? [])
                event.target.value = ''
                if (files.length > 0) { setShotImages((cur) => [...cur, ...files]) }
              }}
            />
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button
                type="button"
                style={S.btn}
                onClick={() => { imageInputRef.current?.click() }}
              >
                选择图片（已选 {shotImages.length} 张）
              </button>
              {shotImages.length > 0 ? (
                <button type="button" style={S.btn} onClick={() => { setShotImages([]) }}>
                  清空
                </button>
              ) : null}
              <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                {shotImages.map((file, index) => (
                  <span
                    key={`${file.name}-${index}`}
                    style={{ fontSize: 11, padding: '2px 7px', borderRadius: 10, background: 'rgba(255,255,255,0.06)', color: '#c9d1d9' }}
                  >
                    {index + 1}. {file.name.slice(0, 18)}
                  </span>
                ))}
              </div>
            </div>

            <div style={{ fontWeight: 600, margin: '14px 0 8px' }}>② 生成成片</div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button
                type="button"
                style={S.btnPrimary}
                disabled={busy !== ''}
                onClick={() => { void doProduce() }}
              >
                {busy === 'produce' ? '生成中…' : '生成成片（mp4）'}
              </button>
              <button
                type="button"
                style={S.btn}
                disabled={busy !== ''}
                onClick={() => {
                  const scenes = plan.scenes ?? []
                  if (scenes.length === 0) { return }
                  writeShootTask({
                    product: product.trim(),
                    platform,
                    shots: scenes.map((scene) => ({
                      scene_number: scene.scene_number,
                      title: scene.title,
                      beat: scene.beat,
                      duration_seconds: scene.duration_seconds,
                      actions: scene.actions,
                    })),
                    createdAt: new Date().toISOString(),
                  })
                  setVfView({ section: 'shot' })
                }}
              >
                高斯泼溅机位预演
              </button>
              <span style={{ ...S.dim }}>
                配音用免费的 Edge TTS，字幕自动烧录，成片带三层 AI 生成标识，全程零付费。
              </span>
            </div>

            {produceProgress !== null ? (
              <div style={{ marginTop: 10, fontSize: 12, color: '#8ab4f8' }}>
                {produceProgress.stage} · {Math.round(produceProgress.percent * 100)}% · {produceProgress.message}
              </div>
            ) : null}
            {produceError !== '' ? (
              <div style={{ marginTop: 10, fontSize: 12, color: '#f28b82' }}>⚠ {produceError}</div>
            ) : null}
            {produceResult !== null ? (
              <div style={{ marginTop: 10, fontSize: 12 }}>
                <div style={{ color: '#81c995', marginBottom: 4 }}>
                  ✓ 成片完成：{produceResult.duration.toFixed(1)} 秒 · {produceResult.clipCount} 个镜头
                </div>
                <div style={{ ...S.dim, marginBottom: 6, wordBreak: 'break-all' }}>
                  {produceResult.labelledPath}
                </div>
                {(produceResult.notes ?? []).length > 0 ? (
                  <div style={{ ...S.dim, fontSize: 11, marginBottom: 6 }}>
                    制作记录：{(produceResult.notes ?? []).join(' · ')}
                  </div>
                ) : null}
                {(produceResult.warnings ?? []).length > 0 ? (
                  <div style={{ ...S.warn, fontSize: 11 }}>
                    {(produceResult.warnings ?? []).map((warning, index) => (
                      <div key={index}>⚠ {warning}</div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <div style={{ ...S.dim, marginTop: 10 }}>
        边界（写在这里免得被当成保证）：平台规格未实机上传验证；品类秒数是推导值；合规词表不是法律意见；
        卖点改写只给方向，落地要换成真实可测的内容。
        成片链路已在本地 sidecar 上实测跑通（4 镜 → 29 秒 mp4 + 三层 AI 标识，零付费），
        但**没有**实机发布到任何平台验证过。
      </div>
    </div>
  )
}

/* ───────────────────────── 页面 ───────────────────────── */



/** 掌柜 AI 的入口：电商工作台（自检条 + 电商面板）。 */
export function EcommercePanel(): ReactElement {
  return (
    <div style={S.wrap}>
      <SelfCheck />
      <EcomPanel />
    </div>
  )
}
