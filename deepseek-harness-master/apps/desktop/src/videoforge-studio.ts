/** App-owned local MCP bridge for the bundled Blender workbench. @module */
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

/** Owns only the bridge spawned by this application; existing bridges stay alive. */
export class VideoForgeStudioBridge {
  private child: ChildProcess | undefined
  private stopped = false

  constructor(
    private readonly node: string,
    private readonly backendDir: string,
    private readonly outputDir: string | undefined,
    private readonly port = 8787,
  ) {}

  /** Start once and require the expected local bridge protocol. */
  async start(): Promise<void> {
    const health = async (): Promise<boolean> => {
      const response = await fetch(`http://127.0.0.1:${this.port}/health`, { signal: AbortSignal.timeout(800) })
      const body: unknown = await response.json()
      return typeof body === 'object' && body !== null && 'service' in body
        && body.service === 'videoforge-studio-bridge' && 'protocol' in body && body.protocol === 1
    }
    try {
      if (await health()) return
      throw new Error('8787 端口已有其他服务，场景桥没有启动')
    } catch (error) {
      if (error instanceof Error && error.message.includes('已有其他服务')) throw error
    }
    if (this.stopped) return
    const candidates = [
      join(this.backendDir, '_internal', 'backend', 'data', 'studio_workbench', 'server', 'agent-bridge.mjs'),
      join(this.backendDir, 'data', 'studio_workbench', 'server', 'agent-bridge.mjs'),
    ]
    const script = candidates.find((path) => existsSync(path))
    if (script === undefined) throw new Error('随包场景工作台缺失，请重新安装当前版本')
    const child = spawn(this.node, [script, '--port', String(this.port), ...(this.outputDir ? ['--out', this.outputDir] : [])], {
      windowsHide: true,
      stdio: ['ignore', 'ignore', 'pipe'],
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    })
    this.child = child
    let failure: Error | undefined
    child.on('error', (error) => { failure = error })
    child.stderr?.on('data', () => { /* Bridge diagnostics stay out of user data logs. */ })
    for (let attempt = 0; attempt < 30 && !this.stopped; attempt += 1) {
      if (failure !== undefined) throw failure
      if (child.exitCode !== null) throw new Error('场景工作台桥提前退出')
      try { if (await health()) return } catch { /* Wait for the loopback listener. */ }
      await new Promise<void>((resolve) => setTimeout(resolve, 200))
    }
    this.stop()
    throw new Error('场景工作台桥启动超时')
  }

  /** Stop this application's child without touching a pre-existing bridge. */
  stop(): void {
    this.stopped = true
    this.child?.kill()
    this.child = undefined
  }
}
