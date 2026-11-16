/**
 * 掌柜 AI · **全景图 → 可漫游 3D 场景**（几何一致版）。
 *
 * ★ 为什么要有这个文件（老实现的问题，看代码就知道）：
 *
 *   老 `buildPanoPly` 做的是：等距圆柱全景图 → **半径 8m 的圆柱内壁**贴图。
 *   - 所有点都在同一个半径上 ⇒ **视差恒等于零** ⇒ 你一动就穿帮；
 *   - 上下是平的直线（`py = eye + (halfHeight - y) * arc`）⇒ 不是 360° 无死角；
 *   - 老 `buildLdiPly` 的"深度"是**亮度+饱和度+边缘的启发式**（lum/sat/detail），
 *     按它分层切壳 ⇒ 从侧面看就是一叠壳子（用户骂的"散块"）。
 *
 *   本文件的做法：**球面重投影 + 深度反投影**，每个像素变成一个真实的三维点：
 *   - 等距圆柱 (x,y) → 球面经纬 (θ,φ) → 单位方向向量 dir；
 *   - 深度 d（0=远 … 1=近）→ 距离 dist → 位置 P = eye + dir · dist；
 *   ⇒ **视差、遮挡都自洽**，走动不会穿帮 —— 这才是"场景"。
 *
 * 深度来源有两种：
 *   1. `Depth Anything V2`（ONNX，浏览器内跑，见 depth-model.ts）—— 真单目深度；
 *   2. 兜底：启发式（比老实现保守：亮度 + 垂直位置 + 边缘细节）。
 *
 * @module
 */

/** 生成结果。 */
export interface ScenePlyResult {
  readonly bytes: Uint8Array
  readonly points: number
  /** 相机初始位姿（站在全景图的拍摄点）。 */
  readonly camera: { readonly position: readonly number[]; readonly target: readonly number[]; readonly fov: number }
  /** 用到的近/远距离（米）。 */
  readonly near: number
  readonly far: number
  /** 深度来源说明（写进 UI，让用户知道这是估算还是模型推的）。 */
  readonly depthSource: 'model' | 'heuristic'
}

/** SH 0 阶系数（与老实现同口径）。 */
const SH_C0 = 0.28209479177387814

/** 眼高（米）—— 全景图的拍摄点高度。 */
const SCENE_EYE = 1.6

/** 取一张图的像素（沿用老实现的解码口径）。 */
async function pixelsOf(
  file: Blob,
  size: number,
): Promise<{ data: Uint8ClampedArray; width: number; height: number }> {
  const bitmap = await createImageBitmap(file)
  const scale = Math.min(1, size / Math.max(bitmap.width, bitmap.height))
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
 * 双线性采样一张深度图到目标尺寸。
 * @param source - 源深度（0..1，1=最近）。
 * @param sourceWidth - 源宽。
 * @param sourceHeight - 源高。
 * @param targetWidth - 目标宽。
 * @param targetHeight - 目标高。
 * @returns 目标尺寸的深度。
 */
function resampleDepth(
  source: Float32Array,
  sourceWidth: number,
  sourceHeight: number,
  targetWidth: number,
  targetHeight: number,
): Float32Array {
  const out = new Float32Array(targetWidth * targetHeight)
  for (let y = 0; y < targetHeight; y += 1) {
    const sy = Math.min(sourceHeight - 1, Math.max(0, ((y + 0.5) * sourceHeight) / targetHeight - 0.5))
    const y0 = Math.floor(sy)
    const y1 = Math.min(sourceHeight - 1, y0 + 1)
    const fy = sy - y0
    for (let x = 0; x < targetWidth; x += 1) {
      const sx = Math.min(sourceWidth - 1, Math.max(0, ((x + 0.5) * sourceWidth) / targetWidth - 0.5))
      const x0 = Math.floor(sx)
      const x1 = Math.min(sourceWidth - 1, x0 + 1)
      const fx = sx - x0
      const top = (source[y0 * sourceWidth + x0] ?? 0) * (1 - fx) + (source[y0 * sourceWidth + x1] ?? 0) * fx
      const bottom = (source[y1 * sourceWidth + x0] ?? 0) * (1 - fx) + (source[y1 * sourceWidth + x1] ?? 0) * fx
      out[y * targetWidth + x] = top * (1 - fy) + bottom * fy
    }
  }
  return out
}

/**
 * 兜底深度（启发式）：亮度 + 垂直位置 + 边缘细节。
 *
 * ★ 比老实现保守很多：老实现拿亮度当深度（亮=近），会把"天空很亮"变成"天空贴脸"。
 *   这里改成：细节多 → 近；大片平滑（天空/墙） → 远；再叠一点垂直位置先验。
 *
 * @param data - RGBA 像素。
 * @param width - 宽。
 * @param height - 高。
 * @returns 深度（0..1，1=最近）。
 */
function heuristicDepth(data: Uint8ClampedArray, width: number, height: number): Float32Array {
  const count = width * height
  const lum = new Float32Array(count)
  const detail = new Float32Array(count)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x
      const p = index * 4
      lum[index] = ((data[p] ?? 0) + (data[p + 1] ?? 0) + (data[p + 2] ?? 0)) / (3 * 255)
      const dx = x > 0 ? Math.abs(lum[index] - (lum[index - 1] ?? 0)) : 0
      const dy = y > 0 ? Math.abs(lum[index] - (lum[index - width] ?? 0)) : 0
      detail[index] = Math.min(1, Math.sqrt(dx * dx + dy * dy) * 8)
    }
  }
  // 细节图的均值（当"细节密度"参考）
  let detailMean = 0
  for (let index = 0; index < count; index += 1) { detailMean += detail[index] ?? 0 }
  detailMean /= Math.max(1, count)

  const out = new Float32Array(count)
  for (let y = 0; y < height; y += 1) {
    // 垂直位置先验：脚下（y 大）稍近，天顶（y 小）远
    const vertical = y / Math.max(1, height - 1)
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x
      const d = (detail[index] ?? 0) / Math.max(0.05, detailMean)
      const near = Math.min(1, d * 0.55)
      out[index] = Math.max(0, Math.min(1, near * 0.75 + (1 - vertical) * 0.18 + 0.12))
    }
  }
  return out
}

/**
 * 生成全景图对应的 3D 场景（点云高斯 .ply）。
 *
 * @param file - 全景图（等距圆柱，长边绕一圈）。
 * @param options - 可选参数。
 * @returns .ply 字节与场景信息。
 */
export async function buildScenePly(
  file: Blob,
  options: {
    /** 采样宽度（长边）。点太多文件会巨大，1024 起步够用。 */
    readonly size?: number
    /** 模型给的深度（0..1，1=最近）；不给就用启发式。 */
    readonly depth?: Float32Array
    readonly depthWidth?: number
    readonly depthHeight?: number
    /** 最近/最远距离（米）。 */
    readonly near?: number
    readonly far?: number
  } = {},
): Promise<ScenePlyResult> {
  const size = options.size ?? 1024
  const near = options.near ?? 0.8
  const far = options.far ?? 60

  const { data, width, height } = await pixelsOf(file, size)

  // ── 深度 ──
  let depth: Float32Array
  let depthSource: 'model' | 'heuristic'
  if (options.depth !== undefined && options.depthWidth !== undefined && options.depthHeight !== undefined) {
    depth = resampleDepth(options.depth, options.depthWidth, options.depthHeight, width, height)
    depthSource = 'model'
  } else {
    depth = heuristicDepth(data, width, height)
    depthSource = 'heuristic'
  }

  const count = width * height

  // ── 颜色（3×3 高斯预平滑，否则每个 splat 一块纯色，放大全是色块）──
  const rgb = new Float32Array(count * 3)
  for (let index = 0; index < count; index += 1) {
    rgb[index * 3] = (data[index * 4] ?? 0) / 255
    rgb[index * 3 + 1] = (data[index * 4 + 1] ?? 0) / 255
    rgb[index * 3 + 2] = (data[index * 4 + 2] ?? 0) / 255
  }
  {
    const smoothed = new Float32Array(count * 3)
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const index = y * width + x
        let sr = 0, sg = 0, sb = 0, total = 0
        for (let dy = -1; dy <= 1; dy += 1) {
          const ny = Math.min(height - 1, Math.max(0, y + dy))
          for (let dx = -1; dx <= 1; dx += 1) {
            const nx = Math.min(width - 1, Math.max(0, x + dx))
            const ni = ny * width + nx
            const weight = dx === 0 && dy === 0 ? 4 : 1
            sr += (rgb[ni * 3] ?? 0) * weight
            sg += (rgb[ni * 3 + 1] ?? 0) * weight
            sb += (rgb[ni * 3 + 2] ?? 0) * weight
            total += weight
          }
        }
        smoothed[index * 3] = sr / total
        smoothed[index * 3 + 1] = sg / total
        smoothed[index * 3 + 2] = sb / total
      }
    }
    rgb.set(smoothed)
  }

  // ── 位置抖动（规则点阵放大时很刺眼）──
  const jitterX = new Float32Array(count)
  const jitterY = new Float32Array(count)
  {
    let seed = 0x9e3779b9
    for (let index = 0; index < count; index += 1) {
      seed = (seed * 1664525 + 1013904223) >>> 0
      jitterX[index] = (seed >>> 8) / 16777216 - 0.5
      seed = (seed * 1664525 + 1013904223) >>> 0
      jitterY[index] = (seed >>> 8) / 16777216 - 0.5
    }
  }

  // ── 写 .ply（3DGS 格式：f_dc / opacity / scale / rot）──
  const header = [
    'ply',
    'format binary_little_endian 1.0',
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

  // 每个像素的角径（用来定高斯大小）
  const arcY = Math.PI / Math.max(1, height)
  const arcX = (Math.PI * 2) / Math.max(1, width)

  let offset = 0
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x

      // ── 球面经纬 ──
      // θ：方位角，长边绕一圈；φ：纬度，y=0 是天顶
      const theta = ((x + 0.5 + (jitterX[index] ?? 0) * 0.9) / width) * Math.PI * 2 - Math.PI
      const phi = Math.PI / 2 - ((y + 0.5 + (jitterY[index] ?? 0) * 0.9) / height) * Math.PI
      const cosPhi = Math.cos(phi)
      const dirX = cosPhi * Math.sin(theta)
      const dirY = Math.sin(phi)
      const dirZ = cosPhi * Math.cos(theta)

      // ── 深度 → 距离 ──
      // depth: 1 = 最近。距离用幂曲线铺开，让近处细节密、远处稀。
      const rel = Math.max(0, Math.min(1, depth[index] ?? 0))
      const t = Math.pow(1 - rel, 1.7)
      const dist = near + (far - near) * t

      const px = dirX * dist
      const py = SCENE_EYE + dirY * dist
      const pz = dirZ * dist

      // ── 高斯朝向：正对眼点（billboard），保证从任何角度都看得见 ──
      const toEyeX = -px
      const toEyeY = SCENE_EYE - py
      const toEyeZ = -pz
      const [qw, qx, qy, qz] = quatToward(toEyeX, toEyeY, toEyeZ)

      // ── 高斯大小：像素在那个距离上的角径 × 距离 ──
      const pixelAngle = Math.max(arcX, arcY)
      const sizeWorld = pixelAngle * dist * 0.85
      const logScale = Math.log(Math.max(1e-6, sizeWorld))
      const logThin = Math.log(Math.max(1e-6, pixelAngle * dist * 0.12))

      const r = rgb[index * 3] ?? 0
      const g = rgb[index * 3 + 1] ?? 0
      const b = rgb[index * 3 + 2] ?? 0

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
  }

  return {
    bytes,
    points: count,
    camera: { position: [0, SCENE_EYE, 0], target: [0, SCENE_EYE, -1], fov: 70 },
    near,
    far,
    depthSource,
  }
}

/**
 * 求一个把 +Z 轴旋到给定方向的单位四元数。
 * @param dx - 方向 x。
 * @param dy - 方向 y。
 * @param dz - 方向 z。
 * @returns 四元数 [w,x,y,z]。
 */
function quatToward(dx: number, dy: number, dz: number): [number, number, number, number] {
  const length = Math.hypot(dx, dy, dz)
  if (length < 1e-9) { return [1, 0, 0, 0] }
  const nz = dz / length
  const nx = dx / length
  const ny = dy / length
  // 从 (0,0,1) 旋到 n：绕 axis = cross((0,0,1), n)
  const ax = -ny
  const ay = nx
  const az = 0
  const sinHalf = Math.hypot(ax, ay, az)
  if (sinHalf < 1e-9) {
    // 平行或反平行
    if (nz > 0) { return [1, 0, 0, 0] }
    return [0, 1, 0, 0]
  }
  const angle = Math.atan2(sinHalf, nz)
  const s = Math.sin(angle / 2) / sinHalf
  return [Math.cos(angle / 2), ax * s, ay * s, az * s]
}

/* 自动化/调试入口：挂到 window，方便用 CDP 注入文件做端到端验证。 */
;(globalThis as unknown as { __vfBuildScene?: typeof buildScenePly }).__vfBuildScene = buildScenePly
