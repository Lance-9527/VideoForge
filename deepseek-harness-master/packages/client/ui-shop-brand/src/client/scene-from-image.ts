/**
 * 图片 → 可拆解的 3D 场景（LDI 分层深度图），**在浏览器里算**。
 *
 * ## 为什么在客户端算
 * 后端（Python sidecar）是 PyInstaller 打好的二进制，改 `backend/*.py` 不会生效
 * （实测：加一个 marker 重启后读不到）。而这条链路要的只是**纯几何计算**，
 * 用 typed array 在浏览器里做几百毫秒就完事，产物直接走**已有**的
 * `POST /api/studio/splat/exports`（base64 落盘）→ 再用路径式 URL 给 SuperSplat 导入。
 *
 * ## 算法（与 Python 版 img2scene_v6 同一套）
 * 1. 明度低 / 饱和高 / 细节多 ⇒ 判断为「近」。
 * 2. 深度 d ∈ [D·0.45, D]，按针孔投影摆位：X = x0·(d/D)，Y = y0·(d/D)，z = D−d。
 *    ⇒ 从原机位 (0,0,D) 看，所有片叠起来就是原图；换机位 ⇒ 视差。
 * 3. 输出 splat 规范的 binary PLY（f_dc_* / opacity / scale_* / rot_*）。
 *
 * ## 2026-11-03 增强（用《清明上河图》长卷实测后补的）
 * - **保持宽高比**：老版 `resize((size,size))` 会把 750×2264 的竖长卷压成正方形。
 *   现在按**长边**定尺寸、短边按比例。
 * - **按深度分层导出多份 PLY**：每层是独立资产，可单独摆放/复用（"里面的物体都能复用"）。
 * - **原图一起保存**：图片本身作为资产入库，方便回到 2D 重新处理。
 *
 * @module
 */

const SH_C0 = 0.28209479177387814
const D_CAM = 6
const HALF_W = 3
const NEAR_K = 0.45
const SIGMA_K = 1.7

/** 沿一条轴做盒式模糊（cumsum，边缘复制）。 */
function blurAxis(source: Float32Array, width: number, height: number, radius: number, axis: 'x' | 'y'): Float32Array {
  if (radius <= 0) return source.slice()
  const out = new Float32Array(source.length)
  const length = axis === 'y' ? height : width
  const lines = axis === 'y' ? width : height
  for (let line = 0; line < lines; line += 1) {
    // 前缀和
    const prefix = new Float64Array(length + 1)
    for (let i = 0; i < length; i += 1) {
      const index = axis === 'y' ? i * width + line : line * width + i
      prefix[i + 1] = (prefix[i] ?? 0) + (source[index] ?? 0)
    }
    for (let i = 0; i < length; i += 1) {
      const lo = Math.max(0, i - radius)
      const hi = Math.min(length - 1, i + radius)
      const count = hi - lo + 1
      const index = axis === 'y' ? i * width + line : line * width + i
      out[index] = ((prefix[hi + 1] ?? 0) - (prefix[lo] ?? 0)) / count
    }
  }
  return out
}

function blur2d(source: Float32Array, width: number, height: number, radius: number): Float32Array {
  return blurAxis(blurAxis(source, width, height, radius, 'x'), width, height, radius, 'y')
}

function normalize(values: Float32Array): Float32Array {
  let low = Infinity
  let high = -Infinity
  for (const value of values) { if (value < low) low = value; if (value > high) high = value }
  const span = Math.max(1e-6, high - low)
  const out = new Float32Array(values.length)
  for (let i = 0; i < values.length; i += 1) out[i] = ((values[i] ?? 0) - low) / span
  return out
}

/**
 * 把图片画到 canvas 上并取像素。
 * ★ **保持宽高比**：长边定到 size，短边按比例。
 *   老版是 size×size 正方形，竖长卷（《清明上河图》750×2264）会被压扁成方的。
 */
async function pixelsOf(
  file: Blob,
  size: number,
  wrap: 'long' | 'short' = 'long',
): Promise<{ data: Uint8ClampedArray; width: number; height: number }> {
  const bitmap = await createImageBitmap(file)
  // ★ 绕圈方向：**长边绕一圈**。
  //   等距柱状全景（2:1）本来就是长边横向；竖图（长卷）也该把长边绕圈，
  //   否则短边（如 750px）绕 360° ⇒ 每像素 0.48°，放大后必糊；
  //   长边绕圈能拿到 3 倍角分辨率（2264px ⇒ 0.16°/像素）。
  const swap = wrap === 'long' ? bitmap.height > bitmap.width : bitmap.width > bitmap.height
  const srcW = swap ? bitmap.height : bitmap.width
  const srcH = swap ? bitmap.width : bitmap.height
  const longest = Math.max(srcW, srcH)
  const factor = size / longest
  const width = Math.max(8, Math.round(srcW * factor))
  const height = Math.max(8, Math.round(srcH * factor))
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (context === null) throw new Error('浏览器不支持 2D canvas')
  context.save()
  if (swap) {
    // 竖图转横：长边成为水平方向（围绕柱面）
    context.translate(width, 0)
    context.rotate(Math.PI / 2)
    context.drawImage(bitmap, 0, 0, height, width)
  } else {
    context.drawImage(bitmap, 0, 0, width, height)
  }
  context.restore()
  bitmap.close()
  return { data: context.getImageData(0, 0, width, height).data, width, height }
}

/**
 * 生成 LDI splat 的二进制 PLY。
 *
 * @param file - 场景照片。
 * @param size - 采样边长（512 ⇒ 262144 点，约 14MB）。
 * @returns PLY 字节 + 点数 + 「还原原图」的机位。
 */
export async function buildLdiPly(file: Blob, size = 512): Promise<{
  readonly bytes: Uint8Array
  readonly points: number
  readonly camera: { readonly position: readonly number[]; readonly target: readonly number[]; readonly fov: number }
  /** 每个像素的深度（0=远 … 1=近），用于按层拆分 */
  readonly depth: Float32Array
  /** 采样后的宽高（保持原图比例） */
  readonly width: number
  readonly height: number
}> {
  const { data, width, height } = await pixelsOf(file, size)
  const count = width * height
  const lum = new Float32Array(count)
  const sat = new Float32Array(count)
  const detail = new Float32Array(count)

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = (y * width + x) * 4
      const r = (data[index] ?? 0) / 255
      const g = (data[index + 1] ?? 0) / 255
      const b = (data[index + 2] ?? 0) / 255
      lum[y * width + x] = (r + g + b) / 3
      sat[y * width + x] = Math.max(r, g, b) - Math.min(r, g, b)
      const dx = x > 0 ? Math.abs((lum[y * width + x] ?? 0) - (lum[y * width + x - 1] ?? 0)) : 0
      const dy = y > 0 ? Math.abs((lum[y * width + x] ?? 0) - (lum[(y - 1) * width + x] ?? 0)) : 0
      detail[y * width + x] = Math.sqrt(dx * dx + dy * dy)
    }
  }

  // 行均值的低频项（大块天空/地面要更远）
  const rowLum = new Float32Array(count)
  const rowSat = new Float32Array(count)
  const rowDetail = new Float32Array(count)
  for (let y = 0; y < height; y += 1) {
    let sumLum = 0
    let sumSat = 0
    let sumDetail = 0
    for (let x = 0; x < width; x += 1) {
      sumLum += lum[y * width + x] ?? 0
      sumSat += sat[y * width + x] ?? 0
      sumDetail += detail[y * width + x] ?? 0
    }
    const avgLum = sumLum / width
    const avgSat = sumSat / width
    const avgDetail = sumDetail / width
    for (let x = 0; x < width; x += 1) {
      rowLum[y * width + x] = avgLum
      rowSat[y * width + x] = avgSat
      rowDetail[y * width + x] = avgDetail
    }
  }
  const smoothLum = blurAxis(rowLum, width, height, 12, 'y')
  const smoothSat = blurAxis(rowSat, width, height, 12, 'y')
  const smoothDetail = blurAxis(rowDetail, width, height, 12, 'y')

  const nLum = normalize(lum)
  const nSat = normalize(sat)
  const nDetail = normalize(detail)
  const nRowLum = normalize(smoothLum)
  const nRowSat = normalize(smoothSat)
  const nRowDetail = normalize(smoothDetail)

  const raw = new Float32Array(count)
  for (let i = 0; i < count; i += 1) {
    raw[i] = 0.34 * (1 - (nLum[i] ?? 0))
      + 0.18 * (nSat[i] ?? 0)
      + 0.26 * (nDetail[i] ?? 0)
      + 0.12 * (1 - (nRowLum[i] ?? 0))
      + 0.06 * (1 - (nRowSat[i] ?? 0))
      + 0.04 * (1 - (nRowDetail[i] ?? 0))
  }
  const depth = normalize(blur2d(raw, width, height, 5))

  const scale = (2 * HALF_W) / Math.max(width, height)
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
  let offset = 0
  const half = width / 2
  const halfHeight = height / 2
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x
      const d = D_CAM * (1 - (depth[index] ?? 0) * (1 - NEAR_K))
      const k = d / D_CAM
      const px = (x - half) * scale * k
      // ★ y 轴朝上：图片顶部（y=0）对应场景上方，所以要反号
      const py = -(y - halfHeight) * scale * k
      const pz = D_CAM - d
      const pixel = index * 4
      const r = (data[pixel] ?? 0) / 255
      const g = (data[pixel + 1] ?? 0) / 255
      const b = (data[pixel + 2] ?? 0) / 255
      const sigma = Math.log(Math.max(1e-6, scale * k * SIGMA_K))

      view.setFloat32(offset, px, true); offset += 4
      view.setFloat32(offset, py, true); offset += 4
      view.setFloat32(offset, pz, true); offset += 4
      view.setFloat32(offset, (r - 0.5) / SH_C0, true); offset += 4
      view.setFloat32(offset, (g - 0.5) / SH_C0, true); offset += 4
      view.setFloat32(offset, (b - 0.5) / SH_C0, true); offset += 4
      view.setFloat32(offset, 4.59511985013459, true); offset += 4
      view.setFloat32(offset, sigma, true); offset += 4
      view.setFloat32(offset, sigma, true); offset += 4
      view.setFloat32(offset, sigma, true); offset += 4
      view.setFloat32(offset, 1, true); offset += 4
      view.setFloat32(offset, 0, true); offset += 4
      view.setFloat32(offset, 0, true); offset += 4
      view.setFloat32(offset, 0, true); offset += 4
    }
  }

  const fov = 2 * (Math.atan(((Math.max(width, height) / 2) * scale) / D_CAM) * 180) / Math.PI
  return {
    bytes,
    points: count,
    camera: { position: [0, 0, D_CAM], target: [0, 0, 0], fov },
    depth,
    width,
    height,
  }
}

/**
 * 把一个 LDI 结果按深度切成若干层，每层单独导出成 PLY 字节。
 * 每层就是一个可复用的"物件"（远景/中景/近景…），可单独摆放、显隐、改名。
 */
export function splitLdiLayers(
  built: { readonly bytes: Uint8Array; readonly depth: Float32Array; readonly width: number; readonly height: number },
  layers: number,
): { readonly bytes: Uint8Array; readonly points: number }[] {
  const { width, height, depth } = built
  const count = width * height
  const stride = 14 * 4
  // 头部是 ASCII，直到 end_header\n
  const headerEnd = (() => {
    const probe = new TextEncoder().encode('end_header\n')
    for (let i = 0; i + probe.length <= built.bytes.length; i += 1) {
      let hit = true
      for (let j = 0; j < probe.length; j += 1) {
        if (built.bytes[i + j] !== probe[j]) { hit = false; break }
      }
      if (hit) { return i + probe.length }
    }
    return 0
  })()
  const body = built.bytes.subarray(headerEnd)
  const out: { readonly bytes: Uint8Array; readonly points: number }[] = []
  for (let layer = 0; layer < layers; layer += 1) {
    const low = layer / layers
    const high = (layer + 1) / layers
    const picked: number[] = []
    for (let index = 0; index < count; index += 1) {
      const d = depth[index] ?? 0
      const inRange = layer === layers - 1 ? (d >= low && d <= high) : (d >= low && d < high)
      if (inRange) { picked.push(index) }
    }
    if (picked.length === 0) { continue }
    const header = [
      'ply',
      'format binary_little_endian 1.0',
      `element vertex ${picked.length}`,
      'property float x', 'property float y', 'property float z',
      'property float f_dc_0', 'property float f_dc_1', 'property float f_dc_2',
      'property float opacity',
      'property float scale_0', 'property float scale_1', 'property float scale_2',
      'property float rot_0', 'property float rot_1', 'property float rot_2', 'property float rot_3',
      'end_header',
      '',
    ].join('\n')
    const headerBytes = new TextEncoder().encode(header)
    const bytes = new Uint8Array(headerBytes.length + picked.length * stride)
    bytes.set(headerBytes, 0)
    let offset = headerBytes.length
    for (const index of picked) {
      const source = index * stride
      bytes.set(body.subarray(source, source + stride), offset)
      offset += stride
    }
    out.push({ bytes, points: picked.length })
  }
  return out
}

/* ── base64 编码在 `base64.ts`（上传大文件时用）── */


/* ══════════════════════════════════════════════════════════════════════
 * 360° 全景场景（圆柱内壁 + 地面）—— 用户要的"身临其境、360° 无死角、可漫游"
 *
 * ## 为什么不是 LDI 分层深度
 * LDI 是"片"：从侧面看是散块，能拆件但不身临其境。
 * 全景是把长卷绕成 360° 的柱面内壁：你站在圆心，往任何方向看都是连续的画，
 * 走动时有视差 ⇒ 真的可以漫游。
 *
 * ## 几何
 * - 横向 → 方位角 0°…360°（绕一圈）
 * - 纵向 → 高度，**各向同性**（1 像素 = 弧长/像素，比例不失真）
 * - 每个像素 → 一个朝向圆心的 splat（billboard 法线指向圆心）
 * - 另加一个地面圆盘（取图片底部颜色），脚下有地 ⇒ 身临其境
 * ══════════════════════════════════════════════════════════════════════ */

const PANO_RADIUS = 8
const PANO_EYE = 1.6
const PANO_SPLAT_SCALE = 0.75

/** 把 (0,0,1) 旋到指定方向的四元数（w,x,y,z）；θ=0 附近要特判 180°。 */
function quatToward(dx: number, dy: number, dz: number): [number, number, number, number] {
  const length = Math.hypot(dx, dy, dz) || 1
  const nx = dx / length
  const nz = dz / length
  const dot = nz
  if (Math.abs(nx) < 1e-6) {
    // 正好 180°（θ=0）或 0°（θ=π）：axis 退化
    if (nz < 0) { return [0, 1, 0, 0] }
    return [1, 0, 0, 0]
  }
  let qw = 1 + dot
  let qx = 0
  let qy = nx
  let qz = 0
  const norm = Math.hypot(qw, qx, qy, qz) || 1
  qw /= norm; qx /= norm; qy /= norm; qz /= norm
  return [qw, qx, qy, qz]
}

/**
 * 图片 → 360° 全景高斯泼溅场景（binary PLY）。
 *
 * @param file - 场景照片（长卷最合适：横向绕一圈）。
 * @param size - 图片长边像素（1024 ⇒ 约 35 万 splat；2048 ⇒ 约 140 万）。
 * @param radius - 柱面半径（米），越大越像"环幕"，越小越"贴脸"。
 * @returns PLY 字节 + 点数 + 相机初始机位（站在圆心）。
 */
/**
 * @param size - 图片长边像素。
 *   ★ 清晰度的**物理上限**由原图横向像素决定：横向 W 像素绕 360° ⇒ 每像素 360/W 度。
 *   750 宽的原图 ⇒ 0.48°/像素 ⇒ 在 45° 视口里每像素约 5 个屏幕像素（必然发软）。
 *   想更清晰就**给更高分辨率的原图**（3000px 宽以上效果会明显好转）。
 */
export async function buildPanoPly(
  file: Blob,
  size = 2048,
  radius = PANO_RADIUS,
  wrap: 'long' | 'short' = 'long',
): Promise<{
  readonly bytes: Uint8Array
  readonly points: number
  readonly camera: { readonly position: readonly number[]; readonly target: readonly number[]; readonly fov: number }
  readonly radius: number
  readonly eyeHeight: number
}> {
  const { data, width, height } = await pixelsOf(file, size, wrap)
  const count = width * height
  const arc = (2 * Math.PI * radius) / width

  // ★ 颜色预平滑（3×3 高斯）：每 splat 只有一个纯色，放大后会看到明显色块。
  //   预先把邻域颜色混进来 = 在 splat 域做一次低通，肉眼就连续了。
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

  // 地面颜色：图片最下面一条的平均色（用平滑后的）
  let groundR = 0.45, groundG = 0.42, groundB = 0.34
  {
    const bandStart = Math.max(0, height - Math.max(1, Math.floor(height / 40)))
    let sumR = 0, sumG = 0, sumB = 0, count2 = 0
    for (let y = bandStart; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const index = (y * width + x) * 3
        sumR += rgb[index] ?? 0
        sumG += rgb[index + 1] ?? 0
        sumB += rgb[index + 2] ?? 0
        count2 += 1
      }
    }
    if (count2 > 0) { groundR = sumR / count2; groundG = sumG / count2; groundB = sumB / count2 }
  }

  // ★ 位置抖动：规则点阵在放大时非常刺眼，加随机偏移就不规则了（确定性 PRNG）
  const jitterX = new Float32Array(count)
  const jitterY = new Float32Array(count)
  {
    let seed = 0x9e3779b9
    for (let index = 0; index < count; index += 1) {
      seed = (seed * 1664525 + 1013904223) >>> 0
      jitterX[index] = ((seed >>> 8) / 16777216 - 0.5) * arc * 0.9
      seed = (seed * 1664525 + 1013904223) >>> 0
      jitterY[index] = ((seed >>> 8) / 16777216 - 0.5) * arc * 0.9
    }
  }

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
  const halfHeight = height / 2
  let offset = 0
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x
      const r = rgb[index * 3] ?? 0
      const g = rgb[index * 3 + 1] ?? 0
      const b = rgb[index * 3 + 2] ?? 0

      const theta = ((x + 0.5 + (jitterX[index] ?? 0)) / width) * Math.PI * 2 - Math.PI
      const sinT = Math.sin(theta)
      const cosT = Math.cos(theta)
      const px = radius * sinT
      const pz = radius * cosT
      const py = PANO_EYE + (halfHeight - (y + 0.5 + (jitterY[index] ?? 0))) * arc

      // 朝向圆心（billboard 法线 = 局部 z 轴）
      const [qw, qx, qy, qz] = quatToward(-sinT, 0, -cosT)
      const logScale = Math.log(Math.max(1e-6, arc * PANO_SPLAT_SCALE))
      const logThin = Math.log(Math.max(1e-6, arc * 0.12))

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

  // 地面圆盘（14 圈 × 96 段）
  const rings = 14
  const segments = 96
  const groundCount = rings * segments
  const groundBytes = new Uint8Array(groundCount * stride)
  const groundView = new DataView(groundBytes.buffer)
  let groundOffset = 0
  const groundSize = Math.log(Math.max(1e-6, arc * 0.9))
  for (let ring = 1; ring <= rings; ring += 1) {
    const rr = radius * (ring / rings)
    for (let segment = 0; segment < segments; segment += 1) {
      const angle = (segment / segments) * Math.PI * 2
      groundView.setFloat32(groundOffset, rr * Math.sin(angle), true); groundOffset += 4
      groundView.setFloat32(groundOffset, 0, true); groundOffset += 4
      groundView.setFloat32(groundOffset, rr * Math.cos(angle), true); groundOffset += 4
      groundView.setFloat32(groundOffset, (groundR - 0.5) / SH_C0, true); groundOffset += 4
      groundView.setFloat32(groundOffset, (groundG - 0.5) / SH_C0, true); groundOffset += 4
      groundView.setFloat32(groundOffset, (groundB - 0.5) / SH_C0, true); groundOffset += 4
      groundView.setFloat32(groundOffset, 4.59511985013459, true); groundOffset += 4
      groundView.setFloat32(groundOffset, groundSize, true); groundOffset += 4
      groundView.setFloat32(groundOffset, groundSize, true); groundOffset += 4
      groundView.setFloat32(groundOffset, groundSize, true); groundOffset += 4
      groundView.setFloat32(groundOffset, 1, true); groundOffset += 4
      groundView.setFloat32(groundOffset, 0, true); groundOffset += 4
      groundView.setFloat32(groundOffset, 0, true); groundOffset += 4
      groundView.setFloat32(groundOffset, 0, true); groundOffset += 4
    }
  }

  const merged = new Uint8Array(headerBytes.length + (count + groundCount) * stride)
  merged.set(headerBytes, 0)
  merged.set(bytes.subarray(headerBytes.length), headerBytes.length)
  merged.set(groundBytes, headerBytes.length + count * stride)

  return {
    bytes: merged,
    points: count + groundCount,
    camera: { position: [0, PANO_EYE, 0], target: [0, PANO_EYE, -radius], fov: 60 },
    radius,
    eyeHeight: PANO_EYE,
  }
}

/* 自动化/调试入口：把两个生成器挂到 window，
 * 这样可以用 CDP 注入文件做端到端验证（原生文件对话框会阻塞页面，点按钮测不了）。
 * 也方便以后接 Agent/MCP 直接调用。 */
;(globalThis as unknown as {
  __vfBuildPano?: typeof buildPanoPly
  __vfBuildLdi?: typeof buildLdiPly
}).__vfBuildPano = buildPanoPly
;(globalThis as unknown as {
  __vfBuildLdi?: typeof buildLdiPly
}).__vfBuildLdi = buildLdiPly
