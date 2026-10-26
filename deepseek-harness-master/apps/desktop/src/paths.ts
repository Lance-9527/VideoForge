/** Filesystem ownership for the Electron-managed desktop installation. */

import { join } from 'node:path'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'

/**
 * VideoForge 二次开发（2026-10-21）：**我们自己的数据目录**。
 *
 * ★ 为什么必须隔离：DSH 默认把 profile / sessions / attachments 放在
 *   `~/.dsh`（= `%USERPROFILE%\.dsh`）。如果我们的 app 也用它，
 *   就会**写进用户已有的 DSH 环境**（和他的项目、会话、配置混在一起）——
 *   这正是"我们不碰用户的 DSH"这条硬指标的落点。
 *
 * ★ 做法：用 DSH **自带的** `DSH_HOME` 环境变量开关（`resolveDshHome()` 读它），
 *   不改 core（`packages/util/home-paths` 保持上游原样 → 未来跟版本冲突面最小）。
 *   位置：`%LOCALAPPDATA%\VideoForge\dsh`
 *   —— 与 Python sidecar 的业务数据目录 `%LOCALAPPDATA%\VideoForge` 同根不同层，
 *      这样 40 个老项目（sidecar 的 SQLite）原样能用。
 */
export const VIDEOFORGE_DSH_HOME_DIR_NAME = 'dsh'

/** Resolve the VideoForge-owned Harness home (never the shared `~/.dsh`). */
export function resolveVideoForgeDshHome(userData: string): string {
  return join(userData, VIDEOFORGE_DSH_HOME_DIR_NAME)
}

/** Stable desktop installation paths under the shared Harness home. */
export interface DesktopPaths {
  readonly profile: string
  readonly lock: string
}

/**
 * Resolve every Electron-owned path without changing the shared data roots.
 * @param dshHome - Harness home shared with npm-installed dsh.
 * @returns immutable desktop path set.
 */
export function resolveDesktopPaths(dshHome: string = resolveDshHome()): DesktopPaths {
  return {
    profile: join(dshHome, 'profiles', 'desktop'),
    lock: join(dshHome, 'profiles', 'desktop', 'lock'),
  }
}
