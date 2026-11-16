/**
 * 掌柜 AI · **模型运行时服务**（本地静态服务）。
 *
 * ★ 为什么需要它：Depth Anything V2 要在**渲染器里**跑（onnxruntime-web，WASM 后端），
 *   但两样东西不适合塞进 JS bundle：
 *     · ort-wasm-simd-threaded.wasm ≈ 12 MB（进 bundle 会拖慢启动）
 *     · model_quantized.onnx ≈ 26 MB
 *   所以主进程起一个**只绑 127.0.0.1**的静态服务，把 resources 里的
 *   `models/` 与 `ort/` 目录伺服给渲染器。
 *
 * 安全：只监听回环地址；路径做规范化并校验前缀，防止目录穿越。
 *
 * @module
 */
import { createServer } from 'node:http'
import type { Server } from 'node:http'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { join, normalize, sep } from 'node:path'

/** MIME 表（只列用得到的）。 */
const MIME: Record<string, string> = {
  '.wasm': 'application/wasm',
  '.mjs': 'text/javascript',
  '.js': 'text/javascript',
  '.onnx': 'application/octet-stream',
  '.json': 'application/json',
  '.bin': 'application/octet-stream',
}

/** 模型运行时服务的默认端口。 */
export const MODEL_RUNTIME_PORT = 8791

/** 端口顺延的上限（8791..8802）。 */
const PORT_ATTEMPTS = 12

/** 服务要伺服的两个资源目录。 */
const SERVED_ROOTS = ['models', 'ort'] as const

/** 服务句柄。 */
export interface ModelServer {
  readonly port: number
  close(): void
}

/**
 * 起模型运行时服务。
 *
 * ★ 端口会被**如实告诉渲染器**（`DESKTOP_IPC.localServices`）：端口被占用时
 *   这里顺延到下一个可用端口，客户端必须知道真实端口，否则会静默退化成
 *   启发式深度（2026-11-04 实测踩到）。
 *
 * @param resourcesRoot - resources 目录（含 models/ 与 ort/）。
 * @param preferredPort - 期望端口（被占用就顺延）。
 * @returns 服务句柄。
 */
export async function startModelServer(resourcesRoot: string, preferredPort = MODEL_RUNTIME_PORT): Promise<ModelServer> {
  const missing = SERVED_ROOTS.filter((name) => !existsSync(join(resourcesRoot, name)))
  if (missing.length > 0) {
    // ★ 资源不齐就别起服务：起一个空服务只会让客户端报一句看不懂的
    //   「模型加载失败」，而真正的原因在这里。
    throw new Error(`模型运行时资源缺失：${missing.map((name) => join(resourcesRoot, name)).join('、')}`)
  }
  const roots = SERVED_ROOTS.map((name) => join(resourcesRoot, name))

  const server: Server = createServer((request, response) => {
    const url = new URL(request.url ?? '/', 'http://127.0.0.1')
    if (request.method === 'OPTIONS') {
      response.writeHead(204, {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Max-Age': '86400',
      }).end()
      return
    }
    // 形如 /models/xxx 或 /ort/xxx
    const parts = url.pathname.split('/').filter((part) => part !== '')
    const rootName = parts[0] ?? ''
    if (!roots.some((root) => root.endsWith(sep + rootName))) {
      response.writeHead(404).end('not found')
      return
    }
    const root = join(resourcesRoot, rootName)
    const relative = parts.slice(1).join('/')
    // 规范化后必须还在 root 里面（防目录穿越）
    const target = normalize(join(root, relative))
    if (!target.startsWith(root + sep) && target !== root) {
      response.writeHead(403).end('forbidden')
      return
    }
    if (!existsSync(target) || !statSync(target).isFile()) {
      response.writeHead(404).end('not found')
      return
    }
    const dot = target.lastIndexOf('.')
    const mime = dot >= 0 ? (MIME[target.slice(dot).toLowerCase()] ?? 'application/octet-stream') : 'application/octet-stream'
    response.writeHead(200, {
      'Content-Type': mime,
      'Content-Length': statSync(target).size,
      'Cache-Control': 'no-cache',
      // ★ CORS：渲染器页面在 dsh-app:// 源下，用 import() 加载这里的模块是**跨源**的，
      //   没有这组头浏览器会直接拒掉（2026-11-04 实测踩到）。
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
      'Access-Control-Allow-Headers': '*',
      'Cross-Origin-Resource-Policy': 'cross-origin',
    })
    createReadStream(target).pipe(response)
  })

  for (let attempt = 0; attempt < PORT_ATTEMPTS; attempt += 1) {
    const port = preferredPort + attempt
    const bound = await new Promise<boolean>((resolve) => {
      server.once('error', () => { resolve(false) })
      server.listen(port, '127.0.0.1', () => { resolve(true) })
    })
    if (bound) {
      return { port, close: () => { server.close() } }
    }
  }
  throw new Error(`模型服务端口 ${preferredPort}..${preferredPort + PORT_ATTEMPTS - 1} 都被占用`)
}
