/**
 * 桌面应用的**品牌参数** —— 让同一套 Electron 外壳能产出多个独立应用。
 *
 * ## 为什么要有这层
 * 2026-11-03 起不止一个产品：
 *   · `videoforge` —— 视频创作台（VideoForge）
 *   · `shop`       —— 掌柜 AI（电商 agent 应用）
 * 它们共用 `apps/desktop` 的外壳与 sidecar，但**品牌、数据目录、profile 组合必须各自分开**，
 * 否则两个应用会抢同一个 `%LOCALAPPDATA%` 与同一批 bundle。
 *
 * ## 怎么选
 * 打包时由 `scripts/package-target.ts --brand <id>` 决定，并把解析结果写进
 * `resources/brand.json`（随包发布）。运行时主进程读它；
 * 开发形态读不到就退回默认品牌（VideoForge）。
 *
 * @module
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/** 一个品牌的全部可切换项。 */
export interface DesktopBrand {
  /** 品牌 id（打包时选它）。 */
  readonly id: string
  /** 窗口标题 / 任务栏 / Alt-Tab 显示名。 */
  readonly appName: string
  /** electron-builder 的 productName —— 决定 exe 文件名。 */
  readonly productName: string
  /** 产物文件名前缀（如 `videoforge` / `shopsteward`）。 */
  readonly artifactPrefix: string
  /** 数据根目录名（%LOCALAPPDATA%\<这个>）。 */
  readonly dataDirName: string
  /** 图标文件名（在 resources/ 下）。 */
  readonly icon: string
  /**
   * 应用 ID（Windows 的身份标识）。
   *
   * ★ 两个品牌必须不同 —— 相同的话 Windows 认为它们是同一个应用：
   *   第二个装不进去（NSIS 退出码 2），而且卸载会互删。
   */
  readonly appId?: string
  /**
   * 本地 sidecar 的端口。
   *
   * ★ 两个应用必须不同 —— 端口相同的话第二个应用拉不起 sidecar，
   *   表现是「点了没反应 / 起不来」（因为端口被第一个占着）。
   */
  readonly sidecarPort?: number
  /**
   * Windows 托盘图标的文件名（放在 `apps/desktop/resources/`）。
   *
   * ★ 打包后统一复制成 `resources/tray.ico`，主进程按品牌选源文件。
   *   不按品牌来的话，两个应用的托盘会显示成同一个图标。
   */
  readonly trayIcon?: string
  /** profile 的 bundle 列表 —— 决定这个应用加载哪些能力。 */
  readonly bundles: readonly string[]
  /** "沿用继承的 DSH_HOME" 的逃生阀环境变量名（每个品牌一个，避免互相踩）。 */
  readonly keepDshHomeEnv: string
}

/** 默认品牌（开发形态、brand.json 缺失时的兜底）。 */
export const DEFAULT_BRAND: DesktopBrand = {
  id: 'videoforge',
  appName: 'VideoForge',
  productName: 'VideoForge',
  artifactPrefix: 'videoforge',
  dataDirName: 'VideoForge',
  icon: 'icon-windows.png',
  bundles: [
    '@deepseek-ai/dsh-base',
    '@deepseek-ai/dsh-web-app',
    '@deepseek-ai/dsh-vf-video',
  ],
  keepDshHomeEnv: 'VIDEOFORGE_KEEP_DSH_HOME',
}

/**
 * 从品牌目录里按 id 取品牌。
 * @param catalog - 品牌目录（`brands.json` 的内容）。
 * @param id - 品牌 id。
 * @returns 对应品牌；找不到时返回默认品牌。
 */
export function pickBrand(catalog: { readonly brands?: Record<string, DesktopBrand> } | undefined, id: string): DesktopBrand {
  const found = catalog?.brands?.[id]
  return found ?? DEFAULT_BRAND
}

/**
 * 在运行时读出当前品牌。
 * @param resourcesPath - `process.resourcesPath`（打包形态）。
 * @param appPath - `app.getAppPath()`（开发形态）。
 * @returns 当前品牌；读不到就用默认品牌。
 */
export function readBrand(resourcesPath: string | undefined, appPath: string | undefined): DesktopBrand {
  const candidates: string[] = []
  if (resourcesPath !== undefined && resourcesPath !== '') {
    candidates.push(join(resourcesPath, 'brand.json'))
  }
  if (appPath !== undefined && appPath !== '') {
    candidates.push(join(appPath, 'resources', 'brand.json'))
  }
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(readFileSync(candidate, 'utf8')) as DesktopBrand
      if (typeof parsed?.id === 'string' && typeof parsed?.appName === 'string') {
        return { ...DEFAULT_BRAND, ...parsed }
      }
    } catch {
      // 读不到就试下一个候选路径
    }
  }
  return DEFAULT_BRAND
}

/**
 * 当前进程用的品牌 —— **不 import electron**。
 *
 * ★ 为什么不能 import electron：`apps/desktop/src/project-manager.ts` 会被
 *   `scripts/prepare-dsh.ts` 在**纯 Node** 里 import（算运行时闭包）。
 *   那里 `import { app } from 'electron'` 会直接抛
 *   「The requested module 'electron' does not provide an export named 'app'」。
 *   Electron 的 `process.resourcesPath` 在 Node 里是 undefined，正好当探针。
 *
 * 解析顺序：
 *   1. `<resourcesPath>/brand.json` —— 打包形态（package-target 写进去的）
 *   2. 环境变量 `DSH_DESKTOP_BRAND` + 仓库里的 `resources/brands.json` —— 开发形态
 *   3. 默认品牌（VideoForge）
 */
export function currentBrand(): DesktopBrand {
  const resourcesPath = (process as unknown as { resourcesPath?: string }).resourcesPath
  if (resourcesPath !== undefined && resourcesPath !== '') {
    const resolved = readBrand(resourcesPath, undefined)
    if (resolved.id !== DEFAULT_BRAND.id) { return resolved }
  }
  const envId = (process.env.DSH_DESKTOP_BRAND ?? '').trim()
  if (envId !== '') {
    // 品牌目录在本文件的上两级（apps/desktop/resources/brands.json）
    const catalogPath = join(import.meta.dirname ?? '.', '..', 'resources', 'brands.json')
    try {
      const catalog = JSON.parse(readFileSync(catalogPath, 'utf8')) as { brands?: Record<string, DesktopBrand> }
      const picked = pickBrand(catalog, envId)
      if (picked.id === envId) { return picked }
    } catch {
      // 目录读不到就退回默认品牌
    }
  }
  return DEFAULT_BRAND
}
