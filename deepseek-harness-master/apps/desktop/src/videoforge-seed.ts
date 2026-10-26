/**
 * VideoForge 首启种子 —— 让**装出来的应用开箱就能对话**。
 *
 * ★ 为什么需要：DSH 的模型是「插件 + 凭证 + 设置」三件套：
 *   ① `DSH_HOME/.credentials.yaml` 里要有 `refs:`（如 `MINIMAX_CN_API_KEY`）
 *   ② `DSH_HOME/settings.yaml` 里要有 `llm-pi-ai:` 段（provider 与模型目录）
 *   ③ profile patch 由 ① ② 在启动时导入
 *   三者缺一，模型选择器就是空的，用户打开产品只能看界面、说不了话。
 *   而我们的 DSH_HOME 是**独立**的（`%LOCALAPPDATA%\VideoForge\dsh`），
 *   用户的 `~/.dsh` 里那份配置不会被自动继承 ⇒ 必须由我们把种子放进去。
 *
 * ★ 为什么放在 `apps/desktop/` 而不是 core 包：这是**产品装配**的事，
 *   不是 DSH 的职责。core 一行不改，未来跟上游合并无冲突。
 *
 * ★ 为什么要「只在缺或明显是空壳时才写」：
 *   - 用户自己登录过 / 自己配过 key ⇒ 绝不能覆盖（那是用户资产）。
 *   - 我们上一次启动自动生成的 `.credentials.yaml` 只有浏览器会话记录、
 *     没有任何 `refs:` 与 `llm-*` record ⇒ 那是空壳，可以直接换成种子（并留 .bak）。
 */

import { copyFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

/** 随应用附带的种子目录名（`electron-builder` 的 `extraResources` 用它）。 */
export const VIDEOFORGE_SEED_DIR_NAME = 'videoforge-seed'

/** 种子文件名（不带前导点的那份给人看；落盘时按目标名改名）。 */
export const SEED_FILES = [
  { seed: 'settings.yaml', target: 'settings.yaml' },
  { seed: 'credentials.yaml', target: '.credentials.yaml' },
] as const

/** 一次种子的结果，便于日志与自检。 */
export interface VideoForgeSeedResult {
  /** 真正写入的文件（目标名）。 */
  readonly written: readonly string[]
  /** 已存在所以跳过的文件（目标名）。 */
  readonly kept: readonly string[]
  /** 种子目录不存在或读不到时的原因（人类可读）。 */
  readonly problem?: string
}

/**
 * 判断一份 `.credentials.yaml` 是不是「我们自动生成的空壳」。
 *
 * 空壳的特征：没有任何 `refs:` 段，也没有任何 `llm-` 开头的 record。
 * 只有浏览器会话记录时，模型是拿不到 key 的。
 *
 * @param contents - 文件内容。
 * @returns 是否是空壳（可以安全替换）。
 */
export function isPlaceholderCredentials(contents: string): boolean {
  if (!contents.includes('refs:')) return true
  return !/^\s{2}llm-/mu.test(contents)
}

/**
 * 把一个种子文件放进 DSH_HOME。
 *
 * 规则：
 *   1. 目标不存在 ⇒ 直接写。
 *   2. 目标是凭证空壳 ⇒ 备份成 `.bak-<时间戳>` 再写。
 *   3. 其它情况（用户自己配过）⇒ 跳过，绝不覆盖。
 *
 * @param dshHome - 独立的 DSH_HOME。
 * @param seedDirectory - 种子目录。
 * @param seed - 种子文件名。
 * @param target - 目标文件名（可以带前导点）。
 * @returns `'written'` | `'kept'`。
 */
export function plantSeedFile(
  dshHome: string,
  seedDirectory: string,
  seed: string,
  target: string,
): 'written' | 'kept' {
  const source = join(seedDirectory, seed)
  const destination = join(dshHome, target)
  if (!existsSync(source)) return 'kept'
  // ★ 设置文档被 DSH 导入后会改名为 `settings.yaml.imported`（app 的既有行为）。
  //   见到它 = 本轮会话已经种过了，不能再写一遍 —— 否则每次启动都会重复导入、
  //   反复改 profile patch。
  if (target === 'settings.yaml' && existsSync(`${destination}.imported`)) return 'kept'
  if (existsSync(destination)) {
    if (target !== '.credentials.yaml') return 'kept'
    const current = readFileSync(destination, 'utf8')
    if (!isPlaceholderCredentials(current)) return 'kept'
    copyFileSync(destination, `${destination}.bak-${String(Date.now())}`)
  }
  copyFileSync(source, destination)
  return 'written'
}

/**
 * 在 DSH 起来之前把种子放进独立的 DSH_HOME。
 *
 * ★ 必须在 `process.env.DSH_HOME` 定下来之后、Host 子进程起来之前调用
 *   （`main.ts` 的模块作用域块里），否则 DSH 已经读过设置，种子这一轮不生效。
 *
 * @param dshHome - 独立的 DSH_HOME。
 * @param resourcesPath - 打包后的 `process.resourcesPath`，或开发形态的 app 根目录。
 * @returns 写入/保留清单；种子不存在时 `problem` 说明原因（不抛异常）。
 */
export function seedVideoForgeHome(dshHome: string, resourcesPath: string): VideoForgeSeedResult {
  const seedDirectory = join(resourcesPath, VIDEOFORGE_SEED_DIR_NAME)
  if (!existsSync(seedDirectory)) {
    return { written: [], kept: [], problem: `没有随包附带的首启种子（${seedDirectory}）` }
  }
  mkdirSync(dshHome, { recursive: true })
  const written: string[] = []
  const kept: string[] = []
  for (const entry of SEED_FILES) {
    const outcome = plantSeedFile(dshHome, seedDirectory, entry.seed, entry.target)
    if (outcome === 'written') written.push(entry.target)
    else kept.push(entry.target)
  }
  return { written, kept }
}
