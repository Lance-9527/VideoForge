/**
 * 掌柜 AI · **全景图 → 多视图深度重建场景**（客户端实现）。
 *
 * ★ 这条路为什么是"多视图"，而不是"把全景图贴到球上"：
 *
 *   - 贴到球上（老实现 `buildPanoPly`）：所有点半径相同 ⇒ **视差为零**，
 *     走动立刻穿帮，而且上下是平的圆柱，不是 360° 无死角。
 *   - 本实现：把全景图按**球面**采样成 N 张**透视视图** → 每张算深度 →
 *     像素反投影成三维点 → 合并。每个方向有自己的距离，
 *     视差来自**假设的深度结构**，不是测量到的几何。
 *
 * ★ Depth Anything V2 的论文（DSOF）也明确说：等距圆柱图直接喂模型会判错
 *   （天空/地面颠倒），必须先转成**正立的**透视视图 —— 这里就是这么做的 ✓
 *
 * ★ 三条几何前提（独立审查用数值实测确认过的错误，这里是修好后的状态）：
 *   1. `samplePerspective` 产出的 `dirs` **已经是全局方向**（它就是用来查
 *      全景图像素的）⇒ 合并点云时**不能再乘一次 Rz@Rx**。否则方位角被转两遍，
 *      全景的正面/背面、左半/右半互相叠印 ⇒ 产出"双层壳"。
 *   2. `up = fwd × right`（右手系）⇒ 透视图正立（顶行朝上、底行朝下）。
 *   3. 深度用**跨视图统一归一化**（全局单一 (scale, shift)），
 *      而不是每张视图各自 min-max —— 后者会让同一张表面在不同视图落到不同距离，
 *      重叠区按构造互相矛盾。
 *   正立性与"只旋转一次"两条断言直接接在采样/合并路径上，失败即抛错。
 *
 * ★ 尺度诚实性：深度是**单目先验的相对深度**，**绝对尺度未知**；
 *   `near`/`far` 是**人为假设**的距离区间，不是测量值。
 *   产出用于预览与再构图，不能当作真实几何。
 *
 * @module
 */
import { estimateDepth, type DepthProvider } from './depth-model.ts'

/** 生成的场景。 */
export interface PanoramaScene {
  readonly bytes: Uint8Array
  readonly points: number
  /** 相机应该站的位置（全景图的拍摄点）。 */
  readonly eye: readonly [number, number, number]
  /** 深度来源。 */
  readonly provider: DepthProvider
  /** 视图数（多视图采样的规模）。 */
  readonly views: number
}

/** SH 0 阶系数。 */
const SH_C0 = 0.28209479177387814

/** 眼高（米）。 */
const EYE_HEIGHT = 1.6

/** 最近/最远距离（米）—— **人为假设**的尺度区间，不是深度模型测出来的值。 */
const DEFAULT_NEAR = 0.8
const DEFAULT_FAR = 60

/**
 * 视图方向 → 全局方向的变换（列向量形式）。
 *
 * ★ 必须是恒等：`samplePerspective` 产出的方向**已经是全局方向**。
 *   这里再乘一次 Rz@Rx 就是把方位角旋转两遍（yaw 45° → 落到 90°），
 *   24 个视图会坍缩成 18 个唯一落点，正面/背面互相叠印 ⇒ "双层壳"。
 */
const VIEW_TO_GLOBAL = {
  xx: 1, xy: 0, xz: 0,
  yx: 0, yy: 1, yz: 0,
  zx: 0, zy: 0, zz: 1,
} as const

/**
 * 把视图方向过一遍全局变换。
 * @param dx - 方向 x。
 * @param dy - 方向 y。
 * @param dz - 方向 z。
 * @returns 全局方向。
 */
function toGlobalDirection(dx: number, dy: number, dz: number): [number, number, number] {
  const m = VIEW_TO_GLOBAL
  return [
    m.xx * dx + m.xy * dy + m.xz * dz,
    m.yx * dx + m.yy * dy + m.yz * dz,
    m.zx * dx + m.zy * dy + m.zz * dz,
  ]
}

/**
 * 断言：视图方向到全局方向**只变换一次**（且这次变换是恒等）。
 *
 * 判据：方向过一遍实际使用的变换矩阵，结果必须与原方向相同；
 * 矩阵本身还必须是右手系的正交旋转（行列式为 +1）。
 *
 * @param dirs - 像素方向（长宽 × 3）。
 * @param count - 像素数。
 * @throws 若变换后方向与原方向不同，或矩阵不是正交旋转。
 */
function assertSingleRotation(dirs: Float32Array, count: number): void {
  const m = VIEW_TO_GLOBAL
  const det = m.xx * (m.yy * m.zz - m.yz * m.zy)
    - m.xy * (m.yx * m.zz - m.yz * m.zx)
    + m.xz * (m.yx * m.zy - m.yy * m.zx)
  if (Math.abs(det - 1) > 1e-9) {
    throw new Error(`视图→全局变换不是右手系旋转（det=${det}）`)
  }
  for (let index = 0; index < count; index += 1) {
    const dx = dirs[index * 3] ?? 0
    const dy = dirs[index * 3 + 1] ?? 0
    const dz = dirs[index * 3 + 2] ?? 0
    const [gx, gy, gz] = toGlobalDirection(dx, dy, dz)
    if (Math.abs(gx - dx) > 1e-9 || Math.abs(gy - dy) > 1e-9 || Math.abs(gz - dz) > 1e-9) {
      throw new Error(
        `视图方向被重复旋转：(${dx.toFixed(6)}, ${dy.toFixed(6)}, ${dz.toFixed(6)})`
        + ` → (${gx.toFixed(6)}, ${gy.toFixed(6)}, ${gz.toFixed(6)})`,
      )
    }
  }
}

/**
 * 断言：采样出的透视图是**正立**的。
 *
 * `up` 取反会让整张透视图上下颠倒（地面在上、天空在下），
 * 直接违反"先转成正立的透视视图再预测深度"这个前提。
 *
 * @param dirs - 像素方向（长宽 × 3）。
 * @param size - 视图边长。
 * @throws 若顶行不朝上（phi > 0）或底行不朝下（phi < 0）。
 */
function assertUprightView(dirs: Float32Array, size: number): void {
  const bottomRow = (size - 1) * size
  let topMin = Number.POSITIVE_INFINITY
  let bottomMax = Number.NEGATIVE_INFINITY
  for (let x = 0; x < size; x += 1) {
    const topDy = dirs[x * 3 + 1] ?? 0
    const bottomDy = dirs[(bottomRow + x) * 3 + 1] ?? 0
    const topPhi = Math.asin(Math.max(-1, Math.min(1, topDy)))
    const bottomPhi = Math.asin(Math.max(-1, Math.min(1, bottomDy)))
    if (topPhi < topMin) { topMin = topPhi }
    if (bottomPhi > bottomMax) { bottomMax = bottomPhi }
  }
  if (!(topMin > 0)) {
    throw new Error(`透视图不正立：顶行 phi=${topMin.toFixed(6)}（应 > 0，朝上）`)
  }
  if (!(bottomMax < 0)) {
    throw new Error(`透视图不正立：底行 phi=${bottomMax.toFixed(6)}（应 < 0，朝下）`)
  }
}

/**
 * 解码图片，取像素。
 * @param file - 图片。
 * @param maxSide - 长边上限。
 * @returns 像素（长宽 × 4）与尺寸。
 */
async function decode(file: Blob, maxSide: number): Promise<{
  data: Uint8ClampedArray
  width: number
  height: number
}> {
  const bitmap = await createImageBitmap(file)
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height))
  const width = Math.max(2, Math.round(bitmap.width * scale))
  const height = Math.max(2, Math.round(bitmap.height * scale))
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (context === null) { throw new Error('拿不到 2D 画布') }
  context.drawImage(bitmap, 0, 0, width, height)
  bitmap.close()
  return { data: context.getImageData(0, 0, width, height).data, width, height }
}

/**
 * 从等距圆柱全景图里按针孔相机取一张透视视图。
 *
 * 产出的 `dirs` 是**全局方向**（球面坐标：yaw 绕 Y、pitch 抬降），
 * 因为它们就是用来查全景图像素的；下游不要再对它们做第二次旋转。
 *
 * @param pano - 全景图像素。
 * @param panoWidth - 全景宽。
 * @param panoHeight - 全景高。
 * @param yaw - 方位角（弧度）。
 * @param pitch - 俯仰角（弧度，正为上）。
 * @param fovDeg - 视场角（度）。
 * @param size - 输出边长。
 * @returns 视图像素、有效掩码、每个像素的方向向量。
 * @throws 若采样出的透视图不正立（顶行不朝上或底行不朝下）。
 */
function samplePerspective(
  pano: Uint8ClampedArray,
  panoWidth: number,
  panoHeight: number,
  yaw: number,
  pitch: number,
  fovDeg: number,
  size: number,
): { rgb: Float32Array; mask: Float32Array; dirs: Float32Array } {
  const half = Math.tan((fovDeg * Math.PI) / 360)
  const cy = Math.cos(yaw)
  const sy = Math.sin(yaw)
  const cp = Math.cos(pitch)
  const sp = Math.sin(pitch)
  // 前向：yaw 绕 Y，pitch 向上
  const fwdX = sy * cp
  const fwdY = sp
  const fwdZ = cy * cp
  // 右向
  const rightX = cy
  const rightY = 0
  const rightZ = -sy
  // ★ 上向 = fwd × right（右手系）：用 right × fwd 会把 up 取反，
  //   透视图上下颠倒（地面在上、天空在下）。
  let upX = fwdY * rightZ - fwdZ * rightY
  let upY = fwdZ * rightX - fwdX * rightZ
  let upZ = fwdX * rightY - fwdY * rightX
  const upLen = Math.hypot(upX, upY, upZ) || 1
  upX /= upLen
  upY /= upLen
  upZ /= upLen

  const rgb = new Float32Array(size * size * 3)
  const mask = new Float32Array(size * size)
  const dirs = new Float32Array(size * size * 3)

  for (let y = 0; y < size; y += 1) {
    const gy = 1 - ((y + 0.5) / size) * 2
    for (let x = 0; x < size; x += 1) {
      const gx = ((x + 0.5) / size) * 2 - 1
      let dx = fwdX + gx * half * rightX + gy * half * upX
      let dy = fwdY + gx * half * rightY + gy * half * upY
      let dz = fwdZ + gx * half * rightZ + gy * half * upZ
      const len = Math.hypot(dx, dy, dz) || 1
      dx /= len
      dy /= len
      dz /= len
      const index = y * size + x
      dirs[index * 3] = dx
      dirs[index * 3 + 1] = dy
      dirs[index * 3 + 2] = dz

      // 方向 → 全景图像素（等距圆柱）
      const theta = Math.atan2(dx, dz)
      const phi = Math.asin(Math.max(-1, Math.min(1, dy)))
      let u = Math.round((((theta + Math.PI) / (2 * Math.PI)) * panoWidth)) % panoWidth
      if (u < 0) { u += panoWidth }
      const v = Math.min(panoHeight - 1, Math.max(0, Math.round(((Math.PI / 2 - phi) / Math.PI) * panoHeight)))
      const p = (v * panoWidth + u) * 4
      rgb[index * 3] = pano[p] ?? 0
      rgb[index * 3 + 1] = pano[p + 1] ?? 0
      rgb[index * 3 + 2] = pano[p + 2] ?? 0
      // 极点附近全景图不可信（畸变/空白）→ 标无效
      mask[index] = Math.abs(phi) < (72 * Math.PI) / 180 ? 1 : 0
    }
  }
  // ★ 断言：这张透视图是正立的（顶行朝上、底行朝下）。
  assertUprightView(dirs, size)
  return { rgb, mask, dirs }
}

/**
 * 一个采样视图及其深度。
 */
interface ViewSample {
  readonly rgb: Float32Array
  readonly mask: Float32Array
  /** 全局方向（长宽 × 3）。 */
  readonly dirs: Float32Array
  /** 未归一化的原始深度值（越大越近）。 */
  readonly depth: Float32Array
  readonly provider: DepthProvider
}

/**
 * 生成全景图对应的 3D 场景。
 *
 * @param file - 全景图（等距圆柱，长边绕一圈）。
 * @param options - 可选参数。
 * @returns 场景点云（3DGS 格式 .ply）。
 * @throws 若透视图不正立、视图方向被重复旋转，或所有视图的深度没有有效变化。
 */
export async function buildPanoramaScene(
  file: Blob,
  options: {
    /** 全景图长边采样上限。 */
    readonly panoSize?: number
    /** 每个视图的边长。 */
    readonly viewSize?: number
    /** 视场角（度）。 */
    readonly fov?: number
    /** 方位角个数。 */
    readonly yawCount?: number
    /** 俯仰角个数。 */
    readonly pitchCount?: number
    /** 假设的最近距离（米）—— 人为假设值，不是测量值。 */
    readonly near?: number
    /** 假设的最远距离（米）—— 人为假设值，不是测量值。 */
    readonly far?: number
  } = {},
): Promise<PanoramaScene> {
  const panoSize = options.panoSize ?? 2048
  // 160×160×24 视图 ≈ 61 万点（ply ≈ 34MB，base64 上传 ≈ 46MB）——
  // 再大 sidecar 的 JSON 上传会吃不消（它是 base64 的 JSON）。
  const viewSize = options.viewSize ?? 160
  const fov = options.fov ?? 90
  const yawCount = options.yawCount ?? 8
  const pitchCount = options.pitchCount ?? 3
  const near = options.near ?? DEFAULT_NEAR
  const far = options.far ?? DEFAULT_FAR

  const { data, width, height } = await decode(file, panoSize)

  const samples: ViewSample[] = []
  for (let pi = 0; pi < pitchCount; pi += 1) {
    // 俯仰角：上中下三档（中间 0，上下 ±25°）
    const pitch = pitchCount === 1 ? 0 : ((pi / (pitchCount - 1)) * 2 - 1) * (25 * Math.PI) / 180
    for (let yi = 0; yi < yawCount; yi += 1) {
      const yaw = ((yi / yawCount) * 2 - 1) * Math.PI
      const view = samplePerspective(data, width, height, yaw, pitch, fov, viewSize)
      // ★ 断言：视图方向到全局方向只变换一次（恒等），不能被重复旋转。
      assertSingleRotation(view.dirs, viewSize * viewSize)
      const { map, provider } = await estimateDepth(view.rgb, viewSize, viewSize)
      samples.push({ rgb: view.rgb, mask: view.mask, dirs: view.dirs, depth: map.data, provider })
    }
  }

  // ★ 深度来源：两套来源的数值刻度不同，混在一起会错位 ⇒ 只保留一套。
  //   取**占多数**的那套（平手取模型）：个别视图推理失败降级成启发式时，
  //   不能只剩一两个模型视图，否则重建出来的场景只有一视角的锥面。
  const modelSamples = samples.filter((sample) => sample.provider === 'depth-anything-v2')
  const heuristicSamples = samples.filter((sample) => sample.provider === 'heuristic')
  const useModel = modelSamples.length >= heuristicSamples.length && modelSamples.length > 0
  const chosen = useModel ? modelSamples : heuristicSamples
  const provider: DepthProvider = useModel ? 'depth-anything-v2' : 'heuristic'

  // ★ 跨视图统一归一化：所有视图共用**同一个** (scale, shift)。
  //   逐视图各自 min-max 会把同一张表面在不同视图放到不同距离。
  let globalMin = Number.POSITIVE_INFINITY
  let globalMax = Number.NEGATIVE_INFINITY
  for (const sample of chosen) {
    for (let index = 0; index < sample.depth.length; index += 1) {
      const value = sample.depth[index] ?? 0
      if (value < globalMin) { globalMin = value }
      if (value > globalMax) { globalMax = value }
    }
  }
  const span = globalMax - globalMin
  if (!Number.isFinite(span) || span <= 0) {
    throw new Error('多视图深度没有有效变化，无法重建场景（输入图可能是纯色或空白）')
  }

  const positions: number[] = []
  const colors: number[] = []
  for (const sample of chosen) {
    for (let index = 0; index < sample.mask.length; index += 1) {
      if (sample.mask[index] === 0) { continue }
      const raw = sample.depth[index] ?? 0
      const rel = Math.max(0, Math.min(1, (raw - globalMin) / span))
      // 相对深度 → 假设的距离区间（near/far 是人为假设值，不是测量值）
      const dist = near + (far - near) * Math.pow(1 - rel, 1.7)
      const dx = sample.dirs[index * 3] ?? 0
      const dy = sample.dirs[index * 3 + 1] ?? 0
      const dz = sample.dirs[index * 3 + 2] ?? 0
      // ★ dirs 已是全局方向，这里只过一次恒等变换（见 assertSingleRotation）。
      const [gx, gy, gz] = toGlobalDirection(dx, dy, dz)
      positions.push(gx * dist, EYE_HEIGHT + gy * dist, gz * dist)
      colors.push(sample.rgb[index * 3] ?? 0, sample.rgb[index * 3 + 1] ?? 0, sample.rgb[index * 3 + 2] ?? 0)
    }
  }

  const count = colors.length / 3
  const views = chosen.length
  const header = [
    'ply',
    'format binary_little_endian 1.0',
    `comment 掌柜 AI 多视图深度重建 · 视图数 ${views} · 深度 ${provider}`,
    'comment 深度为单目先验相对深度，绝对尺度未知；距离区间为假设值',
    `comment assumed range ${near}-${far} m`,
    `comment eye ${EYE_HEIGHT}`,
    `element vertex ${count}`,
    'property float x', 'property float y', 'property float z',
    'property float f_dc_0', 'property float f_dc_1', 'property float f_dc_2',
    'property float opacity',
    'property float scale_0', 'property float scale_1', 'property float scale_2',
    'property float rot_0', 'property float rot_1', 'property float rot_2', 'property float rot_3',
    'end_header',
    '',
  ].join('\n')
  const headerBytes = new TextEncoder().encode(header)
  const stride = 14 * 4
  const bytes = new Uint8Array(headerBytes.length + count * stride)
  bytes.set(headerBytes, 0)
  const view = new DataView(bytes.buffer, headerBytes.length)
  let offset = 0
  for (let index = 0; index < count; index += 1) {
    const px = positions[index * 3] ?? 0
    const py = positions[index * 3 + 1] ?? 0
    const pz = positions[index * 3 + 2] ?? 0
    // 高斯朝向眼点（billboard）
    const ex = -px
    const ey = EYE_HEIGHT - py
    const ez = -pz
    const length = Math.hypot(ex, ey, ez) || 1
    const nx = ex / length
    const ny = ey / length
    const nz = ez / length
    const axisX = -ny
    const axisY = nx
    const sinHalf = Math.hypot(axisX, axisY)
    let qw = 1
    let qx = 0
    let qy = 0
    let qz = 0
    if (sinHalf < 1e-9) {
      if (nz < 0) { qw = 0; qx = 1 }
    } else {
      const angle = Math.atan2(sinHalf, nz)
      const s = Math.sin(angle / 2) / sinHalf
      qw = Math.cos(angle / 2)
      qx = axisX * s
      qy = axisY * s
    }
    const size = Math.max(1e-6, length * 0.007)
    const logScale = Math.log(size)
    const logThin = Math.log(Math.max(1e-6, length * 0.0015))
    const r = (colors[index * 3] ?? 0) / 255
    const g = (colors[index * 3 + 1] ?? 0) / 255
    const b = (colors[index * 3 + 2] ?? 0) / 255

    view.setFloat32(offset, px, true); offset += 4
    view.setFloat32(offset, py, true); offset += 4
    view.setFloat32(offset, pz, true); offset += 4
    view.setFloat32(offset, (r - 0.5) / SH_C0, true); offset += 4
    view.setFloat32(offset, (g - 0.5) / SH_C0, true); offset += 4
    view.setFloat32(offset, (b - 0.5) / SH_C0, true); offset += 4
    view.setFloat32(offset, 4.59511985013459, true); offset += 4
    view.setFloat32(offset, logScale, true); offset += 4
    view.setFloat32(offset, logScale, true); offset += 4
    view.setFloat32(offset, logThin, true); offset += 4
    view.setFloat32(offset, qw, true); offset += 4
    view.setFloat32(offset, qx, true); offset += 4
    view.setFloat32(offset, qy, true); offset += 4
    view.setFloat32(offset, qz, true); offset += 4
  }

  return {
    bytes,
    points: count,
    eye: [0, EYE_HEIGHT, 0],
    provider,
    views,
  }
}

/* 自动化/调试入口：挂到 window，方便用 CDP 注入文件做端到端验证。 */
;(globalThis as unknown as { __vfBuildPanoramaScene?: typeof buildPanoramaScene }).__vfBuildPanoramaScene = buildPanoramaScene
