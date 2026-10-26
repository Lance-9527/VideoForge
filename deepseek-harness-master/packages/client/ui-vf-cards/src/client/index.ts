/** VideoForge conversation cards: one keyed toolview per `vf_*` Tool. */
import { createElement } from 'react'
import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-client-ui-tool/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { VfToolRow, type VfCardActions } from './VfCard.tsx'

export { VfToolRow, GateCard, ProgressCard, ArtifactCard, readPayload, vfMoney, COST_LABEL } from './VfCard.tsx'
export type { VfCardPayload, VfCardActions, VfMedia } from './VfCard.tsx'

/** Required services: slot registry + the session tracker that turns an id into a session scope. */
export const inject = ['slots', 'sessions']

/**
 * Tool names this bundle renders with a VideoForge card.
 *
 * `tool.call.toolview` is keyed by wire Tool name and explicitly admits names from
 * any package (`packages/client/ui-tool/src/client/contract/slots.ts:26`), so claiming
 * these keys replaces the generic row with a card **in the transcript** and needs no
 * core change. `vf_status` / `vf_project` deliberately keep the generic row: they are
 * lookups, not steps.
 */
const STEP_TOOLS = [
  'vf_script',
  'vf_assets',
  'vf_looks',
  'vf_shots',
  'vf_previz',
  'vf_draft',
  'vf_final',
  'vf_compose',
] as const

/** The slice of `ctx.sessions` this bundle needs. */
interface SessionScopeTracker {
  scope: (id: string) => { get: (name: string) => unknown } | undefined
}

/**
 * Install one card row per VideoForge step tool.
 *
 * 两条关键写法（都是真机踩出来的）：
 *
 * 1. **必须用 `ctx.slots.inject(...)` 包一层**再 register（同 ui-deliverables / QueueDock）：
 *    槽位是别的包声明的，直接 register 可能早于声明 ⇒ 注册丢了、卡片不出现，
 *    而且不报错（表现就是"工具调用了但只有一行默认文本"）。
 * 2. **`send` 要走会话作用域**：`conversation.send()` 内部用 `scopeId(this.ctx)` 取会话标签，
 *    根作用域调用会抛
 *    `conversation.send requires a session scope — address one via ctx.sessions.scope(id).conversation`；
 *    旧代码 `void conversation.send(...)` 把这个 reject 吞了 ⇒ 表现为"按钮点了没反应"。
 *    所以用槽位的 `inject(sessionId)` 工厂，在工厂里 `ctx.sessions.scope(sessionId)`。
 *
 * @param ctx - Client root context.
 */
export function apply(ctx: ClientContext): void {
  const sessions = ctx.sessions as unknown as SessionScopeTracker
  for (const key of STEP_TOOLS) {
    // ★ 与 ui-deliverables 完全一致的写法：`slots.inject(槽位, () => slots.register(...))`，
    //   每个 key 独立注册、返回 register 自己的 disposer（不要自己包一层函数 —— 实测那样会在
    //   重渲染后整批失效，卡片又变回默认文本行）。
    ctx.slots.inject('tool.call.toolview', () => ctx.slots.register({
      name: 'tool.call.toolview',
      key,
      inject: (sessionId: string): { actions: VfCardActions } => {
        const actx = sessions.scope(sessionId)
        const conversation = actx?.get('conversation') as { send: (text: string) => Promise<void> } | undefined
        return {
          actions: {
            send: (text: string): void => {
              if (conversation === undefined) {
                console.error('[vf-cards] 找不到该会话的 conversation 服务，sessionId=', sessionId)
                return
              }
              void conversation.send(text).catch((error: unknown) => {
                console.error('[vf-cards] send failed:', error)
              })
            },
          },
        }
      },
    }, (props: unknown) => {
      const view = props as { phase?: string; block?: unknown; actions?: VfCardActions }
      if (view.actions === undefined) {
        // 这条只在注入链路断掉时出现；有它就能一眼看出"点了没反应"是注入没到。
        console.warn('[vf-cards] 卡片没拿到 actions（注入未生效）', key)
      }
      return createElement(VfToolRow, { phase: view.phase, block: view.block, actions: view.actions })
    }))
  }
}
