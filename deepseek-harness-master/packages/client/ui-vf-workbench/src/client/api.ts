/**
 * Sidecar 客户端：只认 `http://127.0.0.1:8765`（可用 localStorage 覆盖）。
 * 所有请求都打 VideoForge 自己的 Python sidecar —— 面板不持有任何业务状态，
 * 数据库是唯一真相（v2 方案的纪律 S3）。
 */

/** sidecar 基址：默认 8765；面板设置里改过就存在 localStorage。 */
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

/** sidecar 统一信封 `{code,message,data}` → 取出 data；非 0 码抛错。 */
async function unwrap<T>(res: Response): Promise<T> {
  const text = await res.text()
  let body: any = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    throw new Error(`sidecar 返回的不是 JSON（HTTP ${res.status}）：${text.slice(0, 200)}`)
  }
  if (!res.ok) {
    const msg = body?.detail?.message ?? body?.message ?? `HTTP ${res.status}`
    throw new Error(String(msg))
  }
  if (body && typeof body === 'object' && 'code' in body) {
    if (body.code !== '000000' && body.code !== 0) {
      throw new Error(String(body.message ?? '请求失败'))
    }
    return body.data as T
  }
  return body as T
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${sidecarBase()}${path}`, { method: 'GET' })
  return unwrap<T>(res)
}

async function post<T>(path: string, payload: unknown): Promise<T> {
  const res = await fetch(`${sidecarBase()}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  return unwrap<T>(res)
}

// ── 类型（只写面板用到的字段）──────────────────────────────────

export interface VfProject {
  id: string
  title?: string
  name?: string
  description?: string
  status?: string
  created_at?: string
  updated_at?: string
}

export interface VfStageEvidence {
  done?: boolean
  detail?: string
  have?: number
  total?: number
  [k: string]: unknown
}

export interface VfStage {
  key: string
  title: string
  tool: string
  why?: string
  cost?: string
  cost_label?: string
  eta?: string
  needs?: string[]
  gate?: boolean
  tweaks?: string[]
  status: 'done' | 'skipped' | 'current' | 'gate' | 'waiting' | string
  evidence?: VfStageEvidence
  missing_prereq?: string[]
}

export interface VfStageStatus {
  project_id?: string
  headline?: string
  done_count?: number
  total?: number
  stages: VfStage[]
  next?: VfStage | null
  pending_gate?: unknown
}

export interface VfEstimate {
  stage: string
  title: string
  cost_yuan: number | null
  cost_known?: boolean
  cost_floor_yuan?: number | null
  eta: string
  eta_seconds: number
  needs_approval: boolean
  will_spend?: boolean | null
  message?: string
  basis?: string
  units?: number
  lines?: Array<Record<string, unknown>>
  budget?: Record<string, unknown>
}

export interface VfRunResult {
  job_id: string
  stage: string
  title?: string
  status?: string
  stream?: string
  reused?: boolean
  message?: string
}

export interface VfJobProgress {
  job_id: string
  project_id?: string
  stage?: string
  status?: string
  progress?: number
  message?: string
  error?: string
  result?: Record<string, unknown>
}

export interface VfCharacter {
  id: string
  name?: string
  description?: string
  reference_image_path?: string
  status?: string
}

export interface VfScene {
  id: string
  name?: string
  description?: string
  reference_image_path?: string
}

export interface VfScript {
  title?: string
  logline?: string
  summary?: string
  content?: string
  scenes?: unknown
  [k: string]: unknown
}

// ── 业务调用 ──────────────────────────────────────────────────

export const api = {
  health: () => get<Record<string, unknown>>('/api/health'),
  /** 成片播放地址：`GET /api/projects/{pid}/final` 直接回 video/mp4（`download=1` 落盘）。 */
  finalUrl: (pid: string, download = false) =>
    `${sidecarBase()}/api/projects/${pid}/final${download ? '?download=1' : ''}`,
  /** 任意本地媒体 → HTTP 引用（sidecar `/api/media?path=`，限数据/缓存/输出目录）。 */
  mediaUrl: (path: string) => `${sidecarBase()}/api/media?path=${encodeURIComponent(path)}`,
  /** 逐镜抽帧联络表：sidecar 拼图后回一个 `/api/media?path=…` 的 url。 */
  framesheet: (pid: string, per = 3, cols = 6) =>
    get<{ url?: string; [k: string]: unknown }>(
      `/api/projects/${pid}/framesheet?per=${per}&cols=${cols}`,
    ),
  listProjects: async (): Promise<VfProject[]> => {
    const d = await get<{ projects?: VfProject[] }>('/api/projects')
    return d?.projects ?? (d as unknown as VfProject[]) ?? []
  },
  stageStatus: (pid: string) => get<VfStageStatus>(`/api/projects/${pid}/stage/status`),
  estimate: (pid: string, stage: string, opts?: { provider?: string; numCandidates?: number }) => {
    const q = new URLSearchParams({ project_id: pid })
    if (opts?.provider) q.set('provider', opts.provider)
    if (opts?.numCandidates && opts.numCandidates > 0) q.set('num_candidates', String(opts.numCandidates))
    return get<VfEstimate>(`/api/v2/stage/${stage}/estimate?${q.toString()}`)
  },
  run: (pid: string, stage: string, params?: Record<string, unknown>) =>
    post<VfRunResult>('/api/v2/run', { project_id: pid, stage, params: params ?? {} }),
  cancel: (jobId: string) => post<{ cancelled: boolean; message?: string }>(`/api/v2/jobs/${jobId}/cancel`, {}),
  job: (jobId: string) => get<VfJobProgress>(`/api/v2/jobs/${jobId}`),
  characters: async (pid: string): Promise<VfCharacter[]> => {
    const d = await get<{ characters?: VfCharacter[] }>(`/api/projects/${pid}/characters`)
    return d?.characters ?? []
  },
  scenes: async (pid: string): Promise<VfScene[]> => {
    const d = await get<{ scenes?: VfScene[] }>(`/api/projects/${pid}/scenes`)
    return d?.scenes ?? []
  },
  script: (pid: string) => get<VfScript | null>(`/api/projects/${pid}/script`),
  shots: async (pid: string): Promise<Array<Record<string, unknown>>> => {
    const d = await get<{ shots?: Array<Record<string, unknown>> }>(`/api/projects/${pid}/shots`)
    return d?.shots ?? []
  },
}

/**
 * 订阅一个 job 的进度（SSE）。
 * 事件名：hello / progress / done / ping / error。
 * 返回取消函数。连接断了不会自动重连 —— 面板自己显示"已断开"。
 */
export function subscribeJob(
  jobId: string,
  on: {
    progress?: (e: any) => void
    done?: (e: any) => void
    error?: (message: string) => void
    hello?: (e: any) => void
  },
): () => void {
  let closed = false
  let es: EventSource | null = null
  try {
    es = new EventSource(`${sidecarBase()}/api/v2/jobs/${jobId}/events`)
  } catch (e) {
    on.error?.(String(e))
    return () => {}
  }
  const bind = (name: string, fn?: (e: any) => void) => {
    if (!fn) return
    es!.addEventListener(name, (ev: MessageEvent) => {
      if (closed) return
      try {
        fn(JSON.parse(ev.data))
      } catch {
        /* 单条解析失败不打断整条流 */
      }
    })
  }
  bind('hello', on.hello)
  bind('progress', on.progress)
  bind('done', on.done)
  const onBroken = (): void => {
    if (!closed) on.error?.('进度连接断开了（sidecar 可能没起或任务已结束）')
  }
  es.addEventListener('error', onBroken)
  return () => {
    closed = true
    try {
      es?.close()
    } catch {
      /* ignore */
    }
  }
}
