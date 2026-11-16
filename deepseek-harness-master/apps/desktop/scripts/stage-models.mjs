/**
 * 掌柜 AI · 模型运行时资源**预备**（staging）。
 *
 * ★ 为什么需要这一步（2026-11-04 实测）：
 *   `resources/ort/` 里的 onnxruntime-web 文件以前是**手工从 pnpm store 拷进去的**
 *   —— 仓库里没有任何脚本或依赖记录它们，干净 checkout 打不出包。
 *   现在 `onnxruntime-web@1.21.0` 是 `packages/client/ui-shop-brand` 的
 *   devDependency（**不进 client bundle**：DSH 的 client 加载器只认 `client.js`，
 *   把外部包当独立 bundle 加载会报 "client.xxx.js failed to load"；
 *   运行时是 `import()` 一个 URL，加依赖只是为了能稳定拿到 dist 文件），
 *   这个脚本负责把 dist 里的三个文件**确定地**拷进 `resources/ort/`。
 *
 * ★ 模型权重（26MB 的 `model_quantized.onnx`）**不在 npm 上**，
 *   所以它由配套的 `scripts/fetch-model-payload.mjs` 从 HF 模型仓库拉取
 *   （按 sha256 校验），本脚本只核对它在不在、体积对不对。
 *
 * 用法：`node scripts/stage-models.mjs`（打包前自动调用）。
 *
 * @module
 */
import { createRequire } from 'node:module'
import { copyFileSync, existsSync, mkdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  MODEL_DIRECTORY,
  MODEL_FILES,
  MODEL_MIN_BYTES,
  ORT_FILES,
  verifyModelPayload,
} from './model-payload.mjs'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const APP_ROOT = resolve(SCRIPT_DIR, '..')
const REPOSITORY_ROOT = resolve(APP_ROOT, '..', '..')

/** 提供 onnxruntime-web 的工作区包（devDependency 记在它名下）。 */
const ORT_PROVIDER_PACKAGE = join(REPOSITORY_ROOT, 'packages', 'client', 'ui-shop-brand')

/**
 * 找到 onnxruntime-web 的 dist 目录。
 * @returns {string} dist 目录的绝对路径。
 */
function resolveOrtDist() {
  const require = createRequire(join(ORT_PROVIDER_PACKAGE, 'package.json'))
  try {
    // `onnxruntime-web/wasm` 的入口就在 dist 里（包的 exports 映射到 dist/ort.wasm.*）。
    return dirname(require.resolve('onnxruntime-web/wasm'))
  } catch (error) {
    throw new Error(`desktop stage: 找不到 onnxruntime-web（先跑 pnpm install）：`
      + `${error instanceof Error ? error.message : String(error)}`)
  }
}

/**
 * 把 onnxruntime-web 的运行时文件拷进 `resources/ort/`。
 * @param {string} source dist 目录。
 * @param {string} target `resources/ort` 目录。
 */
function stageOrt(source, target) {
  mkdirSync(target, { recursive: true })
  for (const name of ORT_FILES) {
    const from = join(source, name)
    if (!existsSync(from)) {
      throw new Error(`desktop stage: onnxruntime-web 缺少 ${name}（${source}）`)
    }
    copyFileSync(from, join(target, name))
  }
}

/**
 * 打印每个文件的大小（人能一眼看出「少了一个 12MB 的 wasm」）。
 * @param {string} root 资源根目录。
 */
function report(root) {
  const { models, ort } = verifyModelPayload(root)
  const lines = []
  for (const [name, size] of Object.entries(models)) {
    lines.push(`  models/${MODEL_DIRECTORY}/${name}  ${(size / 1024 / 1024).toFixed(2)} MB`)
  }
  for (const [name, size] of Object.entries(ort)) {
    lines.push(`  ort/${name}  ${(size / 1024 / 1024).toFixed(2)} MB`)
  }
  console.log(`[desktop stage] 模型运行时资源就绪：\n${lines.join('\n')}`)
}

const resourcesRoot = join(APP_ROOT, 'resources')
const ortSource = resolveOrtDist()
stageOrt(ortSource, join(resourcesRoot, 'ort'))

// ★ 模型权重不在 npm 上：只核对它在不在、体积对不对（拉取由 fetch-model-payload.mjs 负责）。
for (const name of MODEL_FILES) {
  const path = join(resourcesRoot, 'models', MODEL_DIRECTORY, name)
  if (!existsSync(path)) {
    throw new Error(`desktop stage: 缺少模型文件 ${path}`
      + '（Depth Anything V2 Small 不在 npm 上：先跑 `node scripts/fetch-model-payload.mjs`'
      + ' 从 HF 模型仓库拉取并校验 sha256；也可以手动把 model_quantized.onnx / config.json /'
      + ' preprocessor_config.json 放进 apps/desktop/resources/models/'
      + `${MODEL_DIRECTORY}/）`)
  }
}
const modelBytes = statSync(join(resourcesRoot, 'models', MODEL_DIRECTORY, 'model_quantized.onnx')).size
if (modelBytes < MODEL_MIN_BYTES) {
  throw new Error(`desktop stage: 模型权重不完整（${modelBytes} 字节，至少 ${MODEL_MIN_BYTES}）`)
}

report(resourcesRoot)
