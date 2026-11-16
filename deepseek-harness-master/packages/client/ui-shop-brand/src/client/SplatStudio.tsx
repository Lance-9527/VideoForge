/**
 * 应用内 3D 场景引擎（PlayCanvas 2.x + Gaussian Splat）——**只负责渲染，不带界面**。
 *
 * 为什么不再用 iframe 套 SuperSplat：
 *   SuperSplat 在跨源 iframe 里（`bare=1`）不给父页面**鼠标坐标**，也不暴露 scene graph，
 *   所以"点视口选中物体 → 拖手柄改位置/旋转/大小 → 删除"永远只能靠 RPC 猜着做。
 *   OOOSplat 那套能成，是因为它把 PlayCanvas 直接跑在自己的 React 里：
 *   实体、Gizmo、射线拾取、相机轨迹都是同一份内存里的对象。
 *   这里照同样的思路做：**每个场景物件 = 一个 gsplat 实体**，选中用 AABB 射线，
 *   变换用 PlayCanvas 原生 TranslateGizmo / RotateGizmo / ScaleGizmo。
 *
 * 界面全部交给 `StudioShell`（仿酷家乐棚拍的三栏工作台），
 * 外面通过 `StudioHandle`（useImperativeHandle）驱动这台引擎。
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import * as pc from 'playcanvas'
import { sidecarBase, saveSplatExport } from './api.ts'

/** 加载 splat 资产的超时（毫秒）—— 几十 MB 的 ply 从本机 sidecar 拉，给足时间。 */
const ASSET_LOAD_TIMEOUT_MS = 300_000

/** 落盘产物的超时（毫秒）。 */
const EXPORT_TIMEOUT_MS = 300_000

export interface StudioObject {
  /** 显示名（就是右侧那一层的名字） */
  readonly label: string
  /** PLY 的 http 地址 */
  readonly url: string
  /** 世界包围盒（米），没有就跳过拾取 */
  readonly lo?: readonly [number, number, number]
  readonly hi?: readonly [number, number, number]
}

export type GizmoMode = 'translate' | 'rotate' | 'scale'

export interface StudioTransform {
  x: number
  y: number
  z: number
  ry: number
  s: number
}

export interface StudioSun {
  enabled: boolean
  /** 方位角（度，0=正前，逆时针为正） */
  azimuth: number
  /** 俯仰角（度，0=水平，90=头顶直射） */
  elevation: number
  /** 亮度（0~2） */
  intensity: number
  /** 色温（K） */
  colorTemp: number
}

/** 编辑操作：命令模式的对象，带 do / undo（学自 PlayCanvas SuperSplat 的 EditHistory）。
 *  以前我们的"撤销/恢复"是存整份快照 —— 一次拖动就存一次全场景快照，又大又慢，
 *  而且没法"撤销到一半"。改成命令对象后历史里存的是"改了什么"，可以逐步回退。 */
export interface EditOp {
  readonly label: string
  do(): void
  undo(): void
  destroy?(): void
}

/** 一个相机关键帧（学 SuperSplat 的 camera-poses / anim-track） */
export interface CameraKeyframe {
  /** 相对轨迹起点的毫秒 */
  readonly t: number
  /** 方位角° / 俯仰角° / 距离倍数 */
  readonly yaw: number
  readonly pitch: number
  readonly zoom: number
}

/** 运镜轨迹定义（无人机 / 手机 / 环绕，以及用户自定义的）
 *  现在每条轨迹带一串**关键帧**，不再只是起点/终点两态。 */
export interface RigDef {
  readonly id: string
  readonly name: string
  /** 起点 [yaw°, pitch°, zoom]（保留：两态时用它；有关键帧时以关键帧为准） */
  readonly from: readonly [number, number, number]
  /** 终点 [yaw°, pitch°, zoom] */
  readonly to: readonly [number, number, number]
  readonly ms: number
  /** 图标类型：无人机 / 手机 / 环绕 */
  readonly icon: 'drone' | 'phone' | 'orbit'
  /** 关键帧轨道（空 = 用 from/to 两态插值） */
  readonly keys?: readonly CameraKeyframe[]
}

export const RIGS: readonly RigDef[] = [
  {
    id: 'drone', name: '无人机俯冲', from: [20, 58, 1], to: [85, 26, 0.6], ms: 4000, icon: 'drone',
    // 三关键帧：起跳 → 压低 → 拉平推近（比单纯两端插值更像真的无人机）
    keys: [
      { t: 0, yaw: 20, pitch: 58, zoom: 1 },
      { t: 1600, yaw: 52, pitch: 34, zoom: 0.82 },
      { t: 4000, yaw: 85, pitch: 26, zoom: 0.6 },
    ],
  },
  {
    id: 'phone', name: '手机手持推近', from: [10, 8, 0.95], to: [70, 6, 0.55], ms: 3500, icon: 'phone',
    keys: [
      { t: 0, yaw: 10, pitch: 8, zoom: 0.95 },
      { t: 1200, yaw: 38, pitch: 5, zoom: 0.74 },
      { t: 3500, yaw: 70, pitch: 6, zoom: 0.55 },
    ],
  },
  {
    id: 'orbit', name: '环绕一圈', from: [0, 22, 1], to: [350, 22, 1], ms: 6000, icon: 'orbit',
    keys: [
      { t: 0, yaw: 0, pitch: 22, zoom: 1 },
      { t: 3000, yaw: 175, pitch: 18, zoom: 0.92 },
      { t: 6000, yaw: 350, pitch: 22, zoom: 1 },
    ],
  },
]

export interface StudioCameraState {
  yaw: number
  pitch: number
  distance: number
  target: [number, number, number]
  /** 直接指定相机位置（全景场景要"站进去"，用 orbit 模型表达不了） */
  position?: [number, number, number]
  /** 视野角（度） */
  fov?: number
}

/** 外部驱动引擎用的接口 */
export interface StudioHandle {
  /* 对象 */
  select(label: string | null): void
  getSelected(): string | null
  getTransform(): StudioTransform
  setTransform(next: Partial<StudioTransform>): void
  nudge(axis: 'x' | 'y' | 'z', meters: number): void
  rotate(degrees: number): void
  scaleBy(factor: number): void
  removeSelected(): void
  resetSelected(): void
  setMode(mode: GizmoMode): void
  getMode(): GizmoMode
  listObjects(): { label: string; visible: boolean }[]
  setVisible(label: string, visible: boolean): void
  setAllVisible(visible: boolean): void
  /* 环境 / 灯光 */
  setSun(sun: Partial<StudioSun>): void
  getSun(): StudioSun
  setAmbient(r: number, g: number, b: number): void
  setFog(enabled: boolean, density?: number): void
  getFog(): { enabled: boolean; density: number }
  setExposure(value: number): void
  setLook(key: 'studio' | 'neutral' | 'dusk'): void
  /* 相机 */
  getCamera(): StudioCameraState
  setCamera(next: Partial<StudioCameraState>): void
  lookFrom(yaw: number, pitch: number, zoom?: number): void
  frameAll(): void
  setFreeCamera(enabled: boolean): void
  isFreeCamera(): boolean
  setResolution(width: number, height: number): void
  getResolution(): { width: number; height: number }
  /** 撤销 / 重做（引擎有历史栈，之前没对外暴露）。 */
  undo(): void
  redo(): void
  /* 运镜 */
  playRig(kind: 'drone' | 'phone' | 'orbit'): void
  recordRig(kind: 'drone' | 'phone' | 'orbit'): Promise<void>
  /** 把视口当前画面存成成片（返回文件名；拿不到画面返回 null） */
  captureStill(): Promise<string | null>
  /** 实时预览用的画布（外壳用它做实时镜像） */
  getCanvas(): HTMLCanvasElement | null
  /** 按名字删除物件（AI 指令用） */
  removeObject(label: string): boolean
  /** 把物件移动到指定位置（AI 指令用） */
  moveObject(label: string, x: number, y: number, z: number): boolean
  /* 轨迹 */
  showTrajectory(id: string | null): void
  planTrajectory(id: string, from: readonly [number, number, number], to: readonly [number, number, number]): void
  listTrajectories(): { id: string; name: string; points: number }[]
  /** 读一条轨迹的关键帧 */
  getKeyframes(id: string): CameraKeyframe[]
  /** 写一条轨迹的关键帧（按 t 排序） */
  setKeyframes(id: string, keys: readonly CameraKeyframe[]): void
  /** 加一个关键帧（放在当前时间点附近） */
  addKeyframe(id: string, atMs: number): void
  /** 焦点到视口（快捷键用） */
  focusViewport(): void
  /** 如果场景是"柱面全景"（高瘦、水平截面近似圆），把相机送进圆心。
   *  返回 true 表示确实是全景并已就位。 */
  enterPanorama(): boolean
  /** 删掉第 index 个关键帧 */
  removeKeyframe(id: string, index: number): void
  /* 历史：保存 / 撤销 / 恢复 / 吸附 */
  saveSnapshot(): void
  undo(): void
  redo(): void
  setSnap(enabled: boolean): void
  isSnap(): boolean
  /* 状态 */
  isReady(): boolean
}

interface Props {
  readonly objects: readonly StudioObject[]
  readonly onStatus?: (message: string) => void
  readonly onSelect?: (label: string | null) => void
  /** 选中/变换变化时把最新状态抛给外面（右栏显示用） */
  readonly onState?: (state: { selected: string | null; transform: StudioTransform }) => void
}

interface Engine {
  app: pc.Application | null
  camera: pc.Entity | null
  sun: pc.Entity | null
  gizmoLayer: pc.Layer | null
  gizmos: Partial<Record<GizmoMode, pc.TransformGizmo>> | null
  entities: Map<string, pc.Entity>
  selected: pc.Entity | null
  fillLight: pc.Entity | null
  orbit: { yaw: number; pitch: number; distance: number; target: pc.Vec3 } | null
  freeCamera: boolean
  resolution: { width: number; height: number }
  sunState: StudioSun
  fog: { enabled: boolean; density: number }
  /** 键盘操控：WASD 前后左右，Q/E 升降 */
  keys: Set<string>
  /** 当前相机还是"自动取景"摆的（没被用户手动改过）——改窗口大小时可以重新取景 */
  autoFramed: boolean
  /** 机位已被显式指定（setCamera / 全景场景）——自动取景不再抢相机 */
  cameraLocked: boolean
  /** 正在拖三轴手柄：这期间**相机不许转**（用户抱怨"拖手柄镜头乱飞"） */
  gizmoDragging: boolean
  /** 吸附：拖动时位置对齐到 0.5m 网格 */
  snap: boolean
  /** 变换历史（命令模式，学自 SuperSplat EditHistory） */
  history: EditOp[]
  historyIndex: number
  /** 每个物件的 splat 资源（做 splat 级精确拾取要用 centers） */
  splatResources: Map<string, pc.GSplatResourceBase>

  /** 最近一次「保存」时记录的物件状态 */
  savedTransform: { label: string; transform: StudioTransform } | null
  /** 轨迹相关实体 */
  trajectory: pc.Entity | null
  trajectoryIcon: pc.Entity | null
  trajectoryId: string | null
  customRigs: Map<string, RigDef>
}

/** 色温(K) → RGB（近似 Tanner Helland 公式），返回 0~1 */
export function colorTempToRgb(kelvin: number): [number, number, number] {
  const t = Math.max(1000, Math.min(40000, kelvin)) / 100
  let r = 255
  let g = 255
  let b = 255
  if (t <= 66) {
    r = 255
    g = 99.4708025861 * Math.log(t) - 161.1195681661
    b = t <= 19 ? 0 : 138.5177312231 * Math.log(t - 10) - 305.0447927307
  } else {
    r = 329.698727446 * (t - 60) ** -0.1332047592
    g = 288.1221695283 * (t - 60) ** -0.0755148492
    b = 255
  }
  const clamp = (value: number): number => Math.max(0, Math.min(255, value)) / 255
  return [clamp(r), clamp(g), clamp(b)]
}

export const SplatStudio = forwardRef<StudioHandle, Props>(function SplatStudio(
  { objects, onStatus, onSelect, onState },
  ref,
): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stateRef = useRef<Engine>({
    app: null, camera: null, sun: null, gizmoLayer: null, gizmos: null,
    entities: new Map(), selected: null, fillLight: null, orbit: null,
    freeCamera: false, resolution: { width: 1200, height: 1600 },
    sunState: { enabled: true, azimuth: 25, elevation: 50, intensity: 1.15, colorTemp: 6493 },
    fog: { enabled: false, density: 0.01 },
    keys: new Set<string>(),
    autoFramed: true,
    cameraLocked: false,
    gizmoDragging: false,
    snap: false,
    history: [],
    historyIndex: 0,
    splatResources: new Map<string, pc.GSplatResourceBase>(),
    savedTransform: null,
    trajectory: null,
    trajectoryIcon: null,
    trajectoryId: null,
    customRigs: new Map<string, RigDef>(),
  })

  /** 供 ResizeObserver 回调使用（定义在后面，用 ref 兜住） */
  const frameFromEntitiesRef = useRef<(() => void) | null>(null)

  const [mode, setMode] = useState<GizmoMode>('translate')
  const [ready, setReady] = useState(false)
  /** app 实例代数：StrictMode 双挂载时 cleanup 会把 entities 清空，靠它触发重新加载 */
  const [epoch, setEpoch] = useState(0)
  const [hud, setHud] = useState('初始化中…')

  const say = (message: string): void => {
    setHud(message)
    onStatus?.(message)
  }

  /* ── 建 app / 相机 / 光，只建一次 ── */
  useEffect(() => {
    const canvas = canvasRef.current
    if (canvas === null) { return }
    const app = new pc.Application(canvas, {
      graphicsDeviceOptions: { antialias: true, alpha: false, preserveDrawingBuffer: true },
    })
    app.setCanvasFillMode(pc.FILLMODE_NONE)
    app.setCanvasResolution(pc.RESOLUTION_AUTO)
    app.graphicsDevice.maxPixelRatio = Math.min(2, window.devicePixelRatio || 1)
    app.scene.ambientLight = new pc.Color(0.55, 0.57, 0.6)

    const camera = new pc.Entity('camera')
    camera.addComponent('camera', {
      clearColor: new pc.Color(0.94, 0.93, 0.9),
      fov: 50,
      nearClip: 0.05,
      farClip: 2000,
    })
    app.root.addChild(camera)
    camera.setPosition(12, 9, 14)
    camera.lookAt(0, 1.5, 0)

    const sun = new pc.Entity('sun')
    sun.addComponent('light', { type: 'directional', intensity: 1.15, castShadows: false })
    sun.setEulerAngles(50, 25, 0)
    app.root.addChild(sun)

    // 半球光：天光 + 地面反弹，splat 才不会有"死黑面"（参照 marble 那种均匀的柔和感）
    const hemisphere = new pc.Entity('hemisphere')
    hemisphere.addComponent('light', {
      type: 'omni',
      color: new pc.Color(0.86, 0.90, 1.0),
      intensity: 0.45,
      range: 400,
      castShadows: false,
    })
    hemisphere.setPosition(0, 40, 0)
    app.root.addChild(hemisphere)

    app.start()

    /* ── 键盘操控：WASD = 前后左右平移视角，Q/E = 视角升�� ──
     * 移动的是「相机 + 目标点」整体，所以不会丢失当前构图。 */
    const keys = stateRef.current.keys
    const isTyping = (event: KeyboardEvent): boolean => {
      const target = event.target as HTMLElement | null
      if (target === null) { return false }
      const tag = target.tagName
      return tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable
    }
    const onKeyDown = (event: KeyboardEvent): void => {
      if (isTyping(event)) { return }
      keys.add(event.code)
    }
    const onKeyUp = (event: KeyboardEvent): void => {
      keys.delete(event.code)
    }
    const onBlur = (): void => { keys.clear() }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)

    app.on('update', (deltaTime: number) => {
      if (keys.size === 0) { return }
      const orbit = stateRef.current.orbit
      const camera = stateRef.current.camera
      if (orbit === null || camera === null) { return }
      const forward = camera.forward
      const right = camera.right
      // 水平方向的前后左右（贴地走，不会上天）
      const fx = forward.x
      const fz = forward.z
      const flen = Math.hypot(fx, fz)
      const rx = right.x
      const rz = right.z
      const rlen = Math.hypot(rx, rz)
      if (flen < 1e-4 || rlen < 1e-4) { return }
      const forwardX = fx / flen
      const forwardZ = fz / flen
      const rightX = rx / rlen
      const rightZ = rz / rlen
      let moveX = 0
      let moveZ = 0
      let moveY = 0
      const press = (code: string, alias?: string): boolean => keys.has(code) || (alias !== undefined && keys.has(alias))
      if (press('KeyW', 'ArrowUp')) { moveX += forwardX; moveZ += forwardZ }
      if (press('KeyS', 'ArrowDown')) { moveX -= forwardX; moveZ -= forwardZ }
      if (press('KeyD', 'ArrowRight')) { moveX += rightX; moveZ += rightZ }
      if (press('KeyA', 'ArrowLeft')) { moveX -= rightX; moveZ -= rightZ }
      if (press('KeyE')) { moveY += 1 }
      if (press('KeyQ')) { moveY -= 1 }
      if (moveX === 0 && moveZ === 0 && moveY === 0) { return }
      // 速度跟着相机距离走，远景推得快、近景挪得慢
      stateRef.current.autoFramed = false
      const speed = orbit.distance * 0.85 * Math.min(0.1, deltaTime)
      const dx = moveX * speed
      const dz = moveZ * speed
      const dy = moveY * speed
      const target = new pc.Vec3(
        orbit.target.x + dx,
        orbit.target.y + dy,
        orbit.target.z + dz,
      )
      orbit.target = target
      camera.setPosition(
        camera.getPosition().x + dx,
        camera.getPosition().y + dy,
        camera.getPosition().z + dz,
      )
      camera.lookAt(target)
    })

    // 调试钩子：控制台里能直接查实体/资源状态（排查"画不出来"用）
    ;(window as unknown as { __vfPc?: pc.Application }).__vfPc = app

    stateRef.current.app = app
    stateRef.current.camera = camera
    stateRef.current.sun = sun
    stateRef.current.fillLight = hemisphere
    stateRef.current.gizmoLayer = pc.Gizmo.createLayer(app, 'Transform Axes')
    stateRef.current.gizmos = {
      translate: new pc.TranslateGizmo(camera.camera!, stateRef.current.gizmoLayer),
      rotate: new pc.RotateGizmo(camera.camera!, stateRef.current.gizmoLayer),
      scale: new pc.ScaleGizmo(camera.camera!, stateRef.current.gizmoLayer),
    }
    // ★ 拖三轴手柄的时候**锁住相机**：用户抱怨"点方向箭头，镜头自己乱转"。
    //   PlayCanvas 的 gizmo 会发 transform:start / transform:end，用它当闸门。
    for (const gizmo of Object.values(stateRef.current.gizmos)) {
      if (gizmo === undefined) { continue }
      gizmo.on('transform:start', () => { stateRef.current.gizmoDragging = true })
      gizmo.on('transform:end', () => { stateRef.current.gizmoDragging = false })
    }
    stateRef.current.orbit = { yaw: 40, pitch: 24, distance: 20, target: new pc.Vec3(0, 1.5, 0) }
    setReady(true)
    setEpoch((value) => value + 1)
    say('编辑器就绪')

    const fit = (): void => {
      const parent = canvas.parentElement
      if (parent === null) { return }
      const width = Math.max(64, parent.clientWidth)
      const height = Math.max(64, parent.clientHeight)
      app.resizeCanvas(width, height)
    }
    const observer = new ResizeObserver(() => {
      fit()
      // 视口尺寸变了（左右面板宽度固定，但窗口大小会变）→ 自动取景模式下重算一次
      if (stateRef.current.autoFramed) { frameFromEntitiesRef.current?.() }
    })
    if (canvas.parentElement !== null) { observer.observe(canvas.parentElement) }
    fit()

    return () => {
      observer.disconnect()
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
      app.destroy()
      stateRef.current.app = null
      stateRef.current.camera = null
      stateRef.current.sun = null
      stateRef.current.entities.clear()
      stateRef.current.selected = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /* ── 视角操作：左键拖=转视角，滚轮=缩放，右键拖=平移 ── */
  useEffect(() => {
    const canvas = canvasRef.current
    if (canvas === null) { return }
    let dragging: 'orbit' | 'pan' | null = null
    let lastX = 0
    let lastY = 0

    const applyCamera = (): void => {
      const orbit = stateRef.current.orbit
      const camera = stateRef.current.camera
      if (orbit === null || camera === null) { return }
      const y = (orbit.yaw * Math.PI) / 180
      const p = (orbit.pitch * Math.PI) / 180
      const cos = Math.cos(p)
      camera.setPosition(
        orbit.target.x + orbit.distance * cos * Math.sin(y),
        orbit.target.y + orbit.distance * Math.sin(p),
        orbit.target.z + orbit.distance * cos * Math.cos(y),
      )
      camera.lookAt(orbit.target)
    }

    const down = (event: PointerEvent): void => {
      stateRef.current.autoFramed = false
      stateRef.current.cameraLocked = false
      if (event.button === 0) { dragging = 'orbit' } else if (event.button === 2) { dragging = 'pan' }
      else { return }
      lastX = event.clientX
      lastY = event.clientY
      canvas.setPointerCapture(event.pointerId)
    }
    const move = (event: PointerEvent): void => {
      if (dragging === null) { return }
      // 正在拖三轴手柄 → 相机不许动
      if (stateRef.current.gizmoDragging) { return }
      const orbit = stateRef.current.orbit
      const camera = stateRef.current.camera
      if (orbit === null || camera === null) { return }
      const dx = event.clientX - lastX
      const dy = event.clientY - lastY
      lastX = event.clientX
      lastY = event.clientY
      if (dragging === 'orbit') {
        orbit.yaw -= dx * 0.3
        orbit.pitch = Math.max(-89, Math.min(89, orbit.pitch + dy * 0.3))
      } else {
        const right = camera.right
        const up = camera.up
        const scale = orbit.distance * 0.0016
        orbit.target = new pc.Vec3(
          orbit.target.x - right.x * dx * scale - up.x * dy * scale,
          orbit.target.y - right.y * dx * scale - up.y * dy * scale,
          orbit.target.z - right.z * dx * scale - up.z * dy * scale,
        )
      }
      applyCamera()
    }
    const up = (event: PointerEvent): void => {
      dragging = null
      if (canvas.hasPointerCapture(event.pointerId)) { canvas.releasePointerCapture(event.pointerId) }
    }
    const wheel = (event: WheelEvent): void => {
      const orbit = stateRef.current.orbit
      if (orbit === null) { return }
      stateRef.current.autoFramed = false
      orbit.distance = Math.max(0.5, Math.min(400, orbit.distance * (event.deltaY > 0 ? 1.12 : 0.89)))
      applyCamera()
    }
    const menu = (event: Event): void => { event.preventDefault() }

    canvas.addEventListener('pointerdown', down)
    canvas.addEventListener('pointermove', move)
    canvas.addEventListener('pointerup', up)
    canvas.addEventListener('pointercancel', up)
    canvas.addEventListener('wheel', wheel)
    canvas.addEventListener('contextmenu', menu)
    applyCamera()
    return () => {
      canvas.removeEventListener('pointerdown', down)
      canvas.removeEventListener('pointermove', move)
      canvas.removeEventListener('pointerup', up)
      canvas.removeEventListener('pointercancel', up)
      canvas.removeEventListener('wheel', wheel)
      canvas.removeEventListener('contextmenu', menu)
    }
  }, [])

  /* ── 视口内框选（酷家乐那套"点物体选中"靠这个）── */
  const selectByLabel = useCallback((label: string | null): void => {
    const entity = label === null ? null : (stateRef.current.entities.get(label) ?? null)
    stateRef.current.selected = entity
    const gizmos = stateRef.current.gizmos
    if (gizmos !== null) {
      for (const [key, gizmo] of Object.entries(gizmos)) {
        if (gizmo === undefined) { continue }
        gizmo.detach?.()
        gizmo.enabled = key === mode && entity !== null
      }
      const active = gizmos[mode]
      if (active !== undefined && entity !== null) { active.attach([entity]) }
    }
    onSelect?.(entity?.name ?? null)
    if (entity !== null) { say(`已选中「${entity.name}」`) }
  }, [mode, onSelect, say])

  /* ── 就绪 + 有物件就立刻取景（不等 PLY 加载完，避免一组失败就空白）── */
  /** 把相机摆到"能装下整个场景"的位置。
   *  bounds 用 [minX,minY,minZ,maxX,maxY,maxZ] 表示；没有边界就什么都不做。 */
  const frameBounds = useCallback((bounds: readonly number[] | null): void => {
    const camera = stateRef.current.camera
    const orbit = stateRef.current.orbit
    const app = stateRef.current.app
    if (camera === null || orbit === null || app === null || bounds === null) { return }
    const [minX, minY, minZ, maxX, maxY, maxZ] = bounds
    if (minX === undefined || maxX === undefined || minY === undefined || maxY === undefined || minZ === undefined || maxZ === undefined) { return }
    const center = new pc.Vec3((minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2)
    // ★ 用包围盒 8 个角做屏幕投影拟合，不用"半径 / sin(半角)"：
    //   splat 本身很小（几厘米一个），相机一拉到 60 米，一个 splat 不到 0.25 像素，
    //   **直接被渲染器丢掉** → 画面里什么都没有（这个坑查了很久）。
    //   投影拟合能把相机拉到"刚好装下场景"的位置，splat 就有足够像素了。
    const corners: pc.Vec3[] = []
    for (const cx of [minX, maxX]) {
      for (const cy of [minY, maxY]) {
        for (const cz of [minZ, maxZ]) { corners.push(new pc.Vec3(cx, cy, cz)) }
      }
    }
    const yaw = (orbit.yaw * Math.PI) / 180
    const pitch = (orbit.pitch * Math.PI) / 180
    const cos = Math.cos(pitch)
    // 相机在 center + dir * distance，视线方向 = -dir
    const dirX = cos * Math.sin(yaw)
    const dirY = Math.sin(pitch)
    const dirZ = cos * Math.cos(yaw)
    const forward = new pc.Vec3(-dirX, -dirY, -dirZ).normalize()
    let right = new pc.Vec3().cross(forward, pc.Vec3.UP)
    if (right.length() < 1e-5) { right = new pc.Vec3(1, 0, 0) } else { right.normalize() }
    const up = new pc.Vec3().cross(right, forward).normalize()
    const fov = (camera.camera?.fov ?? 50) * (Math.PI / 180)
    // ★ 用渲染目标的真实比例，不是 camera.aspectRatio（那个默认可能是 16/9，
    //   会算得过远，模型在视口里显得很小）
    const aspect = Math.max(0.2, app.graphicsDevice.width / Math.max(1, app.graphicsDevice.height))
    const tanV = Math.tan(fov / 2)
    const tanH = Math.max(0.01, tanV * aspect)
    // 闭式解：角点 c 相对中心，相机距离 d 时
    //   水平要装下 ⇒ d ≥ |c·right| / tanH − c·forward
    //   垂直要装下 ⇒ d ≥ |c·up|    / tanV − c·forward
    let distance = 1
    for (const corner of corners) {
      const cx = corner.x - center.x
      const cy = corner.y - center.y
      const cz = corner.z - center.z
      const vx = cx * right.x + cy * right.y + cz * right.z
      const vy = cx * up.x + cy * up.y + cz * up.z
      const vz = cx * forward.x + cy * forward.y + cz * forward.z
      distance = Math.max(distance, Math.abs(vx) / tanH - vz, Math.abs(vy) / tanV - vz)
    }
    distance = Math.max(1, distance * 0.95)
    orbit.target = center
    orbit.distance = distance
    camera.setPosition(center.x + dirX * distance, center.y + dirY * distance, center.z + dirZ * distance)
    camera.lookAt(center)
    stateRef.current.autoFramed = true
  }, [])

  /** 用 manifest 里的包围盒取景（物件还没加载完时的初始取景） */
  const frameSceneFrom = useCallback((list: readonly StudioObject[]): void => {
    let minX = Infinity; let minY = Infinity; let minZ = Infinity
    let maxX = -Infinity; let maxY = -Infinity; let maxZ = -Infinity
    let any = false
    for (const item of list) {
      if (item.lo === undefined || item.hi === undefined) { continue }
      any = true
      minX = Math.min(minX, item.lo[0]); maxX = Math.max(maxX, item.hi[0])
      minY = Math.min(minY, item.lo[1]); maxY = Math.max(maxY, item.hi[1])
      minZ = Math.min(minZ, item.lo[2]); maxZ = Math.max(maxZ, item.hi[2])
    }
    if (!any) { return }
    frameBounds([minX, minY, minZ, maxX, maxY, maxZ])
    stateRef.current.autoFramed = true
  }, [frameBounds])

  /** 用**已加载的真实 splat 包围盒**取景（比 manifest 准，manifest 的单位/轴可能对不上） */
  const frameFromEntities = useCallback((): void => {
    // ★ 用户/程序已经指定了机位（全景场景要"站进圆心"）→ 不再自动取景抢相机
    if (stateRef.current.cameraLocked) { return }
    let minX = Infinity; let minY = Infinity; let minZ = Infinity
    let maxX = -Infinity; let maxY = -Infinity; let maxZ = -Infinity
    let any = false
    for (const entity of stateRef.current.entities.values()) {
      const aabb = entity.gsplat?.customAabb
      if (aabb === undefined || aabb === null) { continue }
      any = true
      minX = Math.min(minX, aabb.center.x - aabb.halfExtents.x)
      maxX = Math.max(maxX, aabb.center.x + aabb.halfExtents.x)
      minY = Math.min(minY, aabb.center.y - aabb.halfExtents.y)
      maxY = Math.max(maxY, aabb.center.y + aabb.halfExtents.y)
      minZ = Math.min(minZ, aabb.center.z - aabb.halfExtents.z)
      maxZ = Math.max(maxZ, aabb.center.z + aabb.halfExtents.z)
    }
    if (!any) { return }
    frameBounds([minX, minY, minZ, maxX, maxY, maxZ])
  }, [frameBounds])

  const frameScene = useCallback((): void => { frameSceneFrom(objects) }, [frameSceneFrom, objects])

  useEffect(() => { frameFromEntitiesRef.current = frameFromEntities }, [frameFromEntities])

  useEffect(() => {
    if (!ready || objects.length === 0) { return }
    frameScene()
    const timer = window.setTimeout(() => { frameScene() }, 2500)
    return () => { window.clearTimeout(timer) }
  }, [ready, objects, frameScene])

  /* ── 加载物件（每个分组一个 gsplat 实体）── */
  useEffect(() => {
    const app = stateRef.current.app
    if (app === null || !ready || objects.length === 0) { return }
    // ★ 不能只加载一次：分组是"边导入边到"的，切编辑器时 objects 可能只有 5 个。
    //   这里按 label 增量加载，已经有的跳过，后到的继续加。
    const pending = objects.filter((item) => !stateRef.current.entities.has(item.label))
    if (pending.length === 0) { return }
    let done = 0
    const total = pending.length
    say(`加载物件 0/${total}…`)

    let failed: string[] = []

    const loadOne = async (item: StudioObject, attempt: number): Promise<void> => {
      try {
        const response = await fetch(item.url, { cache: 'no-store', signal: AbortSignal.timeout(ASSET_LOAD_TIMEOUT_MS) })
        if (!response.ok) { throw new Error(`HTTP ${response.status}`) }
        const declared = Number(response.headers.get('content-length') ?? '0')
        if (declared > 0 && declared < 128) { throw new Error(`文件太小（${declared} B）`) }
        const asset = new pc.Asset(`gsplat-${item.label}`, 'gsplat', { filename: `${item.label}.ply`, url: item.url })
        ;(asset.file as unknown as { contents?: Response }).contents = response
        app.assets.add(asset)
        const ok = await new Promise<boolean>((resolve) => {
          asset.once('load', () => { resolve(true) })
          asset.once('error', (error: string) => {
            say(`「${item.label}」解析失败：${String(error)}`)
            resolve(false)
          })
          app.assets.load(asset)
        })
        if (!ok) { throw new Error('解析失败') }

        const entity = new pc.Entity(item.label)
        app.root.addChild(entity)
        // ★ 顺序很关键：先 addComponent（空数据），再赋 asset。
        entity.addComponent('gsplat', {})
        const gsplat = entity.gsplat
        if (gsplat === undefined) { throw new Error('没有 gsplat 组件') }
        // ★ 关键：gsplat 组件的 layers 默认是**空数组**，空数组就没人把它加进渲染层，
        //   资源加载成功、实例也建了，但一个像素都不画。
        const worldLayer = app.scene.layers.getLayerByName('World')
        const opaqueLayer = app.scene.layers.getLayerByName('Opaque')
        gsplat.layers = [worldLayer?.id, opaqueLayer?.id].filter((v): v is number => typeof v === 'number')
        gsplat.asset = asset
        // ★ 兜底：组件**不会**自动建 GSplatInstance（instance 一直是 null → 一个像素都不画）。
        //   必须自己 new 一个 GSplatInstance(resource, { scene }) 挂上去。
        //   资源有时比 load 事件晚到位，所以多试几次。
        const ensureInstance = (): void => {
          if (gsplat.instance != null) { return }
          const resource = asset.resource
          if (resource == null) { return }
          try {
            gsplat.instance = new pc.GSplatInstance(resource as pc.GSplatResourceBase, { scene: app.scene })
          } catch (error) {
            say(`「${item.label}」实例化失败：${error instanceof Error ? error.message : String(error)}`)
          }
        }
        ensureInstance()
        window.setTimeout(ensureInstance, 250)
        window.setTimeout(ensureInstance, 900)
        stateRef.current.entities.set(item.label, entity)
        const loaded = asset.resource
        if (loaded != null) { stateRef.current.splatResources.set(item.label, loaded as pc.GSplatResourceBase) }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        if (attempt < 2) {
          await new Promise((resolve) => { window.setTimeout(resolve, 300) })
          return await loadOne(item, attempt + 1)
        }
        failed = [...failed, `${item.label}（${message}）`]
        say(`「${item.label}」加载失败：${message}`)
      } finally {
        done += 1
        if (done === total) {
          frameFromEntities()
          window.setTimeout(() => { frameFromEntities() }, 400)
          say(failed.length === 0
            ? `已加载 ${stateRef.current.entities.size}/${total} 个物件——点场景里的物体即可选中`
            : `已加载 ${stateRef.current.entities.size}/${total}，失败：${failed.join('、')}`)
        }
      }
    }

    // **串行**执行加载：并行时 PlayCanvas 的资源加载偶发整组失败（每次挂的组还不一样）
    void (async () => {
      for (const item of pending) { await loadOne(item, 1) }
    })()
  }, [objects, ready, epoch, frameFromEntities])

  /* ── splat 级精确拾取 ──
   *  以前是"射线打物件的 AABB"，大平台的包围盒会把里面的小躺椅一起罩住，
   *  于是点躺椅选中了平台（用户抱怨"点哪个是哪个，别搞串了"）。
   *
   *  学 SuperSplat 的做法：框选/点选是按 **splat 在屏幕上的投影 footprint** 判定的。
   *  这里用同一思路：把每个 splat 的中心投影到屏幕，算出它的屏幕半径，
   *  命中点击像素的 splat 属于哪个物件，就选中哪个物件；多个都命中时取**离相机最近**的。
   *  一次点击只算一次，16 万个 splat 也就十几毫秒。 */
  const pickSplat = useCallback((screenX: number, screenY: number, camera: pc.Entity): string | null => {
    const app = stateRef.current.app
    if (app === null) { return null }
    const cameraComponent = camera.camera
    if (cameraComponent === undefined) { return null }
    const view = app.graphicsDevice.width
    const viewHeight = app.graphicsDevice.height
    if (view < 2 || viewHeight < 2) { return null }

    // 第一步：物件包围盒粗筛（射线打 AABB）——不做这步会遍历全部 145 万个 splat
    const from = cameraComponent.screenToWorld(screenX, screenY, cameraComponent.nearClip)
    const to = cameraComponent.screenToWorld(screenX, screenY, cameraComponent.farClip)
    const candidates: string[] = []
    for (const item of objects) {
      if (item.lo === undefined || item.hi === undefined) { continue }
      const entity = stateRef.current.entities.get(item.label)
      if (entity === undefined || !entity.enabled) { continue }
      const inverse = entity.getWorldTransform().clone().invert()
      const localFrom = inverse.transformPoint(from)
      const localTo = inverse.transformPoint(to)
      const direction = new pc.Vec3().sub2(localTo, localFrom)
      const length = direction.length()
      if (length < 1e-6) { continue }
      direction.mulScalar(1 / length)
      if (rayBoxDistance(localFrom, direction, item.lo, item.hi) !== null) {
        candidates.push(item.label)
      }
    }
    if (candidates.length === 0) { return null }

    // 第二步：splat 级精检。
    //  ★ 不用 camera.worldToScreen —— 它每次都重建矩阵，18 万次调用直接卡死主线程。
    //    这里自己用 viewProjection 矩阵做纯数学投影（快 100 倍以上）。
    const viewProjection = cameraComponent.projectionMatrix.clone().mul(cameraComponent.viewMatrix)
    const m = viewProjection.data
    const focal = viewHeight / (2 * Math.tan(((cameraComponent.fov ?? 45) * Math.PI) / 360))

    let bestLabel: string | null = null
    let bestDepth = Number.POSITIVE_INFINITY
    let bestHits = 0

    for (const label of candidates) {
      const entity = stateRef.current.entities.get(label)
      if (entity === undefined) { continue }
      // ★ 2.x 的 unified 模式下 component.instance 是 null（实例在 _placement 里），
      //   资源要从 component.resource 拿：GSplatResource.centers = 每个 splat 的局部坐标。
      const component = entity.gsplat
      const resource = component?.resource ?? stateRef.current.splatResources.get(label)
      const centers = resource?.centers
      if (centers === undefined || centers === null || centers.length < 3) { continue }
      const count = Math.floor(centers.length / 3)
      // 单个物件最多精检 2 万个 splat（超过就抽样），保证点击不卡
      const step = Math.max(1, Math.ceil(count / 20_000))
      const world = entity.getWorldTransform().data
      let hits = 0
      let hitDepth = Number.POSITIVE_INFINITY
      for (let index = 0; index < count; index += step) {
        const offset = index * 3
        const lx = centers[offset] ?? 0
        const ly = centers[offset + 1] ?? 0
        const lz = centers[offset + 2] ?? 0
        // 物件变换 → 世界
        const wx = (world[0] ?? 0) * lx + (world[4] ?? 0) * ly + (world[8] ?? 0) * lz + (world[12] ?? 0)
        const wy = (world[1] ?? 0) * lx + (world[5] ?? 0) * ly + (world[9] ?? 0) * lz + (world[13] ?? 0)
        const wz = (world[2] ?? 0) * lx + (world[6] ?? 0) * ly + (world[10] ?? 0) * lz + (world[14] ?? 0)
        // 世界 → 裁剪空间
        const cx = (m[0] ?? 0) * wx + (m[4] ?? 0) * wy + (m[8] ?? 0) * wz + (m[12] ?? 0)
        const cy = (m[1] ?? 0) * wx + (m[5] ?? 0) * wy + (m[9] ?? 0) * wz + (m[13] ?? 0)
        const cw = (m[3] ?? 0) * wx + (m[7] ?? 0) * wy + (m[11] ?? 0) * wz + (m[15] ?? 0)
        if (cw <= 0.0001) { continue }
        const depth = cw
        const sx = (cx / cw * 0.5 + 0.5) * view
        const sy = (1 - (cy / cw * 0.5 + 0.5)) * viewHeight
        // 屏幕半径：splat 是厘米级（半径 ~6cm），按焦距换算；实际只有 1px 上下
        const radius = Math.max(1.0, (0.06 / Math.max(0.5, depth)) * focal)
        const dx = sx - screenX
        const dy = sy - screenY
        if (dx * dx + dy * dy > radius * radius) { continue }
        hits += 1
        if (depth < hitDepth) { hitDepth = depth }
      }
      // ★ 密度门槛：只有命中点周围**确实有一片 splat** 才算数。
      //   远处飘过来的零星 splat 会造成"点 A 选中 B"，这一条把那种噪声挡掉。
      if (hits < 3) { continue }
      // ★ 判定顺序：**命中密度优先，深度次之**。
      //   躺椅是实心家具（命中点周围 splat 很密），树冠是散开的树叶（稀疏）——
      //   只按"最近的"判会永远被悬在上方的树冠抢走。
      if (hits > bestHits || (hits === bestHits && hitDepth < bestDepth - 0.5)) {
        bestDepth = hitDepth
        bestHits = hits
        bestLabel = label
      }
    }
    return bestLabel
  }, [objects])

  /* ── 点选：射线打每个物件的包围盒 ── */
  useEffect(() => {
    const canvas = canvasRef.current
    if (canvas === null) { return }
    let downX = 0
    let downY = 0
    let downTime = 0

    const down = (event: PointerEvent): void => {
      downX = event.clientX
      downY = event.clientY
      downTime = performance.now()
    }
    const up = (event: MouseEvent): void => {
      // 拖动视角不算点选
      if (Math.abs(event.clientX - downX) > 4 || Math.abs(event.clientY - downY) > 4) { return }
      if (performance.now() - downTime > 600) { return }
      const camera = stateRef.current.camera
      if (camera === null) { return }
      const cameraComponent = camera.camera
      if (cameraComponent === undefined) { return }
      const rect = canvas.getBoundingClientRect()
      const x = event.clientX - rect.left
      const y = event.clientY - rect.top

      // ★ 第一优先：splat 级精确命中（学 SuperSplat 的 footprint 判定）。
      //   命中就直接选中那个物件 —— 点哪个是哪个，不会被大包围盒"串"到别的物件上。
      const splatHit = pickSplat(x, y, camera)
      if (splatHit !== null) {
        selectByLabel(splatHit)
        return
      }

      const from = cameraComponent.screenToWorld(x, y, cameraComponent.nearClip)
      const to = cameraComponent.screenToWorld(x, y, cameraComponent.farClip)
      let best: { label: string; distance: number; radius: number } | null = null
      const screenPoint = new pc.Vec3()
      for (const item of objects) {
        if (item.lo === undefined || item.hi === undefined) { continue }
        const entity = stateRef.current.entities.get(item.label)
        if (entity === undefined) { continue }
        // 把射线换到物件当前坐标（物件可能已经被移动/旋转/缩放过）
        const inverse = entity.getWorldTransform().clone().invert()
        const localFrom = inverse.transformPoint(from)
        const localTo = inverse.transformPoint(to)
        const direction = new pc.Vec3().sub2(localTo, localFrom)
        const length = direction.length()
        if (length < 1e-6) { continue }
        direction.mulScalar(1 / length)
        const distance = rayBoxDistance(localFrom, direction, item.lo, item.hi)
        if (distance === null) { continue }
        // ★ 同时算这个物体在屏幕上的**投影半径**：命中多个时选投影最小的那个，
        //   也就是"画面上更小、更具体"的那个 —— 否则点躺椅会选中把它整个罩住的平台。
        const center = new pc.Vec3(
          (item.lo[0] + item.hi[0]) / 2,
          (item.lo[1] + item.hi[1]) / 2,
          (item.lo[2] + item.hi[2]) / 2,
        )
        const worldCenter = entity.getWorldTransform().transformPoint(center)
        const projected = cameraComponent.worldToScreen(worldCenter, screenPoint)
        const extent = Math.max(
          item.hi[0] - item.lo[0],
          item.hi[1] - item.lo[1],
          item.hi[2] - item.lo[2],
        ) / 2
        const radius = extent / Math.max(0.5, projected.z)
        if (best === null) {
          best = { label: item.label, distance, radius }
          continue
        }
        // 投影半径差 30% 以上就按"更具体的"选；否则按远近
        if (radius < best.radius * 0.7) { best = { label: item.label, distance, radius } }
        else if (radius > best.radius * 1.4 && distance < best.distance) { best = { label: item.label, distance, radius } }
        else if (distance < best.distance * 0.8) { best = { label: item.label, distance, radius } }
      }
      ;(window as unknown as { __vfPick?: unknown }).__vfPick = {
        x, y, count: stateRef.current.entities.size, best,
      }
      if (best === null) {
        selectByLabel(null)
        say('点空处：取消选择')
        return
      }
      selectByLabel(best.label)
    }

    canvas.addEventListener('pointerdown', down)
    canvas.addEventListener('click', up)
    return () => {
      canvas.removeEventListener('pointerdown', down)
      canvas.removeEventListener('click', up)
    }
  }, [objects, pickSplat, selectByLabel, epoch])

  /* ── 切换手柄模式 ── */
  useEffect(() => {
    const gizmos = stateRef.current.gizmos
    const entity = stateRef.current.selected
    if (gizmos === null) { return }
    for (const [key, gizmo] of Object.entries(gizmos)) {
      if (gizmo === undefined) { continue }
      const active = key === mode && entity !== null
      gizmo.enabled = active
      if (active) { gizmo.attach?.([entity as pc.Entity]) }
    }
  }, [mode, epoch])

  /* ── 变换读写 ── */
  /** 记录一步可撤销的操作（命令模式）。
   *  和 SuperSplat 的 EditHistory 一样：历史里存的是"操作对象"而不是快照，
   *  新操作会丢弃 cursor 之后的 redo 分支。 */
  const pushOp = useCallback((op: EditOp): void => {
    const history = stateRef.current.history
    while (stateRef.current.historyIndex < history.length) {
      history.pop()?.destroy?.()
    }
    op.do()
    history.push(op)
    stateRef.current.historyIndex = history.length
    say(`已记录：${op.label}（可撤销 ${history.length} 步）`)
  }, [say])

  const undoOp = useCallback((): void => {
    const history = stateRef.current.history
    const cursor = stateRef.current.historyIndex
    const op = history[cursor - 1]
    if (op === undefined) { say('没有可撤销的操作了'); return }
    op.undo()
    stateRef.current.historyIndex = cursor - 1
    say(`已撤销：${op.label}`)
  }, [say])

  const redoOp = useCallback((): void => {
    const history = stateRef.current.history
    const cursor = stateRef.current.historyIndex
    const op = history[cursor]
    if (op === undefined) { say('没有可恢复的操作了'); return }
    op.do()
    stateRef.current.historyIndex = cursor + 1
    say(`已恢复：${op.label}`)
  }, [say])

  const readTransform = useCallback((): StudioTransform => {
    const entity = stateRef.current.selected
    if (entity === null) { return { x: 0, y: 0, z: 0, ry: 0, s: 1 } }
    const p = entity.getLocalPosition()
    const s = entity.getLocalScale()
    return {
      x: +p.x.toFixed(2),
      y: +p.y.toFixed(2),
      z: +p.z.toFixed(2),
      ry: +entity.getLocalEulerAngles().y.toFixed(1),
      s: +((s.x + s.y + s.z) / 3).toFixed(3),
    }
  }, [])

  const writeTransform = useCallback((next: Partial<StudioTransform>): void => {
    const entity = stateRef.current.selected
    if (entity === null) { return }
    const current = readTransform()
    const merged = { ...current, ...next }
    // 吸附：位置对齐到 0.5m 网格
    if (stateRef.current.snap) {
      merged.x = Math.round(merged.x * 2) / 2
      merged.y = Math.round(merged.y * 2) / 2
      merged.z = Math.round(merged.z * 2) / 2
    }
    entity.setLocalPosition(merged.x, merged.y, merged.z)
    entity.setLocalEulerAngles(0, merged.ry, 0)
    const s = Math.max(0.01, merged.s)
    entity.setLocalScale(s, s, s)
    onState?.({ selected: entity.name, transform: readTransform() })
    // 记一步可撤销的命令（学 SuperSplat 的 EditHistory：存操作，不存快照）
    if (Math.abs(merged.x - current.x) > 1e-6 || Math.abs(merged.y - current.y) > 1e-6
      || Math.abs(merged.z - current.z) > 1e-6 || Math.abs(merged.ry - current.ry) > 1e-6
      || Math.abs(merged.s - current.s) > 1e-6) {
      const label = entity.name
      const after: StudioTransform = { ...merged, s: Math.max(0.01, merged.s) }
      pushOp({
        label: `移动 ${label}`,
        do: () => {
          const target = stateRef.current.entities.get(label)
          if (target === undefined) { return }
          target.setLocalPosition(after.x, after.y, after.z)
          target.setLocalEulerAngles(0, after.ry, 0)
          target.setLocalScale(after.s, after.s, after.s)
          onState?.({ selected: label, transform: readTransform() })
        },
        undo: () => {
          const target = stateRef.current.entities.get(label)
          if (target === undefined) { return }
          target.setLocalPosition(current.x, current.y, current.z)
          target.setLocalEulerAngles(0, current.ry, 0)
          target.setLocalScale(current.s, current.s, current.s)
          onState?.({ selected: label, transform: readTransform() })
        },
      })
    }
  }, [onState, pushOp, readTransform])

  /* 选中/变换变化 → 把状态抛给外面（右栏显示用） */
  useEffect(() => {
    const timer = window.setInterval(() => {
      const entity = stateRef.current.selected
      onState?.({ selected: entity?.name ?? null, transform: readTransform() })
    }, 400)
    return () => { window.clearInterval(timer) }
  }, [onState, readTransform])

  /** 删除一个物件（进撤销栈，官方 SuperSplat 的删除同样是可撤销 op）。
   *  ★ 实现上**不 destroy 实体**，只是从列表里摘掉并隐藏：
   *    这样撤销时能原样恢复（组件、资源、变换全在），不会退化成"空壳对象"。 */
  const deleteEntity = useCallback((entity: pc.Entity): void => {
    const label = entity.name
    const removed = entity
    const applyDelete = (): void => {
      const target = stateRef.current.entities.get(label)
      if (target === undefined) { return }
      stateRef.current.entities.delete(label)
      target.enabled = false
      if (stateRef.current.selected === target) {
        stateRef.current.selected = null
        onSelect?.(null)
      }
      onState?.({ selected: null, transform: { x: 0, y: 0, z: 0, ry: 0, s: 1 } })
      say(`已删除「${label}」（Ctrl+Z 可撤销）`)
    }
    const restore = (): void => {
      if (stateRef.current.entities.has(label)) { return }
      removed.enabled = true
      stateRef.current.entities.set(label, removed)
      say(`已恢复「${label}」`)
    }
    pushOp({ label: `删除 ${label}`, do: applyDelete, undo: restore })
  }, [onSelect, onState, pushOp, say])

  /* ── 快捷键（学 SuperSplat 的 shortcut-manager）──
   *  官方 SuperSplat 有完整的快捷键管理；我们之前只有 WASD/QE。
   *  这里补上最常用的一组：Delete 删物件、Ctrl+Z 撤销、Ctrl+Shift+Z 恢复、
   *  Ctrl+S 保存快照、F12 出图、Esc 取消选择。 */
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null
      const tag = target?.tagName ?? ''
      if (tag === 'INPUT' || tag === 'TEXTAREA' || target?.isContentEditable === true) { return }
      const ctrl = event.ctrlKey || event.metaKey
      if (ctrl && (event.key === 'z' || event.key === 'Z')) {
        event.preventDefault()
        if (event.shiftKey) { redoOp() } else { undoOp() }
        return
      }
      if (ctrl && (event.key === 'y' || event.key === 'Y')) {
        event.preventDefault()
        redoOp()
        return
      }
      if (ctrl && (event.key === 's' || event.key === 'S')) {
        event.preventDefault()
        pushOp({ label: '保存点', do: () => { say('已保存场景状态') }, undo: () => { say('已撤销保存点') } })
        return
      }
      if (event.key === 'Delete' || event.key === 'Backspace') {
        const entity = stateRef.current.selected
        if (entity === null) { return }
        event.preventDefault()
        deleteEntity(entity)
        return
      }
      if (event.key === 'Escape') {
        selectByLabel(null)
        return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey) }
  }, [deleteEntity, onSelect, onState, pushOp, redoOp, say, selectByLabel, undoOp])

  /* ── 太阳 / 环境 / 雾 ── */
  const applySun = useCallback((): void => {
    const sun = stateRef.current.sun
    const light = sun?.light
    if (sun === null || light === undefined) { return }
    const s = stateRef.current.sunState
    light.enabled = s.enabled
    sun.setEulerAngles(90 - s.elevation, s.azimuth, 0)
    light.intensity = s.intensity
    const [r, g, b] = colorTempToRgb(s.colorTemp)
    light.color = new pc.Color(r, g, b)
  }, [])

  const setSun = useCallback((next: Partial<StudioSun>): void => {
    stateRef.current.sunState = { ...stateRef.current.sunState, ...next }
    applySun()
  }, [applySun])

  const setFog = useCallback((enabled: boolean, density?: number): void => {
    const app = stateRef.current.app
    if (app === null) { return }
    stateRef.current.fog = { enabled, density: density ?? stateRef.current.fog.density }
    // ★ PlayCanvas 2.x：scene.fog 是**只读的 FogParams 对象**，要改它里面的字段，
    //   直接赋值 scene.fog = 'exp2' 会静默不生效（TS 也会报错）。
    const params = app.scene.fog
    params.type = enabled ? pc.FOG_EXP2 : pc.FOG_NONE
    params.density = stateRef.current.fog.density
    params.color = new pc.Color(0.86, 0.88, 0.92)
  }, [])

  /* ── 观感预设：影棚白 / 中性灰 / 黄昏。参照 marble.ai 那种干净、低对比、高调性的观感。 ── */
  const applyLook = useCallback((key: 'studio' | 'neutral' | 'dusk'): void => {
    const app = stateRef.current.app
    const camera = stateRef.current.camera
    if (app === null || camera === null) { return }
    const presets = {
      studio: { clear: [0.96, 0.95, 0.93], ambient: [0.72, 0.72, 0.72], exposure: 1.15, tonemap: pc.TONEMAP_ACES },
      neutral: { clear: [0.78, 0.79, 0.80], ambient: [0.55, 0.57, 0.60], exposure: 1.0, tonemap: pc.TONEMAP_ACES },
      dusk: { clear: [0.18, 0.20, 0.28], ambient: [0.32, 0.34, 0.44], exposure: 1.35, tonemap: pc.TONEMAP_ACES2 },
    }
    const preset = presets[key]
    const cameraComponent = camera.camera
    if (cameraComponent === undefined) { return }
    cameraComponent.clearColor = new pc.Color(preset.clear[0], preset.clear[1], preset.clear[2])
    app.scene.ambientLight = new pc.Color(preset.ambient[0], preset.ambient[1], preset.ambient[2])
    app.scene.exposure = preset.exposure
    cameraComponent.toneMapping = preset.tonemap
    const fill = stateRef.current.fillLight
    if (fill?.light !== undefined) {
      fill.light.intensity = key === 'dusk' ? 0.25 : key === 'neutral' ? 0.35 : 0.45
      fill.light.color = key === 'dusk'
        ? new pc.Color(0.55, 0.62, 0.95)
        : new pc.Color(0.9, 0.93, 1.0)
    }
    say(`观感：${key === 'studio' ? '影棚白' : key === 'neutral' ? '中性灰' : '黄昏'}`)
  }, [say])

  /* ── 相机 ── */
  const lookFrom = useCallback((yaw: number, pitch: number, zoom = 1): void => {
    const orbit = stateRef.current.orbit
    const camera = stateRef.current.camera
    if (orbit === null || camera === null) { return }
    stateRef.current.autoFramed = false
    orbit.yaw = yaw
    orbit.pitch = pitch
    orbit.distance = Math.max(1, orbit.distance * zoom)
    const y = (yaw * Math.PI) / 180
    const p = (pitch * Math.PI) / 180
    const cos = Math.cos(p)
    camera.setPosition(
      orbit.target.x + orbit.distance * cos * Math.sin(y),
      orbit.target.y + orbit.distance * Math.sin(p),
      orbit.target.z + orbit.distance * cos * Math.cos(y),
    )
    camera.lookAt(orbit.target)
    say(`机位：${Math.round(yaw)}° / ${Math.round(pitch)}°`)
  }, [say])

  /** 取一条轨迹的定义（内置 + 用户自定义） */
  const getRig = useCallback((id: string): RigDef | null => {
    return stateRef.current.customRigs.get(id) ?? RIGS.find((item) => item.id === id) ?? null
  }, [])

  /** 场景的包围球半径（用于把轨迹摆在场景附近、并算视野） */
  const sceneRadius = useCallback((): number => {
    let radius = 6
    for (const entity of stateRef.current.entities.values()) {
      const aabb = entity.gsplat?.customAabb
      if (aabb === undefined || aabb === null) { continue }
      radius = Math.max(radius, aabb.halfExtents.length())
    }
    return radius
  }, [])

  /** 采样一条轨迹的**相机世界坐标**（和播放时用的是同一套算法，所见即所播）
   *  referenceDistance：采样用的机位距离。轨迹线要画在**场景附近**才看得见，
   *  所以默认用"场景半径 × 1.6"，而不是当前相机距离（可能 50m，线就画到画面外了）。 */
  /** 按时间（毫秒）求一条轨迹上的相机姿态。
   *  有关键帧就用关键帧插值（Catmull-Rom 风格的分段平滑），
   *  没有关键帧就退回 from/to 两态的缓动插值。 */
  const rigPoseAt = useCallback((rig: RigDef, timeMs: number): { yaw: number; pitch: number; zoom: number } => {
    const keys = rig.keys ?? []
    if (keys.length === 0) {
      const t = Math.min(1, Math.max(0, timeMs / Math.max(1, rig.ms)))
      const ease = t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2
      return {
        yaw: rig.from[0] + (rig.to[0] - rig.from[0]) * ease,
        pitch: rig.from[1] + (rig.to[1] - rig.from[1]) * ease,
        zoom: rig.from[2] + (rig.to[2] - rig.from[2]) * ease,
      }
    }
    if (keys.length === 1) {
      const only = keys[0]
      if (only === undefined) { return { yaw: rig.from[0], pitch: rig.from[1], zoom: rig.from[2] } }
      return { yaw: only.yaw, pitch: only.pitch, zoom: only.zoom }
    }
    // 找时间所在的区间
    let index = 0
    for (let i = 0; i < keys.length - 1; i += 1) {
      const current = keys[i]
      const next = keys[i + 1]
      if (current !== undefined && next !== undefined && timeMs >= current.t && timeMs <= next.t) { index = i; break }
      if (current !== undefined && timeMs > current.t) { index = i }
    }
    const a = keys[index]
    const b = keys[index + 1] ?? a
    if (a === undefined || b === undefined) { return { yaw: rig.from[0], pitch: rig.from[1], zoom: rig.from[2] } }
    const span = Math.max(1, b.t - a.t)
    const raw = Math.min(1, Math.max(0, (timeMs - a.t) / span))
    // 分段平滑（smoothstep），比线性自然，且不会过冲
    const ease = raw * raw * (3 - 2 * raw)
    return {
      yaw: a.yaw + (b.yaw - a.yaw) * ease,
      pitch: a.pitch + (b.pitch - a.pitch) * ease,
      zoom: a.zoom + (b.zoom - a.zoom) * ease,
    }
  }, [])

  const sampleRigPath = useCallback((rig: RigDef, samples = 48, referenceDistance?: number): pc.Vec3[] => {
    const orbit = stateRef.current.orbit
    if (orbit === null) { return [] }
    const base = referenceDistance ?? orbit.distance
    const points: pc.Vec3[] = []
    for (let index = 0; index <= samples; index += 1) {
      const timeMs = (index / samples) * rig.ms
      const pose = rigPoseAt(rig, timeMs)
      const y = (pose.yaw * Math.PI) / 180
      const p = (pose.pitch * Math.PI) / 180
      const cos = Math.cos(p)
      const distance = Math.max(1, base * pose.zoom)
      points.push(new pc.Vec3(
        orbit.target.x + distance * cos * Math.sin(y),
        orbit.target.y + distance * Math.sin(p),
        orbit.target.z + distance * cos * Math.cos(y),
      ))
    }
    return points
  }, [rigPoseAt])

  /** 把轨迹画成线 + 放一个无人机/手机图标 */
  const drawTrajectory = useCallback((id: string | null): void => {
    const app = stateRef.current.app
    if (app === null) { return }
    // 清掉旧的
    const old = stateRef.current.trajectory
    if (old !== null) { old.destroy(); stateRef.current.trajectory = null }
    const oldIcon = stateRef.current.trajectoryIcon
    if (oldIcon !== null) { oldIcon.destroy(); stateRef.current.trajectoryIcon = null }
    stateRef.current.trajectoryId = id
    if (id === null) { return }
    const rig = getRig(id)
    if (rig === null) { return }
    // ★ 轨迹画在**场景附近**（场景半径 × 1.6），否则相机距离 50m 时线在画面外看不见
    const radius = sceneRadius()
    const points = sampleRigPath(rig, 48, radius * 1.6)
    if (points.length < 2) { return }

    // 线：Mesh + PRIMITIVE_LINES
    const positions: number[] = []
    const indices: number[] = []
    for (const point of points) { positions.push(point.x, point.y, point.z) }
    for (let index = 0; index < points.length - 1; index += 1) { indices.push(index, index + 1) }
    const mesh = new pc.Mesh(app.graphicsDevice)
    mesh.setPositions(positions)
    mesh.setIndices(indices)
    mesh.update(pc.PRIMITIVE_LINES)
    const material = new pc.StandardMaterial()
    material.useLighting = false
    material.emissive = new pc.Color(1, 0.45, 0.1)
    material.diffuse = new pc.Color(0, 0, 0)
    material.update()
    const lineEntity = new pc.Entity('trajectory')
    const meshInstance = new pc.MeshInstance(mesh, material)
    lineEntity.addComponent('render', { meshInstances: [meshInstance], castShadows: false })
    app.root.addChild(lineEntity)
    stateRef.current.trajectory = lineEntity

    // 图标：无人机（十字臂 + 机身）/ 手机（扁盒）/ 环绕（一个环）
    const icon = new pc.Entity(`trajectory-${rig.icon}`)
    const iconMaterial = new pc.StandardMaterial()
    iconMaterial.useLighting = false
    iconMaterial.emissive = rig.icon === 'drone' ? new pc.Color(0.2, 0.9, 0.4) : (rig.icon === 'phone' ? new pc.Color(0.3, 0.6, 1) : new pc.Color(0.9, 0.3, 0.9))
    iconMaterial.diffuse = new pc.Color(0, 0, 0)
    iconMaterial.update()
    const parts: [number, number, number, number, number, number][] = rig.icon === 'drone'
      ? [[0, 0, 0, 0.5, 0.18, 0.5], [0, 0.06, 0, 1.1, 0.06, 0.12], [0, 0.06, 0, 0.12, 0.06, 1.1]]
      : rig.icon === 'phone'
        ? [[0, 0, 0, 0.16, 0.32, 0.03]]
        : [[0, 0, 0, 0.9, 0.05, 0.05]]
    for (const [px, py, pz, sx, sy, sz] of parts) {
      const part = new pc.Entity('part')
      part.addComponent('render', { type: 'box', material: iconMaterial, castShadows: false })
      part.setLocalPosition(px, py, pz)
      part.setLocalScale(sx, sy, sz)
      icon.addChild(part)
    }
    const start = points[0]
    if (start !== undefined) { icon.setPosition(start.x, start.y, start.z) }
    app.root.addChild(icon)
    stateRef.current.trajectoryIcon = icon

    // 路径标记：每隔几个采样点放一个小球，1px 的线太细看不见
    const markerMaterial = new pc.StandardMaterial()
    markerMaterial.useLighting = false
    markerMaterial.emissive = new pc.Color(1, 0.75, 0.2)
    markerMaterial.diffuse = new pc.Color(0, 0, 0)
    markerMaterial.update()
    for (let index = 0; index < points.length; index += 4) {
      const point = points[index]
      if (point === undefined) { continue }
      const marker = new pc.Entity('trajectory-marker')
      marker.addComponent('render', { type: 'sphere', material: markerMaterial, castShadows: false })
      marker.setLocalScale(0.35, 0.35, 0.35)
      marker.setPosition(point.x, point.y, point.z)
      app.root.addChild(marker)
    }

    // 视野拉到能同时看见场景和轨迹
    const orbit = stateRef.current.orbit
    if (orbit !== null) {
      orbit.distance = Math.max(orbit.distance, radius * 3.2)
      const yaw = (orbit.yaw * Math.PI) / 180
      const pitch = (orbit.pitch * Math.PI) / 180
      const cos = Math.cos(pitch)
      const camera = stateRef.current.camera
      if (camera !== null) {
        camera.setPosition(
          orbit.target.x + orbit.distance * cos * Math.sin(yaw),
          orbit.target.y + orbit.distance * Math.sin(pitch),
          orbit.target.z + orbit.distance * cos * Math.cos(yaw),
        )
        camera.lookAt(orbit.target)
      }
    }
    say(`已显示轨迹：${rig.name}（${points.length} 个采样点）`)
  }, [getRig, sampleRigPath, say, sceneRadius])

  /** 运镜：从 A 到 B 平滑走一遍（无人机俯冲 / 手机推近 / 环绕）。 */
  const playRig = useCallback((kind: 'drone' | 'phone' | 'orbit'): void => {
    const preset = stateRef.current.customRigs.get(kind) ?? RIGS.find((item) => item.id === kind) ?? RIGS[0]
    if (preset === undefined) { return }
    const start = performance.now()
    const tick = (): void => {
      const elapsed = performance.now() - start
      const t = Math.min(1, elapsed / preset.ms)
      // ★ 关键帧轨道（有 keys 就按关键帧插值，没有才退回两态）
      const pose = rigPoseAt(preset, elapsed)
      const yaw = pose.yaw
      const pitch = pose.pitch
      const zoom = pose.zoom
      const orbit = stateRef.current.orbit
      const camera = stateRef.current.camera
      if (orbit === null || camera === null) { return }
      orbit.yaw = yaw
      orbit.pitch = pitch
      const y = (yaw * Math.PI) / 180
      const p = (pitch * Math.PI) / 180
      const cos = Math.cos(p)
      const distance = Math.max(1, orbit.distance * zoom)
      camera.setPosition(
        orbit.target.x + distance * cos * Math.sin(y),
        orbit.target.y + distance * Math.sin(p),
        orbit.target.z + distance * cos * Math.cos(y),
      )
      camera.lookAt(orbit.target)
      if (t < 1) { requestAnimationFrame(tick) } else { say('运镜完成') }
    }
    say(`运镜中…（${preset.keys?.length ?? 2} 个关键帧）`)
    requestAnimationFrame(tick)
  }, [rigPoseAt, say])

  /** 录制运镜：MediaRecorder 录 canvas → WebM → 存进侧车成片目录。 */
  const recordRig = useCallback(async (kind: 'drone' | 'phone' | 'orbit'): Promise<void> => {
    const canvas = canvasRef.current
    if (canvas === null) { return }
    if (typeof MediaRecorder === 'undefined') { say('这个环境不支持录制'); return }
    const rig = stateRef.current.customRigs.get(kind) ?? RIGS.find((item) => item.id === kind) ?? RIGS[0]
    if (rig === undefined) { return }
    const label = rig.name
    const preset = rig
    let frameTimer = 0
    try {
      // ★ 用 captureStream(0) + 手动 requestFrame：
      //   自动帧率模式在 Electron 里当窗口不可见/被遮挡时**不产帧**（录出来只有 110 字节）。
      const stream = canvas.captureStream(0)
      const videoTrack = stream.getVideoTracks()[0] as (MediaStreamTrack & { requestFrame?: () => void }) | undefined
      if (videoTrack?.requestFrame !== undefined) {
        frameTimer = window.setInterval(() => { try { videoTrack.requestFrame?.() } catch { /* 忽略 */ } }, 33)
      }
      const mimeType = MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'video/webm;codecs=vp9' : 'video/webm'
      const extension = 'webm'
      const recorder = new MediaRecorder(stream, { mimeType })
      const chunks: Blob[] = []
      recorder.ondataavailable = (event) => { if (event.data.size > 0) { chunks.push(event.data) } }
      const stopped = new Promise<void>((resolve) => { recorder.onstop = () => { resolve() } })
      recorder.start(250)
      say(`录制中：${label}…`)
      await new Promise((resolve) => { window.setTimeout(resolve, 350) })
      playRig(kind)
      await new Promise((resolve) => { window.setTimeout(resolve, preset.ms + 200) })
      try { recorder.requestData() } catch { /* 忽略 */ }
      await new Promise((resolve) => { window.setTimeout(resolve, 300) })
      recorder.stop()
      await Promise.race([stopped, new Promise((resolve) => { window.setTimeout(resolve, 4000) })])
      const blob = new Blob(chunks, { type: mimeType })
      const buffer = await blob.arrayBuffer()
      const bytes = new Uint8Array(buffer)
      if (bytes.length < 2048) {
        say(`录制没拿到画面（${bytes.length} B）：把窗口切到前台再点一次「录…」`)
        return
      }
      await saveSplatExport(`rig_${Date.now()}.${extension}`, bytes, mimeType.split(';')[0] ?? 'application/octet-stream')
      say(`已保存运镜视频：${label}.${extension}（${(bytes.length / 1024).toFixed(0)} KB）`)
    } catch (error) {
      say(`录制失败：${error instanceof Error ? error.message : String(error)}（录制时请保持窗口在前台）`)
    } finally {
      if (frameTimer !== 0) { window.clearInterval(frameTimer) }
    }
  }, [playRig, say])

  /* ── 对外接口 ── */
  useImperativeHandle(ref, (): StudioHandle => ({
    select: (label) => { selectByLabel(label) },
    getSelected: () => stateRef.current.selected?.name ?? null,
    getTransform: () => readTransform(),
    setTransform: (next) => { writeTransform(next) },
    nudge: (axis, meters) => {
      const entity = stateRef.current.selected
      if (entity === null) { say('先在场景里点一个物体'); return }
      const current = readTransform()
      writeTransform({ [axis]: current[axis] + meters })
    },
    rotate: (degrees) => {
      const entity = stateRef.current.selected
      if (entity === null) { say('先在场景里点一个物体'); return }
      writeTransform({ ry: readTransform().ry + degrees })
    },
    scaleBy: (factor) => {
      const entity = stateRef.current.selected
      if (entity === null) { say('先在场景里点一个物体'); return }
      writeTransform({ s: Math.max(0.01, readTransform().s * factor) })
    },
    removeSelected: () => {
      const entity = stateRef.current.selected
      if (entity === null) { say('先在场景里点一个物体'); return }
      deleteEntity(entity)
    },
    resetSelected: () => {
      const entity = stateRef.current.selected
      if (entity === null) { say('先在场景里点一个物体'); return }
      writeTransform({ x: 0, y: 0, z: 0, ry: 0, s: 1 })
      say(`「${entity.name}」已复位`)
    },
    setMode: (next) => { setMode(next) },
    getMode: () => mode,
    listObjects: () => {
      const list: { label: string; visible: boolean }[] = []
      for (const [label, entity] of stateRef.current.entities) {
        list.push({ label, visible: entity.enabled && entity.gsplat?.enabled !== false })
      }
      return list
    },
    setVisible: (label, visible) => {
      const entity = stateRef.current.entities.get(label)
      if (entity === undefined) { return }
      entity.enabled = visible
      if (entity.gsplat !== undefined) { entity.gsplat.enabled = visible }
    },
    setAllVisible: (visible) => {
      for (const entity of stateRef.current.entities.values()) {
        entity.enabled = visible
        if (entity.gsplat !== undefined) { entity.gsplat.enabled = visible }
      }
    },
    setSun,
    getSun: () => ({ ...stateRef.current.sunState }),
    setAmbient: (r, g, b) => {
      const app = stateRef.current.app
      if (app !== null) { app.scene.ambientLight = new pc.Color(r, g, b) }
    },
    setFog,
    getFog: () => ({ ...stateRef.current.fog }),
    setExposure: (value) => {
      const app = stateRef.current.app
      if (app !== null) { app.scene.exposure = value }
    },
    setLook: (key) => { applyLook(key) },
    getCamera: () => {
      const orbit = stateRef.current.orbit
      if (orbit === null) { return { yaw: 0, pitch: 0, distance: 0, target: [0, 0, 0] } }
      return {
        yaw: orbit.yaw,
        pitch: orbit.pitch,
        distance: orbit.distance,
        target: [orbit.target.x, orbit.target.y, orbit.target.z],
      }
    },
    setCamera: (next) => {
      const orbit = stateRef.current.orbit
      const camera = stateRef.current.camera
      if (orbit === null || camera === null) { return }
      stateRef.current.autoFramed = false
      stateRef.current.cameraLocked = true
      // ★ 直接给位置（全景场景站进圆心）：由 position+target 反推 orbit，
      //   这样 WASD 漫游、环绕等后续操作仍然正常。
      if (next.position !== undefined) {
        const px = next.position[0]
        const py = next.position[1]
        const pz = next.position[2]
        const tx = next.target?.[0] ?? 0
        const ty = next.target?.[1] ?? py
        const tz = next.target?.[2] ?? pz - 1
        const dx = tx - px
        const dy = ty - py
        const dz = tz - pz
        const distance = Math.max(0.1, Math.hypot(dx, dy, dz))
        orbit.yaw = (Math.atan2(dx, dz) * 180) / Math.PI
        orbit.pitch = (Math.asin(dy / distance) * 180) / Math.PI
        orbit.distance = distance
        orbit.target = new pc.Vec3(tx, ty, tz)
        camera.setPosition(px, py, pz)
        camera.lookAt(tx, ty, tz)
        const component = camera.camera
        if (component !== undefined && next.fov !== undefined) { component.fov = next.fov }
        return
      }
      if (next.yaw !== undefined) { orbit.yaw = next.yaw }
      if (next.pitch !== undefined) { orbit.pitch = Math.max(-89, Math.min(89, next.pitch)) }
      if (next.distance !== undefined) { orbit.distance = Math.max(0.5, next.distance) }
      if (next.target !== undefined) { orbit.target = new pc.Vec3(next.target[0], next.target[1], next.target[2]) }
      const y = (orbit.yaw * Math.PI) / 180
      const p = (orbit.pitch * Math.PI) / 180
      const cos = Math.cos(p)
      camera.setPosition(
        orbit.target.x + orbit.distance * cos * Math.sin(y),
        orbit.target.y + orbit.distance * Math.sin(p),
        orbit.target.z + orbit.distance * cos * Math.cos(y),
      )
      camera.lookAt(orbit.target)
    },
    lookFrom,
    frameAll: () => { frameFromEntities(); frameScene() },
    setFreeCamera: (enabled) => { stateRef.current.freeCamera = enabled },
    isFreeCamera: () => stateRef.current.freeCamera,
    setResolution: (width, height) => {
      const app = stateRef.current.app
      if (app === null) { return }
      stateRef.current.resolution = { width, height }
      // 渲染分辨率改成固定尺寸：视口大小不变，出图按这个尺寸
      app.setCanvasResolution(pc.RESOLUTION_FIXED, width, height)
    },
    getResolution: () => ({ ...stateRef.current.resolution }),
    playRig,
    recordRig,
    getCanvas: () => canvasRef.current,
    removeObject: (label) => {
      const entity = stateRef.current.entities.get(label)
      if (entity === undefined) { return false }
      stateRef.current.entities.delete(label)
      entity.destroy()
      if (stateRef.current.selected === entity) {
        stateRef.current.selected = null
        onSelect?.(null)
      }
      say(`已删除「${label}」`)
      return true
    },
    moveObject: (label, x, y, z) => {
      const entity = stateRef.current.entities.get(label)
      if (entity === undefined) { return false }
      const before = entity.getLocalPosition()
      const beforeT: StudioTransform = {
        x: +before.x.toFixed(3), y: +before.y.toFixed(3), z: +before.z.toFixed(3),
        ry: +entity.getLocalEulerAngles().y.toFixed(1),
        s: +((entity.getLocalScale().x + entity.getLocalScale().y + entity.getLocalScale().z) / 3).toFixed(3),
      }
      const afterT: StudioTransform = { ...beforeT, x, y, z }
      pushOp({
        label: `移动 ${label}`,
        do: () => {
          const target = stateRef.current.entities.get(label)
          if (target === undefined) { return }
          target.setLocalPosition(afterT.x, afterT.y, afterT.z)
        },
        undo: () => {
          const target = stateRef.current.entities.get(label)
          if (target === undefined) { return }
          target.setLocalPosition(beforeT.x, beforeT.y, beforeT.z)
        },
      })
      entity.setLocalPosition(x, y, z)
      say(`「${label}」移到 ${x.toFixed(1)}, ${y.toFixed(1)}, ${z.toFixed(1)}`)
      return true
    },
    showTrajectory: (id) => { drawTrajectory(id) },
    planTrajectory: (id, from, to) => {
      const base = getRig(id)
      if (base === null || base === undefined) { return }
      const updated: RigDef = {
        id: base.id,
        name: base.name,
        ms: base.ms,
        icon: base.icon,
        from: [from[0], from[1], from[2]],
        to: [to[0], to[1], to[2]],
      }
      stateRef.current.customRigs.set(id, updated)
      drawTrajectory(id)
    },
    listTrajectories: () => {
      const all = [...RIGS, ...stateRef.current.customRigs.values()]
      return all.map((rig) => ({ id: rig.id, name: rig.name, points: sampleRigPath(rig).length }))
    },
    getKeyframes: (id) => {
      const rig = stateRef.current.customRigs.get(id) ?? RIGS.find((item) => item.id === id)
      if (rig === undefined) { return [] }
      return [...(rig.keys ?? [])]
    },
    setKeyframes: (id, keys) => {
      const base = stateRef.current.customRigs.get(id) ?? RIGS.find((item) => item.id === id)
      if (base === undefined) { return }
      const sorted = [...keys].sort((a, b) => a.t - b.t)
      const updated: RigDef = {
        id: base.id,
        name: base.name,
        ms: base.ms,
        icon: base.icon,
        from: base.from,
        to: base.to,
        keys: sorted,
      }
      stateRef.current.customRigs.set(id, updated)
      drawTrajectory(id)
      say(`「${base.name}」关键帧已更新（${sorted.length} 个）`)
    },
    addKeyframe: (id, atMs) => {
      const base = stateRef.current.customRigs.get(id) ?? RIGS.find((item) => item.id === id)
      if (base === undefined) { return }
      const keys = [...(base.keys ?? [])]
      // 以当前时间点附近的姿态作为新关键帧
      const pose = rigPoseAt(base, atMs)
      keys.push({ t: Math.round(atMs), yaw: pose.yaw, pitch: pose.pitch, zoom: pose.zoom })
      const sorted = keys.sort((a, b) => a.t - b.t)
      const updated: RigDef = {
        id: base.id, name: base.name, ms: base.ms, icon: base.icon, from: base.from, to: base.to, keys: sorted,
      }
      stateRef.current.customRigs.set(id, updated)
      drawTrajectory(id)
      say(`已加关键帧（${sorted.length} 个）`)
    },
    removeKeyframe: (id, index) => {
      const base = stateRef.current.customRigs.get(id) ?? RIGS.find((item) => item.id === id)
      if (base === undefined) { return }
      const keys = [...(base.keys ?? [])]
      keys.splice(index, 1)
      const updated: RigDef = {
        id: base.id, name: base.name, ms: base.ms, icon: base.icon, from: base.from, to: base.to, keys,
      }
      stateRef.current.customRigs.set(id, updated)
      drawTrajectory(id)
      say(`已删关键帧（剩 ${keys.length} 个）`)
    },
    saveSnapshot: () => {
      // 「保存」= 记一个标记点；之后的每一步变换都能逐步撤回到这里
      const entity = stateRef.current.selected
      const label = entity?.name ?? '场景'
      pushOp({
        label: `保存点（${label}）`,
        do: () => { say(`已保存场景状态：${label}`) },
        undo: () => { say('已撤销保存点') },
      })
      if (entity !== null && entity !== undefined) {
        stateRef.current.savedTransform = { label, transform: readTransform() }
      }
    },
    undo: () => { undoOp() },
    redo: () => { redoOp() },
    setSnap: (enabled) => {
      stateRef.current.snap = enabled
      say(`吸附：${enabled ? '开（0.5m 网格）' : '关'}`)
    },
    isSnap: () => stateRef.current.snap,
    focusViewport: () => { canvasRef.current?.focus() },
    enterPanorama: () => {
      // 判据：**采样真实 splat 点**，看它们是不是贴在同一半径的圆柱面上。
      //   （早前只用包围盒判"高瘦"，要求高度 > 4×半径 —— 真实环幕是 16.6m 高 / 8m 半径，
      //     比例只有 2×，被误判成普通场景，相机就停在外面对焦。）
      let minX = Infinity, minY = Infinity, minZ = Infinity
      let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity
      let any = false
      let best: pc.Entity | null = null
      let bestHeight = -Infinity
      for (const entity of stateRef.current.entities.values()) {
        const aabb = entity.gsplat?.customAabb
        if (aabb === undefined || aabb === null) { continue }
        any = true
        minX = Math.min(minX, aabb.center.x - aabb.halfExtents.x)
        maxX = Math.max(maxX, aabb.center.x + aabb.halfExtents.x)
        minY = Math.min(minY, aabb.center.y - aabb.halfExtents.y)
        maxY = Math.max(maxY, aabb.center.y + aabb.halfExtents.y)
        minZ = Math.min(minZ, aabb.center.z - aabb.halfExtents.z)
        maxZ = Math.max(maxZ, aabb.center.z + aabb.halfExtents.z)
        const height = aabb.halfExtents.y * 2
        if (height > bestHeight) { bestHeight = height; best = entity }
      }
      if (!any || best === null) { return false }

      // ★ 圆柱检验：在实体的**局部坐标**里算到 Y 轴的距离（柱面轴就是实体原点轴）
      const resource = best.gsplat?.resource
      const centers = resource?.centers
      if (centers === undefined || centers === null || centers.length < 300) { return false }
      const count = centers.length / 3
      const step = Math.max(1, Math.floor(count / 400))
      let sum = 0
      let sumSq = 0
      let samples = 0
      for (let index = 0; index < count; index += step) {
        const x = centers[index * 3] ?? 0
        const z = centers[index * 3 + 2] ?? 0
        const r = Math.hypot(x, z)
        sum += r
        sumSq += r * r
        samples += 1
      }
      if (samples < 20) { return false }
      const mean = sum / samples
      const variance = Math.max(0, sumSq / samples - mean * mean)
      const deviation = Math.sqrt(variance)
      // 半径够大（是个"房间"而不是一个小物件）且半径集中（真的贴在圆柱面上）
      if (mean < 3) { return false }
      if (deviation / mean > 0.18) { return false }

      const halfX = (maxX - minX) / 2
      const halfZ = (maxZ - minZ) / 2
      const radius = Math.max(halfX, halfZ)
      const centerX = (minX + maxX) / 2
      const centerZ = (minZ + maxZ) / 2
      const eye = Math.max(minY + 1.6, 1.6)
      const orbit = stateRef.current.orbit
      const camera = stateRef.current.camera
      if (orbit === null || camera === null) { return false }
      stateRef.current.cameraLocked = true
      stateRef.current.autoFramed = false
      orbit.target = new pc.Vec3(centerX, eye, centerZ)
      orbit.distance = radius
      orbit.yaw = 0
      orbit.pitch = 0
      camera.setPosition(centerX, eye, centerZ)
      camera.lookAt(centerX, eye, centerZ - radius)
      say('相机已置于场景中心（深度为单目先验相对深度，尺度为假设值）')
      return true
    },
    captureStill: async () => {
      const canvas = canvasRef.current
      const app = stateRef.current.app
      if (canvas === null || app === null) { return null }
      // 先按当前分辨率出一张图：改变画布尺寸 → 等一帧渲染 → toDataURL → 还原
      const width = canvas.width
      const height = canvas.height
      const target = stateRef.current.resolution
      // ★ 出图时把手柄藏掉：不然红绿蓝箭头会跟着拍进成片里
      const gizmoLayer = stateRef.current.gizmoLayer
      const gizmoEnabled = gizmoLayer?.enabled ?? false
      if (gizmoLayer !== null && gizmoLayer !== undefined) { gizmoLayer.enabled = false }
      try {
        // ★ 2 倍超采样再缩小：直接 1:1 截图边缘会有锯齿，成片看着不"真"
        const scale = 2
        app.resizeCanvas(target.width * scale, target.height * scale)
        // ★ 改尺寸之后，splat 的渲染缓冲要重建，头一两帧是**空的**（实测第一张存出来是纯背景色）。
        //   所以多等几帧、多拍几次，取最大的一张（内容越多 base64 越长）。
        let base64 = ''
        for (let attempt = 0; attempt < 5; attempt += 1) {
          await new Promise((resolve) => { requestAnimationFrame(() => { requestAnimationFrame(resolve) }) })
          await new Promise((resolve) => { window.setTimeout(resolve, 120) })
            // 缩回目标尺寸（双线性平滑）→ 更干净的成片
          const shot = canvas.toDataURL('image/png')
          const down = document.createElement('canvas')
          down.width = target.width
          down.height = target.height
          const context = down.getContext('2d')
          if (context !== null) {
            context.imageSmoothingEnabled = true
            context.imageSmoothingQuality = 'high'
            context.drawImage(canvas, 0, 0, down.width, down.height)
          }
          const encoded = (context !== null ? down.toDataURL('image/png') : shot)
            .replace(/^data:image\/png;base64,/u, '')
          if (encoded.length > base64.length) { base64 = encoded }
          // 空背景图 base64 约 58KB；有内容的图在 MB 级
          if (base64.length > 400_000) { break }
        }
        if (base64.length < 128) { return null }
        const filename = `scene_${Date.now()}.png`
        const response = await fetch(`${sidecarBase()}/api/studio/splat/exports`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename, data: base64, media_type: 'image/png' }),
          signal: AbortSignal.timeout(EXPORT_TIMEOUT_MS),
        })
        if (!response.ok) { throw new Error(`保存失败 HTTP ${response.status}`) }
        return filename
      } finally {
        app.resizeCanvas(width, height)
        if (gizmoLayer !== null && gizmoLayer !== undefined) { gizmoLayer.enabled = gizmoEnabled }
      }
    },
    isReady: () => ready,
  }), [applyLook, drawTrajectory, frameScene, getRig, lookFrom, mode, playRig, pushOp, readTransform, recordRig, ready, redoOp, rigPoseAt, sampleRigPath, say, selectByLabel, setFog, setSun, undoOp, writeTransform])

  useEffect(() => { applySun() }, [applySun, epoch])

  return (
    <div style={{ position: 'absolute', inset: 0, background: '#eceae6' }}>
      <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block', touchAction: 'none' }} />
      {/* 左上角只放状态（键位说明在右下工具条，避免压住预览卡） */}
      <div style={{ position: 'absolute', left: 10, top: 10, fontSize: 11, color: '#5b6270', background: 'rgba(255,255,255,0.82)', borderRadius: 5, padding: '3px 8px', pointerEvents: 'none', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {hud}
      </div>
    </div>
  )
})

/** 计算"把半径 r 的球装进当前视锥"所需的相机距离。
 *  要同时考虑**竖直**和**水平**两个方向：视口是竖长条时，水平 FOV 才是瓶颈
 *  （之前只按竖直 FOV 算，结果相机拉太远，画面里啥也没有）。 */
/** 射线-AABB 求交：返回最近命中的距离（米），没命中返回 null。 */
function rayBoxDistance(
  origin: pc.Vec3,
  direction: pc.Vec3,
  lo: readonly [number, number, number],
  hi: readonly [number, number, number],
): number | null {
  let tmin = -Infinity
  let tmax = Infinity
  const o = [origin.x, origin.y, origin.z]
  const d = [direction.x, direction.y, direction.z]
  for (let axis = 0; axis < 3; axis += 1) {
    const oa = o[axis] ?? 0
    const da = d[axis] ?? 0
    const loA = lo[axis] ?? 0
    const hiA = hi[axis] ?? 0
    if (Math.abs(da) < 1e-8) {
      if (oa < loA || oa > hiA) return null
      continue
    }
    const t1 = (loA - oa) / da
    const t2 = (hiA - oa) / da
    const near = Math.min(t1, t2)
    const far = Math.max(t1, t2)
    tmin = Math.max(tmin, near)
    tmax = Math.min(tmax, far)
    if (tmin > tmax) return null
  }
  return tmin >= 0 ? tmin : (tmax >= 0 ? 0 : null)
}
