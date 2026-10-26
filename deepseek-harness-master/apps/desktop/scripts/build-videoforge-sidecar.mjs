/**
 * 生成 VideoForge 视频服务（Python sidecar）的随包载荷。
 *
 * ★ 为什么有这一步（2026-10-21）：
 *   v3 的桌面端（Electron）要拉起一个 Python 后端当 sidecar。载荷是
 *   PyInstaller onedir 产物（VideoForgeService.exe + _internal/），
 *   裁剪掉本仓一处都不 import、却占 237MB 的包（llvmlite / cv2 / pandas /
 *   cryptography / numba），从 444MB 降到 ~207MB，加 exe 共 ~224MB。
 *
 * ★ 关键：**不碰 DSH 的 runtime 树**。
 *   runtime 树是内容寻址 + sha256 清单校验的（desktop-runtime.json），
 *   往里加东西会破坏校验。载荷放在我们自己的
 *   `resources/videoforge-sidecar/`，由 electron-builder 的 extraFiles 带进
 *   `win-unpacked\resources\videoforge-sidecar\`。
 *
 * 用法：
 *   node scripts/build-videoforge-sidecar.mjs --source <dist/VideoForge 目录>
 *   node scripts/build-videoforge-sidecar.mjs --source <dir> --python <python.exe>
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { cp, mkdir, readdir, rm, stat, rename, readFile, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const APP_ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const PAYLOAD_DIR = join(APP_ROOT, 'resources', 'videoforge-sidecar')
const SERVICE_EXE = 'VideoForgeService.exe'

/**
 * 产物目录里要删掉的目录（backend 一处 import 都没有，但体积巨大）。
 *
 * ★ 2026-10-25：构建机上装了 torch 之后，PyInstaller 会**顺带**把它收进包
 *   （backend 源码里 grep 不到任何 `import torch`，是传递依赖被分析到的），
 *   实测 payload 从 ~235MB 涨到 753.8MB，其中 torch 独占 365MB。
 *   裁掉它不影响后端；客户 skill 真需要 torch 时走 `skillenv` 可写扩展目录装，
 *   不该让每个用户的安装包都背 365MB。
 */
const CUT_DIRECTORIES = ['llvmlite', 'llvmlite.libs', 'cv2', 'pandas', 'pandas.libs', 'cryptography', 'numba',
  'torch', 'torch.libs', 'torchgen', 'functorch']

function parseArguments(argv) {
  const options = {}
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index]
    if (flag === '--source' || flag === '--python') {
      options[flag.slice(2)] = argv[index + 1]
      index += 1
    }
  }
  return options
}

async function directorySize(directory) {
  let total = 0
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name)
    total += entry.isDirectory() ? await directorySize(path) : (await stat(path)).size
  }
  return total
}

async function main() {
  const options = parseArguments(process.argv.slice(2))
  const source = options.source
  if (source === undefined) {
    console.error('用法：node scripts/build-videoforge-sidecar.mjs --source <dist/VideoForge 目录>')
    process.exit(2)
  }
  const sourceDirectory = resolve(source)
  if (!existsSync(join(sourceDirectory, 'VideoForge.exe'))) {
    console.error(`源目录里没有 VideoForge.exe：${sourceDirectory}`)
    process.exit(2)
  }
  await rm(PAYLOAD_DIR, { recursive: true, force: true })
  await mkdir(PAYLOAD_DIR, { recursive: true })
  console.log(`复制 ${sourceDirectory} → ${PAYLOAD_DIR}`)
  await cp(sourceDirectory, PAYLOAD_DIR, { recursive: true })
  await rename(join(PAYLOAD_DIR, 'VideoForge.exe'), join(PAYLOAD_DIR, SERVICE_EXE))
  const internal = join(PAYLOAD_DIR, '_internal')
  for (const name of CUT_DIRECTORIES) {
    const path = join(internal, name)
    if (!existsSync(path)) continue
    await rm(path, { recursive: true, force: true })
    console.log(`裁掉 ${name}`)
  }

  // ★ 2026-10-25：**开发期残留**不能随包发出去。
  //   PyInstaller 会把 `backend/data/` 一并收进产物，而那是开发机上的
  //   Blender 白膜渲染帧（实测 87.1MB：`previz_blender/out/镜01/f0001.png…`），
  //   既不是产品资产、也不该出现在用户安装包里。
  //   运行期真正的数据目录是 `%LOCALAPPDATA%\VideoForge\data`（由 VIDEOFORGE_DATA_DIR
  //   注入），所以删掉它不影响功能；旧安装包的 backend 只有 14.8MB，没有这个目录。
  // KEEP_FIXED_SCENE_RENDERER：固定场景渲染器是运行期必需文件（缺了 /renders 直接 503「缺少固定场景渲染器」），
  // 位置在 `backend/data/studio_bridge/render_scene.py`。以前整目录删掉，导致白膜出片只能退回浏览器录制。
  const FIXED_RENDERER_REL = join('backend', 'data', 'studio_bridge', 'render_scene.py')
  const fixedRendererSrc = join(internal, FIXED_RENDERER_REL)
  const fixedRendererBytes = existsSync(fixedRendererSrc) ? await readFile(fixedRendererSrc) : null
  const CUT_RELATIVE_PATHS = ['backend/data', 'backend/outputs', 'backend/cache', 'backend/uploads', 'backend/.pytest_cache']
  for (const rel of CUT_RELATIVE_PATHS) {
    const path = join(internal, rel)
    if (!existsSync(path)) continue
    await rm(path, { recursive: true, force: true })
    console.log(`裁掉开发期目录 ${rel}`)
  }
  if (fixedRendererBytes) {
    const dest = join(internal, FIXED_RENDERER_REL)
    await mkdir(dirname(dest), { recursive: true })
    await writeFile(dest, fixedRendererBytes)
    console.log('保留固定场景渲染器 backend/data/studio_bridge/render_scene.py')
  } else {
    console.warn('⚠ 源目录里没有 backend/data/studio_bridge/render_scene.py —— 打包后白膜出片会 503')
  }

  const bytes = await directorySize(PAYLOAD_DIR)
  console.log(`载荷大小 = ${(bytes / 1024 / 1024).toFixed(1)} MB`)
  console.log('下一步：实跑一次确认后端仍能起（/api/health + /api/projects），'
    + '然后让桌面端 spawn 这个 exe 并带 --service 参数。')
  if (options.python !== undefined) console.log(`（提示：可用 ${options.python} 实跑验证）`)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
