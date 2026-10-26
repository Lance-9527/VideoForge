/**
 * 创作工坊 · sidecar 客户端。
 *
 * 纪律（与 ui-vf-workbench/api.ts 同口径）：
 *  · 面板**不持有业务状态**，只读 sidecar；库是唯一真相。
 *  · 统一信封 `{code,message,data}` → 取 data；非 0 码抛错（把真实原因带到界面）。
 *  · sidecar 基址默认 8765，可用 localStorage 覆盖（排障用）。
 */

/** sidecar 基址：默认 8765；面板里改过就存在 localStorage。 */
export function sidecarBase(): string {
  try {
    const v = window.localStorage.getItem('vf.sidecarBase')
    if (v && v.trim()) return v.trim().replace(/\/+$/, '')
  } catch {
    /* localStorage 不可用时退回默认值 */
  }
  return 'http://127.0.0.1:8765'
}

export function setSidecarBase(base: string): void {
  try {
    window.localStorage.setItem('vf.sidecarBase', base.trim().replace(/\/+$/, ''))
  } catch {
    /* 忽略：只影响下次启动 */
  }
}

interface Envelope<T> {
  code?: string | number
  message?: string
  data?: T
  detail?: { message?: string }
}

/** sidecar 统一信封 `{code,message,data}` → 取出 data；非 0 码抛错。 */
async function unwrap<T>(res: Response): Promise<T> {
  const text = await res.text()
  let body: Envelope<T> | null = null
  try {
    body = text ? (JSON.parse(text) as Envelope<T>) : null
  } catch {
    throw new Error(`sidecar 返回的不是 JSON（HTTP ${res.status}）：${text.slice(0, 200)}`)
  }
  if (!res.ok) {
    const msg = body?.detail?.message ?? body?.message ?? `HTTP ${res.status}`
    throw new Error(String(msg))
  }
  if (body && typeof body === 'object' && 'code' in body) {
    const code = body.code
    if (code !== '000000' && code !== 0) throw new Error(String(body.message ?? '请求失败'))
    return body.data as T
  }
  return body as unknown as T
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(sidecarBase() + path, { method: 'GET' })
  return unwrap<T>(res)
}

/**
 * 长活给足超时：复刻要切镜/抽帧/OCR/ASR，真烧 AI 标识要重编码。
 * 但**默认 dry_run / 只出方案**，所以首屏交互都是快的。
 */
async function postJson<T>(path: string, body: unknown, timeoutMs = 120_000): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(sidecarBase() + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
    return await unwrap<T>(res)
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error(`请求超时（${Math.round(timeoutMs / 1000)}s）：${path}`)
    }
    throw error
  } finally {
    clearTimeout(timer)
  }
}

/* ───────────────────────── 类型（只声明用到的字段） ───────────────────────── */

export interface ServiceStatus {
  ok?: boolean
  ffmpeg?: string | null
  asr?: { configured?: boolean; default_model?: string }
  style_profiles?: Record<string, unknown>
  style_rules_source?: string
  platforms?: unknown[]
  categories?: unknown[]
  compliance_lists?: unknown[]
}

export interface RemakeGate {
  id: string
  title: string
  value: string
  state: 'pass' | 'fail' | 'unknown' | 'skipped' | string
  note?: string
}

export interface RemakeShot {
  index: number
  start: number
  end: number
  duration: number
  shot_size?: string
  camera_motion?: string
  palette?: string
  description?: string
  prompt?: string
  dialogue?: string[]
}

export interface RemakeResult {
  ok?: boolean
  dry_run?: boolean
  shots?: number
  estimated_shots?: number
  est?: { est_cost_yuan?: number; est_seconds?: number }
  source?: { width?: number; height?: number; duration?: number; aspect?: string }
  style_profile?: string
  style_profile_label?: string
  pacing?: {
    mean_shot?: number
    median_shot?: number
    max_shot?: number
    cuts_per_min?: number
    pace_class?: string
    gates?: RemakeGate[]
  }
  dialogue_stats?: { lines?: number; chars?: number; chars_per_sec?: number; line_len_cv?: number }
  shots_preview?: RemakeShot[]
  spec_path?: string
  warnings?: string[]
  asr?: { ok?: boolean; whisper_model?: string; cost_yuan?: number; error?: string }
  compliance?: { can_replicate?: string[]; must_not_replicate?: string[] }
}

export interface PlatformBrief {
  id: string
  name: string
  duration?: { min?: number; max?: number } | null
  aspect?: string
  resolution?: string
  basis?: string
}

export interface CategoryBrief {
  id: string
  name: string
  beats?: string[]
  basis?: string
}

export interface PlatformCheck {
  id: string
  title: string
  level: 'pass' | 'fail' | 'unknown' | string
  detail: string
}

export interface ComplianceHit {
  term: string
  severity: 'error' | 'warn' | string
  category: string
  why?: string
}

export interface SellingPointRewrite {
  original: string
  kind: string
  rewritten: string
  changed?: boolean
  shootable?: boolean
  why?: string
}

export interface EcomPlan {
  ok?: boolean
  product?: string
  platform?: { id?: string; name_zh?: string }
  category?: { id?: string; name_zh?: string }
  target_duration?: number
  beats?: Array<{ beat: string; start: number; end: number }>
  scenes?: Array<{
    scene_number: number
    title: string
    beat: string
    duration_seconds: number
    actions?: string[]
    dialogues?: Array<{ character?: string; text?: string }>
    must_not?: string[]
  }>
  compliance?: { errors?: number; warnings?: number; score?: number; hits?: ComplianceHit[] }
  platform_check?: { checks?: PlatformCheck[] }
  ai_label?: {
    choice?: string
    enabled?: boolean
    text?: string
    warning?: string
    next_step?: string
    manual_steps?: Array<{ kind: string }>
  }
  selling_point_rewrite?: { changed?: number; items?: SellingPointRewrite[]; still_vague?: string[] }
  selling_points?: string[]
  selling_points_raw?: string[]
  warnings?: string[]
}

export interface AiLabelPlan {
  ok?: boolean
  checklist?: { mandatory?: boolean; effective_from?: string; must_do?: string[]; note?: string }
  plan?: {
    font?: string
    font_available?: boolean
    steps?: Array<{ layer: string; kind: string; text?: string; detail?: string }>
  }
  layers_done?: string[]
  out_path?: string
  sidecar?: string
  manual_steps?: Array<{ layer?: string; kind: string; note?: string }>
  metadata?: { comment?: string }
  size_kb?: number
}

/* ───────────────────────── 接口 ───────────────────────── */

export const api = {
  /** 自检：ffmpeg / ASR Key / 风格档 / 平台数 / 品类数 */
  remakeStatus: () => getJson<ServiceStatus>('/api/remake/status'),
  ecommerceStatus: () => getJson<ServiceStatus>('/api/ecommerce/status'),

  /** 平台 + 品类清单（下拉框用） */
  platforms: () => getJson<{ ok?: boolean; platforms?: PlatformBrief[]; categories?: CategoryBrief[] }>(
    '/api/ecommerce/platforms',
  ),

  /**
   * 视频复刻。`dry_run=true` 只估算（不花钱、不动文件）；
   * false 才真跑切镜/抽帧/OCR/ASR（几分钟）。
   */
  remakeAnalyze: (body: {
    video_path: string
    dry_run: boolean
    use_ocr?: boolean
    use_whisper?: boolean
    use_siliconflow?: boolean
    max_shots?: number
  }) => postJson<RemakeResult>('/api/remake/analyze', body, 30 * 60 * 1000),

  /** 电商方案 */
  ecomPlan: (body: {
    product: string
    platform?: string
    category?: string
    selling_points?: string[]
    audience?: string
    duration_sec?: number
    price_line?: string
    ai_label?: string
  }) => postJson<EcomPlan>('/api/ecommerce/plan', body),

  /** 文案合规体检 */
  ecomAudit: (text: string) =>
    postJson<{ ok?: boolean; score?: number; errors?: number; warnings?: number; hits?: ComplianceHit[] }>(
      '/api/ecommerce/audit',
      { text },
    ),

  /** 卖点 → 可拍摄画面 */
  ecomPoints: (selling_points: string[]) =>
    postJson<{ ok?: boolean; items?: SellingPointRewrite[]; changed?: number; still_vague?: string[] }>(
      '/api/ecommerce/selling-points',
      { selling_points },
    ),

  /**
   * AI 内容标识（**可选项**）。
   * apply=false：只出清单与方案，不动文件。
   * apply=true ：真烧进画面 + 写元数据（重编码，耗时随片长）。
   */
  aiLabel: (body: {
    video_path?: string
    apply?: boolean
    mode?: string
    start_seconds?: number
  }) => postJson<AiLabelPlan>('/api/ecommerce/ai-label', body, 30 * 60 * 1000),
}
