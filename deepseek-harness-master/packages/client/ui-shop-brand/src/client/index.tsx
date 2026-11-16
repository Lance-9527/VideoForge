/**
 * 掌柜 AI —— 独立的电商 agent 应用（基于 DSH）。
 *
 * ## 为什么是另一个应用
 * 2026-11-03 Lance 要求：把 VideoForge 里的**电商**与**棚拍**独立出去，
 * 重新做成一个**基于 DSH 的电商 agent 应用**。
 * 于是有了这个 client bundle —— 它只提供电商与棚拍两个业务面，
 * 品牌、窗口标题、左栏信息架构全归自己，DSH core 零改动。
 *
 * ## 接缝
 * - `sidebar.brand.mark` / `sidebar.brand.name` —— 侧栏品牌（与官方品牌同槽竞争）
 * - `conversation.hero.brand.mark` —— 首屏大标题图标
 * - `sidebar.workspaces`（priority -10）—— 左栏三段 + 会话列表
 * - `shell.overlay` —— 主区面板（电商工作台 / 棚拍）
 *
 * @module
 */
import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-api-session-controller/client'
import { ShopLogo, ShopName } from './Brand.tsx'
import { setVfView, vfViewSnapshot } from './view.ts'
import { ShopPanel } from './Panel.tsx'
import { ShopRail } from './Rail.tsx'
import { loadLocalServices } from './local-services.ts'

/** Required service: the UI slot registry. */
export const inject = ['slots']

/** 本应用的产品名（窗口标题 / 首屏都读它）。 */
export const APP_NAME = '掌柜 AI'

/** Actions `ui-workspace` publishes for opening and creating sessions. */
interface UiWorkspaceService {
  openSession: (target: unknown) => void
  startSession: () => void
  archiveSession: (sessionId: string, options?: { readonly stopActivity?: boolean }) => Promise<void>
}

/** Store-shaped service handle (`getSnapshot` + `subscribe`). */
interface SessionService {
  readonly list: { getSnapshot: () => unknown; subscribe?: (listener: () => void) => () => void }
}

/**
 * Keep the Electron window title on the product name this shell ships.
 *
 * The Web document title is DSH's, so the shell re-asserts ours; a MutationObserver
 * keeps it after DSH rewrites the head on navigation.
 */
function ownWindowTitle(): void {
  const apply = (): void => { if (document.title !== APP_NAME) document.title = APP_NAME }
  apply()
  const observer = new MutationObserver(apply)
  observer.observe(document.head, { subtree: true, childList: true, characterData: true })
}

/**
 * Fill the 掌柜 AI-owned surfaces without touching core.
 * @param ctx - Client root context.
 */
export function apply(ctx: ClientContext): void {
  ownWindowTitle()
  // ★ 启动时向主进程问一次本地服务端口（sidecar 跟品牌走、模型运行时跟实际绑定走）。
  //   读到的模型运行时地址会真的接到深度模型上 —— 端口变了不该静默降级。
  void loadLocalServices().catch((error: unknown) => {
    console.warn('[shop] 读取本地服务端口失败，用默认端口：', error)
  })
  let lastSessionId: string | null = null

  // ★ sessions / uiWorkspace 都是**注入作用域**里的服务，root ctx 上取不到
  //   （VideoForge 实测踩过：undefined ⇒ 点「返回对话」没反应）。
  let uiWorkspaceRef: { openSession: (id: string) => void } | undefined
  let sessionScopeRef: {
    scope: (id: string) => { get: (name: string) => unknown } | undefined
  } | undefined
  ctx.inject(['uiWorkspace', 'sessions'], (scope) => {
    uiWorkspaceRef = (scope as unknown as { uiWorkspace: UiWorkspaceService }).uiWorkspace
    sessionScopeRef = scope.sessions as unknown as typeof sessionScopeRef
  })

  const deliver = (conversation: { send: (t: string) => Promise<void> } | undefined, text: string): boolean => {
    if (conversation === undefined) return false
    void conversation.send(text).catch((error: unknown) => {
      console.error('[shop] send failed:', error)
    })
    return true
  }

  const scopedConversation = (sessionId: string): { send: (t: string) => Promise<void> } | undefined => {
    if (sessionScopeRef === undefined) return undefined
    const scoped = sessionScopeRef.scope(sessionId)
    return scoped?.get('conversation') as { send: (t: string) => Promise<void> } | undefined
  }

  const sendToAgent = (sessionId: string, text: string): void => {
    if (sessionScopeRef === undefined) {
      console.error('[shop] 会话服务还没就绪')
      return
    }
    if (deliver(scopedConversation(sessionId), text)) return
    if (uiWorkspaceRef !== undefined) {
      try {
        uiWorkspaceRef.openSession(sessionId)
      } catch (error) {
        console.error('[shop] openSession 失败:', error)
      }
      window.setTimeout(() => {
        if (!deliver(scopedConversation(sessionId), text)) {
          console.error('[shop] 打开会话后仍取不到 conversation 服务，sessionId=', sessionId)
        }
      }, 1200)
    }
  }

  const backToConversation = (): string | void => {
    const view = vfViewSnapshot()
    const target = view.returnSessionId
    if (target === null) {
      setVfView({ section: 'chat', projectId: null, projectTitle: null })
      return
    }
    if (uiWorkspaceRef === undefined) {
      return '对话服务还未就绪，请稍后重试。'
    }
    uiWorkspaceRef.openSession(target)
    lastSessionId = target
    setVfView({ section: 'chat', projectId: null, projectTitle: null })
  }

  ctx.slots.inject('sidebar.brand.mark', () =>
    ctx.slots.register({ name: 'sidebar.brand.mark' }, ShopLogo))

  ctx.slots.inject('sidebar.brand.name', () =>
    ctx.slots.register({ name: 'sidebar.brand.name' }, ShopName))

  ctx.slots.inject('conversation.hero.brand.mark', () =>
    ctx.slots.register({ name: 'conversation.hero.brand.mark' }, (props: { size?: number }) => (
      <ShopLogo size={props.size ?? 34} />
    )))

  // Rail data and actions are DSH's: the services are read for rendering, and
  // session opening/creation goes through uiWorkspace's own methods.
  ctx.inject(['sessions', 'workspaces', 'uiWorkspace'], (scope) => {
    const workspace = (scope as unknown as { uiWorkspace: UiWorkspaceService }).uiWorkspace
    const sessions = scope.sessions as SessionService
    const actions = {
      openSession: (sessionId: string): void => { lastSessionId = sessionId; workspace.openSession(sessionId) },
      startSession: (): void => { lastSessionId = null; workspace.startSession() },
      archiveSession: (sessionId: string): void => { void workspace.archiveSession(sessionId, { stopActivity: true }) },
      currentSessionId: (): string | null => lastSessionId,
    }
    ctx.slots.register({ name: 'sidebar.workspaces', priority: -10 }, () => (
      <ShopRail sessions={sessions as never} actions={actions} />
    ))
  })

  ctx.slots.inject('shell.overlay', function* () {
    yield ctx.slots.register({ name: 'shell.overlay', id: 'shop-panel', order: 20 }, () => (
      <ShopPanel
        onBack={backToConversation}
        onSend={(text: string, sessionId: string) => { sendToAgent(sessionId, text) }}
      />
    ))
  })

  // 排障标记：自动化检查能一眼确认这个客户端包真的激活了
  try {
    ;(globalThis as unknown as Record<string, unknown>).__shopApp = {
      app: APP_NAME,
      at: new Date().toISOString(),
    }
  } catch {
    /* 标记失败不影响功能 */
  }
}
