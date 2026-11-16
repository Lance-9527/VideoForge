/**
 * 掌柜 AI · **单目深度估计**（Depth Anything V2 Small，浏览器内跑）。
 *
 * ★ 为什么是"多视图 + 模型"这条路（不是直接拿全景图喂模型）：
 *
 *   Depth Anything V2 的技术报告（DSOF）明确指出：**等距圆柱全景图会误导单目深度**——
 *   天空会被判成"近"、地面被判成"远"，结果就是"球被翻过来"。
 *   论文给的解法是：**先把全景图转成透视视图，再逐张预测深度**。
 *   本模块就是这条路的实现 ✓
 *
 * ★ 深度是什么、不是什么：
 *   - Depth Anything V2 输出的是**单目先验的相对深度**（相对逆深度，值越大越近）。
 *     单目深度只有 (scale, shift) 两个自由度，**绝对尺度未知**。
 *   - 本模块**不做逐视图归一化**：每张视图各自 min-max 会把同一张表面在不同视图
 *     放到不同距离，重叠区按构造互相矛盾。归一化交给 `pano-scene.ts`：
 *     收集**所有视图**的原始值，统一求全局 min/max，再用**同一个 (scale, shift)**
 *     归一化。
 *   - 下游的 near/far 是**人为假设**的距离区间，不是测量值。
 *
 * 运行方式：onnxruntime-web（**WASM 后端**，不需要 WebGPU）。
 *  - wasm 与模型文件由**主进程起的本地 HTTP 服务**供给（不塞进 JS bundle，避免 12MB wasm 拖慢启动）。
 *  - 模型权重：Depth Anything V2 Small（int8 量化，26MB）。
 *
 * 拿不到模型/推理失败时，**降级到启发式深度**（透视图上的"细节多=近"），
 * 管线照常跑完，只是几何精度差一些 —— 不会让用户卡住。
 *
 * ★ 失败**不是终态**：退避 15 秒后自动重试；界面通过 `depthRuntimeStatus()`
 *   拿到失败原因并显示「深度模型未加载（原因：…）」，还有 `retryDepthRuntime()`
 *   这个手动重试入口（以前失败一次就永不重试，用户只看到「深度 启发式」）。
 *
 * ★ 推理服务的地址由**主进程**告知（`setDepthRuntimeBase`）：端口被占用时
 *   主进程会顺延，客户端必须跟着走，否则深度模型永远加载不上。
 *
 * @module
 */

/**
 * 深度图（**未归一化**的原始深度值）。
 *
 * 约定：**值越大 = 越近**（DAv2 的输出是相对逆深度）。
 * 数值只在同一批视图之间可比；绝对尺度未知。
 */
export interface DepthMap {
  readonly width: number
  readonly height: number
  /** 原始深度值（未归一化）：越大越近。 */
  readonly data: Float32Array
  /** 该视图内的最小原始值。 */
  readonly min: number
  /** 该视图内的最大原始值。 */
  readonly max: number
}

/** 深度来源（给 UI 用的诚实标签）。 */
export type DepthProvider = 'depth-anything-v2' | 'heuristic'

/** 推理服务的默认地址（主进程还没告知真实端口时用）。 */
const DEFAULT_RUNTIME_BASE = 'http://127.0.0.1:8791'

/** 推理服务的地址（主进程起的本地静态服务）。 */
let runtimeBase = DEFAULT_RUNTIME_BASE
/** 主进程是否已经告知过真实地址。 */
let runtimeBaseResolved = false
/** 等地址的等待者（主进程答复后兑现）。 */
let runtimeBaseWaiters: ((base: string) => void)[] = []

/** onnxruntime-web 模块（懒加载，失败也不影响别的功能）。 */
let ortModule: unknown = null
let session: unknown = null
/** 上次失败的原因（人类可读）；`undefined` 表示当前没有失败。 */
let failureReason: string | undefined
/** 上次失败发生在哪一步。 */
let failureStage: 'load' | 'inference' | undefined
/** 允许重试的时刻（毫秒时间戳）。 */
let retryAfter = 0

/** 模型输入尺寸（DAv2 标准预处理就是方形 518×518）。 */
const MODEL_SIZE = 518

/** 失败后多久允许再试一次（毫秒）—— 失败不是终态，隔一会儿自己再试。 */
const RETRY_DELAY_MS = 15_000

/**
 * 设置推理服务地址（主进程告知的真实端口）。
 *
 * ★ 这个函数以前**全仓库没人调用**，端口一变就静默降级成启发式深度，
 *   用户只看到「深度 启发式」四个字（2026-11-04 实测）。
 *   现在由 `local-services.ts` 在读到主进程的答复后调用。
 *
 * @param base - 形如 `http://127.0.0.1:8791`；空串表示「运行时服务没起来」。
 */
export function setDepthRuntimeBase(base: string): void {
  const next = base.trim().replace(/\/+$/, '')
  runtimeBaseResolved = true
  if (next !== runtimeBase) {
    runtimeBase = next
    // 端口变了：旧会话指向旧端口，作废重来。
    session = null
    ortModule = null
    failureReason = undefined
    failureStage = undefined
    retryAfter = 0
  }
  const waiters = runtimeBaseWaiters
  runtimeBaseWaiters = []
  for (const waiter of waiters) { waiter(runtimeBase) }
}

/**
 * 等主进程告知真实端口（超时后用当前地址继续）。
 *
 * @param timeoutMs - 最长等待时间。
 * @returns 推理服务地址。
 */
export function resolveDepthRuntimeBase(timeoutMs = 5_000): Promise<string> {
  if (runtimeBaseResolved) { return Promise.resolve(runtimeBase) }
  return new Promise<string>((resolve) => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const done = (base: string): void => {
      if (timer !== undefined) { clearTimeout(timer) }
      runtimeBaseWaiters = runtimeBaseWaiters.filter((waiter) => waiter !== done)
      resolve(base)
    }
    timer = setTimeout(() => { done(runtimeBase) }, timeoutMs)
    runtimeBaseWaiters.push(done)
  })
}

/**
 * 深度运行时的当前状态（给界面看）。
 */
export interface DepthRuntimeStatus {
  /** 模型会话是否已经建好。 */
  readonly loaded: boolean
  /** 当前推理服务地址。 */
  readonly base: string
  /** 上次失败的原因（人类可读）；没有失败时没有这一项。 */
  readonly failure?: string
  /** 上次失败发生在哪一步。 */
  readonly stage?: 'load' | 'inference'
}

/**
 * 深度运行时的当前状态。
 *
 * @returns 状态（界面用它说明「为什么在用启发式深度」）。
 */
export function depthRuntimeStatus(): DepthRuntimeStatus {
  return {
    loaded: session !== null,
    base: runtimeBase,
    ...(failureReason === undefined ? {} : { failure: failureReason }),
    ...(failureStage === undefined ? {} : { stage: failureStage }),
  }
}

/**
 * 立刻允许再试一次（清掉失败状态）。
 *
 * 失败不是终态：服务可能刚起来、端口可能刚腾出来。
 */
export function retryDepthRuntime(): void {
  failureReason = undefined
  failureStage = undefined
  retryAfter = 0
}

/**
 * 记一次失败：写清原因，并安排一次自动重试。
 * @param error - 失败原因。
 * @param stage - 失败发生在哪一步。
 */
function recordFailure(error: unknown, stage: 'load' | 'inference'): void {
  failureReason = error instanceof Error ? error.message : String(error)
  failureStage = stage
  retryAfter = Date.now() + RETRY_DELAY_MS
  console.warn(`[掌柜 AI] 深度模型${stage === 'load' ? '加载' : '推理'}失败（${RETRY_DELAY_MS / 1000}s 后自动重试），当前用启发式深度：`, error)
}

/**
 * 懒加载 onnxruntime-web 并建会话。
 * @returns true 表示可用。
 */
async function ensureSession(): Promise<boolean> {
  if (session !== null) { return true }
  // ★ 失败不是终态：退避窗口过了就再试一次（以前是一失败就永不重试）。
  if (failureReason !== undefined && Date.now() < retryAfter) { return false }
  try {
    const base = await resolveDepthRuntimeBase()
    if (base === '') { throw new Error('模型运行时服务未启动') }
    // ★ 运行时从本地服务加载，**不打包** onnxruntime-web：
    //   DSH 的 client 加载器只认 `client.js`，把外部包当成独立 bundle 去加载会
    //   报 "client.ort.wasm.bundle.min.js failed to load"（2026-11-04 实测）。
    //   所以这里用变量拼 URL，让打包器无从解析。
    const bundleUrl = `${base}/ort/ort.wasm.bundle.min.mjs`
    const ort = await import(/* @vite-ignore */ bundleUrl) as unknown as {
      env: { wasm: { wasmPaths?: string | { wasm?: string; mjs?: string }; numThreads?: number } }
      InferenceSession: {
        create(path: string, options?: Record<string, unknown>): Promise<{
          run(feeds: Record<string, unknown>): Promise<Array<{ data: Float32Array; dims: readonly number[] }>>
        }>
      }
    }
    const runtime = ort.env.wasm
    runtime.wasmPaths = `${base}/ort/`
    // ★ 单线程：多线程版要用 SharedArrayBuffer，那要求页面本身有 COOP/COEP 头，
    //   而 DSH 的页面不是这样加载的（2026-11-04 实测）。
    runtime.numThreads = 1
    const created = await ort.InferenceSession.create(
      `${base}/models/depth-anything-v2-small/model_quantized.onnx`,
      { executionProviders: ['wasm'], graphOptimizationLevel: 'all' },
    )
    session = created
    ortModule = ort
    failureReason = undefined
    failureStage = undefined
    retryAfter = 0
    return true
  } catch (error) {
    recordFailure(error, 'load')
    return false
  }
}

/**
 * 预处理：RGB → NCHW float32（ImageNet 归一化，DAv2 用同一套）。
 * @param rgb - 输入像素（长宽 × 3，0..255）。
 * @param width - 宽。
 * @param height - 高。
 * @returns NCHW 张量数据。
 */
function toTensor(rgb: Float32Array, width: number, height: number): Float32Array {
  const mean = [0.485, 0.456, 0.406]
  const std = [0.229, 0.224, 0.225]
  const plane = MODEL_SIZE * MODEL_SIZE
  const out = new Float32Array(3 * plane)
  for (let y = 0; y < MODEL_SIZE; y += 1) {
    // 简单双线性缩放（从 width×height 到 518×518）
    const sy = Math.min(height - 1, Math.max(0, ((y + 0.5) * height) / MODEL_SIZE - 0.5))
    const y0 = Math.floor(sy)
    const y1 = Math.min(height - 1, y0 + 1)
    const fy = sy - y0
    for (let x = 0; x < MODEL_SIZE; x += 1) {
      const sx = Math.min(width - 1, Math.max(0, ((x + 0.5) * width) / MODEL_SIZE - 0.5))
      const x0 = Math.floor(sx)
      const x1 = Math.min(width - 1, x0 + 1)
      const fx = sx - x0
      const target = y * MODEL_SIZE + x
      for (let c = 0; c < 3; c += 1) {
        const p00 = rgb[(y0 * width + x0) * 3 + c] ?? 0
        const p01 = rgb[(y0 * width + x1) * 3 + c] ?? 0
        const p10 = rgb[(y1 * width + x0) * 3 + c] ?? 0
        const p11 = rgb[(y1 * width + x1) * 3 + c] ?? 0
        const top = p00 * (1 - fx) + p01 * fx
        const bottom = p10 * (1 - fx) + p11 * fx
        const value = top * (1 - fy) + bottom * fy
        out[c * plane + target] = (value / 255 - mean[c]!) / std[c]!
      }
    }
  }
  return out
}

/**
 * 启发式深度（模型不可用时的降级）：透视图上"细节多=近，平滑=远"。
 *
 * 它是**近度代理**，不是深度测量；数值仍是相对量。
 *
 * @param rgb - 像素（长宽 × 3，0..255）。
 * @param width - 宽。
 * @param height - 高。
 * @returns 深度图（未归一化的原始值 + 该视图的 min/max）。
 */
export function heuristicDepthMap(rgb: Float32Array, width: number, height: number): DepthMap {
  const lum = new Float32Array(width * height)
  for (let index = 0; index < width * height; index += 1) {
    lum[index] = ((rgb[index * 3] ?? 0) + (rgb[index * 3 + 1] ?? 0) + (rgb[index * 3 + 2] ?? 0)) / 3
  }
  const data = new Float32Array(width * height)
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x
        const gx = x > 0 ? Math.abs((lum[index] ?? 0) - (lum[index - 1] ?? 0)) : 0
        const gy = y > 0 ? Math.abs((lum[index] ?? 0) - (lum[index - width] ?? 0)) : 0
      const detail = Math.min(1, Math.sqrt(gx * gx + gy * gy) * 12)
      data[index] = detail
      if (detail < min) { min = detail }
      if (detail > max) { max = detail }
    }
  }
  return { width, height, data, min, max }
}

/**
 * 估计一张图的深度。
 *
 * 返回的是**未归一化的原始深度值**（越大越近）与该视图的 min/max：
 * 逐视图归一化会把同一张表面在不同视图放到不同距离，
 * 跨视图统一归一化由调用方（`pano-scene.ts`）用**同一个 (scale, shift)** 完成。
 *
 * @param rgb - 像素（长宽 × 3，0..255）。
 * @param width - 宽。
 * @param height - 高。
 * @returns 深度图（原始值 + min/max）+ 用了哪个来源。
 */
export async function estimateDepth(
  rgb: Float32Array,
  width: number,
  height: number,
): Promise<{ readonly map: DepthMap; readonly provider: DepthProvider }> {
  if (!(await ensureSession())) {
    return { map: heuristicDepthMap(rgb, width, height), provider: 'heuristic' }
  }
  try {
    const runtime = ortModule as {
      InferenceSession: {
        run: (feeds: Record<string, unknown>) => Promise<Array<{ data: Float32Array; dims: readonly number[] }>>
      }
    }
    const tensor = toTensor(rgb, width, height)
    const feeds = { pixel_values: { data: tensor, dims: [1, 3, MODEL_SIZE, MODEL_SIZE], type: 'float32' } }
    const outputs = await runtime.InferenceSession.run(feeds)
    const raw = outputs[0]
    if (raw === undefined) { throw new Error('模型没有输出') }
    const [, rows, cols] = raw.dims
    const rowCount = rows ?? 1
    const colCount = cols ?? 1
    // ★ 保留**原始数值**（相对逆深度，越大越近），只做尺寸缩放，不做逐视图归一化。
    const data = new Float32Array(width * height)
    for (let y = 0; y < height; y += 1) {
      const sy = Math.min(rowCount - 1, Math.max(0, ((y + 0.5) * rowCount) / height - 0.5))
      const y0 = Math.floor(sy)
      const y1 = Math.min(rowCount - 1, y0 + 1)
      const fy = sy - y0
      for (let x = 0; x < width; x += 1) {
        const sx = Math.min(colCount - 1, Math.max(0, ((x + 0.5) * colCount) / width - 0.5))
        const x0 = Math.floor(sx)
        const x1 = Math.min(colCount - 1, x0 + 1)
        const fx = sx - x0
        const v00 = raw.data[y0 * colCount + x0] ?? 0
        const v01 = raw.data[y0 * colCount + x1] ?? 0
        const v10 = raw.data[y1 * colCount + x0] ?? 0
        const v11 = raw.data[y1 * colCount + x1] ?? 0
        const top = v00 * (1 - fx) + v01 * fx
        const bottom = v10 * (1 - fx) + v11 * fx
        data[y * width + x] = top * (1 - fy) + bottom * fy
      }
    }
    let min = Number.POSITIVE_INFINITY
    let max = Number.NEGATIVE_INFINITY
    for (let index = 0; index < data.length; index += 1) {
      const value = data[index] ?? 0
      if (value < min) { min = value }
      if (value > max) { max = value }
    }
    return { map: { width, height, data, min, max }, provider: 'depth-anything-v2' }
  } catch (error) {
    // ★ 推理失败也要能恢复：记下原因、丢掉会话、退避后自动重建。
    recordFailure(error, 'inference')
    session = null
    ortModule = null
    return { map: heuristicDepthMap(rgb, width, height), provider: 'heuristic' }
  }
}
