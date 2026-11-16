/**
 * VideoForge · 棚拍里的「商品图」面板（2026-11-01 合并）
 *
 * ## 为什么是「面板」而不是「一个页面」
 * 上一版把商品棚拍做成了和场景棚拍并列的第二个页面（还有「← 回到商品棚拍」），
 * 那不是已定的模块：棚拍 = **一个场景页**，商品只是场景页里的一栏素材/一条出图能力。
 * 所以这里把它收成一个面板组件，挂在场景棚拍的右侧，商品图渲染结果进**同一个成片预览**。
 *
 * ## 出图走哪条路
 * 后端 `/api/studio/renders`（Blender Cycles 真渲染）。商品 GLB 的包围盒**在浏览器里读**
 * （GLB 的 JSON chunk → POSITION accessor 的 min/max），据此算相机距离，用户不用猜"填多远"。
 *
 * @module
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties, ChangeEvent, ReactElement } from 'react'
import { sidecarBase } from './api.ts'

/** 读请求的超时（毫秒）。 */
const REQUEST_TIMEOUT_MS = 30_000

/** 上传商品模型的上限 64MB —— 留足时间。 */
const UPLOAD_TIMEOUT_MS = 300_000

const C = {
  cream: '#faf7f0',
  cream2: '#f3efe4',
  ink: '#1a1612',
  ink3: '#5c5348',
  ink4: '#847967',
  brass: '#b08d57',
  brass2: '#8a6d40',
  line: '#d8cfbc',
  danger: '#b5553f',
  ok: '#5c7a4e',
} as const

type PresetKey = 'white_seamless' | 'warm_wood' | 'living_room' | 'black_mirror' | 'color_studio'
type FrameKey = '1:1' | '4:5' | '3:4' | '16:9'
type QualityKey = 'fast' | 'standard' | 'high'

interface ProductAsset {
  readonly id: string
  readonly name: string
  readonly bytes: number
  readonly file_url: string
  /** 登记时间：同名资产取最新那个用 */
  readonly created_at?: number
}

interface Bounds {
  readonly size: readonly [number, number, number]
  readonly tris: number
  /** 包围盒中心（GLB 自身坐标，米） */
  readonly center: readonly [number, number, number]
  /** 包围盒最低点（Z，GLB 自身坐标，米） */
  readonly floor: number
  /** 内容中位数位置（抗离群物体）：摆商品用 */
  readonly stage: readonly [number, number, number]
  /** 能放东西的水平面（又宽又扁的那个图元）：摆商品的首选落点 */
  readonly ground: { x: number; y: number; z: number; area: number } | null
}

const PRESETS: ReadonlyArray<{ key: PresetKey; name: string }> = [
  { key: 'white_seamless', name: '白底无缝' },
  { key: 'warm_wood', name: '暖木桌面' },
  { key: 'living_room', name: '家居角落' },
  { key: 'black_mirror', name: '黑镜展台' },
  { key: 'color_studio', name: '彩棚' },
]

const FRAMES: ReadonlyArray<FrameKey> = ['1:1', '4:5', '3:4', '16:9']

const QUALITIES: ReadonlyArray<{ key: QualityKey; label: string }> = [
  { key: 'fast', label: '预览' },
  { key: 'standard', label: '标准' },
  { key: 'high', label: '高清' },
]

const RESOLUTION: Record<FrameKey, Record<QualityKey, readonly [number, number]>> = {
  '1:1': { fast: [640, 640], standard: [800, 800], high: [1400, 1400] },
  '4:5': { fast: [640, 800], standard: [800, 1000], high: [1120, 1400] },
  '3:4': { fast: [600, 800], standard: [750, 1000], high: [1080, 1440] },
  '16:9': { fast: [960, 540], standard: [1280, 720], high: [1920, 1080] },
}

const STUDIO_SUBJECT_METERS = 0.12
/** 场景模型命名前缀：带这个前缀的资产会被当成"场景"而不是"商品"。 */
const SCENE_PREFIX = '场景_'
/** 场景模型归一化后的最长边（米）：一个泳池庭院按 20m 算比较合理。 */
const SCENE_TARGET_METERS = 20

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(sidecarBase() + path, { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
  if (!response.ok) throw new Error(`读取失败 ${path}（HTTP ${response.status}）`)
  const body = await response.json() as { code?: string | number; message?: string; data?: unknown }
  if (body.code !== undefined && String(body.code) !== '000000' && String(body.code) !== '0') {
    throw new Error(String(body.message ?? '请求失败'))
  }
  return (body.data ?? body) as T
}

async function postJson<T>(path: string, body: unknown, timeoutMs = 120_000): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const response = await fetch(sidecarBase() + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
    const payload = await response.json() as { code?: string | number; message?: string; data?: unknown; detail?: { message?: string } }
    if (!response.ok) throw new Error(String(payload.detail?.message ?? payload.message ?? `HTTP ${response.status}`))
    if (payload.code !== undefined && String(payload.code) !== '000000' && String(payload.code) !== '0') {
      throw new Error(String(payload.message ?? '请求失败'))
    }
    return (payload.data ?? payload) as T
  } finally {
    clearTimeout(timer)
  }
}

/** 读 GLB 的 POSITION min/max ⇒ 商品包围盒（米）。 */
async function glbBounds(url: string): Promise<Bounds | null> {
  try {
    const response = await fetch(sidecarBase() + url, { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
    if (!response.ok) return null
    const buffer = new Uint8Array(await response.arrayBuffer())
    const view = new DataView(buffer.buffer)
    if (buffer.byteLength < 20 || view.getUint32(0, true) !== 0x46546c67) return null
    let offset = 12
    let json: Record<string, unknown> | null = null
    while (offset + 8 <= buffer.byteLength) {
      const length = view.getUint32(offset, true)
      const type = view.getUint32(offset + 4, true)
      if (type === 0x4e4f534a) {
        json = JSON.parse(new TextDecoder().decode(buffer.subarray(offset + 8, offset + 8 + length))) as Record<string, unknown>
        break
      }
      offset += 8 + length
    }
    if (json === null) return null
    const meshes = (json.meshes ?? []) as Array<{ primitives?: Array<{ attributes?: Record<string, number>; indices?: number }> }>
    const accessors = (json.accessors ?? []) as Array<{ min?: number[]; max?: number[]; count?: number }>
    // 逐图元收集包围盒：**用分位数而不是极值**——场景里常有离群物体（远处背景板），
    // 用极值会把主体压得很小，商品也会被摆到空地上。
    const lows: number[][] = []
    const highs: number[][] = []
    const centers: number[][] = []
    let tris = 0
    for (const mesh of meshes) {
      for (const prim of mesh.primitives ?? []) {
        const index = prim.attributes?.['POSITION']
        const accessor = index === undefined ? undefined : accessors[index]
        if (accessor?.min === undefined || accessor.max === undefined) continue
        const lo = [0, 0, 0]
        const hi = [0, 0, 0]
        let valid = true
        for (let axis = 0; axis < 3; axis += 1) {
          const a = accessor.min[axis]
          const b = accessor.max[axis]
          if (typeof a !== 'number' || typeof b !== 'number') { valid = false; break }
          lo[axis] = a
          hi[axis] = b
        }
        if (!valid) continue
        lows.push(lo)
        highs.push(hi)
        centers.push([((lo[0] ?? 0) + (hi[0] ?? 0)) / 2, ((lo[1] ?? 0) + (hi[1] ?? 0)) / 2, ((lo[2] ?? 0) + (hi[2] ?? 0)) / 2])
        if (prim.indices !== undefined) tris += (accessors[prim.indices]?.count ?? 0) / 3
      }
    }
    if (lows.length === 0) return null
    const quantile = (rows: number[][], axis: number, q: number): number => {
      const values = rows.map((row) => row[axis] ?? 0).sort((a, b) => a - b)
      if (values.length === 0) return 0
      const position = (values.length - 1) * q
      const lowIndex = Math.floor(position)
      const highIndex = Math.min(values.length - 1, lowIndex + 1)
      const low = values[lowIndex] ?? 0
      const high = values[highIndex] ?? low
      return low + (high - low) * (position - lowIndex)
    }
    const min: number[] = [quantile(lows, 0, 0.05), quantile(lows, 1, 0.05), quantile(lows, 2, 0.05)]
    const max: number[] = [quantile(highs, 0, 0.95), quantile(highs, 1, 0.95), quantile(highs, 2, 0.95)]
    const stage: [number, number, number] = [
      quantile(centers, 0, 0.5), quantile(centers, 1, 0.5), quantile(centers, 2, 0.5),
    ]

    // ★ 找"能放东西的地面"：**又宽又扁**的那个图元（水平大、厚度小）就是地板/台面。
    //   用内容中位数会把商品摆到墙里或空地上（实测），这个更靠谱。
    let ground: { x: number; y: number; z: number; area: number } | null = null
    for (let index = 0; index < lows.length; index += 1) {
      const lo = lows[index] ?? [0, 0, 0]
      const hi = highs[index] ?? [0, 0, 0]
      const sizeX = (hi[0] ?? 0) - (lo[0] ?? 0)
      const sizeY = (hi[1] ?? 0) - (lo[1] ?? 0)
      const sizeZ = (hi[2] ?? 0) - (lo[2] ?? 0)
      const flat = sizeZ < Math.max(sizeX, sizeY) * 0.12
      if (!flat) continue
      const area = sizeX * sizeY
      if (area < 0.5) continue
      if (ground === null || area > ground.area) {
        ground = {
          x: ((lo[0] ?? 0) + (hi[0] ?? 0)) / 2,
          y: ((lo[1] ?? 0) + (hi[1] ?? 0)) / 2,
          z: hi[2] ?? 0,
          area,
        }
      }
    }
    return {
      size: [(max[0] ?? 0) - (min[0] ?? 0), (max[1] ?? 0) - (min[1] ?? 0), (max[2] ?? 0) - (min[2] ?? 0)],
      tris: Math.round(tris),
      center: [((max[0] ?? 0) + (min[0] ?? 0)) / 2, ((max[1] ?? 0) + (min[1] ?? 0)) / 2, ((max[2] ?? 0) + (min[2] ?? 0)) / 2],
      floor: min[2] ?? 0,
      stage,
      ground,
    }
  } catch {
    return null
  }
}

function fitScaleOf(bounds: Bounds | null): number {
  if (bounds === null) return 1
  const longest = Math.max(bounds.size[0], bounds.size[1], bounds.size[2])
  if (!Number.isFinite(longest) || longest <= 0) return 1
  return Math.min(20, Math.max(0.02, STUDIO_SUBJECT_METERS / longest))
}

function cameraFor(bounds: Bounds | null, scale: number, fov: number) {
  const raw = bounds?.size ?? [0.1, 0.1, 0.1]
  const size: [number, number, number] = [raw[0] * scale, raw[1] * scale, raw[2] * scale]
  const radius = Math.max(size[0], size[1], size[2]) / 2
  const target: [number, number, number] = [0, 0, size[1] / 2]
  const dist = (radius / Math.tan((fov / 2) * (Math.PI / 180))) * 1.55
  return {
    position: [target[0] + dist * 0.574, target[1] - dist * 0.82, target[2] + dist * 0.242] as [number, number, number],
    target,
  }
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => { window.setTimeout(resolve, ms) })

/** 轮询渲染任务直到结束。 */
async function waitForRender(
  jobId: string | undefined,
  onProgress: (status: string, value: number) => void,
): Promise<string> {
  if (jobId === undefined) throw new Error('后端没返回 job_id')
  for (;;) {
    await sleep(1500)
    const state = await getJson<{ status?: string; progress?: number; error?: string }>(`/api/studio/renders/${jobId}`)
    const status = String(state.status ?? 'unknown')
    const value = typeof state.progress === 'number' ? state.progress : 0
    onProgress(status, value)
    if (status === 'succeeded') return jobId
    if (status === 'failed' || status === 'cancelled' || status === 'interrupted') {
      throw new Error(state.error ?? status)
    }
  }
}

/** 商品图面板。 */
export function ProductPanel(props: {
  readonly onStatus: (text: string) => void
  readonly onRendered: (url: string, label: string) => void
}): ReactElement {
  const [products, setProducts] = useState<readonly ProductAsset[]>([])
  const [scenes, setScenes] = useState<readonly ProductAsset[]>([])
  const [productId, setProductId] = useState<string | null>(null)
  const [sceneId, setSceneId] = useState<string | null>(null)
  const [bounds, setBounds] = useState<Bounds | null>(null)
  const [sceneBounds, setSceneBounds] = useState<Bounds | null>(null)
  const [preset, setPreset] = useState<PresetKey>('white_seamless')
  const [frame, setFrame] = useState<FrameKey>('1:1')
  const [quality, setQuality] = useState<QualityKey>('fast')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<{ status: string; value: number } | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const cancelledRef = useRef(false)

  const product = products.find((item) => item.id === productId) ?? null
  const scene = scenes.find((item) => item.id === sceneId) ?? null

  const refresh = useCallback(async () => {
    try {
      const data = await getJson<{ assets?: ProductAsset[] }>('/api/studio/assets')
      const list = data.assets ?? []
      // 同名资产只留**最新**那个：资产库没有删除接口，重新上传同一个模型会堆出好几份，
      // 下拉里出现 5 个一样的名字会让人以为出了 bug。
      const newest = new Map<string, ProductAsset>()
      for (const item of list) {
        const previous = newest.get(item.name)
        if (previous === undefined || (item.created_at ?? 0) > (previous.created_at ?? 0)) {
          newest.set(item.name, item)
        }
      }
      const unique = [...newest.values()]
      const sceneList = unique.filter((item) => item.name.startsWith(SCENE_PREFIX))
      const productList = unique.filter((item) => !item.name.startsWith(SCENE_PREFIX))
      setScenes(sceneList)
      setProducts(productList)
      setProductId((current) => (current !== null && productList.some((item) => item.id === current) ? current : productList[0]?.id ?? null))
      setSceneId((current) => (current !== null && sceneList.some((item) => item.id === current) ? current : null))
    } catch {
      /* 后端没起就不列 */
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    let alive = true
    if (product === null) { setBounds(null); return }
    void (async () => {
      const parsed = await glbBounds(product.file_url)
      if (alive) setBounds(parsed)
    })()
    return () => { alive = false }
  }, [product])

  useEffect(() => {
    let alive = true
    if (scene === null) { setSceneBounds(null); return }
    void (async () => {
      const parsed = await glbBounds(scene.file_url)
      if (alive) setSceneBounds(parsed)
    })()
    return () => { alive = false }
  }, [scene])

  const upload = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (file === undefined) return
    if (!file.name.toLowerCase().endsWith('.glb')) {
      props.onStatus('只支持 .glb 模型（obj/fbx 先在 Blender 里导出成 glb）')
      return
    }
    props.onStatus(`上传中：${file.name}`)
    try {
      const form = new FormData()
      form.append('file', file, file.name)
      const response = await fetch(`${sidecarBase()}/api/studio/assets`, {
        method: 'POST',
        body: form,
        signal: AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
      })
      const payload = await response.json() as { code?: string | number; message?: string; data?: { asset?: ProductAsset } }
      if (!response.ok) throw new Error(String(payload.message ?? `HTTP ${response.status}`))
      await refresh()
      const created = payload.data?.asset
      if (created?.id !== undefined) setProductId(created.id)
      props.onStatus(`已上传：${file.name}`)
    } catch (error) {
      props.onStatus(`上传失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }

  const render = useCallback(async () => {
    if (product === null) { props.onStatus('先选一个商品模型'); return }
    cancelledRef.current = false
    setBusy(true)
    setProgress({ status: '提交中…', value: 0 })
    props.onStatus(scene === null ? `商品图渲染：${product.name}` : `场景渲染：${product.name} + ${scene.name}`)
    try {
      const [width, height] = RESOLUTION[frame][quality]
      const engine = quality === 'fast' ? 'eevee' : 'cycles'
      const samples = quality === 'fast' ? 32 : quality === 'standard' ? 96 : 256

      if (scene === null) {
        // ── 单商品棚（原来的白底/木桌那套）──
        const scale = fitScaleOf(bounds)
        const camera = cameraFor(bounds, scale, 32)
        const sceneSpec = {
          schema: 'videoforge.scene/1',
          scene_id: `vfproduct_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
          revision: 1,
          coordinates: { up: 'Z', handedness: 'right', unit: 'meter' },
          assets: [{ id: product.id }],
          instances: [{
            id: 'product', asset_id: product.id, position: [0, 0, 0],
            quaternion: [0, 0, 0, 1], scale: [scale, scale, scale],
          }],
          camera: {
            vertical_fov: 32,
            keys: [{ frame: 0, position: camera.position, target: camera.target, roll: 0, vertical_fov: 32 }],
          },
          object_tracks: [],
          render: { width, height, fps: 24, frame_count: 1 },
        }
        const submitted = await postJson<{ job_id?: string }>('/api/studio/renders', {
          scene: sceneSpec,
          studio: {
            preset,
            engine,
            samples,
            product_material: 'keep',
            drop_to_floor: true,
            auto_exposure: true,
          },
        })
        await waitForRender(submitted.job_id, (text) => props.onStatus(text))
        props.onRendered(`${sidecarBase()}/api/studio/renders/${String(submitted.job_id)}/files/frame_000000.png`,
          `${product.name} · ${PRESETS.find((item) => item.key === preset)?.name ?? preset}`)
        props.onStatus(`商品图已出：${product.name}`)
        return
      }

      // ── 商品放进真实场景（电商真正的用法：产品进环境）──
      const sceneSize = sceneBounds?.size ?? [10, 10, 3]
      const longest = Math.max(sceneSize[0], sceneSize[1], sceneSize[2])
      const sceneScale = longest > 1e-6 ? SCENE_TARGET_METERS / longest : 1
      const productLongest = Math.max(bounds?.size[0] ?? 0.1, bounds?.size[1] ?? 0.1, bounds?.size[2] ?? 0.1)
      // 场景里商品用**真实尺寸**（12cm 的罐子就还是 12cm），只在尺寸离谱时归一化
      const rawScale = productLongest > 2 || productLongest < 0.02 ? 0.3 / productLongest : 1
      const productScale = Math.min(20, Math.max(0.05, rawScale))
      const productHeight = (bounds?.size[1] ?? 0.1) * productScale

      // 落点：优先"能放东西的水平面"（地板/台面），退回到内容中位数
      const ground = sceneBounds?.ground ?? null
      const stage = sceneBounds?.stage ?? [0, 0, 0]
      const anchorX = ground !== null ? ground.x : stage[0]
      const anchorY = ground !== null ? ground.y : stage[1]
      const anchorZ = ground !== null ? ground.z : (sceneBounds?.floor ?? 0)
      const placePoint: [number, number, number] = [
        anchorX * sceneScale,
        anchorY * sceneScale,
        anchorZ * sceneScale,
      ]

      // 取景：商品为主体，环境进画面（距离按商品大小定，不按整个场景）
      const fov = 38
      const dist = Math.min(6, Math.max(1.5, productHeight * 10))
      const cameraPosition: [number, number, number] = [
        placePoint[0] + dist * 0.574,
        placePoint[1] - dist * 0.82,
        placePoint[2] + dist * 0.34 + productHeight * 0.6,
      ]
      const cameraTarget: [number, number, number] = [
        placePoint[0], placePoint[1], placePoint[2] + productHeight * 0.55,
      ]

      const sceneSpec = {
        schema: 'videoforge.scene/1',
        scene_id: `vfsceneshot_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
        revision: 1,
        coordinates: { up: 'Z', handedness: 'right', unit: 'meter' },
        assets: [{ id: scene.id }, { id: product.id }],
        instances: [
          {
            id: 'scene', asset_id: scene.id, position: [0, 0, 0],
            quaternion: [0, 0, 0, 1], scale: [sceneScale, sceneScale, sceneScale],
          },
          {
            id: 'product', asset_id: product.id,
            position: placePoint,
            quaternion: [0, 0, 0, 1], scale: [productScale, productScale, productScale],
          },
        ],
        camera: {
          vertical_fov: fov,
          keys: [{ frame: 0, position: cameraPosition, target: cameraTarget, roll: 0, vertical_fov: fov }],
        },
        object_tracks: [],
        render: { width, height, fps: 24, frame_count: 1 },
      }
      const submitted = await postJson<{ job_id?: string }>('/api/studio/renders', {
        scene: sceneSpec,
        studio: {
          preset: 'color_studio',
          engine,
          samples,
          product_material: 'keep',
          drop_to_floor: false,
          auto_exposure: false,
          environment: {
            world: [0.62, 0.76, 0.95],
            world_strength: 0.5,
            backdrop_color: [0.52, 0.56, 0.58],
          },
        },
      })
      await waitForRender(submitted.job_id, (text) => props.onStatus(text))
      props.onRendered(`${sidecarBase()}/api/studio/renders/${String(submitted.job_id)}/files/frame_000000.png`,
        `${product.name} @ ${scene.name}`)
      props.onStatus(`场景图已出：${product.name} @ ${scene.name}`)
    } catch (error) {
      props.onStatus(`渲染失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }, [bounds, frame, preset, product, props, quality, scene, sceneBounds])

  return (
    <div style={S.wrap}>
      <div style={S.row}>
        <select
          value={productId ?? ''}
          onChange={(event) => setProductId(event.target.value === '' ? null : event.target.value)}
          style={S.select}
        >
          {products.length === 0 && <option value="">（还没有商品模型）</option>}
          {products.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <button type="button" style={S.miniBtn} title="上传 .glb 商品模型" onClick={() => fileRef.current?.click()}>上传</button>
        <input ref={fileRef} type="file" accept=".glb" style={{ display: 'none' }} onChange={(event) => { void upload(event) }} />
      </div>
      <div style={S.hint}>
        {bounds === null
          ? '尺寸未读到（按 10cm 兜底构图）'
          : `${(bounds.size[0] * 100).toFixed(1)}×${(bounds.size[2] * 100).toFixed(1)}×${(bounds.size[1] * 100).toFixed(1)} cm · ${bounds.tris.toLocaleString()} 面`}
      </div>

      <div style={S.row}>
        <select
          value={sceneId ?? ''}
          onChange={(event) => setSceneId(event.target.value === '' ? null : event.target.value)}
          style={S.select}
          title="选一个场景模型，把商品放进这个真实环境里出图"
        >
          <option value="">（不放进场景 · 白底/木桌棚）</option>
          {scenes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </div>
      {scene !== null && (
        <div style={S.hint}>
          {sceneBounds === null
            ? '场景尺寸未读到'
            : `场景 ${(sceneBounds.size[0]).toFixed(1)}×${(sceneBounds.size[1]).toFixed(1)}×${(sceneBounds.size[2]).toFixed(1)} m · ${sceneBounds.tris.toLocaleString()} 面 · 商品按真实比例放在场景中心`}
        </div>
      )}

      <div style={S.chips}>
        {PRESETS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setPreset(item.key)}
            style={{ ...S.chip, ...(preset === item.key ? S.chipOn : {}), ...(scene !== null ? { opacity: 0.45 } : {}) }}
            disabled={scene !== null}
          >{item.name}</button>
        ))}
      </div>
      <div style={S.chips}>
        {FRAMES.map((item) => (
          <button key={item} type="button" onClick={() => setFrame(item)} style={{ ...S.chip, ...(frame === item ? S.chipOn : {}) }}>{item}</button>
        ))}
      </div>
      <div style={S.chips}>
        {QUALITIES.map((item) => (
          <button key={item.key} type="button" onClick={() => setQuality(item.key)} style={{ ...S.chip, ...(quality === item.key ? S.chipOn : {}) }}>{item.label}</button>
        ))}
      </div>

      <button
        type="button"
        onClick={() => { void render() }}
        disabled={busy || product === null}
        style={{ ...S.primary, opacity: busy || product === null ? 0.5 : 1 }}
      >
        {busy
          ? `${progress?.status ?? '渲染中'} ${Math.round((progress?.value ?? 0) * 100)}%`
          : scene === null ? '出商品图' : '商品进场景出图'}
      </button>
      <div style={S.hint}>本机 Blender 渲染 · 不联网 · 不计费 · 结果进右上成片预览</div>
    </div>
  )
}

const S: Record<string, CSSProperties> = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 6 },
  row: { display: 'flex', gap: 6, alignItems: 'center' },
  select: {
    flex: '1 1 auto', minWidth: 0, fontSize: 12, padding: '4px 6px', borderRadius: 6,
    border: `1px solid ${C.line}`, background: C.cream, color: C.ink,
  },
  miniBtn: {
    flex: '0 0 auto', fontSize: 11, padding: '4px 8px', borderRadius: 6, cursor: 'pointer',
    border: `1px solid ${C.line}`, background: C.cream, color: C.ink3, whiteSpace: 'nowrap',
  },
  hint: { fontSize: 10.5, color: C.ink4, lineHeight: 1.5 },
  chips: { display: 'flex', gap: 4, flexWrap: 'wrap' },
  chip: {
    fontSize: 11, padding: '3px 8px', borderRadius: 5, cursor: 'pointer',
    border: `1px solid ${C.line}`, background: C.cream, color: C.ink3, whiteSpace: 'nowrap',
  },
  chipOn: { background: C.brass, borderColor: C.brass2, color: '#fff' },
  primary: {
    marginTop: 2, padding: '7px 0', fontSize: 12.5, fontWeight: 600, borderRadius: 7, cursor: 'pointer',
    border: `1px solid ${C.brass2}`, background: C.brass, color: '#fff',
  },
}
