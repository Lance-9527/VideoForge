/**
 * 场景棚拍主页面。
 *
 * ★ 这一版把 SuperSplat 的 iframe 整个拆掉了，只留**一套界面**：
 *   视口 = 应用内 PlayCanvas（`SplatStudio` 引擎），界面 = `StudioShell`（三栏工作台）。
 *   之前"内建编辑器 / SuperSplat"两个模式来回切，用户根本不知道自己在哪个模式里，
 *   而且 iframe 那套拿不到鼠标坐标，点选/手柄全靠猜 —— 所以直接砍掉。
 *
 * 本文件只负责：侧车数据（本地场景 / 成片清单）、导入、AI 指令条。
 * 渲染、拾取、变换、灯光、运镜都在引擎里。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { MutableRefObject, ReactElement } from 'react'
import { ProductPanel } from './StudioProduct.tsx'
import { StudioShell } from './StudioShell.tsx'
import type { SplatModelMeta } from './StudioShell.tsx'
import type { StudioHandle, StudioObject } from './SplatStudio.tsx'
import { buildLdiPly, splitLdiLayers } from './scene-from-image.ts'
import { attachFrames, planShotCameras, readShootTask, type ShootTask } from './shoot-task.ts'
import { buildPanoramaScene } from './pano-scene.ts'
import { SCENE_COMMANDS, type CommandRunner } from './scene-commands.ts'
import { sidecarBase, saveSplatExport } from './api.ts'
import { depthRuntimeStatus, retryDepthRuntime } from './depth-model.ts'
import { addCameraMark, readCameraMarks, removeCameraMark, type CameraMark } from './camera-marks.ts'

/** splat 资产清单里的一条 */
interface SplatAssetItem {
  readonly kind: 'scene' | 'image' | 'layer'
  readonly name: string
  readonly label: string
  readonly points?: number
  readonly bytes?: number
}

/** 落盘的资产清单（给人看/给工具读；界面上的清单靠文件名约定推导） */
interface SplatAssetManifest {
  readonly base: string
  readonly createdAt: number
  readonly items: readonly SplatAssetItem[]
}

/** 小请求的超时（毫秒）—— sidecar 没起来时别把界面吊死。 */
const REQUEST_TIMEOUT_MS = 30_000

/** 上传文件的上限 64MB —— 留足时间（sidecar 的上限就是 64MiB）。 */
const UPLOAD_TIMEOUT_MS = 300_000

/**
 * sidecar 基址来自主进程（端口跟品牌走：掌柜 AI 8767），不在这儿写死。
 * @returns sidecar 基址。
 */
function sidecar(): string {
  return sidecarBase()
}

/**
 * 深度来源的诚实说明。
 *
 * ★ 模型没加载上时只写「启发式」等于骗用户：得说清**为什么**没加载上
 *   （模型运行时服务没起来 / 端口不对 / wasm 缺失……）。
 *
 * @param provider - 这次用的深度来源。
 * @returns 说明文字。
 */
function depthNote(provider: 'depth-anything-v2' | 'heuristic'): string {
  if (provider === 'depth-anything-v2') { return 'Depth Anything V2 相对深度' }
  const state = depthRuntimeStatus()
  const reason = state.failure ?? '原因未知'
  return state.stage === 'inference'
    ? `深度推理失败（${reason}），当前用启发式深度`
    : `深度模型未加载（${reason}），当前用启发式深度`
}

interface SplatExport {
  readonly name: string
  readonly bytes?: number
}

/** 底部快捷指令 */
/**
 * 底部快捷条 —— **照 openVideoForge 工作台的操作习惯重排**：
 *
 *   第一排 = **叙事景别**（全景/中景/特写/俯拍/仰拍/平视）
 *            —— openvideoforge 的原话是「底部点『中景』→ 视角自动收到叙事景别」，
 *            所以景别必须自己占一排，不能和别的操作混在一起。
 *   第二排 = 运镜（环绕/无人机/手机）
 *   第三排 = 物件操作 + 复位 + 渲染成片
 *
 * 景别这一排右端还有「＋保存机位」—— openvideoforge 的「＋保存机位」，
 * 把构好的角度存下来，以后随时跳回。掌柜 AI 原来没有这个动作。
 */
const INITIAL_STATUS = '就绪 · 从左边选一个场景，或从图片生成'

/** 机位预设（AI 指令和右栏共用） */
const CAMERA_PRESETS: Record<string, { yaw: number; pitch: number; zoom: number }> = {
  全景: { yaw: 40, pitch: 24, zoom: 1 },
  中景: { yaw: 40, pitch: 22, zoom: 0.8 },
  特写: { yaw: 55, pitch: 16, zoom: 0.55 },
  俯拍: { yaw: 30, pitch: 60, zoom: 1.05 },
  仰拍: { yaw: 40, pitch: -8, zoom: 0.9 },
  平视: { yaw: 40, pitch: 6, zoom: 0.9 },
}

export function VfShotScene(props: {
  readonly onSend?: ((text: string) => void) | undefined
  readonly sessionId?: string | null
  /**
   * 场景指令执行器的注册位（由常驻的 `Panel` 持有）。
   *
   * ★ 轮询器在那一层（与页签无关），这里挂载时把执行器注册进去。
   */
  readonly sceneRunnerRef?: MutableRefObject<CommandRunner | null> | undefined
}): ReactElement {
  const [, setStatus] = useState(INITIAL_STATUS)
  /** ★ 电商那边送过来的拍摄任务（方案 → 镜头表 → 机位）。 */
  const [shootTask, setShootTask] = useState<ShootTask | null>(null)
  /** ★ 机位书签（openvideoforge 的「＋保存机位」）。 */
  const [cameraMarks, setCameraMarks] = useState<CameraMark[]>([])
  const [studioObjects, setStudioObjects] = useState<StudioObject[]>([])
  const [exports, setExports] = useState<readonly SplatExport[]>([])
  const [sources, setSources] = useState<readonly { name: string; bytes: number }[]>([])
  /** 资产清单（生成过的高斯泼溅资产：场景 / 原图 / 可复用图层） */
  const [assetItems, setAssetItems] = useState<readonly SplatAssetItem[]>([])
  const [preview, setPreview] = useState<string | null>(null)
  /** 当前载入模型的坐标与尺度证据（点数/包围盒/竖轴）→ 显示在左栏。 */
  const [modelMeta, setModelMeta] = useState<SplatModelMeta | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [generating, setGenerating] = useState(false)
  /** ★ 深度模型的问题（有值就显示「重试深度模型」入口）。 */
  const [depthIssue, setDepthIssue] = useState<string | null>(null)

  const engineRef = useRef<StudioHandle | null>(null)
  const aiInputRef = useRef<HTMLInputElement | null>(null)
  /** 下一次「从图片生成场景」用哪种模式 */
  const pendingMode = useRef<'pano' | 'ldi'>('pano')
  const imageRef = useRef<HTMLInputElement | null>(null)
  const modelRef = useRef<HTMLInputElement | null>(null)

  /* ── 清单：本地场景 + 已保存成片 ── */
  const refreshLists = useCallback(async (): Promise<void> => {
    try {
      const [out, src] = await Promise.all([
        fetch(`${sidecar()}/api/studio/splat/exports`, { cache: 'no-store', signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) }),
        fetch(`${sidecar()}/api/studio/splat/sources`, { cache: 'no-store', signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) }),
      ])
      if (out.ok) {
        const payload = await out.json() as { exports?: SplatExport[] } | SplatExport[]
        setExports((Array.isArray(payload) ? payload : payload.exports ?? []).slice(-12))
      }
      if (src.ok) {
        const payload = await src.json() as { sources?: { name: string; bytes: number }[] }
        const all = payload.sources ?? []
        setSources(all
          // `xxx__角色.ply` 是场景的内部分组，直接列出来会让人误以为是另一个场景。
          .filter((item) => /\.(ply|splat|ksplat|spz|sog)$/i.test(item.name) && !item.name.includes('__')))
        // ★ 资产清单：从**文件名约定**推导（`基名_种类_时间戳.ply`）。
        //   为什么不用 manifest 文件：侧车的 `sources` 只列 .ply/.splat 等 splat 后缀，
        //   .json 清单列不出来；而 `exports` 是内存态、侧车重启就没了。
        //   文件名约定是**落盘可推导**的，重启不丢。
        const groups = new Map<string, { base: string; stamp: string; scene?: string; layers: { name: string; index: number }[] }>()
        for (const item of all) {
          const matched = /^(.+?)_(360|ldi|层(\d+))_(\d+)\.ply$/i.exec(item.name)
          if (matched === null) { continue }
          const base = matched[1] ?? ''
          const kind = matched[2] ?? ''
          const layerIndex = matched[3] === undefined ? 0 : Number(matched[3])
          const stamp = matched[4] ?? ''
          const key = `${base}_${stamp}`
          const group = groups.get(key) ?? { base, stamp, layers: [] }
          // ★ 正则第 2 组是 "层01" 这种（带序号），不是纯 "层" —— 早前按 === '层' 判断，
          //   结果 8 个图层全被当成"场景"，资产库里就只剩 1 条。
          if (kind.startsWith('层')) {
            group.layers.push({ name: item.name, index: layerIndex })
          } else {
            group.scene = item.name
          }
          groups.set(key, group)
        }
        const loaded: SplatAssetItem[] = []
        for (const group of groups.values()) {
          // 时间戳转成"月-日 时:分"，同名资产也能分辨
          const when = new Date(Number(group.stamp))
          const whenText = Number.isFinite(when.getTime())
            ? `${when.getMonth() + 1}-${String(when.getDate()).padStart(2, '0')} ${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}`
            : group.stamp
          if (group.scene !== undefined) {
            loaded.push({
              kind: 'scene',
              name: group.scene,
              label: `${group.base} · ${group.scene.includes('_360_') ? '多视图深度场景' : '分层深度场景'} · ${whenText}`,
            })
          }
          for (const layer of group.layers.sort((a, b) => a.index - b.index)) {
            loaded.push({
              kind: 'layer',
              name: layer.name,
              label: `${group.base} · 深度层 ${layer.index} · ${whenText}`,
            })
          }
          // 原图：命名约定 `基名_原图_时间戳.png`（与场景同批生成）
          loaded.push({
            kind: 'image',
            name: `${group.base}_原图_${group.stamp}.png`,
            label: `${group.base} · 原始图片 · ${whenText}`,
          })
        }
        setAssetItems(loaded)
      }
    } catch {
      /* 忽略：侧车没起来时不影响手动物件 */
    }
  }, [])

  useEffect(() => {
    void refreshLists()
    const timer = window.setInterval(() => { void refreshLists() }, 8000)
    return () => { window.clearInterval(timer) }
  }, [refreshLists])

  /* ── 读一个 splat 文件的坐标与尺度证据（点数/包围盒/竖轴）──
   *  ★ 这不是装饰：摄影测量重建的点云尺度是任意的，不把数字摆出来，
   *    没人能判断这个模型能不能用。 */
  const fetchMeta = useCallback(async (name: string): Promise<void> => {
    try {
      const response = await fetch(`${sidecar()}/api/studio/splat/metadata?name=${encodeURIComponent(name)}`, { cache: 'no-store', signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
      if (!response.ok) { return }
      const payload = await response.json() as { metadata?: Omit<SplatModelMeta, 'name'> }
      if (payload.metadata === undefined) { return }
      setModelMeta({ name, ...payload.metadata })
    } catch {
      /* 读不到证据不影响主流程 */
    }
  }, [])

  /* ── 导入本地场景：带分组清单就按组拆成多个物件 ──
   *  ★ mode='add' 时**追加**而不是替换 —— 这是"把用户提供的模型接到重建场景里"
   *    的入口：引擎本来就支持场景里多个物件（每件一个 gsplat 实体，可各自
   *    变换/显隐），以前只是导入时把列表整个换掉了。 */
  const importSource = useCallback(async (name: string, mode: 'replace' | 'add' = 'replace'): Promise<void> => {
    setStatus(`导入场景：${name}`)
    try {
      const base = name.replace(/\.(ply|splat|ksplat|sog)$/iu, '')
      let groups: { file?: string; label?: string; lo?: number[]; hi?: number[] }[] = []
      try {
        const response = await fetch(`${sidecar()}/api/studio/splat/files/${encodeURIComponent(base)}.groups.json`, { cache: 'no-store', signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
        if (response.ok) {
          const payload = await response.json() as { groups?: { file?: string; label?: string; lo?: number[]; hi?: number[] }[] }
          groups = payload.groups ?? []
        }
      } catch {
        groups = []
      }
      if (groups.length === 0) {
        // 单文件场景：当成一个物件
        const object: StudioObject = { label: base, url: `${sidecar()}/api/studio/splat/files/${encodeURIComponent(name)}` }
        setStudioObjects((current) => (mode === 'add'
          ? [...current.filter((item) => item.label !== base), object]
          : [object]))
        void fetchMeta(name)
        // ★ 若是柱面全景（高瘦圆柱），等引擎加载完就把相机送进圆心。
        //   大场景要加载 1–2 分钟，固定定时器会在加载完成前就放弃 ⇒ 改成**轮询**。
        let tries = 0
        const enter = window.setInterval(() => {
          tries += 1
          const entered = engineRef.current?.enterPanorama() === true
          if (entered) {
            window.clearInterval(enter)
            setStatus(`已载入场景：${name}（相机已置于场景中心）`)
          } else if (tries > 180) {
            window.clearInterval(enter)
            setStatus(`已载入场景：${name}`)
          }
        }, 1000)
        return
      }
      // 分组场景：Z-up(米) → Y-up(splat 空间)：x, z, -y
      const grouped: StudioObject[] = groups.flatMap((group) => {
        if (group.file === undefined) { return [] }
        const lo = group.lo as [number, number, number] | undefined
        const hi = group.hi as [number, number, number] | undefined
        return [{
          label: group.label ?? group.file,
          url: `${sidecar()}/api/studio/splat/files/${encodeURIComponent(group.file)}`,
          ...(lo !== undefined && hi !== undefined
            ? { lo: [lo[0], lo[2], -hi[1]] as [number, number, number], hi: [hi[0], hi[2], -lo[1]] as [number, number, number] }
            : {}),
        }]
      })
      setStudioObjects((current) => (mode === 'add' ? [...current, ...grouped] : grouped))
      setStatus(`已载入 ${groups.length} 个物件：点场景里的物体即可选中`)
    } catch (error) {
      setStatus(`导入失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }, [fetchMeta])

  /* ── 摆正 + 锚定尺度 + 落位 ──
   *  ★ 尺度必须由人给（"桌子宽 1.2m"）：COLMAP 的 SfM 只解出相对尺度，
   *    "37 个单位"是厘米还是米没有任何信息，机器猜错就会让商品比例全错。
   *  ★ 原文件不动：sidecar 另存一份 `.aligned.ply`，重建结果与归一化版本分开。
   */
  const normalizeSource = useCallback(async (name: string, anchorMeters: number, anchorAxis: string): Promise<void> => {
    setStatus(`对齐中（写盘要一会儿）：${name}`)
    try {
      const response = await fetch(`${sidecar()}/api/studio/splat/normalize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, anchor_meters: anchorMeters, anchor_axis: anchorAxis }),
        // 写 40MB 的 ply 要时间，超时给足。
        signal: AbortSignal.timeout(180_000),
      })
      if (!response.ok) {
        const text = await response.text()
        throw new Error(`HTTP ${response.status} ${text.slice(0, 200)}`)
      }
      const payload = await response.json() as { normalized?: { name?: string; scale?: number } }
      const aligned = payload.normalized?.name
      if (aligned === undefined) { throw new Error('sidecar 没返回归一化产物名') }
      setStatus(`已对齐：${anchorMeters} 米 ≈ ${payload.normalized?.scale?.toFixed(5) ?? '?'} 单位/米，正在载入 ${aligned}`)
      await importSource(aligned)
    } catch (error) {
      setStatus(`对齐失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }, [importSource])

  /** 把一次生成的产物写成资产清单（manifest），之后可在「资产库（可复用）」里一键调出 */
  const saveAssetManifest = useCallback(async (
    base: string,
    items: SplatAssetItem[],
  ): Promise<void> => {
    const manifest: SplatAssetManifest = { base, createdAt: Date.now(), items }
    const text = JSON.stringify(manifest, null, 2)
    const bytes = new TextEncoder().encode(text)
    try {
      await saveSplatExport(`${base}_assets.json`, bytes, 'application/json')
    } catch {
      /* 清单写失败不影响主流程 */
    }
  }, [])

  /** 从资产清单里取一条来用：场景/图层 → 导入场景；原图 → 打开图像预览 */
  const importAsset = useCallback(async (item: SplatAssetItem): Promise<void> => {
    if (item.kind === 'image') {
      setPreview(`${sidecar()}/api/studio/splat/files/${encodeURIComponent(item.name)}?t=${Date.now()}`)
      setStatus(`已打开资产原图：${item.label}`)
      return
    }
    await importSource(item.name)
  }, [importSource])

  /* ── 从图片生成场景 ──
   *  模式：
   *   'pano'（默认）—— **多视图深度重建**：全景图按球面采样成透视视图，
   *     每张用单目先验深度（Depth Anything V2）反投影成点，再合并成场景。
   *     深度是**相对量**，绝对尺度未知；near/far 是人为假设值。
   *   'ldi'          —— 分层深度图：能按深度拆成可复用物件，但侧面看是片
   *
   *  两种模式都会：
   *   ① 主场景 PLY 落盘到资产目录
   *   ② **原图**一起保存（图片本身也是资产）
   *   ③ LDI 模式额外**按深度拆成 N 层**，每层一个独立 PLY → 可单独摆放/复用
   */
  const sceneFromImage = useCallback(async (file: File, mode: 'pano' | 'ldi' = 'pano'): Promise<void> => {
    setGenerating(true)
    setStatus(`从图片生成 ${mode === 'pano' ? '多视图深度重建' : '分层深度'} 场景：${file.name}`)
    try {
      if (mode === 'pano') {
        // ★ 全景图 → **多视图深度重建场景**：
        //   全景图按球面采样成多张透视视图 → 每张算单目深度（Depth Anything V2）
        //   → 像素反投影成三维点 → 合并。
        //   深度是**单目先验相对深度**，绝对尺度未知；距离区间是人为假设值，
        //   产出不是测量到的真实几何。
        setStatus('正在把全景图转成多视图深度重建场景（单目先验深度，尺度为假设值）…')
        const built = await buildPanoramaScene(file)
        // ★ 深度来源要**说清楚**：模型没加载上时只写「启发式」等于骗用户。
        const depthState = depthRuntimeStatus()
        setDepthIssue(depthState.failure ?? null)
        setStatus(`已生成 ${built.points.toLocaleString()} 个点（${built.views} 个视图 · ${depthNote(built.provider)}），正在落盘…`)
        const base = file.name.replace(/\.[^.]+$/u, '').slice(0, 40)
        const stamp = Date.now()
        const filename = `${base}_360_${stamp}.ply`
        await saveSplatExport(filename, built.bytes)
        // 原图也存一份
        const originalName = `${base}_原图_${stamp}.png`
        try {
          const originalBytes = new Uint8Array(await file.arrayBuffer())
          await saveSplatExport(originalName, originalBytes, 'image/png')
        } catch { /* 原图保存失败不影响主流程 */ }
        // ★ 写资产清单：场景 + 原图 → 可复用资产
        await saveAssetManifest(base, [
          { kind: 'scene', name: filename, label: `${base} · 多视图深度场景`, points: built.points },
          { kind: 'image', name: originalName, label: `${base} · 原始图片` },
        ])
        await importSource(filename)
        await refreshLists()
        // ★ 把相机放到场景中心 —— 否则新场景进来是斜的，看不到与全景图一致的方向。
        //   引擎加载 splat 是异步的，取景会在稍后跑，所以这里连设两次。
        const placeCamera = (): void => {
          engineRef.current?.setCamera({
            // ★ 重建场景以拍摄点为球心，相机放在中心才对得上全景图的方向
            position: [built.eye[0], built.eye[1], built.eye[2]],
            target: [built.eye[0], built.eye[1], built.eye[2] - 1],
            fov: 70,
          })
        }
        placeCamera()
        window.setTimeout(placeCamera, 1500)
        window.setTimeout(placeCamera, 3500)
        setStatus(`已生成多视图深度重建场景：${filename}（单目先验相对深度，尺度为假设值，非真实几何）；原图已存`)
        return
      }

      const built = await buildLdiPly(file, 1024)
      setStatus(`已生成 ${built.points.toLocaleString()} 个 splat（${built.width}×${built.height}），正在落盘…`)
      const base = file.name.replace(/\.[^.]+$/u, '').slice(0, 40)
      const stamp = Date.now()
      const filename = `${base}_ldi_${stamp}.ply`
      await saveSplatExport(filename, built.bytes)

      // ② 原图也存一份（图片本身作为资产）
      const originalName = `${base}_原图_${stamp}.png`
      try {
        const originalBytes = new Uint8Array(await file.arrayBuffer())
        await saveSplatExport(originalName, originalBytes, 'image/png')
      } catch {
        // 原图保存失败不影响主流程
      }

      // ③ 按深度拆层（可复用物件）
      const layers = splitLdiLayers(built, 8)
      const layerNames: string[] = []
      let savedLayers = 0
      for (let index = 0; index < layers.length; index += 1) {
        const layer = layers[index]
        if (layer === undefined) { continue }
        const layerName = `${base}_层${String(index + 1).padStart(2, '0')}_${stamp}.ply`
        layerNames.push(layerName)
        try {
          await saveSplatExport(layerName, layer.bytes)
          savedLayers += 1
        } catch {
          // 某一层失败就跳过
        }
      }

      // ★ 写资产清单：场景 + 原图 + 每个可复用图层
      const layerItems: SplatAssetItem[] = []
      for (let index = 0; index < layerNames.length; index += 1) {
        const layerName = layerNames[index]
        if (layerName === undefined) { continue }
        layerItems.push({
          kind: 'layer',
          name: layerName,
          label: `${base} · 深度层 ${index + 1}（远→近）`,
        })
      }
      await saveAssetManifest(base, [
        { kind: 'scene', name: filename, label: `${base} · 分层深度场景`, points: built.points },
        { kind: 'image', name: originalName, label: `${base} · 原始图片` },
        ...layerItems,
      ])
      await importSource(filename)
      await refreshLists()
      setStatus(`已生成分层深度场景：${filename}；另存原图 1 张、可复用深度层 ${savedLayers} 个`)
    } catch (error) {
      setStatus(`生成失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setGenerating(false)
    }
  }, [importSource, refreshLists, saveAssetManifest])

  /* ── 上传本地模型到资产库 ── */
  const uploadModel = useCallback(async (file: File): Promise<void> => {
    setUploading(true)
    setStatus(`上传：${file.name} …`)
    try {
      const form = new FormData()
      form.append('file', file, file.name)
      const response = await fetch(`${sidecar()}/api/studio/assets`, {
        method: 'POST',
        body: form,
        signal: AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
      })
      if (!response.ok) { throw new Error(`HTTP ${response.status}`) }
      const payload = await response.json() as { data?: { name?: string } }
      setStatus(`已加入资产库：${payload.data?.name ?? file.name}`)
    } catch (error) {
      setStatus(`上传失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setUploading(false)
    }
  }, [])

  /* ── 电商拍摄任务：进场景就把它接住（电商段切过来时会重新挂载）── */
  useEffect(() => {
    setShootTask(readShootTask())
    setCameraMarks(readCameraMarks())
  }, [])

  /**
   * 按电商镜头表自动拍摄：逐镜摆机位 → 出图 → 回填帧清单。
   *
   * ★ 这是「电商」与「高斯泼溅」真正咬合的地方：
   *   同一个 splat 场景里换机位，商品**绝对一致**（AI 生图做不到），
   *   而且不用重拍 —— 场景建一次，镜头表改一次就重出一批图。
   */
  const runShootTask = useCallback(async (): Promise<void> => {
    const handle = engineRef.current
    if (handle === null) { setStatus('编辑器还没就绪，稍等一下再试'); return }
    const task = readShootTask()
    if (task === null) { setStatus('没有拍摄任务：先去「电商」段生成方案，再点「按此方案搭场景出片」'); return }
    const plan = planShotCameras(task.shots)
    setBusy(true)
    const frames: string[] = []
    try {
      for (const camera of plan) {
        setStatus(`拍摄中 ${camera.shotNumber}/${plan.length} · ${camera.label}`)
        handle.lookFrom(camera.yaw, camera.pitch, camera.zoom)
        if (camera.orbit) { handle.playRig('orbit') }
        // 等两帧，让相机与运镜落位
        await new Promise<void>((resolve) => { setTimeout(resolve, 320) })
        const name = await handle.captureStill()
        if (name !== null) { frames.push(name) }
      }
      attachFrames(frames)
      setStatus(`拍完 ${frames.length}/${plan.length} 张：${frames.join('、') || '（没拿到画面）'}`)
    } catch (error) {
      setStatus(`拍摄失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }, [])

  /* ── 机位书签：存下这个角度 / 跳回存过的角度 ── */
  const saveCameraMark = useCallback((): void => {
    const handle = engineRef.current
    if (handle === null) { setStatus('编辑器还没就绪'); return }
    const camera = handle.getCamera()
    const next = addCameraMark('机位', camera.yaw, camera.pitch, camera.distance, camera.target)
    setCameraMarks(next)
    setStatus(`已保存机位：${next[next.length - 1]?.label ?? ''}（共 ${next.length} 个）`)
  }, [])

  const jumpCameraMark = useCallback((mark: CameraMark): void => {
    const handle = engineRef.current
    if (handle === null) { setStatus('编辑器还没就绪'); return }
    // ★ 用 setCamera 还原 orbit 四元组（lookFrom 的 zoom 倍数跟这里的 distance 不是一回事）
    handle.setCamera({
      yaw: mark.yaw,
      pitch: mark.pitch,
      distance: mark.distance,
      target: [mark.target[0], mark.target[1], mark.target[2]],
    })
    setStatus(`机位：${mark.label}`)
  }, [])

  const dropCameraMark = useCallback((mark: CameraMark): void => {
    setCameraMarks(removeCameraMark(mark.id))
    setStatus(`已删除机位：${mark.label}`)
  }, [])

  /* ── 立即渲染：把视口当前画面存成成片 ── */
  const renderShot = useCallback(async (): Promise<void> => {
    const handle = engineRef.current
    if (handle === null) { setStatus('编辑器还没就绪'); return }
    setBusy(true)
    setStatus('渲染中…')
    try {
      const filename = await handle.captureStill()
      if (filename === null) { setStatus('渲染没拿到画面（把窗口切到前台再试）'); return }
      setPreview(`${sidecar()}/api/studio/splat/file?name=${encodeURIComponent(filename)}&t=${Date.now()}`)
      setStatus(`已保存成片：${filename}`)
      await refreshLists()
    } catch (error) {
      setStatus(`渲染失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }, [refreshLists])

  /* ── 具名指令总线：页面内输入框与外部 agent 走同一套实现 ── */
  const runCommand = useCallback(async (tool: string, args: Record<string, unknown>): Promise<unknown> => {
    const handle = engineRef.current
    const number = (key: string, fallback: number): number => {
      const value = args[key]
      return typeof value === 'number' && Number.isFinite(value) ? value : fallback
    }
    const text = (key: string): string => typeof args[key] === 'string' ? (args[key] as string) : ''
    switch (tool) {
      case 'get_scene': {
        return {
          camera: handle?.getCamera() ?? null,
          objects: handle?.listObjects() ?? [],
          selected: handle?.getSelected() ?? null,
          marks: readCameraMarks(),
        }
      }
      case 'set_camera': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const position = args['position']
        if (Array.isArray(position) && position.length === 3) {
          const target = args['target']
          const t = Array.isArray(target) && target.length === 3 ? target as number[] : undefined
          handle.setCamera({
            position: [Number(position[0]), Number(position[1]), Number(position[2])],
            // ★ exactOptionalPropertyTypes：可选字段不能显式传 undefined
            ...(t === undefined ? {} : { target: [Number(t[0]), Number(t[1]), Number(t[2])] as [number, number, number] }),
            ...(typeof args['fov'] === 'number' ? { fov: args['fov'] } : {}),
          })
          return { ok: true, mode: 'position' }
        }
        const current = handle.getCamera()
        handle.setCamera({
          yaw: number('yaw', current.yaw),
          pitch: number('pitch', current.pitch),
          distance: number('distance', current.distance),
        })
        return { ok: true, camera: handle.getCamera() }
      }
      case 'set_view': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const preset = CAMERA_PRESETS[text('preset')]
        if (preset === undefined) { throw new Error(`未知景别：${text('preset')}`) }
        handle.lookFrom(preset.yaw, preset.pitch, preset.zoom)
        return { ok: true, preset: text('preset') }
      }
      case 'focus_camera': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const scope = text('scope') === 'selection' ? 'selection' : 'scene'
        if (scope === 'selection') {
          const label = text('label') || handle.getSelected()
          if (label === null) { throw new Error('没有选中物件') }
          // 引擎没有"对焦单个物件"的原语；用取景全部代替（如实说明）
          handle.frameAll()
          return { ok: true, focused: label, note: '引擎暂不支持对焦单个物件，已改为取景全部' }
        }
        handle.frameAll()
        return { ok: true, focused: 'scene' }
      }
      case 'camera_mark_save': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const camera = handle.getCamera()
        const next = addCameraMark(text('label') || '机位', camera.yaw, camera.pitch, camera.distance, camera.target)
        setCameraMarks(next)
        return { ok: true, marks: next }
      }
      case 'camera_mark_list':
        return readCameraMarks()
      case 'camera_mark_jump': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const mark = readCameraMarks().find((item) => item.id === text('id'))
        if (mark === undefined) { throw new Error(`没有这个机位：${text('id')}`) }
        handle.setCamera({
          yaw: mark.yaw, pitch: mark.pitch, distance: mark.distance,
          target: [mark.target[0], mark.target[1], mark.target[2]],
        })
        return { ok: true, mark }
      }
      case 'select_property': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const label = text('label')
        if (label === '') { throw new Error('缺少 label') }
        handle.select(label)
        return { ok: true, selected: handle.getSelected() }
      }
      case 'hide_selection': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const selected = handle.getSelected()
        if (selected === null) { throw new Error('没有选中物件') }
        handle.setVisible(selected, args['visible'] !== false)
        return { ok: true, visible: args['visible'] !== false }
      }
      case 'delete_selection': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        handle.removeSelected()
        return { ok: true }
      }
      case 'transform': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const current = handle.getTransform()
        handle.setTransform({
          x: number('x', current.x), y: number('y', current.y), z: number('z', current.z),
          ry: number('ry', current.ry), s: number('s', current.s),
        })
        return { ok: true, transform: handle.getTransform() }
      }
      case 'separate_selection':
        throw new Error('引擎暂不支持「分离物件」')
      case 'undo': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        handle.undo()
        return { ok: true }
      }
      case 'redo': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        handle.redo()
        return { ok: true }
      }
      case 'clear_scene':
        throw new Error('引擎暂不支持清空场景')
      case 'import_scene': {
        const name = text('name')
        if (name === '') { throw new Error('缺少 name') }
        await importSource(name)
        return { ok: true, imported: name }
      }
      case 'import_url': {
        const url = text('url')
        if (url === '') { throw new Error('缺少 url') }
        throw new Error('这个版本还没有开放 URL 导入（会绕过本地资产清单）')
      }
      case 'capture_viewport': {
        await renderShot()
        return { ok: true }
      }
      case 'play_rig': {
        if (handle === null) { throw new Error('编辑器还没就绪') }
        const kind = text('kind')
        if (kind !== 'drone' && kind !== 'phone' && kind !== 'orbit') { throw new Error(`未知运镜：${kind}`) }
        handle.playRig(kind)
        return { ok: true, kind }
      }
      case 'shoot_shotlist': {
        await runShootTask()
        return { ok: true }
      }
      default:
        throw new Error(`未知指令：${tool}`)
    }
  }, [importSource, renderShot, runShootTask])

  /* ── 把具名指令的执行器注册给常驻层 ──
   *  轮询器住在 `Panel`（与当前页签无关）：切到电商页/对话页时 agent 的命令
   *  照样有人接；这里没挂载时，轮询器会回一条「场景未就绪」的错误结果。
   */
  useEffect(() => {
    const ref = props.sceneRunnerRef
    if (ref === undefined) { return }
    ref.current = runCommand
    return () => {
      if (ref.current === runCommand) { ref.current = null }
    }
  }, [props.sceneRunnerRef, runCommand])

  /* ── AI 指令：能本地做的直接做，做不了的转给会话 ── */
  const runInstruction = useCallback(async (text: string): Promise<void> => {
    const line = text.trim()
    if (line === '') { return }
    setDraft('')
    const handle = engineRef.current
    if (handle === null) { setStatus('编辑器还没就绪，稍等一下再试'); return }
    const objects = handle.listObjects()

    /** 在整句话里找物件名。
     *  ★ 中文两种语序都要认：「删除躺椅」和「把躺床移到3」——
     *   所以不靠"动词+名字"的正则，直接拿场景里的物件名去句子里找。 */
    const findObject = (sentence: string): string | null => {
      for (const item of objects) {
        for (const part of item.label.split('/')) {
          if (part !== '' && sentence.includes(part)) { return item.label }
        }
      }
      return null
    }

    // ① 渲染
    if (/渲染|出图|成片|截图|render/iu.test(line)) { await renderShot(); return }

    // ② 运镜
    if (/环绕|orbit/iu.test(line)) { handle.playRig('orbit'); setStatus('运镜：环绕一圈'); return }
    if (/无人机|drone/iu.test(line)) { handle.playRig('drone'); setStatus('运镜：无人机俯冲'); return }
    if (/手机|推近|phone/iu.test(line)) { handle.playRig('phone'); setStatus('运镜：手机推近'); return }

    // ③ 机位预设
    for (const [name, preset] of Object.entries(CAMERA_PRESETS)) {
      if (line.includes(name)) {
        handle.lookFrom(preset.yaw, preset.pitch, preset.zoom)
        setStatus(`机位：${name}`)
        return
      }
    }
    // ④ 相机微调
    const camera = handle.getCamera()
    if (/拉近|靠近|zoom in/iu.test(line)) { handle.lookFrom(camera.yaw, camera.pitch, 0.82); setStatus('拉近'); return }
    if (/拉远|后退|zoom out/iu.test(line)) { handle.lookFrom(camera.yaw, camera.pitch, 1.22); setStatus('拉远'); return }
    if (/抬高|升高|up/iu.test(line)) { handle.lookFrom(camera.yaw, camera.pitch + 8, 1); setStatus('机位抬高 8°'); return }
    if (/降低|压低|down/iu.test(line)) { handle.lookFrom(camera.yaw, camera.pitch - 8, 1); setStatus('机位降低 8°'); return }
    if (/左转|往左/iu.test(line)) { handle.lookFrom(camera.yaw - 15, camera.pitch, 1); setStatus('机位左转 15°'); return }
    if (/右转|往右/iu.test(line)) { handle.lookFrom(camera.yaw + 15, camera.pitch, 1); setStatus('机位右转 15°'); return }

    // ⑤ 物件操作：句子里出现了哪个物件名，就操作哪个
    const factorMatch = /(\d+(?:\.\d+)?)\s*(?:倍|x)/iu.exec(line)
    const degreeMatch = /(-?\d+)\s*度/u.exec(line)
    const named = findObject(line)
    // 没点名就操作当前选中的（"放大1.5倍"这种）
    const target = named ?? handle.getSelected()

    if (/复位|还原|重置|reset/iu.test(line) && named === null) {
      handle.resetSelected()
      setStatus('已复位选中物件')
      return
    }
    if (target !== null) {
      // 移动：把 X 挪到 3 米 / 往左移 2 米
      const moveMatch = /(?:移到|挪到|移动到|移动到|放到)\s*(-?\d+(?:\.\d+)?)/u.exec(line)
      if (moveMatch !== null) {
        const x = Number(moveMatch[1])
        handle.moveObject(target, x, 0, 0)
        setStatus(`「${target}」已移到 X=${x}`)
        return
      }
      const shiftMatch = /(?:往左|向左|左移)\s*(\d+(?:\.\d+)?)/u.exec(line)
      if (shiftMatch !== null) {
        const current = handle.getTransform()
        handle.moveObject(target, current.x - Number(shiftMatch[1]), current.y, current.z)
        setStatus(`「${target}」向左移动 ${shiftMatch[1]}m`)
        return
      }
      if (/删除|删掉|去掉|remove|delete/iu.test(line)) {
        handle.removeObject(target)
        setStatus(`已删除「${target}」`)
        return
      }
      if (/隐藏|藏起来|hide/iu.test(line)) { handle.setVisible(target, false); setStatus(`已隐藏「${target}」`); return }
      if (/显示|show/iu.test(line)) { handle.setVisible(target, true); setStatus(`已显示「${target}」`); return }
      handle.select(target)
      if (/选中|选择|select/iu.test(line)) { setStatus(`已选中「${target}」`); return }
      if (/复位|还原|重置|reset/iu.test(line)) { handle.resetSelected(); setStatus(`「${target}」已复位`); return }
      if (/旋转|转|turn|rotate/iu.test(line)) {
        handle.rotate(degreeMatch !== null ? Number(degreeMatch[1]) : 90)
        setStatus(`「${target}」已旋转`)
        return
      }
      if (/放大|变大|bigger|scale up/iu.test(line)) {
        handle.scaleBy(factorMatch !== null ? Number(factorMatch[1]) : 1.15)
        setStatus(`「${target}」已放大`)
        return
      }
      if (/缩小|变小|smaller|scale down/iu.test(line)) {
        handle.scaleBy(1 / (factorMatch !== null ? Number(factorMatch[1]) : 1.15))
        setStatus(`「${target}」已缩小`)
        return
      }
      setStatus(`已选中「${target}」：右栏「对象」里可以改数值`)
      return
    }

    // ⑥ 列出所有物件
    if (/列出|有哪些物件|物件列表|list/iu.test(line)) {
      const names = objects.map((item) => item.label).join('、')
      setStatus(`场景里有 ${objects.length} 个物件：${names}`)
      return
    }

    // ⑦ 解析不了：交给当前会话（没有会话就明说，别假装做了）
    if (props.onSend === undefined || props.sessionId === null || props.sessionId === undefined) {
      setStatus('这句我解析不了。可用：全景 / 特写 / 俯拍 / 环绕 / 无人机 / 手机 / 选中躺床 / 放大躺床1.5倍 / 旋转椅子90度 / 隐藏椅子 / 删除躺床 / 把躺床移到3 / 列出物件 / 复位 / 渲染成片')
      return
    }
    props.onSend(`${line}\n\n${agentBrief()}`)
    setStatus('已转给当前 AI 会话')
  }, [props, renderShot])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, overflow: 'hidden', background: '#121417' }}>
      <div style={{ flex: '1 1 auto', minHeight: 0, position: 'relative' }}>
        <StudioShell
          engineRef={engineRef}
          objects={studioObjects}
          sources={sources}
          exports={exports}
          previewUrl={preview}
          sceneName="未命名"
          busy={busy}
          generating={generating}
          uploading={uploading}
          onStatus={(message) => { setStatus(message) }}
          onRender={() => { void renderShot() }}
          onImportSource={(name) => { void importSource(name) }}
          assetItems={assetItems}
          onImportAsset={(item) => { void importAsset(item) }}
          onGenerateFromImage={(mode) => { pendingMode.current = mode; imageRef.current?.click() }}
          onUploadModel={() => { modelRef.current?.click() }}
          onOpenExport={(name) => { setPreview(`${sidecar()}/api/studio/splat/file?name=${encodeURIComponent(name)}&t=${Date.now()}`) }}
          onFocusAi={() => { aiInputRef.current?.focus() }}
          cameraMarks={cameraMarks}
          onJumpCameraMark={(mark) => { jumpCameraMark(mark) }}
          onDropCameraMark={(mark) => { dropCameraMark(mark) }}
          shootShots={shootTask === null ? [] : planShotCameras(shootTask.shots).map((camera, index) => ({
            number: camera.shotNumber,
            title: camera.label,
            beat: shootTask.shots[index]?.beat ?? '',
            yaw: camera.yaw,
            pitch: camera.pitch,
            zoom: camera.zoom,
            orbit: camera.orbit,
            frame: shootTask.frames?.[index] ?? null,
          }))}
          onGoShot={(camera) => {
            const handle = engineRef.current
            if (handle === null) { setStatus('编辑器还没就绪'); return }
            handle.lookFrom(camera.yaw, camera.pitch, camera.zoom)
            if (camera.orbit) { handle.playRig('orbit') }
            setStatus(`跳到镜头 #${camera.number}`)
          }}
          productSlot={(
            <ProductPanel
              onStatus={(text) => { setStatus(text) }}
              onRendered={(url) => { setPreview(url); void refreshLists() }}
            />
          )}
          /* ★ 电商主线：顶栏「一键出片」＝ 旧版底部第一排的「按镜头表自动拍摄」。 */
          onAutoShoot={() => { void runShootTask() }}
          /* ★ 机位书签：旧版在底部 chips，现在搬进右栏「镜头」页。 */
          onSaveCameraMark={() => { saveCameraMark() }}
          /* ★ 电商拍摄任务抬头（商品名 · 平台）→ 镜头条左侧。 */
          shootTitle={shootTask === null ? undefined : `${shootTask.product || '未命名商品'} · ${shootTask.platform}`}
          /* ★ 空状态指引提到顶栏（旧版在底部条）。 */
          initialStatus={INITIAL_STATUS}
          /* ★ 坐标与尺度：重建模型要能摆正、锚定尺度，并给出可核验的数字。 */
          modelMeta={modelMeta}
          onNormalizeSource={(name, meters, axis) => { void normalizeSource(name, meters, axis) }}
          /* ★ 「＋加入」= 追加到当前场景而不是替换 —— 这才是"把模型接到重建场景里"。 */
          onAddSource={(name) => { void importSource(name, 'add') }}
          /* ★ AI 指令框：从底部条搬到右栏底部 —— 人和 agent 走同一个入口。 */
          aiInput={(
            <div style={{ display: 'flex', gap: 6 }}>
              <input
                ref={aiInputRef}
                style={{ flex: 1, height: 30, borderRadius: 4, border: '1px solid #d8cfbc', padding: '0 10px', fontSize: 12, color: '#1a1612', background: '#faf7f0', boxShadow: '0 1px 2px rgba(26,22,18,0.05)' }}
                value={draft}
                placeholder="全景 / 特写 / 环绕 / 选中某物 / 放大 / 复位"
                onChange={(event) => { setDraft(event.target.value) }}
                onKeyDown={(event) => { if (event.key === 'Enter') { void runInstruction(draft) } }}
              />
              <button
                type="button"
                disabled={draft.trim() === ''}
                onClick={() => { void runInstruction(draft) }}
                style={{ height: 30, padding: '0 12px', borderRadius: 4, border: 'none', background: draft.trim() === '' ? '#ebe4d2' : '#1a1612', color: draft.trim() === '' ? '#847967' : '#f3efe4', fontSize: 12, fontWeight: 600, cursor: draft.trim() === '' ? 'default' : 'pointer' }}
              >
                发送
              </button>
            </div>
          )}
        />
        <input
          ref={imageRef}
          type="file"
          accept="image/*"
          style={{ display: 'none' }}
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file !== undefined) { void sceneFromImage(file, pendingMode.current) }
          }}
        />
        <input
          ref={modelRef}
          type="file"
          accept=".glb,.gltf,.obj,.ply,.fbx"
          style={{ display: 'none' }}
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file !== undefined) { void uploadModel(file) }
          }}
        />
      </div>

      {/* 底部：只在深度模型出问题时才出现的提示条。
          ★ 其余控件都已并入新界面：景别/运镜/物件 → 右栏；机位书签 → 右栏「镜头」；
            电商拍摄任务 → 顶栏「一键出片」+ 镜头条；AI 指令框 → 右栏底部。 */}
      {depthIssue !== null && (
        <div style={{ flex: '0 0 auto', padding: '5px 12px', borderTop: '1px solid #2a2f36', background: '#1a1d22', display: 'flex', gap: 10, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: '#e0a33e' }}>深度模型未加载：{depthIssue}</span>
          <div style={{ flex: 1 }} />
          <button
            type="button"
            onClick={() => {
              retryDepthRuntime()
              setDepthIssue(null)
              setStatus('已重置深度模型：下一次生成会重新加载（也可以等它自动重试）')
            }}
            style={{ fontSize: 11, padding: '2px 8px', borderRadius: 4, border: '1px solid #39404a', background: '#20242a', color: '#e9ebee', cursor: 'pointer' }}
          >
            重试深度模型
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * 给 agent 的操作说明。
 *
 * ★ 端口现取：写死的 8767 在端口不是 8767 时会把 agent 指到没人监听的地址。
 * @returns 说明文字。
 */
function agentBrief(): string {
  return [
    '',
    '（当前面板：棚拍 · 场景棚拍，视口是应用内 PlayCanvas 高斯泼溅编辑器）',
    '★ 场景操作走**具名指令**（人和 AI 同一套实现）：',
    `  POST ${sidecar()}/api/studio/splat/commands  body {"tool":..,"args":{..}}`,
    '  客户端会取走并执行，结果回传 POST /api/studio/splat/result，取回 GET /api/studio/splat/results。',
    '  可用指令：' + SCENE_COMMANDS.map((c) => `${c.name}(${c.args})`).join(' / '),
    '★ 电商拍摄任务：面板底部会出现任务条，点「按镜头表自动拍摄」就按节拍逐镜出图。',
  ].join('\n')
}
