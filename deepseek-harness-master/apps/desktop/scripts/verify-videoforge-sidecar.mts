/**
 * sidecar 验收脚本：驱动真实的 sidecar 模块（不含 Electron），验证 拉起/健康/数据/退出。
 *
 * 两种来源都验：
 *   ① **随包载荷**（`resources/videoforge-sidecar/VideoForgeService.exe`）——
 *      不依赖 DSH 的 runtime 树，也不需要系统 python
 *   ② 开发形态（系统 python + 源码 backend）
 *
 * 用法：
 *   pnpm exec tsx scripts/verify-videoforge-sidecar.mts            # 两种都验
 *   pnpm exec tsx scripts/verify-videoforge-sidecar.mts --bundled  # 只验随包载荷
 */
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, resolve } from 'node:path'
import { resolveVideoForgeSidecar, VideoForgeSidecar } from '../src/videoforge-sidecar.ts'

const APP_ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const RESOURCES = join(APP_ROOT, 'resources')
const BACKEND = 'D:\\VideoForge-dev\\backend'
const PYTHON = 'C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe'
const LOCALAPPDATA = process.env.LOCALAPPDATA ?? 'C:\\Users\\Administrator\\AppData\\Local'
const DATA_DIR = join(BACKEND, 'data')
const onlyBundled = process.argv.includes('--bundled')

/**
 * 跑一遍并把结论打出来。
 * @param label - 这一轮叫什么。
 * @param packaged - 传给解析器的形态。
 * @param environment - 环境变量覆盖。
 */
async function run(label: string, packaged: boolean, environment: NodeJS.ProcessEnv): Promise<boolean> {
  console.log(`\n══════ ${label} ══════`)
  const layout = resolveVideoForgeSidecar(packaged, RESOURCES, LOCALAPPDATA, environment)
  console.log('sidecar 来源 =', layout.python)
  console.log('参数 =', JSON.stringify(layout.args))
  console.log('backend 目录 =', layout.backendDir)
  console.log('数据目录 =', layout.dataDir, '| 已有真库 =', layout.hasExistingLibrary)
  if (layout.problem !== undefined) { console.error('✗ 解析失败：', layout.problem); return false }
  if (!existsSync(layout.python)) { console.error('✗ 可执行文件不存在：', layout.python); return false }

  const sidecar = new VideoForgeSidecar(layout, { ...process.env, ...environment }, 120_000,
    (message) => { if (message !== '' && !message.startsWith('INFO:')) console.log('  [sidecar]', message) })
  let ok = false
  try {
    ok = await sidecar.start()
    console.log('健康 =', ok, '| status =', sidecar.status, '| failure =', sidecar.failureMessage)
    if (!ok) return false
    const projects = await (await fetch(`http://${layout.host}:${layout.port}/api/projects`)).json()
    const list = projects?.data?.projects ?? []
    console.log('项目数 =', list.length)
    const target = list.find((p: { title: string }) => p.title.includes('雨夜便利店'))
    console.log('含「雨夜便利店」=', target !== undefined)
    if (target) {
      const raw = await (await fetch(`http://${layout.host}:${layout.port}/api/projects/${target.id}/stage/status`)).json()
      const stages = raw?.data?.stages ?? []
      console.log('阶段数 =', stages.length, '|', stages.map((s: { key: string; status: string }) => `${s.key}:${s.status}`).join(' '))
      console.log('完成步数 =', raw?.data?.done_count, '/', raw?.data?.total)
    }
    const health = await (await fetch(`http://${layout.host}:${layout.port}/api/health`)).json()
    console.log('health =', health?.data?.status, '| ffmpeg =', health?.data?.ffmpeg_available)
  } finally {
    const stopped = await sidecar.stop()
    console.log('已停止 =', stopped, '| status =', sidecar.status)
    await new Promise((r) => setTimeout(r, 1500))
    let stillUp = false
    try {
      await fetch(`http://${layout.host}:${layout.port}/api/health`, { signal: AbortSignal.timeout(1500) })
      stillUp = true
    } catch { stillUp = false }
    console.log('8765 仍可访问 =', stillUp, '（false = 没留僵尸）')
    if (stillUp) process.exitCode = 4
  }
  return ok
}

const bundledExe = join(RESOURCES, 'videoforge-sidecar', 'VideoForgeService.exe')
if (existsSync(bundledExe) && !onlyBundled) {
  // ① 随包载荷：数据目录指向**我们自己的** resources 目录旁的库（开发时是源码库的副本）
  await run('① 随包载荷（不依赖 DSH runtime 树）', false, {
    VIDEOFORGE_DATA_DIR: DATA_DIR,
    VIDEOFORGE_PORT: '8765',
  })
} else if (onlyBundled) {
  console.error('✗ 随包载荷不存在：', bundledExe)
  process.exit(2)
}

if (!onlyBundled) {
  // ② 开发形态：系统 python + 源码 backend
  await run('② 开发形态（系统 python + 源码 backend）', false, {
    DSH_DESKTOP_VIDEOFORGE_SIDECAR_DIR: BACKEND,
    DSH_DESKTOP_VIDEOFORGE_PYTHON: PYTHON,
    VIDEOFORGE_DATA_DIR: DATA_DIR,
    VIDEOFORGE_PORT: '8765',
  })
}
