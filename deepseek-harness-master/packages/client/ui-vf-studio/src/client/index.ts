/**
 * 创作工坊 · 客户端插件体。
 *
 * 它把一个**独立功能页**挂进产品：侧栏多一个图标，主列多一页。
 *   `sidebar.panellist`（icon 列表）←→ `main`（keyed 面板）：list id 与 main key 必须同名，
 *   这是 ui-sidebar 的契约（"Each list id addresses the matching main panel"）。
 *
 * 为什么不挂在 `shell.overlay`（workbench 的做法）：overlay 会盖住对话流，
 * 只适合排障。Lance 要的是"单独一个功能"，所以走正式的面板 + 侧栏入口。
 *
 * 不注册 `tool.call.toolview` —— 那批 key 由 ui-vf-cards 独占，两个包注册会互相覆盖。
 */
import type { Context as ClientContext } from '@deepseek-ai/cordis'
// ★ 下面三行不是"没用到的导入"，是这个框架的类型合同：
//   槽位名（`main` / `sidebar.panellist`）和 `ctx.slots` 服务都是**靠 declare module 合并**出来的，
//   声明分散在各包的 client 面里 —— 不导入就报 TS2339 / TS2345（实测踩了两轮）：
//     · `slots` 服务         → ui-renderer  (interface Context { slots: SlotRegistry })
//     · `main` 槽            → ui-layout
//     · `sidebar.panellist`  → ui-sidebar/src/client/contract/slots.ts
//   ★ 必须用 `/client` 子路径：包根的类型是 host 面，引了没用。
//   纯类型导入 → 构建时擦除，不进产物、不触发 client-bundle 纯净性检查。
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import { VfStudioIcon } from './Icon.tsx'
import { VfStudio } from './Studio.tsx'

export { VfStudio } from './Studio.tsx'
export { VfStudioIcon } from './Icon.tsx'
export { api, sidecarBase, setSidecarBase } from './api.ts'
export type {
  AiLabelPlan,
  CategoryBrief,
  EcomPlan,
  PlatformBrief,
  RemakeResult,
  SellingPointRewrite,
} from './api.ts'

/** 面板 id：`sidebar.panellist` 的 list id 与 `main` 的 key 都用它，必须一致。 */
export const STUDIO_PANEL_ID = 'vf-studio'

/** Required service: the slot registry that owns the panel + sidebar layers. */
export const inject = ['slots']

/**
 * 客户端插件体：注册独立功能页。
 *
 * @param ctx - client root context.
 */
export function apply(ctx: ClientContext): void {
  ctx.slots.inject('main', () =>
    ctx.slots.register(
      {
        name: 'main',
        key: STUDIO_PANEL_ID,
      },
      VfStudio,
    ),
  )

  ctx.slots.inject('sidebar.panellist', () =>
    ctx.slots.register(
      {
        name: 'sidebar.panellist',
        id: STUDIO_PANEL_ID,
        order: 30,
        label: '创作工坊',
      },
      VfStudioIcon,
    ),
  )

  // 排障标记：浏览器控制台/自动化检查能一眼确认这个客户端包真的激活了
  // （宿主不给客户端插件落盘日志，不设标记就只能靠肉眼点界面）。
  try {
    ;(globalThis as unknown as Record<string, unknown>).__vfStudio = {
      panel: STUDIO_PANEL_ID,
      at: new Date().toISOString(),
    }
  } catch {
    /* 标记失败不影响功能 */
  }
}
