/** VideoForge shell occupants: brand marks, left rail, and the main-area panel. */
import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-api-session-controller/client'
import { VfLogo, VfName } from './Brand.tsx'
import { setVfView, vfViewSnapshot } from './view.ts'
import { VfModelsSection } from './SettingsSection.tsx'
import { VfPanel } from './Panel.tsx'
import { VfRail } from './Rail.tsx'
import { registerAccountUi } from './account-registration.ts'

/** Required service: the UI slot registry. */
export const inject = ['slots']

/** Actions `ui-workspace` publishes for opening and creating sessions. */
interface UiWorkspaceService {
  /** Real signature is `openSession(target: SessionTarget) => void`; `unknown` keeps this file free of ui-workspace type imports. */
  openSession: (target: unknown) => void
  startSession: () => void
  /** DSH 的归档：归档后会话从列表消失（DSH 没有硬删除会话的客户端 API）。 */
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
  const apply = (): void => { if (document.title !== 'VideoForge') document.title = 'VideoForge' }
  apply()
  const observer = new MutationObserver(apply)
  observer.observe(document.head, { subtree: true, childList: true, characterData: true })
}

/**
 * Fill the VideoForge-owned surfaces without touching core:
 *
 * - `sidebar.brand.mark` / `sidebar.brand.name` — VideoForge brand (after the Web
 *   bundle patch disables `ui-brand-official`).
 * - `sidebar.workspaces` at `priority: -10` — the 会话 / 工作区 / 项目 rail. The
 *   `single` slot renders its lowest-priority occupant, so this shadows
 *   `ui-workspace`'s browser while `ui-workspace` itself stays loaded (its
 *   `uiWorkspace` service is load-bearing for a dozen other client bundles).
 * - `shell.overlay` — the start page, gated on the conversation's `data-phase`.
 * - `conversation.input.dock` — the standing backend/progress card.
 * @param ctx - Client root context.
 */
export function apply(ctx: ClientContext): void {
  ownWindowTitle()
  registerAccountUi(ctx)
  let lastSessionId: string | null = null

  // ★ 2026-10-22（用户两次强调）：面板上「← AI 对话」要**回到对应的那个对话**，
  //   而不是只关掉面板。做法：进面板时记下当时看的会话（view.returnSessionId），
  //   点返回时先 openSession 它，再把面板收起。
  // ★ 2026-10-22 第二次修：上一版从 **root ctx 直接摸 uiWorkspace** —— 它是
  //   注入作用域里的服务，root 上取不到（undefined），于是只剩"收起面板"，
  //   用户看到的就是"点了没反应"。现在改成在 ctx.inject 里拿引用。
  let uiWorkspaceRef: { openSession: (id: string) => void } | undefined
  let sessionsRef: { list?: { getSnapshot: () => { ids?: string[] } } } | undefined
  ctx.inject(['uiWorkspace', 'sessions'], (scope) => {
    uiWorkspaceRef = (scope as unknown as { uiWorkspace: { openSession: (id: string) => void } }).uiWorkspace
    sessionsRef = scope.sessions as unknown as typeof sessionsRef
  })

  const backToConversation = (): string | void => {
    // ★ 2026-10-24（用户第二次反馈）：只回到**这个项目绑定/标题匹配**的会话。
    //   以前没绑定时会退回"最近会话"（ids[0]），而最近会话常常是一个空白新对话 ——
    //   用户看到的就是"点回AI对话跳到空白框"。现在没有目标就**留在当前对话**，
    //   只把面板收起，绝不主动切走。
    const view = vfViewSnapshot()
    const target = view.returnSessionId
    if (target === null && view.projectId !== null) {
      return '这个项目尚未绑定可用会话。请打开对应会话后，在项目右键菜单中选择「绑定到当前会话」。'
    }
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
    ctx.slots.register({ name: 'sidebar.brand.mark' }, VfLogo))

  ctx.slots.inject('sidebar.brand.name', () =>
    ctx.slots.register({ name: 'sidebar.brand.name' }, VfName))

  // ★ 2026-10-21 第三轮：首屏大标题左边那个图标也换成我们的 V 标。
  //   槽位 `conversation.hero.brand.mark`（single/root）由 EmptyHero 用
  //   `{ size: 34, className: css.fish }` 渲染 —— 我们接管后就用同一个尺寸出图。
  ctx.slots.inject('conversation.hero.brand.mark', () =>
    ctx.slots.register({ name: 'conversation.hero.brand.mark' }, (props: { size?: number }) => (
      <VfLogo size={props.size ?? 34} />
    )))

  // ★ 「设置 → 模型」按类型分区（文本 / 视频 / 音频）—— 用户要求学旧版 VideoForge 的分法。
  //   挂在官方设置面板的 `settings.section`（list 槽：id/order/label），不另开窗口。
  //
  //   为什么这里要放松类型：`settings.section` 的 SlotMap 声明住在 `dsh-client-ui-settings`，
  //   而本包不想为了一行注册去加一个 client 依赖（那会牵动 dsh.client.inject 与 pnpm 链接）。
  //   运行时那个槽一定存在（设置面板就是它渲染的），所以按结构断言即可。
  const slotsLoose = ctx.slots as unknown as {
    inject: (name: string, register: () => unknown) => void
    register: (options: Record<string, unknown>, component: unknown) => unknown
  }
  slotsLoose.inject('settings.section', () => slotsLoose.register(
    { name: 'settings.section', id: 'videoforge-models', order: 40, label: 'VideoForge 模型' },
    VfModelsSection,
  ))

  // Rail data and actions are DSH's: the services are read for rendering, and
  // session opening/creation goes through uiWorkspace's own methods.
  ctx.inject(['sessions', 'workspaces', 'uiWorkspace'], (scope) => {
    const workspace = (scope as unknown as { uiWorkspace: UiWorkspaceService }).uiWorkspace
    const sessions = scope.sessions as SessionService
    const actions = {
      openSession: (sessionId: string): void => { lastSessionId = sessionId; workspace.openSession(sessionId) },
      startSession: (): void => { lastSessionId = null; workspace.startSession() },
      // ★ 2026-10-24：会话右键「删除」= DSH 的归档（DSH 没有硬删除会话的客户端 API）。
      archiveSession: (sessionId: string): void => { void workspace.archiveSession(sessionId, { stopActivity: true }) },
      // 把「当前正在看的会话」绑到项目上 —— 项目面板的「← AI 对话」靠它精确回到本项目的对话。
      currentSessionId: (): string | null => lastSessionId,
    }
    ctx.slots.register({ name: 'sidebar.workspaces', priority: -10 }, () => (
      <VfRail
        sessions={sessions as never}
        workspaces={scope.workspaces as never}
        actions={actions}
      />
    ))
  })

  // ★ 主区面板（资产 / 后期 / 项目详情）：占 `shell.overlay`，只在左栏选了
  //   「资产 / 后期」或点了某个项目时出现，点「AI 对话」就收起（返回 null）。
  //   这是 VideoForge 自己的信息架构，DSH 里没有对应页面 —— core 零改动。
  ctx.slots.inject('shell.overlay', function* () {
    yield ctx.slots.register({ name: 'shell.overlay', id: 'vf-panel', order: 20 }, () => (
      <VfPanel onBack={backToConversation} />
    ))
  })

  // ★ 去掉底部的「VideoForge 后端已连接 http://127.0.0.1:8765」状态条
  //   （2026-10-21 用户反馈：这条没必要一直在）。后端真挂了，工具调用会直接报错，
  //   模型会把原因说出来，比一直挂个状态条更不吵。
}
