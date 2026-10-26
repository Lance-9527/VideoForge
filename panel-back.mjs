import { readFileSync, writeFileSync } from 'node:fs'

const base = 'D:/VideoForge-dsh/deepseek-harness-master/packages/client/ui-vf-brand/src/client'

// ── ① view.ts：加 returnSessionId ──────────────────────────────────────
let view = readFileSync(`${base}/view.ts`, 'utf8')
if (!view.includes('returnSessionId')) {
  view = view.replace(
    `  /** 选中项目标题（面板标题用，省一次请求）。 */
  readonly projectTitle: string | null
}`,
    `  /** 选中项目标题（面板标题用，省一次请求）。 */
  readonly projectTitle: string | null
  /** 「← AI 对话」要回到哪个会话 —— 进面板之前用户正在看的那个。 */
  readonly returnSessionId: string | null
}`,
  )
  view = view.replace(
    `let state: VfView = { section: 'chat', projectId: null, projectTitle: null }`,
    `let state: VfView = { section: 'chat', projectId: null, projectTitle: null, returnSessionId: null }`,
  )
  view = view.replace(
    `  if (next.section === state.section && next.projectId === state.projectId
    && next.projectTitle === state.projectTitle) return`,
    `  if (next.section === state.section && next.projectId === state.projectId
    && next.projectTitle === state.projectTitle && next.returnSessionId === state.returnSessionId) return`,
  )
  writeFileSync(`${base}/view.ts`, view)
  console.log('① view.ts 加 returnSessionId')
} else {
  console.log('① view.ts 已存在')
}

// ── ② Rail.tsx：点项目/资产/后期时记住当前会话 ─────────────────────────
let rail = readFileSync(`${base}/Rail.tsx`, 'utf8')
if (!rail.includes('returnSessionId')) {
  rail = rail.replace(
    `  const sessionIds = sessions?.ids ?? []`,
    `  // ★ 记住"进面板之前正在看的那个会话"：点「← AI 对话」时回到它。
  //   DSH 的 session list 是 host 顺序（最近的在前），取第一项最接近"当前"。
  const rememberSession = (): string | null => (sessions?.ids ?? [])[0] ?? null
  const sessionIds = sessions?.ids ?? []`,
  )
  rail = rail.replace(
    `              setVfView({ section: item.key })`,
    `              setVfView({ section: item.key, returnSessionId: rememberSession() })`,
  )
  rail = rail.replace(
    `              setVfView({ section: 'assets', projectId: project.id, projectTitle: project.title ?? null })`,
    `              setVfView({
                section: 'assets',
                projectId: project.id,
                projectTitle: project.title ?? null,
                returnSessionId: rememberSession(),
              })`,
  )
  writeFileSync(`${base}/Rail.tsx`, rail)
  console.log('② Rail.tsx 记录 returnSessionId')
} else {
  console.log('② Rail.tsx 已存在')
}

// ── ③ index.tsx：给 Panel 传 onBack ────────────────────────────────────
let index = readFileSync(`${base}/index.tsx`, 'utf8')
if (!index.includes('onBack={')) {
  index = index.replace(
    `  ctx.slots.inject('shell.overlay', function* () {
    yield ctx.slots.register({ name: 'shell.overlay', id: 'vf-panel', order: 20 }, VfPanel)
  })`,
    `  ctx.slots.inject('shell.overlay', function* () {
    yield ctx.slots.register({ name: 'shell.overlay', id: 'vf-panel', order: 20 }, () => (
      <VfPanel onBack={backToConversation} />
    ))
  })`,
  )
  // 在 apply 顶部定义 backToConversation（用 ctx 里的 sessions/uiWorkspace）
  index = index.replace(
    `export function apply(ctx: ClientContext): void {
  ownWindowTitle()`,
    `export function apply(ctx: ClientContext): void {
  ownWindowTitle()

  // ★ 2026-10-22（用户两次强调）：面板上「← AI 对话」要**回到对应的那个对话**，
  //   而不是只关掉面板。做法：进面板时记下当时看的会话（view.returnSessionId），
  //   点返回时先 openSession 它，再把面板收起。
  const backToConversation = (): void => {
    const target = vfViewSnapshot().returnSessionId
    setVfView({ section: 'chat', projectId: null, projectTitle: null })
    if (target === null) return
    const workspace = (ctx as unknown as { uiWorkspace?: { openSession: (id: string) => void } }).uiWorkspace
    workspace?.openSession(target)
  }`,
  )
  index = index.replace(
    `import { VfLogo, VfName } from './Brand.tsx'`,
    `import { VfLogo, VfName } from './Brand.tsx'
import { setVfView, vfViewSnapshot } from './view.ts'`,
  )
  writeFileSync(`${base}/index.tsx`, index)
  console.log('③ index.tsx 传入 onBack')
} else {
  console.log('③ index.tsx 已存在')
}
