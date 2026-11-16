/**
 * 模型运行时载荷（`models/` + `ort/`）的清单与核对。
 *
 * ★ 为什么要有这一层（2026-11-04 实测）：
 *   electron-builder 的 `extraResources` 拷贝会**漏文件且不报错**（同一天两次
 *   打包，sidecar 载荷少了 55 个文件，装上去起不来）。模型与 wasm 走同一条路，
 *   所以不能只靠 electron-builder 拷贝 —— 打包后要**按文件数与体积核对**，
 *   不足就让打包失败，而不是让用户装上一个「深度模型永远加载不上」的包。
 *
 * 本模块是 `.mjs`：`electron-builder.config.mjs` 与 `scripts/stage-models.mjs`
 * 共用它（打包配置不能 import `.ts`）。
 *
 * @module
 */
import { existsSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

/** onnxruntime-web 的运行时文件（渲染器 `import()` 的那三个）。 */
export const ORT_FILES = Object.freeze([
  'ort-wasm-simd-threaded.mjs',
  'ort-wasm-simd-threaded.wasm',
  'ort.wasm.bundle.min.mjs',
])

/** Depth Anything V2 Small 的模型目录（相对 `models/`）。 */
export const MODEL_DIRECTORY = 'depth-anything-v2-small'

/** Depth Anything V2 Small 的模型文件。 */
export const MODEL_FILES = Object.freeze([
  'config.json',
  'preprocessor_config.json',
  'model_quantized.onnx',
])

/**
 * 模型文件的 sha256（下载后必须逐个核对）。
 *
 * ★ 这三个值是实测值（2026-11-16）：从 `onnx-community/depth-anything-v2-small`
 *   下载的字节与本地一直使用的权重**完全一致**，sha256 相同。
 *   模型权重不在 npm 上，只能从模型仓库取，所以校验值必须写死在代码里 ——
 *   否则「下载到一半的文件」会被当成好文件装进包里。
 */
export const MODEL_SHA256 = Object.freeze({
  'config.json': '3aee5b9bc4f711ee885c2526d871f0c8c6c8c4b26b8e04253d0167f6a83264f5',
  'preprocessor_config.json': '03576db3c13dd0471fdf5f5e1428befcb95de063fe699879150b293dc9e0a2c6',
  'model_quantized.onnx': 'fcf51f1b230362b28690bb9d1809bf0431f29cad20534e3f589bd7285547f20d',
})

/**
 * 本地文件名 → 模型仓库里的相对路径。
 *
 * ★ `config.json` / `preprocessor_config.json` 在仓库根，`model_quantized.onnx`
 *   在 `onnx/` 子目录下（上游就是分层放的，不是我们这边特殊）。
 */
export const MODEL_REMOTE_FILES = Object.freeze({
  'config.json': 'config.json',
  'preprocessor_config.json': 'preprocessor_config.json',
  'model_quantized.onnx': 'onnx/model_quantized.onnx',
})

/** 模型仓库（HF 上游，Apache-2.0）。 */
export const MODEL_REPOSITORY = 'onnx-community/depth-anything-v2-small'

/** 两个目录各自期望的文件数。 */
export const EXPECTED_FILE_COUNT = 3

/**
 * 模型权重的下限（字节）—— int8 量化的 Depth Anything V2 Small 约 26MB。
 *
 * ★ 用**十进制 MB**（26 000 000）：实测那份权重是 27 258 801 字节（26.0 MiB），
 *   按 26 × 1024² 算会差几 KB 把好文件判成坏的。
 */
export const MODEL_MIN_BYTES = 26_000_000

/** onnxruntime-web wasm 的下限（字节）—— 约 12MB。 */
export const ORT_WASM_MIN_BYTES = 12_000_000

/**
 * 数一个目录里的文件数（递归）。
 * @param {string} directory 目录。
 * @returns {number} 文件总数。
 */
export function countFiles(directory) {
  let total = 0
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    total += entry.isDirectory() ? countFiles(join(directory, entry.name)) : 1
  }
  return total
}

/**
 * 核对 `models/` 与 `ort/` 载荷是否完整。
 *
 * @param {string} resourcesRoot 含 `models/` 与 `ort/` 的目录。
 * @returns {{ models: Record<string, number>, ort: Record<string, number> }} 每个文件的大小（字节）。
 * @throws 载荷不齐时抛错（让打包失败，而不是产出一个用不了的包）。
 */
export function verifyModelPayload(resourcesRoot) {
  const modelsRoot = join(resourcesRoot, 'models')
  const ortRoot = join(resourcesRoot, 'ort')
  for (const [label, directory] of [['models', modelsRoot], ['ort', ortRoot]]) {
    if (!existsSync(directory)) {
      throw new Error(`desktop package: 模型运行时装载缺失（${directory}）—— ${label} 目录不存在`)
    }
  }
  const modelFiles = countFiles(modelsRoot)
  if (modelFiles !== EXPECTED_FILE_COUNT) {
    throw new Error(`desktop package: 模型装载不完整：models 有 ${modelFiles} 个文件（期望 ${EXPECTED_FILE_COUNT}）`)
  }
  const ortFiles = countFiles(ortRoot)
  if (ortFiles !== EXPECTED_FILE_COUNT) {
    throw new Error(`desktop package: 模型装载不完整：ort 有 ${ortFiles} 个文件（期望 ${EXPECTED_FILE_COUNT}）`)
  }
  const modelSizes = {}
  for (const name of MODEL_FILES) {
    const path = join(modelsRoot, MODEL_DIRECTORY, name)
    if (!existsSync(path)) {
      throw new Error(`desktop package: 模型文件缺失（${path}）`)
    }
    modelSizes[name] = statSync(path).size
  }
  if ((modelSizes['model_quantized.onnx'] ?? 0) < MODEL_MIN_BYTES) {
    throw new Error(`desktop package: 模型权重不完整：model_quantized.onnx `
      + `${modelSizes['model_quantized.onnx'] ?? 0} 字节（至少 ${MODEL_MIN_BYTES}）`)
  }
  const ortSizes = {}
  for (const name of ORT_FILES) {
    const path = join(ortRoot, name)
    if (!existsSync(path)) {
      throw new Error(`desktop package: onnxruntime-web 文件缺失（${path}）`)
    }
    ortSizes[name] = statSync(path).size
  }
  if ((ortSizes['ort-wasm-simd-threaded.wasm'] ?? 0) < ORT_WASM_MIN_BYTES) {
    throw new Error(`desktop package: onnxruntime-web wasm 不完整：ort-wasm-simd-threaded.wasm `
      + `${ortSizes['ort-wasm-simd-threaded.wasm'] ?? 0} 字节（至少 ${ORT_WASM_MIN_BYTES}）`)
  }
  return { models: modelSizes, ort: ortSizes }
}
