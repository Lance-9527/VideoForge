/**
 * 掌柜 AI · 深度模型权重**拉取**（fetch）。
 *
 * ★ 为什么需要这一步（2026-11-16 实测）：
 *   深度估计用的 Depth Anything V2 Small 权重（26MB）**不在 npm 上**，
 *   只在 HuggingFace 模型仓库里。仓库不收这个二进制（`.gitignore` 已排除
 *   `resources/models/`），所以干净 checkout 之后必须能自己把它取回来，
 *   否则打包会直接卡在「模型文件缺失」。
 *
 * ★ 为什么要校验 sha256：
 *   26MB 的下载可能断在半路，或者拿到的是别的东西。校验值写死在
 *   `model-payload.mjs` 里（实测与本地长期使用的权重完全一致），
 *   对不上就删掉重下，绝不把坏文件带进包里。
 *
 * ★ 为什么要两个源：
 *   实测本机 `huggingface.co` 不通（HTTP 000），`hf-mirror.com` 通（HTTP 200）。
 *   所以顺序是**镜像优先、官方兜底**，谁先通用谁。
 *
 * 用法：`node scripts/fetch-model-payload.mjs`（打包前自动调用）。
 *
 * @module
 */
import { createHash } from 'node:crypto'
import { createReadStream, createWriteStream, existsSync, mkdirSync, renameSync, rmSync, statSync } from 'node:fs'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  MODEL_DIRECTORY,
  MODEL_FILES,
  MODEL_REMOTE_FILES,
  MODEL_REPOSITORY,
  MODEL_SHA256,
} from './model-payload.mjs'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const APP_ROOT = resolve(SCRIPT_DIR, '..')

/** 下载源（按顺序尝试，谁先通用谁）。 */
const SOURCES = Object.freeze([
  { label: 'hf-mirror', base: 'https://hf-mirror.com' },
  { label: 'huggingface', base: 'https://huggingface.co' },
])

/** 单个文件的下载超时（毫秒）—— 26MB 的权重在慢网下也要留够时间。 */
const REQUEST_TIMEOUT_MS = 20 * 60 * 1000

/**
 * 算一个文件的 sha256。
 * @param {string} path 文件路径。
 * @returns {Promise<string>} 十六进制摘要。
 */
async function sha256Of(path) {
  const hash = createHash('sha256')
  await pipeline(createReadStream(path), hash)
  return hash.digest('hex')
}

/**
 * 下载一个文件到临时路径。
 * @param {string} url 完整下载地址。
 * @param {string} target 落地路径（先写临时文件，成功后改名）。
 * @returns {Promise<void>} 解析即下载完成。
 */
async function download(url, target) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch(url, { signal: controller.signal, redirect: 'follow' })
    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}`)
    }
    await pipeline(Readable.fromWeb(response.body), createWriteStream(target))
  } finally {
    clearTimeout(timer)
  }
}

/**
 * 把模型文件取到 `resources/models/<模型目录>/`。
 *
 * @param {string} resourcesRoot 资源根目录。
 * @returns {Promise<void>} 解析即全部就位。
 */
async function fetchModelPayload(resourcesRoot) {
  const target = join(resourcesRoot, 'models', MODEL_DIRECTORY)
  mkdirSync(target, { recursive: true })

  for (const name of MODEL_FILES) {
    const path = join(target, name)
    const expected = MODEL_SHA256[name]
    if (existsSync(path) && await sha256Of(path) === expected) {
      console.log(`[desktop fetch] 已有且校验通过，跳过：${name}`)
      continue
    }
    const remotePath = MODEL_REMOTE_FILES[name]
    let lastError = null
    let fetched = false
    for (const source of SOURCES) {
      const url = `${source.base}/${MODEL_REPOSITORY}/resolve/main/${remotePath}`
      const temporary = `${path}.download`
      try {
        console.log(`[desktop fetch] 下载 ${name} ← ${source.label}`)
        await download(url, temporary)
        const actual = await sha256Of(temporary)
        if (actual !== expected) {
          rmSync(temporary, { force: true })
          throw new Error(`sha256 不匹配（期望 ${expected}，实际 ${actual}）`)
        }
        renameSync(temporary, path)
        console.log(`[desktop fetch]   ✓ ${name}（${(statSync(path).size / 1024 / 1024).toFixed(2)} MB）`)
        fetched = true
        break
      } catch (error) {
        rmSync(temporary, { force: true })
        lastError = error
        console.warn(`[desktop fetch]   ✗ ${source.label}：${error instanceof Error ? error.message : String(error)}`)
      }
    }
    if (!fetched) {
      throw new Error(`desktop fetch: 模型文件 ${name} 下载失败`
        + `（${MODEL_REPOSITORY}/${remotePath}）——`
        + `最后错误：${lastError instanceof Error ? lastError.message : String(lastError)}`
        + '。可手动下载后放进 ' + `${target}/`)
    }
  }
}

const resourcesRoot = join(APP_ROOT, 'resources')
await fetchModelPayload(resourcesRoot)
console.log(`[desktop fetch] 模型权重就绪：${join(resourcesRoot, 'models', MODEL_DIRECTORY)}`)
