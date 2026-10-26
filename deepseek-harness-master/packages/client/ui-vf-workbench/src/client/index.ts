/**
 * `packages/client/ui-vf-workbench` —— **布局/落地层**（不再拥有卡片实现）。
 *
 * 分工（2026-10-21 去重后定稿）：
 *   · 对话卡片（提案/审批/进度/产物）→ **`@deepseek-ai/dsh-client-ui-vf-cards` 独占**
 *     （`tool.call.toolview` 的 keyed 注册只在那边；两个包注册同一批 key 会互相覆盖）
 *   · 本包只留：整屏工作台布局 + 它的接口层（api/SSE/产物预览取材）
 *
 * 载荷与视觉契约见 `docs/vf-card-payload.md`。
 */
import type { Context as ClientContext } from '@deepseek-ai/cordis'
import { VfWorkbench } from './VfWorkbench.tsx'

export { VfWorkbench } from './VfWorkbench.tsx'
export { api, sidecarBase, setSidecarBase, subscribeJob } from './api.ts'
export type {
  VfEstimate,
  VfProject,
  VfRunResult,
  VfStage,
  VfStageStatus,
} from './api.ts'

/** Required service: the slot registry that owns the overlay layer. */
export const inject = ['slots']

/**
 * 客户端插件体：整屏工作台**默认不挂**（默认挂会盖住对话流里的卡片）。
 *
 * 打开方式：地址栏加 `?vf=workbench`（只用于对照/排障）。
 * ⚠️ 不要在本包注册工具卡槽位 —— 那批 key 由 `ui-vf-cards` 独占。
 *
 * @param ctx - client root context.
 */
export function apply(ctx: ClientContext): void {
  let optIn = false
  try {
    optIn = new URLSearchParams(window.location.search).get('vf') === 'workbench'
  } catch {
    optIn = false
  }
  if (!optIn) return
  ctx.slots.inject('shell.overlay', () => ctx.slots.register({
    name: 'shell.overlay',
    id: 'vf.workbench',
    order: 20,
  }, VfWorkbench))
}
