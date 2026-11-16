/** Validate the assembled application, including native Office conversion outside ASAR. */
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseArgs } from 'node:util'
import { desktopArtifactOutput, resolveDesktopBuildTarget, resolveDesktopTargetBuildPaths } from './desktop-build-paths.mjs'
import { readDesktopRuntime, verifyDesktopRuntime } from '../src/runtime-tree.ts'
import { verifyWindowsCode } from './windows-runtime-signature.mjs'
import { smokePreparedRuntime } from './smoke-prepared-runtime.ts'
import { resolveDesktopPackageTarget } from './package-target.ts'

const paths = resolveDesktopTargetBuildPaths()
const { values } = parseArgs({ options: { unsigned: { type: 'boolean', default: false }, 'skip-notarization': { type: 'boolean', default: false } }, allowPositionals: false })
const target = resolveDesktopBuildTarget()
const windows = target === 'win-x64'
const artifacts = desktopArtifactOutput(paths, { unsigned: values.unsigned, skipNotarization: values['skip-notarization'] }).directory
const application = windows ? join(artifacts, 'win-unpacked')
  : join(artifacts, target === 'mac-arm64' ? 'mac-arm64' : 'mac', 'DeepSeek Harness.app', 'Contents')
const resources = join(application, windows ? 'resources' : 'Resources')
// ★★ exe 名跟着**品牌**走（productName 决定 exe 文件名）：
//   VideoForge.exe / ShopSteward.exe / DeepSeek Harness.exe。
//   这里按 `resources/brand.json` 的 productName 解析；读不到就退回「目录里找 .exe」。
function resolveWindowsExecutable(directory: string, resourcesDirectory: string): string {
  try {
    const brand = JSON.parse(readFileSync(join(resourcesDirectory, 'brand.json'), 'utf8')) as { productName?: string }
    if (typeof brand.productName === 'string' && brand.productName !== '') {
      const candidate = join(directory, `${brand.productName}.exe`)
      if (existsSync(candidate)) { return candidate }
    }
  } catch {
    // 读不到品牌文件就往下走
  }
  // 兜底：目录里唯一的 .exe
  const found = readdirSync(directory).find((name) => name.toLowerCase().endsWith('.exe'))
  return found === undefined ? join(directory, 'DeepSeek Harness.exe') : join(directory, found)
}
const executable = windows
  ? resolveWindowsExecutable(application, resources)
  : join(application, 'MacOS', 'DeepSeek Harness')
const descriptor = await verifyDesktopRuntime(paths.dsh, readDesktopRuntime(paths.dsh).release.version,
  resolveDesktopPackageTarget(target))
if (windows && !values.unsigned) await verifyWindowsCode(application)
await smokePreparedRuntime(join(resources, 'app.asar', 'dsh'), executable, join(resources, 'runtime'), descriptor)
