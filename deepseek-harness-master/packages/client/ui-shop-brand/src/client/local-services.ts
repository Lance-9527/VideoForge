/**
 * 掌柜 AI · **本地服务地址**（sidecar / 深度模型运行时）。
 *
 * ★ 端口的真相源是**主进程**，不是客户端：
 *   · sidecar 端口跟品牌走（掌柜 AI 8767）—— `VIDEOFORGE_PORT` 对品牌应用无效
 *     （见 `apps/desktop/src/videoforge-sidecar.ts`）。
 *   · 模型运行时端口跟**实际绑定结果**走（默认 8791，被占用时顺延）。
 *   客户端各处硬编码端口 ⇒ 端口一变，电商/资产/splat/命令队列**全线静默失败**
 *   （2026-11-04 实测）。这里统一从主进程问一次，之后各处都从这里取。
 *
 * 普通浏览器（`dsh web`）里没有桌面外壳 ⇒ 落到默认端口。
 *
 * @module
 */
import { setDepthRuntimeBase } from './depth-model.ts'

/** 本地服务地址。 */
export interface LocalServices {
  /** sidecar 基址（形如 `http://127.0.0.1:8767`）。 */
  readonly sidecarBase: string
  /** 深度模型运行时基址（形如 `http://127.0.0.1:8791`）；主进程没起服务时为空串。 */
  readonly depthRuntimeBase: string
}

/** 没有桌面外壳时的默认地址（掌柜 AI 的品牌端口）。 */
const FALLBACK: LocalServices = {
  sidecarBase: 'http://127.0.0.1:8767',
  depthRuntimeBase: 'http://127.0.0.1:8791',
}

/** 桌面外壳暴露的读取器（普通浏览器里没有这个全局）。 */
interface DesktopLocalServicesBridge {
  readonly localServices?: () => Promise<{
    readonly sidecarBase?: string
    readonly depthRuntimeBase?: string
  }>
}

/** 已知的地址（读到之前是默认值）。 */
let current: LocalServices = FALLBACK

/** 读取中的 promise（只问一次）。 */
let pending: Promise<LocalServices> | undefined

/**
 * 当前已知的本地服务地址。
 * @returns 地址（读到主进程的答复之前是默认值）。
 */
export function localServices(): LocalServices {
  return current
}

/**
 * 向主进程问一次本地服务地址。
 *
 * 读到的模型运行时地址会**真的**接到深度模型上（`setDepthRuntimeBase`），
 * 否则端口一变就静默退化成启发式深度。
 *
 * @returns 本地服务地址。
 */
export function loadLocalServices(): Promise<LocalServices> {
  pending ??= readWithRetry()
  return pending
}

/**
 * 读一次（失败或「模型运行时还没起来」时重试）。
 *
 * 主进程可能在窗口与页面就绪的**一瞬**还没准备好，这时读会失败；
 * 模型运行时服务也是异步起的，第一次读可能拿到空地址。
 * 端口读不到就退化成默认端口 = 深度模型永远加载不上，所以这里重试。
 *
 * @returns 本地服务地址。
 */
async function readWithRetry(): Promise<LocalServices> {
  let lastError: unknown
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const answer = await read()
      if (answer.depthRuntimeBase !== '' || attempt === 2) { return answer }
    } catch (error) {
      lastError = error
    }
    await new Promise(resolve => setTimeout(resolve, 800))
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError))
}

/**
 * 归一化地址：去掉尾部斜杠；空串算「没有」。
 * @param value - 主进程给的地址。
 * @returns 归一化后的地址；空串或缺省时是 `undefined`。
 */
function normalize(value: string | undefined): string | undefined {
  if (value === undefined) { return undefined }
  const trimmed = value.trim().replace(/\/+$/, '')
  return trimmed === '' ? undefined : trimmed
}

/**
 * 读一次主进程给的地址。
 * @returns 本地服务地址。
 */
async function read(): Promise<LocalServices> {
  const bridge = (globalThis as typeof globalThis & { dshDesktop?: DesktopLocalServicesBridge }).dshDesktop
  const reader = bridge?.localServices
  if (reader === undefined) {
    // 普通浏览器里没有桌面外壳：默认端口就是最终答案，别让深度模型干等。
    setDepthRuntimeBase(FALLBACK.depthRuntimeBase)
    return FALLBACK
  }
  const answer = await reader()
  const sidecarBase = normalize(answer.sidecarBase) ?? FALLBACK.sidecarBase
  // ★ 空串是有意义的：主进程明确说「模型运行时服务没起来」。
  //   这时不能拿默认端口去撞 —— 用户看到的应该是「服务未启动」，
  //   而不是一句「连不上 8791」。
  const depthRuntimeBase = answer.depthRuntimeBase === undefined
    ? FALLBACK.depthRuntimeBase
    : normalize(answer.depthRuntimeBase) ?? ''
  current = { sidecarBase, depthRuntimeBase }
  setDepthRuntimeBase(depthRuntimeBase)
  return current
}
