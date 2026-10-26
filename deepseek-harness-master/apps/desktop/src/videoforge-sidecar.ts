/**
 * VideoForge Python sidecar —— 随桌面应用拉起，随桌面应用退出。
 *
 * ★ 为什么单独一个模块（而不是塞进 core 包）：
 *   sidecar 是**我们自己的**东西（VideoForge 业务后端），不是 DSH 的职责。
 *   放在 `apps/desktop/` 里，core 改动面保持最小，未来跟上游冲突面最小。
 *
 * ★ 机制照 DSH 自己的来（不发明新机制）：
 *   DSH 的 `DesktopHostProcess`（`./host-process.ts`）就是这么管它自己那个
 *   Web Host 子进程的 —— `spawn` + 读 stdout/stderr + `stop()` 里先 SIGTERM
 *   再 SIGKILL 兜底。本模块是同一套骨架，只是换成 python + `main.py`。
 *
 * ★ 数据目录（最容易出错的一环）：
 *   sidecar 读哪个库由 `VIDEOFORGE_DATA_DIR` 决定（`backend/core/data_root.py`
 *   是唯一真相源）。这里只负责**选一个库并显式告诉它**：
 *     1. `%LOCALAPPDATA%\VideoForge\data` —— 用户装的那份（用户的老项目在这）
 *     2. backend 自己的 `data/` —— 开发/随包附带的那份
 *   规则：**优先选已经有真库（videoforge.db）的那一个**；
 *   两个都没有就用 1（首次运行时在正确位置建库，而不是在临时目录里建一个）。
 */

import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { isAbsolute, join } from 'node:path'

/** Sidecar 的数据库文件名（与 `backend/core/data_root.py` 一致）。 */
const DB_FILENAME = 'videoforge.db'

/** 用户安装版的业务数据目录名（与 Python 侧 `APP_NAME` 一致）。 */
const APP_NAME = 'VideoForge'

/** 默认端口（与 `backend/main.py` 的 `VIDEOFORGE_PORT` 默认值一致）。 */
const DEFAULT_PORT = 8765

/** 启动后等待健康检查的默认上限。 */
const DEFAULT_HEALTH_TIMEOUT_MS = 90_000

/** Sidecar 生命周期状态。 */
export type VideoForgeSidecarStatus = 'idle' | 'starting' | 'ready' | 'failed' | 'stopped'

/** Sidecar 的落点：怎么找到 python、backend、数据目录。 */
export interface VideoForgeSidecarLayout {
  /** 可执行文件（随包附带时是 `VideoForgeService.exe`，开发时是 python）。 */
  readonly python: string
  /** 传给可执行文件的参数。 */
  readonly args: readonly string[]
  /** backend 根目录（里面有 `main.py`；随包附带的载荷里是解包后的 backend）。 */
  readonly backendDir: string
  /** 业务数据目录（`VIDEOFORGE_DATA_DIR`）。 */
  readonly dataDir: string
  /** 监听地址。 */
  readonly host: string
  /** 监听端口。 */
  readonly port: number
}

/** 解析结果附带"为什么这么选"，便于日志与界面提示。 */
export interface VideoForgeSidecarResolution extends VideoForgeSidecarLayout {
  /** 选中的数据目录里是否已经有真的库。 */
  readonly hasExistingLibrary: boolean
  /** 解析失败的原因（人类可读）。 */
  readonly problem?: string
}

/** 随包附带的 sidecar 运行时目录名（打包时放这里）。 */
export const VIDEOFORGE_SIDECAR_DIR_NAME = 'videoforge-sidecar'

/**
 * 判断一个目录里是否已经有真的库。
 * @param directory - 数据目录。
 * @returns 是否存在 `videoforge.db`。
 */
export function hasLibrary(directory: string): boolean {
  return existsSync(join(directory, DB_FILENAME))
}

/**
 * 选数据目录（sidecar 读哪个库）。
 *
 * 规则（**与执行方式无关**，所以"用哪个 exe 起"和"读哪个库"是两件独立的事）：
 *   1. `DSH_DESKTOP_VIDEOFORGE_DATA_DIR`（开发形态显式指定）
 *   2. `VIDEOFORGE_DATA_DIR`（谁启动 app 时指定的；**只认绝对路径**）
 *   3. 执行目录下的 `data/` —— 且**必须已经有真库**才选它
 *   4. `%LOCALAPPDATA%\VideoForge\data` —— 生产位置；没有就由 Python 侧在那里建
 *
 * ★ 为什么第 3 条要求"已经有真库"：
 *   随包载荷的目录里没有用户的数据，落到它身上就会让用户**看不到自己的项目**；
 *   而指向一个还没有库的位置又会被 Python 侧凭空建一个空库。
 *   所以"有真库才选"是唯一不会骗人的规则。
 *
 * @param backendDir - 执行目录（随包载荷目录，或源码 backend 目录）。
 * @param localAppData - `%LOCALAPPDATA%`。
 * @param environment - 进程环境变量。
 * @returns 数据目录与"是否已有真库"。
 */
export function resolveSidecarDataDir(
  backendDir: string,
  localAppData: string,
  environment: NodeJS.ProcessEnv = process.env,
): { readonly dataDir: string; readonly hasExistingLibrary: boolean } {
  const packagedDataDir = join(localAppData, APP_NAME, 'data')
  const explicit = (environment.DSH_DESKTOP_VIDEOFORGE_DATA_DIR ?? environment.VIDEOFORGE_DATA_DIR ?? '').trim()
  if (explicit !== '' && isAbsolute(explicit)) {
    return { dataDir: explicit, hasExistingLibrary: hasLibrary(explicit) }
  }
  const ownDataDir = join(backendDir, 'data')
  if (hasLibrary(ownDataDir)) return { dataDir: ownDataDir, hasExistingLibrary: true }
  return { dataDir: packagedDataDir, hasExistingLibrary: hasLibrary(packagedDataDir) }
}

/**
 * 解析 sidecar 的落点。
 *
 * 打包形态（`app.isPackaged`）：随应用附带 `resources/runtime/videoforge-sidecar/`
 * （里面是 python 运行时 + backend 代码）；数据目录取用户那份。
 *
 * 开发形态：backend 目录与 python 可执行文件都可用环境变量指定
 * （`DSH_DESKTOP_VIDEOFORGE_SIDECAR_DIR` / `DSH_DESKTOP_VIDEOFORGE_PYTHON`），
 * 没指定就用仓库里的默认位置。
 *
 * @param packaged - 应用是否已打包。
 * @param resourcesPath - 打包后的 `process.resourcesPath`。
 * @param localAppData - `%LOCALAPPDATA%`。
 * @param environment - 进程环境变量。
 * @returns 解析结果；解析不出来时 `problem` 说明原因。
 */
export function resolveVideoForgeSidecar(
  packaged: boolean,
  resourcesPath: string,
  localAppData: string,
  environment: NodeJS.ProcessEnv = process.env,
): VideoForgeSidecarResolution {
  const host = environment.VIDEOFORGE_HOST ?? '127.0.0.1'
  const port = Number(environment.VIDEOFORGE_PORT ?? DEFAULT_PORT)

  // 随包载荷可能落在两个位置：
  //   ① `resources/videoforge-sidecar`（正确位置；electron-builder `extraResources`）
  //   ② app 根目录 `videoforge-sidecar`（历史 `extraFiles` 落点）
  // 两处都认，避免"安装包能打出来、真机却报视频服务缺失"。
  const payloadCandidates = [
    join(resourcesPath, VIDEOFORGE_SIDECAR_DIR_NAME),
    join(resourcesPath, '..', VIDEOFORGE_SIDECAR_DIR_NAME),
  ]
  const payload = payloadCandidates.find((directory) => existsSync(join(directory, 'VideoForgeService.exe')))
    ?? payloadCandidates[0]!

  if (packaged) {
    const exe = join(payload, 'VideoForgeService.exe')
    const data = resolveSidecarDataDir(payload, localAppData, environment)
    if (!existsSync(exe)) {
      return {
        python: exe, args: ['--service'], backendDir: payload, dataDir: data.dataDir, host, port,
        hasExistingLibrary: data.hasExistingLibrary,
        problem: `随应用附带的视频服务缺失（${exe}）`,
      }
    }
    return {
      python: exe, args: ['--service'], backendDir: payload, dataDir: data.dataDir, host, port,
      hasExistingLibrary: data.hasExistingLibrary,
    }
  }

  // 开发形态：显式指定优先（DSH_DESKTOP_VIDEOFORGE_SIDECAR_DIR），
  // 其次用**随包附带的那一份**（装出来的应用在开发形态下也能自测），
  // 最后才回落到系统 python + 源码 backend。
  const explicitBackend = environment.DSH_DESKTOP_VIDEOFORGE_SIDECAR_DIR
  if (explicitBackend !== undefined && explicitBackend !== ''
    && existsSync(join(explicitBackend, 'main.py'))) {
    const data = resolveSidecarDataDir(explicitBackend, localAppData, environment)
    return {
      python: environment.DSH_DESKTOP_VIDEOFORGE_PYTHON ?? 'python', args: ['main.py'],
      backendDir: explicitBackend, dataDir: data.dataDir, host, port,
      hasExistingLibrary: data.hasExistingLibrary,
    }
  }

  const bundled = join(payload, 'VideoForgeService.exe')
  if (existsSync(bundled)) {
    const data = resolveSidecarDataDir(payload, localAppData, environment)
    return {
      python: bundled, args: ['--service'], backendDir: payload, dataDir: data.dataDir, host, port,
      hasExistingLibrary: data.hasExistingLibrary,
    }
  }

  const backendDir = 'D:\\VideoForge-dev\\backend'
  const python = environment.DSH_DESKTOP_VIDEOFORGE_PYTHON ?? 'python'
  if (!existsSync(join(backendDir, 'main.py'))) {
    const data = resolveSidecarDataDir(backendDir, localAppData, environment)
    return {
      python, args: ['main.py'], backendDir, dataDir: data.dataDir, host, port,
      hasExistingLibrary: data.hasExistingLibrary,
      problem: `找不到 VideoForge 后端（${backendDir}\\main.py）`,
    }
  }
  const data = resolveSidecarDataDir(backendDir, localAppData, environment)
  return { python, args: ['main.py'], backendDir, dataDir: data.dataDir, host, port, hasExistingLibrary: data.hasExistingLibrary }
}

/**
 * 启动参数：把数据目录、端口显式告诉子进程（`backend/core/data_root.py` 读它）。
 *
 * ★ 2026-10-26：**给白膜工作台通道指定 node.exe**。
 *   `core/previz_channel.py` 起 openVideoForge 桥/页面要 node；打包后 App 的 PATH
 *   里通常没有 node（用户机器也不一定装 Node.js），而桌面端**自己随包带了**
 *   `resources/runtime/primary-runtime/dependencies/node/bin/node.exe`。
 *   不把这条路径告诉 sidecar，通道就会以「找不到 node.exe」失败、白膜退回 Blender
 *   —— 明明能力在手却用不上。这里：env 已显式指定就不覆盖；否则用随包 node。
 *
 * @param layout - sidecar 落点。
 * @param environment - 父进程环境变量。
 * @returns 子进程环境变量。
 */
export function sidecarEnvironment(
  layout: VideoForgeSidecarLayout,
  environment: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const bundledNode = resolveBundledNode(layout.backendDir)
  const nodeExplicit = (environment.VIDEOFORGE_NODE ?? '').trim() !== ''
  return {
    ...environment,
    VIDEOFORGE_DATA_DIR: layout.dataDir,
    VIDEOFORGE_HOST: layout.host,
    VIDEOFORGE_PORT: String(layout.port),
    // 白膜工作台通道（openVideoForge 桥）用的 node：显式指定优先，其次随包 node。
    ...(nodeExplicit || bundledNode === '' ? {} : { VIDEOFORGE_NODE: bundledNode }),
    // DSH 的子进程用独立的 DSH_HOME；这里不碰用户的 ~/.dsh。
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
  }
}

/**
 * 找随包附带的 node.exe（Electron 桌面端运行时依赖树里的那一份）。
 *
 * 载荷目录是 `resources/videoforge-sidecar`，运行时树是
 * `resources/runtime/primary-runtime/dependencies/node/bin/node.exe`；
 * 开发形态下 backendDir 是源码 backend 目录 → 直接返回空串（走系统 node）。
 *
 * @param backendDir - sidecar 载荷 / backend 目录。
 * @returns node.exe 绝对路径；没有就返回空串。
 */
export function resolveBundledNode(backendDir: string): string {
  const candidates = [
    join(backendDir, '..', 'runtime', 'primary-runtime', 'dependencies', 'node', 'bin', 'node.exe'),
    join(backendDir, '..', '..', 'runtime', 'primary-runtime', 'dependencies', 'node', 'bin', 'node.exe'),
  ]
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  return ''
}

/**
 * VideoForge sidecar 子进程。
 *
 * 生命周期与 `DesktopHostProcess` 同构：start() 拉起并等健康检查，stop() 先 SIGTERM
 * 再 SIGKILL 兜底，保证**不留僵尸占着 8765 端口**。
 */
export class VideoForgeSidecar {
  private child: ChildProcess | undefined
  private exitPromise: Promise<void> | undefined
  private currentStatus: VideoForgeSidecarStatus = 'idle'
  private failure: string | undefined
  private stopping = false

  /**
   * @param layout - sidecar 落点。
   * @param environment - 子进程继承的环境变量。
   * @param healthTimeoutMs - 等待健康检查的上限。
   * @param onLog - 日志回调。
   */
  constructor(
    private readonly layout: VideoForgeSidecarLayout,
    private readonly environment: NodeJS.ProcessEnv = process.env,
    private readonly healthTimeoutMs: number = DEFAULT_HEALTH_TIMEOUT_MS,
    private readonly onLog?: (message: string) => void,
  ) {}

  /** 当前状态。 */
  get status(): VideoForgeSidecarStatus {
    return this.currentStatus
  }

  /** 失败原因（人类可读，给界面用）。 */
  get failureMessage(): string | undefined {
    return this.failure
  }

  /** 健康检查地址。 */
  get healthUrl(): string {
    return `http://${this.layout.host}:${this.layout.port}/api/health`
  }

  /**
   * 拉起 sidecar 并等它健康。
   *
   * ★ 失败**不抛异常**：桌面应用必须照常起来，只是工作台会看到一句人话，
   *   而不是整片空白或一串栈。
   * @returns 是否健康。
   */
  async start(): Promise<boolean> {
    if (this.child !== undefined) return this.currentStatus === 'ready'
    this.currentStatus = 'starting'
    this.failure = undefined
    const child = spawn(this.layout.python, [...this.layout.args], {
      cwd: this.layout.backendDir,
      env: sidecarEnvironment(this.layout, this.environment),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.child = child
    child.stdout?.setEncoding('utf8')
    child.stderr?.setEncoding('utf8')
    child.stderr?.on('data', (chunk: string) => { this.onLog?.(chunk.trimEnd()) })
    child.stdout?.on('data', (chunk: string) => { this.onLog?.(chunk.trimEnd()) })
    child.once('error', (error: Error) => {
      this.failure = `视频服务启动失败：${error.message}`
      this.currentStatus = 'failed'
    })
    this.exitPromise = new Promise<void>((resolve) => {
      child.once('close', (code) => {
        if (!this.stopping && this.currentStatus !== 'ready') {
          this.failure = `视频服务已退出（代码 ${String(code)}）`
          this.currentStatus = 'failed'
        }
        resolve()
      })
    })
    const healthy = await this.waitForHealth()
    if (healthy) {
      this.currentStatus = 'ready'
      this.onLog?.(`VideoForge 视频服务就绪：${this.healthUrl}`)
    } else {
      this.currentStatus = 'failed'
      this.failure ??= '视频服务未启动，请点击重试'
    }
    return healthy
  }

  /**
   * 停止 sidecar：先 SIGTERM，超时再 SIGKILL。
   * @returns 是否已退出。
   */
  async stop(): Promise<boolean> {
    const child = this.child
    if (child === undefined) return true
    this.stopping = true
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM')
    const exited = this.exitPromise ?? Promise.resolve()
    if (!await this.exitsWithin(exited, 5_000)) {
      child.kill('SIGKILL')
      if (!await this.exitsWithin(exited, 5_000)) return false
    }
    this.child = undefined
    this.currentStatus = 'stopped'
    return true
  }

  private async waitForHealth(): Promise<boolean> {
    const deadline = Date.now() + this.healthTimeoutMs
    while (Date.now() < deadline) {
      if (this.child?.exitCode !== null && this.child?.exitCode !== undefined) return false
      try {
        const response = await fetch(this.healthUrl, { signal: AbortSignal.timeout(2_000) })
        if (response.ok) return true
      } catch { /* 还没起来，继续等 */ }
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
    return false
  }

  private async exitsWithin(exit: Promise<void>, milliseconds: number): Promise<boolean> {
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<false>((resolve) => {
      timer = setTimeout(() => { resolve(false) }, milliseconds)
      timer.unref()
    })
    try {
      return await Promise.race([exit.then(() => true), timeout])
    } finally {
      if (timer !== undefined) clearTimeout(timer)
    }
  }
}
