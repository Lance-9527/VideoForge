/** VideoForge 主区面板：资产 / 后期 / 项目详情。
 *
 * 为什么是「覆盖在对话之上的整块面板」而不是改 core 的路由：
 * 左栏「资产 / 后期」是 VideoForge 自己的信息架构，DSH 的 web-app 里没有对应页面；
 * 我们只占 `shell.overlay` 这一个槽（core 零改动），选中时盖住对话主区，
 * 点「AI 对话」就把它收起来 —— 用户看到的是「点了确实切过去了」。
 *
 * 数据全部来自 Python 服务（与旧版 VideoForge 同一个库），不复制任何业务逻辑。
 *
 * @module
 */
import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { setVfView, useVfView } from './view.ts'
import { AssetLibrary } from './AssetLibrary.tsx'

/** Python 视频服务地址（与其余壳件一致）。 */
const SIDECAR = 'http://127.0.0.1:8765'

/** 面板配色（与卡片同一套暗色 token）。 */
const C = {
  bg: '#0d0d10',
  card: '#141419',
  line: '#26262e',
  text: '#f0f0f3',
  dim: '#9a9aa4',
  dimmer: '#6c6c76',
  accent: '#7ac130',
  warn: '#ffb03a',
} as const

/** 一个项目的最小信息。 */
interface ProjectInfo {
  readonly id?: string
  readonly title?: string
  readonly description?: string
  readonly updated_at?: string
}

/** 一个阶段行。 */
interface StageRow {
  readonly key?: string
  readonly title?: string
  readonly status?: string
}

/** 分镜体检项（后端 prompt_checks：Q1 只有物件没人 / Q2 单动作空转 / Q7 状态没改变…） */
interface PromptCheck { id?: string, level?: string, title?: string, detail?: string, shot?: string }

/** 一张资产图（角色 / 场景 / 分镜参考）。 */
interface AssetImage {
  readonly key: string
  readonly label: string
  readonly kind: string
  readonly path: string
}

/** 一条白膜预演视频（能看出走位/运镜/节奏，不是静帧）。 */
interface AssetVideo {
  readonly key: string
  readonly label: string
  readonly path: string
}

/** 一个产物（成片 / 白膜 / 音频）。 */
/** 一镜的写实关键帧（= 出片时的首帧，用户确认过的那张）。 */
interface ShotKeyframe {
  readonly index?: number
  readonly keyframe_path?: string
}

interface OutputItem {
  readonly name?: string
  readonly url?: string
  readonly download_url?: string
  readonly kind?: string
  readonly is_final?: boolean
  readonly duration?: number
  readonly validation_status?: string
  readonly validation_note?: string
  readonly accepted?: boolean
}

/** 把 sidecar 返回的路径变成可直接放进 <img>/<video> 的 URL。 */
function mediaUrl(path: string | null | undefined): string | null {
  const value = String(path ?? '').trim()
  if (value === '') return null
  if (/^https?:/u.test(value)) return value
  return `${SIDECAR}/api/media?path=${encodeURIComponent(value)}`
}

/** 拉一个 JSON（失败返回 null，绝不抛）。 */
async function fetchJson<T>(path: string): Promise<T | null> {
  try {
    const response = await fetch(`${SIDECAR}${path}`)
    if (!response.ok) return null
    const body = await response.json() as { data?: unknown }
    return (body.data ?? body) as T
  } catch {
    return null
  }
}

/**
 * 主区面板。`section === 'chat'` 时返回 null（完全让位给对话）。
 */
export function VfPanel(props: { onBack?: () => string | void } = {}) {
  const view = useVfView()
  const [project, setProject] = useState<ProjectInfo | null>(null)
  const [stages, setStages] = useState<readonly StageRow[]>([])
  const [doneCount, setDoneCount] = useState<number | null>(null)
  const [total, setTotal] = useState<number | null>(null)
  const [images, setImages] = useState<readonly AssetImage[]>([])
  const [videos, setVideos] = useState<readonly AssetVideo[]>([])
  const [playing, setPlaying] = useState<string | null>(null)
  const [checks, setChecks] = useState<readonly PromptCheck[]>([])
  const [outputs, setOutputs] = useState<readonly OutputItem[]>([])
  const [keyframes, setKeyframes] = useState<readonly ShotKeyframe[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [zoom, setZoom] = useState<AssetImage | null>(null)
  const [navigationError, setNavigationError] = useState<string | null>(null)

  const projectId = view.projectId

  // ★ 2026-10-24：资产库导入成功后用它触发一次重新拉取（否则面板还显示旧资产）
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    if (view.section === 'chat' || projectId === null) return
    let cancelled = false
    setNavigationError(null)
    setLoading(true)
    setError(null)
    void (async () => {
      const info = await fetchJson<ProjectInfo>(`/api/projects/${projectId}`)
      const status = await fetchJson<{ stages?: StageRow[], done_count?: number, total?: number }>(
        `/api/projects/${projectId}/stage/status`)
      const characters = await fetchJson<{ characters?: { name?: string, reference_image_path?: string }[] }>(
        `/api/projects/${projectId}/characters`)
      const scenes = await fetchJson<{ scenes?: { name?: string, reference_image_path?: string }[] }>(
        `/api/projects/${projectId}/scenes`)
      // ★ 2026-10-23：分镜体检（prompt_checks）—— 提示词自己的问题要在出片**前**看见
      const shots = await fetchJson<{ shots?: {
        order_index?: number, previz_path?: string, previz_video?: string, candidates?: unknown,
        layer3_constraints?: { prompts?: { prompt_checks?: PromptCheck[], compiled_by?: string } } | string
      }[] }>(
        `/api/projects/${projectId}/shots`)
      const out = await fetchJson<{ outputs?: OutputItem[] }>(`/api/projects/${projectId}/outputs`)
      // ★ 关键帧（= 出片时的首帧）：有它说明"用户看到的构图 = 成片的构图"已成立
      const prompts = await fetchJson<{ shots?: ShotKeyframe[] }>(`/api/projects/${projectId}/prompts`)
      if (cancelled) return

      const list: AssetImage[] = []
      const listVideos: AssetVideo[] = []
      for (const c of characters?.characters ?? []) {
        const url = mediaUrl(c.reference_image_path)
        if (url !== null) list.push({ key: `c-${list.length}`, label: c.name ?? '角色', kind: '角色', path: url })
      }
      for (const s of scenes?.scenes ?? []) {
        const url = mediaUrl(s.reference_image_path)
        if (url !== null) list.push({ key: `s-${list.length}`, label: s.name ?? '场景', kind: '场景', path: url })
      }
      for (const shot of shots?.shots ?? []) {
        const no = String((shot.order_index ?? 0) + 1).padStart(2, '0')
        // ★ 2026-10-22：优先给**白膜视频**（能看出走位/运镜/节奏）；
        //   只有帧图时才退回静帧，不然用户只能看到一张不变的灰图。
        const video = mediaUrl(shot.previz_video)
        if (video !== null) {
          listVideos.push({ key: `pv-${listVideos.length}`, label: `镜${no} 白膜预演`, path: video })
          continue
        }
        const url = mediaUrl(shot.previz_path)
        if (url !== null) list.push({ key: `p-${list.length}`, label: `镜${no} 白膜`, kind: '白膜', path: url })
      }

      setProject(info)
      setStages(status?.stages ?? [])
      setDoneCount(status?.done_count ?? null)
      setTotal(status?.total ?? null)
      setImages(list)
      setVideos(listVideos)
      setOutputs(out?.outputs ?? [])
      // 抽分镜体检：layer3 可能是字符串（老数据）
      const checks: PromptCheck[] = []
      for (const shot of shots?.shots ?? []) {
        type L3 = { prompts?: { prompt_checks?: PromptCheck[], compiled_by?: string } }
        let l3: L3 | undefined
        const raw: unknown = shot.layer3_constraints
        if (typeof raw === 'string') {
          try { l3 = JSON.parse(raw) as L3 } catch { l3 = undefined }
        } else if (raw !== null && typeof raw === 'object') {
          l3 = raw as L3
        }
        const list2 = l3?.prompts?.prompt_checks
        if (!Array.isArray(list2)) continue
        const no = (shot.order_index ?? 0) + 1
        for (const c of list2) checks.push({ ...c, shot: `镜${String(no).padStart(2, '0')}` })
      }
      setChecks(checks)
      setKeyframes((prompts?.shots ?? []).filter((s) => Boolean(s && s.keyframe_path)))
      setLoading(false)
      if (info === null && status === null) setError('连不上视频服务（8765）—— 看看应用是否还在启动中')
    })()
    return () => { cancelled = true }
  }, [view.section, projectId, refreshKey])

  const back = (): void => {
    if (props.onBack === undefined) {
      setVfView({ section: 'chat', projectId: null, projectTitle: null })
      return
    }
    setNavigationError(props.onBack() ?? null)
  }

  if (view.section === 'chat') return null

  if (projectId === null) {
    return (
      <div style={shellStyle}>
        <div style={{ maxWidth: 520, margin: '14vh auto 0', textAlign: 'center', color: C.dim }}>
          <div style={{ fontSize: 18, color: C.text, marginBottom: 8 }}>
            {view.section === 'assets' ? '资产' : '后期'}
          </div>
          <div style={{ fontSize: 13, lineHeight: 1.8 }}>
            先从左边选一个项目 —— 资产和成片都是**按项目**的。<br />
            还没项目？回到「AI 对话」说一句「做个 12 秒短片」就能立项。
          </div>
          <button type="button" style={primaryButton} onClick={back}>
            回到 AI 对话
          </button>
        </div>
      </div>
    )
  }

  const title = project?.title ?? view.projectTitle ?? '项目'
  const finalOutput = outputs.find((o) => o.is_final === true && o.kind === 'video') ?? null
  const others = outputs.filter((o) => o !== finalOutput)

  return (
    <div style={shellStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 22px', borderBottom: `1px solid ${C.line}` }}>
        {/* ★ 2026-10-23（用户要求）：这儿以前是行灰字，看着像标签、点着也不明显。
            改成真正的按钮（有边框/背景/悬停），点了就回**这个项目的会话**。 */}
        <button
          type="button"
          onClick={back}
          title="回到这个项目对应的 AI 会话"
          style={backButton}
          onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(120,180,255,0.16)'; e.currentTarget.style.borderColor = 'rgba(120,180,255,0.55)' }}
          onMouseLeave={(e) => { e.currentTarget.style.background = 'rgba(120,180,255,0.08)'; e.currentTarget.style.borderColor = 'rgba(120,180,255,0.32)' }}
        >
          ← AI 对话（回到对应会话）
        </button>
        <div style={{ fontWeight: 650, fontSize: 15 }}>{title}</div>
        {doneCount !== null && total !== null && (
          <span style={{ fontSize: 12, color: C.dim }}>进度 {doneCount}/{total}</span>
        )}
        <span style={{ marginLeft: 'auto', fontSize: 12, color: C.dimmer }}>
          {view.section === 'assets' ? '资产' : '后期'}
        </span>
      </div>

      <div style={{ padding: '16px 22px 60px', overflowY: 'auto', height: 'calc(100% - 58px)' }}>
        {navigationError !== null && <div role="alert" style={{ color: C.warn, fontSize: 13, marginBottom: 12 }}>{navigationError}</div>}
        {loading && <div style={{ color: C.dim, fontSize: 13 }}>读取中…</div>}
        {error !== null && <div style={{ color: C.warn, fontSize: 13 }}>{error}</div>}

        {stages.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 18 }}>
            {stages.map((stage) => {
              const status = String(stage.status ?? '')
              const color = status === 'done' ? C.accent : status === 'current' ? C.warn : C.dimmer
              return (
                <span key={stage.key} style={{
                  fontSize: 11, padding: '3px 8px', borderRadius: 999, color,
                  border: `1px solid color-mix(in srgb, ${color} 40%, transparent)`,
                  background: `color-mix(in srgb, ${color} 10%, transparent)`,
                }}>
                  {stage.title ?? stage.key} · {status === 'done' ? '完成' : status === 'current' ? '进行中' : status === 'skipped' ? '跳过' : '待做'}
                </span>
              )
            })}
          </div>
        )}

        {view.section === 'post' && (
          <div>
            {finalOutput !== null ? (
              <div style={{ marginBottom: 20 }}>
                <div style={{ fontSize: 12, color: C.dim, marginBottom: 8 }}>成片</div>
                <video
                  src={`${SIDECAR}${finalOutput.url ?? ''}`}
                  controls
                  style={{ width: 'min(720px, 100%)', borderRadius: 10, background: '#000' }}
                />
                <div style={{ fontSize: 12, color: C.dimmer, marginTop: 6 }}>
                  {finalOutput.name}{finalOutput.duration ? ` · ${finalOutput.duration.toFixed(1)}s` : ''}
                </div>
              </div>
            ) : (
              <div style={{ color: C.dim, fontSize: 13, lineHeight: 1.8 }}>
                这个项目还没有成片。走完「480P 小样 → 1080P 成片 → 后期成片」就会出现在这里。
                <br />（现在这一步需要花钱，会在对话里先弹审批。）
              </div>
            )}
            {others.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 12, color: C.dim, marginBottom: 8 }}>
                  其它产物（点文件名就在这一栏里播；不点就是文件名 + 下载）
                </div>
                {others.map((item) => {
                  const name = item.name ?? '产物'
                  const url = item.url ? `${SIDECAR}${item.url}` : ''
                  const downloadUrl = item.download_url ? `${SIDECAR}${item.download_url}` : url
                  const isVideo = /\.(mp4|mov|webm|m4v)$/iu.test(name) || item.kind === 'video'
                  const open = playing === name
                  const rejected = item.validation_status === 'rejected' || item.accepted === false
                  return (
                    <div key={name} style={{ marginBottom: 8 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 }}>
                        {/* ★ 2026-10-23（用户要求）：视频产物以前只有一行文件名 + 下载，
                            在后期面板里没法直接看。现在点名字就地播放，再点收起。 */}
                        <button
                          type="button"
                          disabled={!isVideo || url === ''}
                          onClick={() => { setPlaying(open ? null : name) }}
                          title={isVideo ? (open ? '收起播放' : '点击播放') : '这个产物不是视频'}
                          style={{
                            padding: '3px 8px', borderRadius: 7, font: 'inherit', fontSize: 13,
                            border: `1px solid ${isVideo ? 'rgba(120,180,255,0.32)' : C.line}`,
                            background: open ? 'rgba(120,180,255,0.18)' : 'transparent',
                            color: isVideo ? '#bcd7ff' : C.dim,
                            cursor: isVideo && url !== '' ? 'pointer' : 'default',
                            maxWidth: 520, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                          }}
                        >
                          {isVideo ? (open ? '▶ ' : '▷ ') : ''}{name}
                        </button>
                        {downloadUrl !== '' && (
                          <a href={downloadUrl} download style={{ color: C.accent, marginLeft: 2 }}>下载</a>
                        )}
                      </div>
                      {rejected && (
                        <div style={{ color: C.warn, fontSize: 12, marginTop: 4, lineHeight: 1.5 }}>
                          {item.validation_note ?? '已生成，可播放和下载；但与给定白膜的一致性未通过，未选为正式成片。'}
                        </div>
                      )}
                      {open && url !== '' && (
                        <video
                          src={url}
                          controls
                          autoPlay
                          playsInline
                          style={{ width: 'min(640px, 100%)', marginTop: 6, borderRadius: 10, background: '#000', display: 'block' }}
                        />
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {view.section === 'assets' && (
          <div>
            {/* ★ 2026-10-24（用户要求「资产要能复用 + 工作区资产可设置」）：
                跨项目复用资产库；设置项只放真正会生效的（范围/只看有图）。 */}
            <AssetLibrary
              projectId={projectId}
              onImported={() => { setRefreshKey((n) => n + 1) }}
            />
            {/* ★ 2026-10-23：分镜体检（prompt_checks）—— 提示词自己的毛病要在**出片前**看见，
                而不是等生成出来才发现"6 秒只有一只手"「宣传片像恐怖片」。 */}
            {checks.length > 0 ? (
              <div style={{ marginBottom: 16, border: `1px solid ${C.line}`, borderRadius: 10, padding: '10px 12px' }}>
                <div style={{ fontSize: 12, color: C.dim, marginBottom: 8 }}>
                  分镜体检 {checks.length} 条（出片前就能修的提示词问题；红=必须改，黄=建议改）
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {checks.map((c, i) => {
                    const bad = (c.level ?? '') === 'error'
                    return (
                      <div key={`ck-${i}`} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12 }}>
                        <span style={{
                          flex: '0 0 auto', padding: '1px 6px', borderRadius: 999, fontSize: 11,
                          color: bad ? '#ff6b6b' : '#e0b341',
                          border: `1px solid ${bad ? 'rgba(255,107,107,0.5)' : 'rgba(224,179,65,0.5)'}`,
                        }}>
                          {c.id ?? 'Q'}
                        </span>
                        <span style={{ flex: '0 0 auto', color: C.dim }}>{c.shot ?? ''}</span>
                        <span style={{ color: C.text }}>
                          {c.title ?? ''}
                          {c.detail ? <span style={{ color: C.dim }}> —— {c.detail}</span> : null}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>
            ) : null}
            {videos.length > 0 ? (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12, color: C.dim, marginBottom: 8 }}>
                  白膜预演视频（Blender 本地渲染，零费用 —— 看的是走位 / 运镜 / 节奏，不是成片质感）
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12 }}>
                  {videos.map((v) => (
                    <div key={v.key} style={{ border: `1px solid ${C.line}`, borderRadius: 10, overflow: 'hidden', background: '#000' }}>
                      <video
                        src={v.path}
                        controls
                        loop
                        muted
                        playsInline
                        preload="metadata"
                        style={{ width: '100%', display: 'block', background: '#000' }}
                      />
                      <div style={{ padding: '6px 8px', fontSize: 12, color: C.text }}>{v.label}</div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            {keyframes.length > 0 ? (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12, color: C.dim, marginBottom: 8 }}>
                  关键帧（出片就是用这张当首帧 —— 你看到的构图 = 成片的构图）
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
                  {keyframes.map((k) => (
                    <img
                      key={String(k.index)}
                      src={`${SIDECAR}/api/media?path=${encodeURIComponent(String(k.keyframe_path))}`}
                      alt={`第 ${Number(k.index ?? 0) + 1} 镜关键帧`}
                      style={{ width: '100%', borderRadius: 10, border: `1px solid ${C.line}`, display: 'block' }}
                    />
                  ))}
                </div>
              </div>
            ) : null}
            {images.length === 0 && !loading && (
              <div style={{ color: C.dim, fontSize: 13 }}>还没有资产图。走「角色/场景 → 定妆照/场景图」两步后会出现在这里。</div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 12 }}>
              {images.map((image) => (
                <button
                  key={image.key}
                  type="button"
                  onClick={() => { setZoom(image) }}
                  style={{ padding: 0, border: `1px solid ${C.line}`, borderRadius: 10, background: C.card, cursor: 'zoom-in', overflow: 'hidden', textAlign: 'left' }}
                >
                  <img src={image.path} alt={image.label} style={{ width: '100%', height: 110, objectFit: 'cover', display: 'block' }} />
                  <div style={{ padding: '6px 8px' }}>
                    <div style={{ fontSize: 11, color: C.dimmer }}>{image.kind}</div>
                    <div style={{ fontSize: 12, color: C.text, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{image.label}</div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {zoom !== null && (
        <div
          onClick={() => { setZoom(null) }}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60, cursor: 'zoom-out' }}
        >
          <img src={zoom.path} alt={zoom.label} style={{ maxWidth: '88vw', maxHeight: '84vh', borderRadius: 10 }} />
        </div>
      )}
    </div>
  )
}

/** Electron's non-standard CSS property is absent from React's browser CSS types. */
type DesktopSurfaceStyle = CSSProperties & { WebkitAppRegion?: 'drag' | 'no-drag' }

const shellStyle: DesktopSurfaceStyle = {
  position: 'fixed',
  left: 'var(--dsh-windows-sidebar-width, 280px)',
  // Fixed overlays do not inherit AppFrame's Windows caption padding.
  top: 'var(--dsh-frame-chrome-top, 0px)',
  right: 0,
  bottom: 0,
  zIndex: 35,
  background: C.bg,
  color: C.text,
  pointerEvents: 'auto',
  WebkitAppRegion: 'no-drag',
  fontFamily: 'inherit',
  overflow: 'hidden',
}

/** 「← AI 对话（回到对应会话）」—— 真按钮（有边框/底色/悬停），不是一行灰字。 */
const backButton: DesktopSurfaceStyle = {
  padding: '6px 12px',
  borderRadius: 9,
  border: '1px solid rgba(120,180,255,0.32)',
  background: 'rgba(120,180,255,0.08)',
  color: '#bcd7ff',
  font: 'inherit',
  fontSize: 12.5,
  fontWeight: 600,
  cursor: 'pointer',
  WebkitAppRegion: 'no-drag',
  transition: 'background .15s ease, border-color .15s ease',
  whiteSpace: 'nowrap',
}

const primaryButton = {
  marginTop: 16,
  padding: '8px 16px',
  borderRadius: 9,
  border: `1px solid ${C.accent}`,
  background: 'color-mix(in srgb, #7ac130 16%, transparent)',
  color: C.accent,
  font: 'inherit',
  fontSize: 13,
  cursor: 'pointer',
} as const
