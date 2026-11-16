/**
 * 掌柜 AI · **棚拍工作台**（拍摄台版，2026-11-05 重设计）。
 *
 * ## 为什么要重做
 * 旧版照搬酷家乐 Koolshot 的通用 3D 软件骨架（顶栏 + 8 项左竖排 + 资源库 + 5 tab 检查器），
 * 那是给专业建模师的界面，不是给电商运营的。实测出来四个真问题：
 *   1. **同一功能两个入口**：景别在右栏「相机」和底部 chips 各一份；运镜、物件同理。
 *      用户学一遍记不住一半，另一半是噪音。
 *   2. **底部条 4 排 chips ≈ 140px**，把 3D 视口挤扁——而视口才是这个页面的主角。
 *   3. **电商主线被压扁**：镜头表 → 逐镜拍摄 是这个产品的主流程，却只是底部一个按钮。
 *   4. **死控件多**：预览卡的 3 个 tab、分类标签、「2D/3D」、「网格」都是摆设。
 *
 * ## 这一版的原则
 *   - **一个控制面**：所有参数只在右栏出现一次（不再有底部 chips）。
 *   - **主流程在明处**：镜头表变成视口正下方的**镜头条**，当前镜高亮，点一下就跳机位。
 *   - **视口是主角**：视口吃掉所有垂直空间；周边控件浮在视口上，不占布局。
 *   - **人是 AI 的同一条路**：右栏底部固定 AI 指令框，agent 走同一套具名指令总线。
 *
 * ## 工程边界（重要）
 *   ★ 只重做「操作界面」。**引擎（SplatStudio / StudioHandle 契约）、sidecar 具名指令总线、
 *     shoot-task / camera-marks 的 localStorage 契约一律不动** —— 那是这个产品的真价值。
 *     Props 只增不减（新增的都是可选 prop）。
 *
 * @module
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { SplatStudio } from './SplatStudio'
import type { StudioHandle, StudioObject, StudioTransform, CameraKeyframe } from './SplatStudio'
import { cameraSizeLabel, type CameraMark } from './camera-marks'
import { setVfView } from './view.ts'

/**
 * 重建模型的坐标与尺度证据（来自 sidecar 的 `/api/studio/splat/metadata`）。
 * ★ 为什么要把数字摆到界面上：摄影测量重建出来的点云，坐标系与尺度都是
 *   "任意的"。不把点数/包围盒/竖轴/一米对应多少单位摆在眼前，就没人能判断
 *   这个模型到底能不能用。
 */
export interface SplatModelMeta {
  readonly name: string
  readonly points: number
  readonly extent: readonly number[]
  readonly max_edge: number
  readonly vertical_is_y_up: boolean
  readonly scale_anchors: readonly { readonly meters: number; readonly scale: number }[]
}

export interface LibraryItem {
  readonly id: string
  readonly label: string
  readonly kind: 'model' | 'material' | 'light' | 'template' | 'splat'
  readonly thumb?: string
  readonly meta?: string
}

interface Props {
  /** 引擎句柄由外层持有（AI 指令条要用） */
  readonly engineRef?: React.RefObject<StudioHandle | null> | React.MutableRefObject<StudioHandle | null>
  readonly objects: readonly StudioObject[]
  readonly library?: readonly LibraryItem[]
  readonly sceneName?: string
  readonly busy?: boolean
  readonly onStatus?: (message: string) => void
  readonly onRender?: () => void
  readonly onUpload?: () => void
  readonly onSave?: () => void
  readonly onUndo?: () => void
  readonly onRedo?: () => void
  /** 右上预览缩略图的图（渲染成片后传进来） */
  readonly previewUrl?: string | null
  /** 可导入的本地场景（.ply / .splat） */
  readonly sources?: readonly { readonly name: string }[]
  /** 机位书签（openvideoforge 的「＋保存机位」）。 */
  readonly cameraMarks?: readonly CameraMark[]
  /** 点机位书签 → 跳回那个角度。 */
  readonly onJumpCameraMark?: (mark: CameraMark) => void
  /** 删掉一个机位书签。 */
  readonly onDropCameraMark?: (mark: CameraMark) => void
  /** 电商镜头表 → 机位（分镜板用）。 */
  readonly shootShots?: readonly {
    readonly number: number
    readonly title: string
    readonly beat: string
    readonly yaw: number
    readonly pitch: number
    readonly zoom: number
    readonly orbit: boolean
    readonly frame: string | null
  }[]
  /** 跳到某个镜头的机位。 */
  readonly onGoShot?: (shot: {
    readonly number: number
    readonly yaw: number
    readonly pitch: number
    readonly zoom: number
    readonly orbit: boolean
  }) => void
  readonly onImportSource?: (name: string) => void
  /** 可复用的高斯泼溅资产（场景 / 原图 / 深度层） */
  readonly assetItems?: readonly {
    readonly kind: 'scene' | 'image' | 'layer'
    readonly name: string
    readonly label: string
    readonly points?: number
  }[]
  readonly onImportAsset?: (item: { readonly kind: 'scene' | 'image' | 'layer'; readonly name: string; readonly label: string; readonly points?: number }) => void
  /** 从图片生成场景：'pano' = 360° 全景（身临其境），'ldi' = 分层深度（可拆物件） */
  readonly onGenerateFromImage?: (mode: 'pano' | 'ldi') => void
  readonly onUploadModel?: () => void
  readonly exports?: readonly { readonly name: string }[]
  readonly onOpenExport?: (name: string) => void
  readonly generating?: boolean
  readonly uploading?: boolean
  readonly onFocusAi?: () => void
  readonly onPickLibraryItem?: (item: LibraryItem) => void
  /** 商品图面板（渲染商品进场景） */
  readonly productSlot?: JSX.Element
  /**
   * 「按镜头表自动拍摄」—— 旧版在底部第一排，电商主线被压扁；现在提到顶栏主按钮。
   * ★ 可选 prop：没给就隐藏这个按钮（不影响旧行为）。
   */
  readonly onAutoShoot?: (() => void) | undefined
  /** 「＋保存机位」—— 同上，从底部 chips 搬进右栏「镜头」。 */
  readonly onSaveCameraMark?: (() => void) | undefined
  /** AI 指令输入框（由外层持有 ref 与草稿状态，这里只负责摆位置）。 */
  readonly aiInput?: (JSX.Element) | undefined
  /** 电商拍摄任务抬头（商品名 · 平台），显示在镜头条左侧。 */
  readonly shootTitle?: (string) | undefined
  /**
   * 初始状态文案（显示在顶栏）。
   * ★ 旧版把"就绪 · 从左边选一个场景，或从图片生成"放在底部条，新版收到顶栏 ——
   *   空状态的第一句指引应该一直在视线里。
   */
  readonly initialStatus?: (string) | undefined
  /**
   * 「对齐并锚定」—— 把重建模型摆正、按参考物锚定尺度、落到地面 y=0。
   * ★ 尺度**必须由人给**（"桌子宽 1.2m"）：COLMAP 的 SfM 只解出相对尺度，
   *   机器猜不出 37 个单位是厘米还是米，猜错就会让商品比例全错。
   */
  readonly onNormalizeSource?: ((name: string, anchorMeters: number, anchorAxis: string) => void) | undefined
  /** 当前载入模型的坐标/尺度证据（显示在左栏「场景」组里）。 */
  readonly modelMeta?: (SplatModelMeta | null) | undefined
  /**
   * 「＋加入」—— 把这个模型**追加**到当前场景，而不是替换。
   * ★ 这是"用户提供的模型接到重建场景里"的入口：引擎本来就支持场景里多件物件
   *   （每件一个 gsplat 实体，可各自变换/显隐）。
   */
  readonly onAddSource?: ((name: string) => void) | undefined
}

/**
 * 工作台配色 —— **承自 OpenVideoForge 的设计系统**（暖米色纸面 + 铜金强调）。
 *
 * ★ 为什么不用自己那套深色：掌柜AI 的「棚拍」与 OpenVideoForge 的「白膜工作台」
 *   是同一种工具的两个场景来源（程序化白膜 / 真实高斯泼溅）。两套界面长得不一样，
 *   用户要学两遍 —— 所以这里直接沿用已经成型的视觉语言。
 * ★ 只搬 token 与控件规格，不搬类名：OpenVideoForge 的类名没有前缀（.btn/.pill/…，
 *   200+ 顶层类名），整份 CSS 进来会与 DSH 和掌柜AI 自己的类名撞车。
 *   token 原值见 D:\KnowledgeVault\projects\cinemap-teardown\product\css\cinemap.css
 */
const T = {
  bg: '#e7e0d0',
  panel: '#f3efe4',
  panel2: '#faf7f0',
  panel3: '#ebe4d2',
  line: '#d8cfbc',
  line2: '#e8e1d0',
  text: '#1a1612',
  text2: '#5c5348',
  text3: '#847967',
  accent: '#8a6d40',
  accentSoft: 'rgba(176, 141, 87, 0.12)',
  ok: '#6e8a75',
  warn: '#b08d57',
  danger: '#c2634e',
} as const

/** 右栏检视器的四个上下文页。 */
type InspectorTab = 'shot' | 'object' | 'scene' | 'rig'

/** 左栏三组。 */
type ShelfTab = 'scene' | 'product' | 'output'

const RESOLUTIONS = [
  { label: '1200x1600', w: 1200, h: 1600 },
  { label: '1080x1920', w: 1080, h: 1920 },
  { label: '1920x1080', w: 1920, h: 1080 },
  { label: '1080x1080', w: 1080, h: 1080 },
  { label: '800x600', w: 800, h: 600 },
]

/** 一条带标签的滑块。 */
function Slider(props: {
  label: string
  value: number
  min: number
  max: number
  step: number
  unit?: string
  onChange: (value: number) => void
}): JSX.Element {
  const { label, value, min, max, step, unit, onChange } = props
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
        <span style={{ fontSize: 11, color: T.text2 }}>{label}</span>
        <span style={{ fontSize: 11, color: T.text, fontVariantNumeric: 'tabular-nums' }}>
          {Math.round(value * 100) / 100}{unit ?? ''}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => { onChange(Number(event.target.value)) }}
        style={{ width: '100%', accentColor: T.accent }}
      />
    </div>
  )
}

/** 右栏里的小节标题。 */
function Section(props: { title: string; extra?: (JSX.Element) | undefined }): JSX.Element {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', margin: '14px 0 8px' }}>
      <span style={{ fontSize: 11, fontWeight: 600, color: T.text, letterSpacing: '0.04em' }}>{props.title}</span>
      {props.extra}
    </div>
  )
}

/** 一枚药丸按钮。 */
function Pill(props: {
  label: string
  onClick: () => void
  active?: boolean
  disabled?: boolean
  tone?: 'accent' | 'plain' | 'danger'
  title?: string
}): JSX.Element {
  const tone = props.tone ?? 'plain'
  const accent = tone === 'accent'
  return (
    <button
      type="button"
      title={props.title}
      disabled={props.disabled === true}
      onClick={props.onClick}
      style={{
        height: 26, padding: '0 10px', borderRadius: 13, cursor: props.disabled === true ? 'default' : 'pointer',
        fontSize: 12,
        border: `1px solid ${props.active === true ? T.accent : tone === 'accent' ? 'transparent' : T.line2}`,
        background: props.active === true ? T.accentSoft : accent ? T.accent : T.panel2,
        color: props.active === true ? '#8a6d40' : accent ? '#fff' : tone === 'danger' ? T.danger : T.text2,
        opacity: props.disabled === true ? 0.45 : 1,
      }}
    >
      {props.label}
    </button>
  )
}

/** 左栏/右栏里的一行条目。 */
function PanelRow(props: { title: string; desc: string; onClick: () => void; active?: boolean }): JSX.Element {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={props.onClick}
      onKeyDown={(event) => { if (event.key === 'Enter') { props.onClick() } }}
      style={{
        display: 'flex', gap: 8, alignItems: 'center', padding: '6px 8px', borderRadius: 6,
        cursor: 'pointer', marginBottom: 2,
        border: `1px solid ${props.active === true ? T.accent : 'transparent'}`,
        background: props.active === true ? T.accentSoft : 'transparent',
      }}
    >
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontSize: 12, color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{props.title}</div>
        <div style={{ fontSize: 11, color: T.text3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{props.desc}</div>
      </div>
    </div>
  )
}

/** 右栏里的数值输入。 */
function NumberField(props: { label: string; value: number; step?: number; onChange: (value: number) => void }): JSX.Element {
  const { label, value, step = 0.05, onChange } = props
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 4, border: `1px solid ${T.line2}`, borderRadius: 4, padding: '0 6px', height: 26, background: T.panel2 }}>
      <span style={{ fontSize: 11, color: T.text3, width: 34, flexShrink: 0 }}>{label}</span>
      <input
        type="number"
        step={step}
        value={value}
        onChange={(event) => { onChange(Number(event.target.value)) }}
        style={{ width: '100%', border: 'none', outline: 'none', fontSize: 12, color: T.text, background: 'transparent' }}
      />
    </label>
  )
}

export function StudioShell(props: Props): JSX.Element {
  const localRef = useRef<StudioHandle | null>(null)
  const engineRef = props.engineRef ?? localRef
  const [tab, setTab] = useState<InspectorTab>('shot')
  const [shelf, setShelf] = useState<ShelfTab>('scene')
  /** 素材抽屉是否展开。★ 默认收起 —— 视口是主角，素材按需从左窄轨拉出来。 */
  const [shelfOpen, setShelfOpen] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [transform, setTransform] = useState<StudioTransform>({ x: 0, y: 0, z: 0, ry: 0, s: 1 })
  const [objects, setObjects] = useState<{ label: string; visible: boolean }[]>([])
  const [sun, setSun] = useState({ enabled: true, azimuth: 25, elevation: 50, intensity: 1.15, colorTemp: 6493 })
  const [fog, setFogState] = useState({ enabled: false, density: 0.01 })
  const [exposure, setExposure] = useState(1.15)
  const [resolution, setResolution] = useState({ width: 1200, height: 1600 })
  const [freeCam, setFreeCam] = useState(false)
  const [mode, setMode] = useState<'translate' | 'rotate' | 'scale'>('translate')
  const [snap, setSnap] = useState(true)
  const [status, setStatus] = useState(props.initialStatus ?? '就绪')
  /** 当前显示的轨迹 id */
  const [trajectory, setTrajectory] = useState<string | null>(null)  /** 当前轨迹的关键帧 */
  const [keyframes, setKeyframes] = useState<CameraKeyframe[]>([])
  /** 选中的关键帧下标 */
  const [activeKey, setActiveKey] = useState<number | null>(null)
  /** 实时预览镜像的画布 */
  const previewCanvasRef = useRef<HTMLCanvasElement | null>(null)
  /** 当前镜头（镜头条里的高亮项） */
  const [shotIndex, setShotIndex] = useState(0)
  /** 尺度锚定用的参考物真实尺寸（米）—— 机器猜不出来，必须人给。 */
  const [anchorMeters, setAnchorMeters] = useState(1.2)
  /** 参考物对应的方向：width / height / depth。 */
  const [anchorAxis, setAnchorAxis] = useState('width')

  const say = useCallback((message: string): void => {
    setStatus(message)
    props.onStatus?.(message)
  }, [props])

  const engine = (): StudioHandle | null => engineRef.current

  const shots = props.shootShots ?? []
  const currentShot = shots[shotIndex] ?? null

  /** 从引擎读回关键帧 */
  const refreshKeys = useCallback((): void => {
    if (trajectory === null) { setKeyframes([]); return }
    setKeyframes(engine()?.getKeyframes(trajectory) ?? [])
  }, [trajectory])

  /** 改一个关键帧（本地先改，点「应用」才写进引擎） */
  const updateKey = (index: number, patch: Partial<CameraKeyframe>): void => {
    setKeyframes((current) => current.map((key, i) => (i === index ? { ...key, ...patch } : key)))
  }

  /** 轨迹总时长（毫秒） */
  const duration = useMemo(() => {
    const last = keyframes[keyframes.length - 1]
    return last?.t ?? 4000
  }, [keyframes])

  /** 应用一个灯光预设（色温 + 亮度 + 角度） */
  const applyLightPreset = (preset: { colorTemp: number; intensity: number; elevation: number }): void => {
    engine()?.setSun({ colorTemp: preset.colorTemp, intensity: preset.intensity, elevation: preset.elevation })
    setSun((current) => ({ ...current, colorTemp: preset.colorTemp, intensity: preset.intensity, elevation: preset.elevation }))
  }

  /* 引擎状态回灌右栏 */
  const handleState = useCallback((state: { selected: string | null; transform: StudioTransform }): void => {
    setSelected(state.selected)
    setTransform(state.transform)
  }, [])

  /* 实时预览：把视口画面同步到右上角的小窗（用户要求"预览跟随操作"） */
  useEffect(() => {
    const timer = window.setInterval(() => {
      const target = previewCanvasRef.current
      const handle = engineRef.current
      if (target === null || handle === null) { return }
      const source = handle.getCanvas()
      if (source === null || source.width < 2 || source.height < 2) { return }
      if (target.width !== source.width || target.height !== source.height) {
        target.width = source.width
        target.height = source.height
      }
      const context = target.getContext('2d')
      if (context === null) { return }
      context.clearRect(0, 0, target.width, target.height)
      context.drawImage(source, 0, 0, target.width, target.height)
    }, 120)
    return () => { window.clearInterval(timer) }
  }, [engineRef])

  /* 定期同步物件列表（显示/隐藏开关） */
  useEffect(() => {
    const timer = window.setInterval(() => {
      const handle = engineRef.current
      if (handle === null) { return }
      setObjects(handle.listObjects())
    }, 700)
    return () => { window.clearInterval(timer) }
  }, [])

  /* 分辨率变化 → 引擎 */
  const changeResolution = (width: number, height: number): void => {
    setResolution({ width, height })
    engine()?.setResolution(width, height)
    say(`渲染分辨率 ${width}x${height}`)
  }

  /** 跳到某镜的机位（镜头条 / HUD / 右栏共用）。 */
  const goShot = (index: number): void => {
    const shot = shots[index]
    if (shot === undefined) { return }
    setShotIndex(index)
    props.onGoShot?.({ number: shot.number, yaw: shot.yaw, pitch: shot.pitch, zoom: shot.zoom, orbit: shot.orbit })
  }

  /** 拍当前这一镜：摆好机位 → 把视口存成片。 */
  const shootCurrent = (index: number): void => {
    goShot(index)
    props.onRender?.()
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, background: T.bg, color: T.text, fontSize: 13 }}>

      {/* ── 顶栏 ──
          ★ 结构照抄 OpenVideoForge：左边场景名、中间一排**分组药丸**（工具开关）、
            右边次要操作 + 一个主按钮（墨底米字，hover 转铜金）。 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, height: 46, padding: '0 12px', background: T.panel, borderBottom: `1px solid ${T.line}`, flexShrink: 0 }}>
        <span style={{ fontSize: 13, fontWeight: 600, fontFamily: "'Fraunces', Georgia, serif", whiteSpace: 'nowrap', flexShrink: 0 }}>掌柜 AI · 棚拍</span>
        <span style={{ fontSize: 12, color: T.text3, whiteSpace: 'nowrap', flexShrink: 0 }}>{props.sceneName ?? '未命名'}</span>
        <div style={{ width: 1, height: 16, background: T.line, flexShrink: 0 }} />
        <span style={{ fontSize: 11, color: T.text2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }} title={status}>
          {status}
        </span>

        {/* 工具开关药丸组（OpenVideoForge 的 .tb-seg 同款） */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 6, flexShrink: 0 }}>
          {([
            { id: 'translate', label: '移动' },
            { id: 'rotate', label: '旋转' },
            { id: 'scale', label: '缩放' },
          ] as const).map((item) => (
            <button
              key={item.id}
              type="button"
              title={`手柄：${item.label}`}
              onClick={() => { setMode(item.id); engine()?.setMode(item.id); say(`手柄：${item.label}`) }}
              style={{
                height: 28, padding: '0 12px', borderRadius: 999, cursor: 'pointer', fontSize: 12, fontWeight: 500,
                background: mode === item.id ? 'rgba(176,141,87,0.10)' : 'transparent',
                border: `1px solid ${mode === item.id ? 'rgba(176,141,87,0.34)' : 'transparent'}`,
                color: mode === item.id ? T.accent : T.text3,
              }}
            >
              {item.label}
            </button>
          ))}
          <div style={{ width: 1, height: 16, background: T.line, margin: '0 4px' }} />
          <button
            type="button"
            title="吸附到 0.5m 网格"
            onClick={() => { const next = !snap; setSnap(next); engine()?.setSnap(next); say(next ? '吸附：开（0.5m 网格）' : '吸附：关') }}
            style={{
              height: 28, padding: '0 12px', borderRadius: 999, cursor: 'pointer', fontSize: 12, fontWeight: 500,
              background: snap ? 'rgba(176,141,87,0.10)' : 'transparent',
              border: `1px solid ${snap ? 'rgba(176,141,87,0.34)' : 'transparent'}`,
              color: snap ? T.accent : T.text3,
            }}
          >
            吸附
          </button>
          <button
            type="button"
            title="自由相机：键盘自己飞"
            onClick={() => { const next = !freeCam; setFreeCam(next); engine()?.setFreeCamera(next); say(next ? '自由相机：开' : '自由相机：关') }}
            style={{
              height: 28, padding: '0 12px', borderRadius: 999, cursor: 'pointer', fontSize: 12, fontWeight: 500,
              background: freeCam ? 'rgba(176,141,87,0.10)' : 'transparent',
              border: `1px solid ${freeCam ? 'rgba(176,141,87,0.34)' : 'transparent'}`,
              color: freeCam ? T.accent : T.text3,
            }}
          >
            自由相机
          </button>
        </div>

        <div style={{ flex: 1, minWidth: 0 }} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <IconButton label="撤销" title="撤销（Ctrl+Z）" onClick={() => { engine()?.undo() }} />
          <IconButton label="重做" title="重做" onClick={() => { engine()?.redo() }} />
          <IconButton label="保存" title="保存场景快照" onClick={() => { engine()?.saveSnapshot(); say('已保存场景快照（可撤销/恢复）') }} />
          <div style={{ width: 1, height: 16, background: T.line }} />
          {props.onAutoShoot !== undefined && (
            <button
              type="button"
              disabled={props.busy === true || shots.length === 0}
              title={shots.length === 0 ? '还没有镜头表：先去「电商」段生成方案' : '按镜头表逐镜摆机位并出图'}
              onClick={() => { props.onAutoShoot?.() }}
              style={{
                height: 30, padding: '0 16px', borderRadius: 4, border: 'none',
                background: props.busy === true || shots.length === 0 ? T.panel3 : T.text,
                color: props.busy === true || shots.length === 0 ? T.text3 : T.panel2,
                fontSize: 12.5, fontWeight: 600, cursor: props.busy === true || shots.length === 0 ? 'default' : 'pointer',
                whiteSpace: 'nowrap',
                transition: 'background-color .2s ease',
              }}
            >
              {props.busy === true ? '拍摄中…' : '一键出片'}
            </button>
          )}
        </div>
      </div>

      {/* ── 主体 ── */}
      <div style={{ display: 'flex', flex: 1, minHeight: 0, position: 'relative' }}>

        {/* 左窄轨（OpenVideoForge 同款：图标 + 两字 + 序号）。
            ★ 为什么要窄轨：素材面板是"抽屉"，浮在视口之上。3D 视口是这个页面的主角，
              1280 宽的窗口里给它 440px 是不够的 —— openvideoforge 给的是 1100px。 */}
        <div style={{ width: 56, background: T.panel, borderRight: `1px solid ${T.line}`, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, padding: '8px 0', flexShrink: 0, position: 'relative', zIndex: 21 }}>
          {([
            { id: 'scene', label: '场景', icon: '◫', no: '01' },
            { id: 'product', label: '商品', icon: '◈', no: '02' },
            { id: 'output', label: '成片', icon: '▤', no: '03' },
          ] as const).map((item) => {
            const active = shelfOpen && shelf === item.id
            return (
              <button
                key={item.id}
                type="button"
                title={item.label}
                onClick={() => { setShelf(item.id); setShelfOpen(true) }}
                style={{
                  width: 46, padding: '6px 0', border: 'none', cursor: 'pointer',
                  background: active ? T.panel2 : 'transparent',
                  borderRadius: 6, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1,
                  color: active ? T.text : T.text3,
                  boxShadow: active ? '0 1px 2px rgba(26,22,18,0.05)' : 'none',
                }}
              >
                <span style={{ fontSize: 15, lineHeight: 1 }}>{item.icon}</span>
                <span style={{ fontSize: 11, fontWeight: active ? 600 : 400 }}>{item.label}</span>
                <span style={{ fontSize: 9, color: T.text3 }}>{item.no}</span>
              </button>
            )
          })}
          <div style={{ flex: 1 }} />
          <button
            type="button"
            title={shelfOpen ? '收起素材面板' : '展开素材面板'}
            onClick={() => { setShelfOpen(!shelfOpen) }}
            style={{ width: 46, height: 26, border: 'none', background: 'transparent', color: T.text3, cursor: 'pointer', fontSize: 13 }}
          >
            {shelfOpen ? '«' : '»'}
          </button>
        </div>

        {/* 素材抽屉：浮在视口之上，不占布局 */}
        {shelfOpen && (
          <div style={{ position: 'absolute', left: 56, top: 0, bottom: 0, width: 264, background: T.panel2, borderRight: `1px solid ${T.line}`, boxShadow: '0 18px 48px rgba(26,22,18,0.14)', zIndex: 20, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
            <div style={{ flex: 1, overflowY: 'auto', padding: '10px 10px 16px' }}>

              {shelf === 'scene' && (
                <>
                  <GroupTitle title="本地场景" count={(props.sources ?? []).length} />

                  {/* ★ 尺度锚定控件：重建模型的尺度是任意的，必须由人给一个参考物。
                      放在列表上方，因为「对齐」按钮用的是这里的值。 */}
                  {props.onNormalizeSource !== undefined && (
                    <div style={{ display: 'flex', gap: 5, alignItems: 'center', marginBottom: 8 }}>
                      <span style={{ fontSize: 10, color: T.text3, flexShrink: 0 }}>参考物</span>
                      <input
                        type="number"
                        step={0.1}
                        min={0.05}
                        value={anchorMeters}
                        title="参考物的真实尺寸（米）。COLMAP 只解出相对尺度，这里给的是绝对锚点。"
                        onChange={(event) => { setAnchorMeters(Number(event.target.value)) }}
                        style={{ width: 58, height: 24, borderRadius: 4, border: `1px solid ${T.line2}`, fontSize: 11, padding: '0 4px', color: T.text, background: T.panel2 }}
                      />
                      <span style={{ fontSize: 10, color: T.text3, flexShrink: 0 }}>米</span>
                      <select
                        value={anchorAxis}
                        onChange={(event) => { setAnchorAxis(event.target.value) }}
                        title="参考物对应场景的哪个方向"
                        style={{ height: 24, borderRadius: 4, border: `1px solid ${T.line2}`, fontSize: 11, color: T.text, background: T.panel2, flex: 1 }}
                      >
                        <option value="width">宽</option>
                        <option value="height">高</option>
                        <option value="depth">深</option>
                      </select>
                    </div>
                  )}

                  {/* 证据卡：这个模型到底有多少点、多大、朝向对不对 */}
                  {props.modelMeta != null && (
                    <div style={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 6, padding: 8, marginBottom: 8 }}>
                      <div style={{ fontSize: 11, color: T.text, fontWeight: 600, marginBottom: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={props.modelMeta.name}>
                        {props.modelMeta.name}
                      </div>
                      <div style={{ fontSize: 10, color: T.text3, lineHeight: 1.8 }}>
                        点数 {props.modelMeta.points.toLocaleString()}
                        {' · '}最长边 {props.modelMeta.max_edge.toFixed(2)}
                        {' · '}{props.modelMeta.vertical_is_y_up ? '竖轴已朝上' : '竖轴需摆正'}
                        <br />
                        尺度待锚定（{anchorMeters} 米 ≈ {props.modelMeta.max_edge > 0 ? Math.round(anchorMeters / props.modelMeta.max_edge * 100000) / 100000 : 0} 单位/米）
                      </div>
                    </div>
                  )}

                  {(props.sources ?? []).length === 0 ? (
                    <Empty
                      lines={['还没有本地场景。', '把 .ply / .splat 放进「成片目录」，或直接上传模型。']}
                      action={{ label: '上传模型 / 场景', onClick: () => { props.onUploadModel?.() } }}
                    />
                  ) : (
                    (props.sources ?? []).slice(0, 20).map((item) => (
                      <div key={item.name} style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <PanelRow title={item.name} desc="点击载入这个场景" onClick={() => { props.onImportSource?.(item.name); say(`正在载入：${item.name}`) }} />
                        </div>
                        {props.onAddSource !== undefined && (
                          <button
                            type="button"
                            title="追加到当前场景（不替换已载入的模型）"
                            onClick={() => { props.onAddSource?.(item.name); say(`已加入场景：${item.name}`) }}
                            style={{ fontSize: 10, padding: '3px 6px', borderRadius: 4, border: `1px solid ${T.line2}`, background: T.panel2, color: T.text3, cursor: 'pointer', flexShrink: 0 }}
                          >
                            ＋
                          </button>
                        )}
                        {props.onNormalizeSource !== undefined && /\.(ply|splat)$/iu.test(item.name) && (
                          <button
                            type="button"
                            title="摆正 + 按参考物锚定尺度 + 落到地面 y=0（另存为 .aligned.ply）"
                            onClick={() => { props.onNormalizeSource?.(item.name, anchorMeters, anchorAxis) }}
                            style={{ fontSize: 10, padding: '3px 6px', borderRadius: 4, border: `1px solid ${T.line2}`, background: T.panel2, color: T.text3, cursor: 'pointer', flexShrink: 0 }}
                          >
                            对齐
                          </button>
                        )}
                      </div>
                    ))
                  )}

                  <GroupTitle title="从图片生成场景" />
                  <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
                    <Pill
                      label={props.generating === true ? '生成中…' : '图片 → 360° 场景'}
                      onClick={() => { props.onGenerateFromImage?.('pano') }}
                      disabled={props.generating === true}
                      title="把图片绕成 360° 的场景：站在圆心可环视、可漫游"
                    />
                    <Pill
                      label="图片 → 分层拆件"
                      onClick={() => { props.onGenerateFromImage?.('ldi') }}
                      disabled={props.generating === true}
                      title="分层深度图：能按深度拆成多个可复用物件"
                    />
                  </div>

                  <GroupTitle title="资产库（可复用）" count={(props.assetItems ?? []).length} />
                  {(props.assetItems ?? []).length === 0 ? (
                    <div style={{ fontSize: 11, color: T.text3, lineHeight: 1.7, padding: '2px 2px 6px' }}>
                      用「图片 → 360° 场景 / 分层拆件」生成的资产会收在这里，可反复调用。
                    </div>
                  ) : (
                    (props.assetItems ?? []).map((item) => (
                      <PanelRow
                        key={`${item.kind}:${item.name}`}
                        title={item.label}
                        desc={`${item.kind === 'scene' ? '场景' : item.kind === 'layer' ? '分层' : '原图'}${item.points !== undefined ? ` · ${item.points.toLocaleString()} 点` : ''}`}
                        onClick={() => { props.onImportAsset?.(item) }}
                      />
                    ))
                  )}
                </>
              )}

              {shelf === 'product' && (
                <>
                  <GroupTitle title="商品" />
                  {props.productSlot ?? <div style={{ fontSize: 11, color: T.text3 }}>商品面板没挂上。</div>}
                  <div style={{ marginTop: 10 }}>
                    <Pill
                      label={props.uploading === true ? '上传中…' : '从本地添加模型'}
                      onClick={() => { props.onUploadModel?.() }}
                      disabled={props.uploading === true}
                      title="支持 glb / obj / ply"
                    />
                  </div>
                </>
              )}

              {shelf === 'output' && (
                <>
                  <GroupTitle title="已保存成片" count={(props.exports ?? []).length} />
                  {(props.exports ?? []).length === 0 ? (
                    <Empty
                      lines={['还没有成片。', '右上「一键出片」按镜头表出图，或右下角拍一张。']}
                      action={{ label: '拍一张', onClick: () => { props.onRender?.() } }}
                    />
                  ) : (
                    (props.exports ?? []).slice().reverse().map((item) => (
                      <PanelRow key={item.name} title={item.name} desc="点击查看" onClick={() => { props.onOpenExport?.(item.name) }} />
                    ))
                  )}
                </>
              )}
            </div>
          </div>
        )}

        {/* 中列：视口 + 镜头条 */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>

          {/* 视口 */}
          <div style={{ flex: 1, minHeight: 0, position: 'relative', background: T.bg }}>
            <SplatStudio
              ref={engineRef as React.Ref<StudioHandle>}
              objects={props.objects}
              onStatus={(message) => { setStatus(message); props.onStatus?.(message) }}
              onSelect={(label) => { setSelected(label) }}
              onState={handleState}
            />

            {/* 顶部：当前镜头横条 —— 主流程的锚点。
                ★ 原来是视口左上角一张 236px 的卡片：1280 宽的窗口里视口只剩 440px，
                  卡片会和右上角的预览卡撞在一起。改成**贴顶的全宽细条**，不占横向空间。 */}
            {currentShot !== null && (
              <div style={{ position: 'absolute', left: 0, right: 0, top: 0, display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', background: 'linear-gradient(180deg, rgba(250,247,240,0.94), rgba(250,247,240,0.68))', borderBottom: '1px solid rgba(216,207,188,0.8)', backdropFilter: 'blur(3px)' }}>
                <span style={{ fontSize: 11, color: '#8a6d40', fontWeight: 600, flexShrink: 0 }}>#{currentShot.number}</span>
                <span style={{ fontSize: 12, color: T.text, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 200 }}>{currentShot.title}</span>
                <span style={{ fontSize: 11, color: T.text3, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {currentShot.beat || '镜头'} · {shotSizeLabel(currentShot.zoom)}{currentShot.orbit ? ' · 环绕' : ''}
                </span>
                {currentShot.frame !== null && <span style={{ fontSize: 11, color: T.ok, flexShrink: 0 }}>已出图</span>}
                <div style={{ flex: 1 }} />
                <button
                  type="button"
                  title="上一镜"
                  disabled={shotIndex === 0}
                  onClick={() => { goShot(shotIndex - 1) }}
                  style={{ width: 26, height: 24, border: `1px solid ${T.line2}`, borderRadius: 5, background: 'rgba(32,36,42,0.92)', color: T.text2, cursor: 'pointer', fontSize: 12, flexShrink: 0 }}
                >
                  ‹
                </button>
                <button
                  type="button"
                  title="下一镜"
                  disabled={shotIndex >= shots.length - 1}
                  onClick={() => { goShot(shotIndex + 1) }}
                  style={{ width: 26, height: 24, border: `1px solid ${T.line2}`, borderRadius: 5, background: 'rgba(32,36,42,0.92)', color: T.text2, cursor: 'pointer', fontSize: 12, flexShrink: 0 }}
                >
                  ›
                </button>
                <button
                  type="button"
                  onClick={() => { shootCurrent(shotIndex) }}
                  style={{ height: 24, padding: '0 12px', borderRadius: 5, border: 'none', background: T.text, color: T.panel2, fontSize: 12, fontWeight: 600, cursor: 'pointer', flexShrink: 0 }}
                >
                  拍这一镜
                </button>
              </div>
            )}

            {/* 左下：实时预览缩略图（挪到左下，避开顶部横条）
                ★ 卡片是**深色**的：OpenVideoForge 的实时渲染预览也是黑底 —— 预览图
                  本身是画面内容，纸面底会把它衬脏。 */}
            <div style={{ position: 'absolute', left: 12, bottom: 12, width: 132, background: 'rgba(10,9,7,0.90)', border: '1px solid rgba(250,247,240,0.18)', borderRadius: 8, overflow: 'hidden', boxShadow: '0 8px 24px rgba(26,22,18,0.28)' }}>
              <div style={{ height: 82, background: '#0a0907', display: 'flex', alignItems: 'center', justifyContent: 'center', position: 'relative' }}>
                <canvas
                  ref={previewCanvasRef}
                  style={{ width: '100%', height: '100%', objectFit: 'contain', display: props.previewUrl === undefined || props.previewUrl === null ? 'block' : 'none' }}
                />
                {props.previewUrl !== undefined && props.previewUrl !== null && (
                  <img src={props.previewUrl} alt="预览" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
                )}
              </div>
              <div style={{ padding: '3px 7px', fontSize: 10, color: '#847967', borderTop: '1px solid #2f2820' }}>
                实时预览 · {resolution.width}x{resolution.height}
              </div>
            </div>

            {/* 右下：视角 · 分辨率 · 自由相机 */}
            <div style={{ position: 'absolute', right: 12, bottom: 12, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
              <div style={{ display: 'flex', gap: 4, background: 'rgba(250,247,240,0.94)', border: `1px solid ${T.line}`, borderRadius: 6, padding: 3, boxShadow: '0 4px 14px rgba(26,22,18,0.10)' }}>
                {[
                  { label: '正', yaw: 0, pitch: 12 },
                  { label: '背', yaw: 180, pitch: 12 },
                  { label: '左', yaw: -90, pitch: 12 },
                  { label: '右', yaw: 90, pitch: 12 },
                  { label: '顶', yaw: 0, pitch: 80 },
                ].map((item) => (
                  <button
                    key={item.label}
                    type="button"
                    onClick={() => { engine()?.lookFrom(item.yaw, item.pitch); say(`视角：${item.label}`) }}
                    style={{ width: 28, height: 24, border: 'none', borderRadius: 4, background: 'transparent', color: T.text2, cursor: 'pointer', fontSize: 11 }}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', background: 'rgba(250,247,240,0.94)', border: `1px solid ${T.line}`, borderRadius: 6, padding: '3px 8px', boxShadow: '0 4px 14px rgba(26,22,18,0.10)' }}>
                <button
                  type="button"
                  onClick={() => { const next = !freeCam; setFreeCam(next); engine()?.setFreeCamera(next); say(next ? '自由相机：开' : '自由相机：关') }}
                  style={{ border: 'none', background: 'transparent', cursor: 'pointer', fontSize: 11, color: freeCam ? T.accent : T.text2 }}
                >
                  自由相机{freeCam ? ' ●' : ''}
                </button>
                <select
                  value={`${resolution.width}x${resolution.height}`}
                  onChange={(event) => {
                    const found = RESOLUTIONS.find((item) => item.label === event.target.value)
                    if (found !== undefined) { changeResolution(found.w, found.h) }
                  }}
                  style={{ height: 22, border: `1px solid ${T.line}`, borderRadius: 4, fontSize: 11, color: T.text, background: T.panel2 }}
                >
                  {RESOLUTIONS.map((item) => <option key={item.label} value={item.label}>{item.label}</option>)}
                </select>
              </div>
            </div>

            {/* 底部居中的操作提示胶囊（OpenVideoForge 同款：黑底米字，不抢画面） */}
            <div style={{ position: 'absolute', left: '50%', bottom: 96, transform: 'translateX(-50%)', display: 'flex', alignItems: 'center', gap: 8, padding: '6px 14px', borderRadius: 999, background: 'rgba(26,22,18,0.86)', color: '#f3efe4', fontSize: 11, pointerEvents: 'none', whiteSpace: 'nowrap' }}>
              左键旋转 · 滚轮缩放 · 右键平移 · WASD 前后左右 · Q/E 升降
            </div>
          </div>

          {/* 镜头条：电商镜头表的主位 —— 形态照抄 OpenVideoForge 的「分镜时间线」：
              一条可读的条（标题/计数/播放控制）+ 一行镜头卡。 */}
          <div style={{ flexShrink: 0, borderTop: `1px solid ${T.line}`, background: T.panel }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px', borderBottom: `1px solid ${T.line2}` }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: T.text, whiteSpace: 'nowrap' }}>● 分镜时间线</span>
              <span style={{ fontSize: 11, color: T.text3, whiteSpace: 'nowrap' }}>{shots.length} 个镜头</span>
              {props.shootTitle !== undefined && (
                <span style={{ fontSize: 11, color: T.text2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 220, minWidth: 0 }} title={props.shootTitle}>
                  {props.shootTitle}
                </span>
              )}
              <div style={{ flex: 1, minWidth: 8 }} />
              {currentShot !== null && (
                <button
                  type="button"
                  onClick={() => { shootCurrent(shotIndex) }}
                  style={{ height: 26, padding: '0 12px', borderRadius: 4, border: 'none', background: T.text, color: T.panel2, fontSize: 12, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                >
                  拍这一镜
                </button>
              )}
            </div>

            {shots.length === 0 ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 11, color: T.text3, padding: '8px 10px' }}>
                <span>还没有镜头表 —— 电商方案里的「钩子 / 痛点 / 演示 / 证据 / CTA」会变成这里的镜头卡。</span>
                <button
                  type="button"
                  onClick={() => { setVfView({ section: 'ecom' }) }}
                  style={{ height: 26, padding: '0 10px', borderRadius: 4, border: `1px solid ${T.line}`, background: T.panel2, color: T.text2, cursor: 'pointer', fontSize: 11 }}
                >
                  去电商段生成方案
                </button>
              </div>
            ) : (
              <div style={{ display: 'flex', gap: 8, alignItems: 'stretch', overflowX: 'auto', padding: '8px 10px' }}>
                <button
                  type="button"
                  title="去电商段补充镜头"
                  onClick={() => { setVfView({ section: 'ecom' }) }}
                  style={{ flexShrink: 0, width: 88, borderRadius: 6, border: `1px dashed ${T.line}`, background: 'transparent', color: T.text3, cursor: 'pointer', fontSize: 11 }}
                >
                  ＋ 加镜头
                </button>
                {shots.map((shot, index) => {
                  const active = index === shotIndex
                  return (
                    <button
                      key={shot.number}
                      type="button"
                      onClick={() => { goShot(index) }}
                      style={{
                        flexShrink: 0, width: 136, textAlign: 'left', cursor: 'pointer',
                        background: active ? T.panel2 : 'transparent',
                        border: `1px solid ${active ? T.accent : T.line}`,
                        borderRadius: 6, padding: '7px 9px', color: T.text,
                        boxShadow: active ? '0 1px 2px rgba(26,22,18,0.05)' : 'none',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 3 }}>
                        <span style={{ fontSize: 11, fontWeight: 600, color: active ? T.accent : T.text2 }}>#{shot.number}</span>
                        <span style={{ fontSize: 11, color: T.text2, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{shot.title}</span>
                        {shot.frame !== null && <span style={{ fontSize: 10, color: T.ok }}>✓</span>}
                      </div>
                      <div style={{ fontSize: 10, color: T.text3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {shot.beat || '镜头'} · {shotSizeLabel(shot.zoom)}{shot.orbit ? ' · 环绕' : ''}
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        </div>

        {/* 右栏：上下文检视器 + AI 指令 */}
        <div style={{ width: 296, background: T.panel, borderLeft: `1px solid ${T.line}`, display: 'flex', flexDirection: 'column', minHeight: 0, flexShrink: 0 }}>
          <div style={{ display: 'flex', borderBottom: `1px solid ${T.line}`, flexShrink: 0 }}>
            {([
              { id: 'shot', label: '镜头' },
              { id: 'object', label: '物件' },
              { id: 'scene', label: '场景' },
              { id: 'rig', label: '运镜' },
            ] as const).map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => { setTab(item.id) }}
                style={{
                  flex: 1, height: 34, border: 'none', cursor: 'pointer', fontSize: 12,
                  background: tab === item.id ? T.panel : 'transparent',
                  color: tab === item.id ? T.text : T.text3,
                  fontWeight: tab === item.id ? 600 : 400,
                  borderBottom: tab === item.id ? `2px solid ${T.accent}` : '2px solid transparent',
                }}
              >
                {item.label}
              </button>
            ))}
          </div>

          <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px 16px' }}>

            {tab === 'shot' && (
              <>
                <Section title="当前镜头" extra={<span style={{ fontSize: 11, color: T.text3 }}>{shots.length > 0 ? `${shotIndex + 1}/${shots.length}` : '—'}</span>} />
                {currentShot === null ? (
                  <div style={{ fontSize: 11, color: T.text3, lineHeight: 1.7, marginBottom: 10 }}>
                    还没有镜头表。先去「电商」段生成方案，这里会出现每个镜头。
                  </div>
                ) : (
                  <div style={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 6, padding: 9, marginBottom: 12 }}>
                    <div style={{ fontSize: 12, color: T.text, marginBottom: 3 }}>#{currentShot.number} {currentShot.title}</div>
                    <div style={{ fontSize: 11, color: T.text3, marginBottom: 8 }}>
                      {currentShot.beat || '镜头'} · {shotSizeLabel(currentShot.zoom)}{currentShot.orbit ? ' · 环绕运镜' : ''}
                      {currentShot.frame !== null ? ' · 已出图' : ''}
                    </div>
                    <button
                      type="button"
                      onClick={() => { shootCurrent(shotIndex) }}
                      style={{ width: '100%', height: 28, border: 'none', borderRadius: 5, background: T.text, color: T.panel2, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}
                    >
                      拍这一镜
                    </button>
                  </div>
                )}

                <Section title="景别" />
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 5, marginBottom: 8 }}>
                  {CAMERA_PRESET_LIST.map((item) => (
                    <button
                      key={item.label}
                      type="button"
                      title={item.desc}
                      onClick={() => { engine()?.lookFrom(item.yaw, item.pitch, item.zoom); say(`机位：${item.label}`) }}
                      style={{ height: 28, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel2, color: T.text2, cursor: 'pointer', fontSize: 12 }}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                {/* ★ 框选整个场景 —— 加载外部重建模型后必点：
                    COLMAP 出的点云尺度与朝向是任意的（固定机位对不上，画面会糊成一团），
                    frameAll 按模型包围盒把相机拉到合适位置。 */}
                <button
                  type="button"
                  onClick={() => { engine()?.frameAll(); say('已框选整个场景') }}
                  style={{ width: '100%', height: 28, borderRadius: 4, border: `1px solid ${T.line}`, background: T.panel3, color: T.text2, cursor: 'pointer', fontSize: 12, marginBottom: 12, whiteSpace: 'nowrap' }}
                >
                  框选整个场景
                </button>

                <Section
                  title="机位书签"
                  extra={props.onSaveCameraMark !== undefined ? (
                    <button
                      type="button"
                      onClick={() => { props.onSaveCameraMark?.() }}
                      style={{ border: 'none', background: 'transparent', color: '#8a6d40', fontSize: 11, cursor: 'pointer', padding: 0 }}
                    >
                      ＋保存当前
                    </button>
                  ) : undefined}
                />
                {(props.cameraMarks ?? []).length === 0 ? (
                  <div style={{ fontSize: 11, color: T.text3, lineHeight: 1.7 }}>构好角度后点「＋保存当前」，随时跳回来。</div>
                ) : (
                  (props.cameraMarks ?? []).map((mark) => (
                    <div key={mark.id} style={{ display: 'flex', gap: 4, alignItems: 'center', marginBottom: 2 }}>
                      <div style={{ flex: 1, cursor: 'pointer' }} onClick={() => { props.onJumpCameraMark?.(mark) }}>
                        <PanelRow title={mark.label} desc={`${cameraSizeLabel(mark.distance)} · ${Number.isFinite(mark.distance) ? mark.distance.toFixed(1) : '—'}m`} onClick={() => { props.onJumpCameraMark?.(mark) }} />
                      </div>
                      <button
                        type="button"
                        title="删除这个机位"
                        onClick={() => { props.onDropCameraMark?.(mark) }}
                        style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, border: `1px solid ${T.line2}`, background: T.panel2, color: T.text3, cursor: 'pointer' }}
                      >
                        ×
                      </button>
                    </div>
                  ))
                )}
              </>
            )}

            {tab === 'object' && (
              <>
                <Section title="场景物件" extra={<span style={{ fontSize: 11, color: T.text3 }}>{objects.length} 项</span>} />
                <div style={{ border: `1px solid ${T.line}`, borderRadius: 6, overflow: 'hidden', marginBottom: 12 }}>
                  {objects.length === 0 && <div style={{ padding: 10, fontSize: 12, color: T.text3 }}>还没有加载物件</div>}
                  {objects.map((item) => (
                    <div
                      key={item.label}
                      onClick={() => { engine()?.select(item.label); say(`已选中「${item.label}」`) }}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px', cursor: 'pointer',
                        background: selected === item.label ? T.accentSoft : T.panel2,
                        borderBottom: `1px solid ${T.line}`,
                      }}
                    >
                      <span
                        onClick={(event) => {
                          event.stopPropagation()
                          const next = !item.visible
                          engine()?.setVisible(item.label, next)
                          say(`「${item.label}」${next ? '显示' : '隐藏'}`)
                        }}
                        style={{ fontSize: 12, color: item.visible ? T.accent : T.text3, width: 14 }}
                      >
                        {item.visible ? '◉' : '○'}
                      </span>
                      <span style={{ fontSize: 12, color: selected === item.label ? '#8a6d40' : T.text, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {item.label}
                      </span>
                    </div>
                  ))}
                </div>

                <Section title="变换" extra={<span style={{ fontSize: 11, color: T.text3 }}>{selected ?? '未选中'}</span>} />
                <div style={{ display: 'flex', gap: 5, marginBottom: 10 }}>
                  {([
                    { id: 'translate', label: '移动' },
                    { id: 'rotate', label: '旋转' },
                    { id: 'scale', label: '缩放' },
                  ] as const).map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => { setMode(item.id); engine()?.setMode(item.id); say(`手柄：${item.label}`) }}
                      style={{
                        flex: 1, height: 26, borderRadius: 5, cursor: 'pointer', fontSize: 12,
                        border: `1px solid ${mode === item.id ? T.accent : T.line2}`,
                        background: mode === item.id ? T.accentSoft : T.panel2,
                        color: mode === item.id ? '#8a6d40' : T.text2,
                      }}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 10 }}>
                  <NumberField label="X" value={transform.x} onChange={(value) => { engine()?.setTransform({ x: value }) }} />
                  <NumberField label="Y" value={transform.y} onChange={(value) => { engine()?.setTransform({ y: value }) }} />
                  <NumberField label="Z" value={transform.z} onChange={(value) => { engine()?.setTransform({ z: value }) }} />
                  <NumberField label="角度°" value={transform.ry} onChange={(value) => { engine()?.setTransform({ ry: value }) }} />
                </div>
                <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
                  <NumberField label="缩放" value={transform.s} step={0.05} onChange={(value) => { engine()?.setTransform({ s: value }) }} />
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button type="button" onClick={() => { engine()?.resetSelected(); say('已复位') }} style={{ flex: 1, height: 28, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel2, color: T.text2, cursor: 'pointer', fontSize: 12 }}>复位</button>
                  <button type="button" onClick={() => { engine()?.removeSelected(); say('已删除该物件') }} style={{ flex: 1, height: 28, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel2, color: T.danger, cursor: 'pointer', fontSize: 12 }}>删除</button>
                </div>
              </>
            )}

            {tab === 'scene' && (
              <>
                <Section title="观感" />
                <div style={{ display: 'flex', gap: 5, marginBottom: 12 }}>
                  {LOOK_PRESETS.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      title={item.desc}
                      onClick={() => { engine()?.setLook(item.id); say(`观感：${item.label}`) }}
                      style={{ flex: 1, height: 28, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel2, color: T.text2, cursor: 'pointer', fontSize: 12 }}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                <Slider label="曝光" value={exposure * 100} min={20} max={250} step={1} unit="%" onChange={(value) => { const next = value / 100; setExposure(next); engine()?.setExposure(next) }} />

                <Section
                  title="阳光"
                  extra={(
                    <button
                      type="button"
                      onClick={() => { const next = !sun.enabled; setSun({ ...sun, enabled: next }); engine()?.setSun({ enabled: next }); say(`阳光：${next ? '开' : '关'}`) }}
                      style={{ width: 30, height: 16, borderRadius: 9, border: 'none', cursor: 'pointer', background: sun.enabled ? T.accent : T.line2, position: 'relative' }}
                    >
                      <span style={{ position: 'absolute', top: 2, left: sun.enabled ? 16 : 2, width: 12, height: 12, borderRadius: 6, background: '#fff', transition: 'left .15s' }} />
                    </button>
                  )}
                />
                <Slider label="方位角" value={sun.azimuth} min={-180} max={180} step={1} unit="°" onChange={(value) => { setSun({ ...sun, azimuth: value }); engine()?.setSun({ azimuth: value }) }} />
                <Slider label="俯仰角" value={sun.elevation} min={0} max={90} step={1} unit="°" onChange={(value) => { setSun({ ...sun, elevation: value }); engine()?.setSun({ elevation: value }) }} />
                <Slider label="色温" value={sun.colorTemp} min={2000} max={12000} step={50} unit="K" onChange={(value) => { setSun({ ...sun, colorTemp: value }); engine()?.setSun({ colorTemp: value }) }} />
                <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
                  {LIGHT_PRESETS.map((preset) => (
                    <button
                      key={preset.label}
                      type="button"
                      title={preset.desc}
                      onClick={() => { applyLightPreset(preset); say(`光源：${preset.label}`) }}
                      style={{ flex: 1, height: 26, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel2, color: T.text2, cursor: 'pointer', fontSize: 12 }}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
                <Slider label="亮度" value={sun.intensity * 100} min={0} max={200} step={1} unit="%" onChange={(value) => { const intensity = value / 100; setSun({ ...sun, intensity }); engine()?.setSun({ intensity }) }} />

                <Section
                  title="全局雾"
                  extra={(
                    <button
                      type="button"
                      onClick={() => { const next = !fog.enabled; setFogState({ ...fog, enabled: next }); engine()?.setFog(next); say(`全局雾：${next ? '开' : '关'}`) }}
                      style={{ width: 30, height: 16, borderRadius: 9, border: 'none', cursor: 'pointer', background: fog.enabled ? T.accent : T.line2, position: 'relative' }}
                    >
                      <span style={{ position: 'absolute', top: 2, left: fog.enabled ? 16 : 2, width: 12, height: 12, borderRadius: 6, background: '#fff', transition: 'left .15s' }} />
                    </button>
                  )}
                />
                {fog.enabled && (
                  <Slider label="雾浓度" value={fog.density * 100} min={0.1} max={5} step={0.1} onChange={(value) => { const density = value / 100; setFogState({ ...fog, density }); engine()?.setFog(true, density) }} />
                )}

                <Section title="输出与吸附" />
                <div style={{ display: 'flex', gap: 5, marginBottom: 8, flexWrap: 'wrap' }}>
                  {RESOLUTIONS.slice(0, 3).map((item) => (
                    <button
                      key={item.label}
                      type="button"
                      onClick={() => { changeResolution(item.w, item.h) }}
                      style={{
                        flex: 1, height: 26, borderRadius: 5, cursor: 'pointer', fontSize: 11,
                        border: `1px solid ${resolution.width === item.w && resolution.height === item.h ? T.accent : T.line2}`,
                        background: resolution.width === item.w && resolution.height === item.h ? T.accentSoft : T.panel2,
                        color: resolution.width === item.w && resolution.height === item.h ? '#8a6d40' : T.text2,
                      }}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                <Pill
                  label={`吸附：${snap ? '开' : '关'}`}
                  active={snap}
                  onClick={() => {
                    const next = !snap
                    setSnap(next)
                    engine()?.setSnap(next)
                    say(next ? '吸附：开（0.5m 网格）' : '吸附：关')
                  }}
                />
                <div style={{ fontSize: 11, color: T.text3, lineHeight: 1.7, marginTop: 10 }}>
                  渲染分辨率改的是**出图尺寸**，视口显示不变。<br />
                  要更高分辨率成片，用顶栏「一键出片」走本地渲染。
                </div>
              </>
            )}

            {tab === 'rig' && (
              <>
                <Section title="运镜轨迹" extra={<span style={{ fontSize: 11, color: T.text3 }}>视口里会画出轨迹线</span>} />
                {RIG_LIST.map((item) => (
                  <div key={item.id} style={{ border: `1px solid ${trajectory === item.id ? T.accent : T.line}`, borderRadius: 6, padding: 8, marginBottom: 8, background: trajectory === item.id ? T.accentSoft : T.panel2 }}>
                    <div style={{ fontSize: 12, color: T.text, marginBottom: 2 }}>{item.name}</div>
                    <div style={{ fontSize: 11, color: T.text3, marginBottom: 6 }}>{item.desc}</div>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button
                        type="button"
                        onClick={() => {
                          const next = trajectory === item.id ? null : item.id
                          setTrajectory(next)
                          engine()?.showTrajectory(next)
                          setActiveKey(null)
                          if (next === null) { setKeyframes([]) }
                          else { setKeyframes(engine()?.getKeyframes(next) ?? []) }
                        }}
                        style={{ flex: 1, height: 26, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel, color: T.text2, cursor: 'pointer', fontSize: 12 }}
                      >
                        {trajectory === item.id ? '隐藏轨迹' : '显示轨迹'}
                      </button>
                      <button
                        type="button"
                        onClick={() => { engine()?.playRig(item.id as 'drone' | 'phone' | 'orbit'); say(`运镜：${item.name}`) }}
                        style={{ flex: 1, height: 26, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel, color: T.text2, cursor: 'pointer', fontSize: 12 }}
                      >
                        ▶ 预览
                      </button>
                      <button
                        type="button"
                        onClick={() => { void engine()?.recordRig(item.id as 'drone' | 'phone' | 'orbit') }}
                        style={{ flex: 1, height: 26, borderRadius: 5, border: 'none', background: T.text, color: T.panel2, cursor: 'pointer', fontSize: 12 }}
                      >
                        ⏺ 录制
                      </button>
                    </div>
                  </div>
                ))}

                <Section title="关键帧时间线" extra={<span style={{ fontSize: 11, color: T.text3 }}>{trajectory === null ? '先选一条轨迹' : RIG_LIST.find((r) => r.id === trajectory)?.name}</span>} />
                {trajectory !== null && (
                  <>
                    <div style={{ position: 'relative', height: 34, marginBottom: 8, borderRadius: 5, background: T.panel2, border: `1px solid ${T.line}` }}>
                      {keyframes.map((key, index) => {
                        const ratio = Math.min(1, Math.max(0, key.t / Math.max(1, duration)))
                        return (
                          <button
                            key={`${key.t}-${index}`}
                            type="button"
                            title={`${(key.t / 1000).toFixed(2)}s　方位角 ${key.yaw.toFixed(0)}°　俯仰 ${key.pitch.toFixed(0)}°　距离 ${key.zoom.toFixed(2)}×`}
                            onClick={() => { setActiveKey(index); say(`选中第 ${index + 1} 个关键帧`) }}
                            style={{
                              position: 'absolute', left: `calc(${ratio * 100}% - 6px)`, top: 8, width: 12, height: 12,
                              transform: 'rotate(45deg)', borderRadius: 2,
                              background: activeKey === index ? T.accent : T.panel,
                              border: `1px solid ${T.accent}`, cursor: 'pointer', padding: 0,
                            }}
                          />
                        )
                      })}
                      <span style={{ position: 'absolute', left: 4, top: 2, fontSize: 10, color: T.text3 }}>0s</span>
                      <span style={{ position: 'absolute', right: 4, top: 2, fontSize: 10, color: T.text3 }}>{(duration / 1000).toFixed(1)}s</span>
                    </div>

                    {activeKey !== null && keyframes[activeKey] !== undefined && (
                      <div style={{ border: `1px solid ${T.line}`, borderRadius: 6, padding: 8, marginBottom: 8, background: T.panel2 }}>
                        <div style={{ fontSize: 12, color: T.text, marginBottom: 6 }}>第 {activeKey + 1} 个关键帧</div>
                        <div style={{ display: 'flex', gap: 5, alignItems: 'center', marginBottom: 5 }}>
                          <span style={{ fontSize: 11, color: T.text3, width: 34 }}>时间</span>
                          <input
                            type="number" step={100}
                            value={keyframes[activeKey]?.t ?? 0}
                            onChange={(event) => { updateKey(activeKey, { t: Number(event.target.value) }) }}
                            title="毫秒"
                            style={{ width: 62, height: 24, borderRadius: 4, border: `1px solid ${T.line2}`, fontSize: 11, padding: '0 4px', color: T.text, background: T.panel }}
                          />
                          <span style={{ fontSize: 11, color: T.text3 }}>ms</span>
                        </div>
                        {([
                          { key: 'yaw', label: '方位角' },
                          { key: 'pitch', label: '俯仰角' },
                          { key: 'zoom', label: '距离倍数' },
                        ] as const).map((row) => (
                          <div key={row.key} style={{ display: 'flex', gap: 5, alignItems: 'center', marginBottom: 5 }}>
                            <span style={{ fontSize: 11, color: T.text3, width: 34 }}>{row.label}</span>
                            <input
                              type="number" step={row.key === 'zoom' ? 0.05 : 1}
                              value={Number((keyframes[activeKey]?.[row.key] ?? 0).toFixed(2))}
                              onChange={(event) => { updateKey(activeKey, { [row.key]: Number(event.target.value) }) }}
                              style={{ width: 62, height: 24, borderRadius: 4, border: `1px solid ${T.line2}`, fontSize: 11, padding: '0 4px', color: T.text, background: T.panel }}
                            />
                          </div>
                        ))}
                        <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                          <button
                            type="button"
                            onClick={() => { engine()?.addKeyframe(trajectory, duration / 2); refreshKeys() }}
                            style={{ flex: 1, height: 26, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel, color: T.text2, cursor: 'pointer', fontSize: 12 }}
                          >
                            ＋ 加关键帧
                          </button>
                          <button
                            type="button"
                            onClick={() => { engine()?.removeKeyframe(trajectory, activeKey); setActiveKey(null); refreshKeys() }}
                            style={{ flex: 1, height: 26, borderRadius: 5, border: 'none', background: T.panel3, color: T.text2, cursor: 'pointer', fontSize: 12 }}
                          >
                            − 删除本帧
                          </button>
                        </div>
                      </div>
                    )}

                    <button
                      type="button"
                      onClick={() => {
                        engine()?.setKeyframes(trajectory, keyframes)
                        say('关键帧已写入，视口里的轨迹线已更新')
                      }}
                      style={{ width: '100%', height: 28, borderRadius: 5, border: 'none', background: T.text, color: T.panel2, cursor: 'pointer', fontSize: 12, marginBottom: 8 }}
                    >
                      应用关键帧到轨迹
                    </button>
                  </>
                )}
                <div style={{ fontSize: 11, color: T.text3, lineHeight: 1.7 }}>
                  录制要把窗口放在前台，否则 Chromium 不合成画面（会提示"没拿到画面"）。
                </div>
              </>
            )}
          </div>

          {/* AI 指令：人和 agent 走同一套具名指令总线 */}
          <div style={{ flexShrink: 0, borderTop: `1px solid ${T.line}`, padding: '8px 10px', background: T.panel2 }}>
            <div style={{ fontSize: 10, color: T.text3, marginBottom: 5 }}>AI 指令：全景 / 特写 / 环绕 / 选中某物 / 放大 / 复位</div>
            {props.aiInput ?? (
              <button
                type="button"
                onClick={() => { props.onFocusAi?.() }}
                style={{ width: '100%', height: 30, borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel, color: T.text3, cursor: 'pointer', fontSize: 12 }}
              >
                点这里指挥场景
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/** 顶栏的一个次要按钮（对应 OpenVideoForge 的 `.btn`：纸面底 + 描边 + 轻投影）。 */
function IconButton(props: { label: string; title: string; onClick: () => void }): JSX.Element {
  return (
    <button
      type="button"
      title={props.title}
      onClick={props.onClick}
      style={{
        height: 28, padding: '0 11px', borderRadius: 4, cursor: 'pointer',
        background: T.panel2, color: T.text2, fontSize: 12,
        border: `1px solid ${T.line}`, boxShadow: '0 1px 2px rgba(26,22,18,0.05)',
      }}
    >
      {props.label}
    </button>
  )
}

/** 左栏的分组标题。 */
function GroupTitle(props: { title: string; count?: number }): JSX.Element {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '12px 0 6px' }}>
      <span style={{ fontSize: 11, fontWeight: 600, color: T.text2, letterSpacing: '0.04em' }}>{props.title}</span>
      {props.count !== undefined && <span style={{ fontSize: 10, color: T.text3 }}>{props.count}</span>}
      <div style={{ flex: 1, height: 1, background: T.line }} />
    </div>
  )
}

/** 空状态：不给"这里还没有内容"，给下一步能点什么。 */
function Empty(props: { lines: readonly string[]; action?: { readonly label: string; readonly onClick: () => void } }): JSX.Element {
  return (
    <div style={{ background: T.panel2, border: `1px dashed ${T.line2}`, borderRadius: 6, padding: 10, marginBottom: 6 }}>
      {props.lines.map((line, index) => (
        <div key={index} style={{ fontSize: 11, color: T.text3, lineHeight: 1.7 }}>{line}</div>
      ))}
      {props.action !== undefined && (
        <button
          type="button"
          onClick={props.action.onClick}
          style={{ marginTop: 8, height: 26, padding: '0 10px', borderRadius: 5, border: `1px solid ${T.line2}`, background: T.panel, color: T.text2, cursor: 'pointer', fontSize: 11 }}
        >
          {props.action.label}
        </button>
      )}
    </div>
  )
}

/** 景别预设（与旧的景别 chips 同一套数值）。 */
const CAMERA_PRESET_LIST = [
  { label: '全景', desc: '整个场景都进画面', yaw: 40, pitch: 24, zoom: 1 },
  { label: '中景', desc: '主体居中', yaw: 40, pitch: 22, zoom: 0.8 },
  { label: '特写', desc: '贴近主体', yaw: 55, pitch: 16, zoom: 0.55 },
  { label: '俯拍', desc: '从上往下看', yaw: 30, pitch: 60, zoom: 1.05 },
  { label: '仰拍', desc: '从下往上看', yaw: 40, pitch: -8, zoom: 0.9 },
  { label: '平视', desc: '人眼高度', yaw: 40, pitch: 6, zoom: 0.9 },
]

/** 观感预设。 */
const LOOK_PRESETS = [
  { id: 'studio' as const, label: '影棚白', desc: '干净的高调性，像产品图' },
  { id: 'neutral' as const, label: '中性灰', desc: '灰底，适合展示材质' },
  { id: 'dusk' as const, label: '黄昏', desc: '暖调，适合场景氛围' },
]

/** 灯光预设。 */
const LIGHT_PRESETS = [
  { label: '红日', desc: '2400K 暖光', colorTemp: 2400, intensity: 1.0, elevation: 18 },
  { label: '正午', desc: '6500K 顶光', colorTemp: 6500, intensity: 1.15, elevation: 62 },
  { label: '月光', desc: '9000K 冷光', colorTemp: 9000, intensity: 0.55, elevation: 40 },
]

/** 运镜。 */
const RIG_LIST = [
  { id: 'drone', name: '无人机俯冲', desc: '高机位俯冲推进' },
  { id: 'phone', name: '手机手持推近', desc: '平视缓慢靠近' },
  { id: 'orbit', name: '环绕一圈', desc: '水平环绕 360°' },
]

/**
 * 距离倍数 → 景别名（镜头卡片上显示用）。
 * @param zoom - 距离倍数（0.55=特写，1=中景）。
 * @returns 景别名。
 */
function shotSizeLabel(zoom: number): string {
  if (zoom < 0.6) { return '特写' }
  if (zoom < 0.85) { return '近景' }
  if (zoom < 1.05) { return '中景' }
  return '全景'
}
