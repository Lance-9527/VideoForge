/** VideoForge left rail: 会话 / 工作区 / 项目, rendered over DSH's own services. @module */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent, ReactNode } from 'react'

import { setVfView, useVfView } from './view.ts'

/** Old VideoForge tokens (`frontend/css/main.css` `:root`) — same values as the cards. */
const VF = {
  bg1: '#111114',
  bg2: '#18181c',
  border1: '#2a2a30',
  text1: '#f0f0f3',
  text2: '#b8b8c0',
  text3: '#7a7a85',
  accent: '#7ac130',
  accentSoft: 'rgba(122, 193, 48, 0.12)',
  accentBorder: 'rgba(122, 193, 48, 0.32)',
  radiusMd: 10,
  radiusFull: 999,
} as const

/** Sidecar the 项目 section lists from (same port as the rest of the shell). */
const SIDECAR = 'http://127.0.0.1:8765'

/** One listed session summary, narrowed to the fields this rail renders. */
interface RailSession {
  readonly id?: string
  readonly title?: string
  readonly messageCount?: number
}

/** One sidecar project row. */
interface RailProject {
  readonly id?: string
  readonly title?: string
  /** Sidecar project settings bag — carries the DSH session binding (see `sessionBinding`). */
  readonly settings?: Record<string, unknown> | null
}

/** The workspace tabs VideoForge owns on the left rail. */
const WORKSPACE_TABS = [
  { key: 'chat', label: 'AI 对话' },
  { key: 'assets', label: '资产' },
  { key: 'post', label: '后期' },
] as const

/** Store shape the services expose (`getSnapshot` + `subscribe`). */
interface ReadableStore<T> {
  getSnapshot: () => T
  subscribe?: (listener: () => void) => () => void
}

/** Sessions service surface used here (see `@deepseek-ai/dsh-api-session-controller`). */
interface SessionsService {
  readonly list: ReadableStore<{ readonly ids?: readonly string[]; readonly byId?: Record<string, RailSession> }>
}

/** Workspace service surface used here (see `@deepseek-ai/dsh-api-workspace-controller/client`). */
interface WorkspacesService {
  readonly list: ReadableStore<{
    readonly items?: readonly unknown[]
    /** 已归档会话：`sessions.list` 里仍然有它们，列表必须自己过滤掉。 */
    readonly archivedSessionIds?: readonly string[]
  }>
}

/** Actions the rail drives — all of them are DSH's own or the sidecar's, none invented here. */
export interface RailActions {
  openSession: (sessionId: string) => void
  startSession: () => void
  /** DSH's own archive action: archived sessions leave this list (that is DSH's "delete"). */
  archiveSession?: (sessionId: string) => void
  /** Names of the session the shell is currently showing, for binding a project to it. */
  currentSessionId?: () => string | null
}

/**
 * Subscribe to a store without assuming a hook contract.
 * @param store - Store exposing `getSnapshot` and (optionally) `subscribe`.
 * @returns The current snapshot, refreshed on change.
 */
function useStore<T>(store: ReadableStore<T> | undefined): T | undefined {
  const [value, setValue] = useState<T | undefined>(() => store?.getSnapshot())
  useEffect(() => {
    if (store === undefined) return
    setValue(store.getSnapshot())
    if (store.subscribe === undefined) return
    return store.subscribe(() => setValue(store.getSnapshot()))
  }, [store])
  return value
}

/** 右键菜单里的一项。 */
interface MenuItem {
  readonly label: string
  readonly danger?: boolean
  readonly run: () => void
}

/** 右键菜单状态。 */
interface MenuState {
  readonly x: number
  readonly y: number
  readonly items: readonly MenuItem[]
}

/** 一个可滚动的列表容器：会话/项目多了以后必须能滑，否则看不到全部。 */
function ScrollList({ children, maxHeight }: { children: ReactNode; maxHeight: number }) {
  return (
    <div style={{ maxHeight, overflowY: 'auto', overflowX: 'hidden', paddingRight: 2 }}>
      {children}
    </div>
  )
}

/** One rail row; `active` mirrors DSH's own selection, never a local guess. */
function Row({ label, meta, active, onClick, onContextMenu }: {
  label: string
  meta?: string | undefined
  active?: boolean | undefined
  onClick?: (() => void) | undefined
  onContextMenu?: ((event: ReactMouseEvent) => void) | undefined
}) {
  return (
    <div
      onClick={onClick}
      onContextMenu={onContextMenu}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '7px 9px',
        borderRadius: VF.radiusMd,
        fontSize: 13,
        cursor: onClick === undefined ? 'default' : 'pointer',
        color: active === true ? VF.text1 : VF.text2,
        background: active === true ? VF.accentSoft : 'transparent',
        border: `1px solid ${active === true ? VF.accentBorder : 'transparent'}`,
      }}
    >
      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {label}
      </span>
      {meta !== undefined && <span style={{ fontSize: 11, color: VF.text3 }}>{meta}</span>}
    </div>
  )
}

/** Section header, matching the old rail's small uppercase captions. */
function Section({ title, action, onAction }: { title: string; action?: string; onAction?: () => void }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 4px 5px' }}>
      <span style={{ flex: 1, fontSize: 11, letterSpacing: '0.08em', color: VF.text3 }}>{title}</span>
      {action !== undefined && (
        <button
          type="button"
          onClick={onAction}
          style={{
            fontSize: 11,
            padding: '1px 7px',
            borderRadius: VF.radiusFull,
            border: `1px solid ${VF.border1}`,
            background: 'transparent',
            color: VF.text3,
            cursor: 'pointer',
            font: 'inherit',
          }}
        >
          {action}
        </button>
      )}
    </div>
  )
}

/** 右键菜单：点空白处或按 Esc 关闭。 */
function ContextMenu({ state, onClose }: { state: MenuState; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey) }
  }, [onClose])

  return (
    <>
      {/* 透明遮罩：点任何地方先关闭菜单（比监听 document 更可靠，不会被 React 事件顺序坑） */}
      <div
        onClick={onClose}
        onContextMenu={(event) => { event.preventDefault(); onClose() }}
        style={{ position: 'fixed', inset: 0, zIndex: 10_000 }}
      />
      <div
        style={{
          position: 'fixed',
          left: state.x,
          top: state.y,
          zIndex: 10_001,
          minWidth: 150,
          padding: 4,
          borderRadius: 8,
          border: `1px solid ${VF.border1}`,
          background: VF.bg2,
          boxShadow: '0 10px 28px rgba(0,0,0,0.45)',
        }}
      >
        {state.items.map((item) => (
          <button
            key={item.label}
            type="button"
            onClick={() => { onClose(); item.run() }}
            style={{
              display: 'block',
              width: '100%',
              textAlign: 'left',
              padding: '6px 9px',
              borderRadius: 6,
              border: 'none',
              background: 'transparent',
              color: item.danger === true ? '#ff6b6b' : VF.text2,
              fontSize: 12.5,
              cursor: 'pointer',
              font: 'inherit',
            }}
            onMouseEnter={(event) => { event.currentTarget.style.background = 'rgba(255,255,255,0.06)' }}
            onMouseLeave={(event) => { event.currentTarget.style.background = 'transparent' }}
          >
            {item.label}
          </button>
        ))}
      </div>
    </>
  )
}

/**
 * 把「项目」绑定到某个 DSH 会话 id。
 *
 * 为什么绑定写在项目 settings 里（而不是只在内存/localStorage）：
 *   · sidecar 的 `ProjectUpdate` 本来就支持 `settings` 这个自由字段，不用改表结构；
 *   · 项目会跟着库走（换机器/重装都还在），localStorage 会丢；
 *   · 面板上的「← AI 对话」就是靠它精确回到**这个项目的**会话，而不是"最近的会话"。
 */
async function bindSessionToProject(projectId: string, sessionId: string): Promise<boolean> {
  try {
    const response = await fetch(`${SIDECAR}/api/projects/${encodeURIComponent(projectId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: { dsh_session_id: sessionId } }),
    })
    return response.ok
  } catch {
    return false
  }
}

/** 删掉一个 sidecar 项目（真删除，不是归档）。 */
async function deleteSidecarProject(projectId: string): Promise<boolean> {
  try {
    const response = await fetch(`${SIDECAR}/api/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' })
    return response.ok
  } catch {
    return false
  }
}

/**
 * The VideoForge left rail.
 *
 * Data and actions come from DSH services (`sessions.list`, `workspaces.list`, and
 * `uiWorkspace.openSession/startSession`), so multi-session, search, restore and
 * create keep working; only the rendering moves here.
 * @param props - Services plus the action bridge supplied by the shell registration.
 */
export function VfRail(props: { sessions?: SessionsService; workspaces?: WorkspacesService; actions: RailActions }) {
  const sessions = useStore(props.sessions?.list)
  const workspaces = useStore(props.workspaces?.list)
  const view = useVfView()
  const [projects, setProjects] = useState<readonly RailProject[] | null>(null)
  const [menu, setMenu] = useState<MenuState | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const notify = useCallback((message: string) => {
    setToast(message)
    if (toastTimer.current !== undefined) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => { setToast(null) }, 2600)
  }, [])

  useEffect(() => () => { if (toastTimer.current !== undefined) clearTimeout(toastTimer.current) }, [])

  const reloadProjects = useCallback(async () => {
    try {
      const response = await fetch(`${SIDECAR}/api/projects`)
      const body = await response.json() as { data?: RailProject[] | { projects?: RailProject[] } }
      const list = Array.isArray(body.data) ? body.data : body.data?.projects ?? []
      setProjects(list)
    } catch {
      setProjects(null)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const response = await fetch(`${SIDECAR}/api/projects`)
        const body = await response.json() as { data?: RailProject[] | { projects?: RailProject[] } }
        const list = Array.isArray(body.data) ? body.data : body.data?.projects ?? []
        if (!cancelled) setProjects(list)
      } catch {
        if (!cancelled) setProjects(null)
      }
    })()
    return () => { cancelled = true }
  }, [])

  const allSessionIds = sessions?.ids ?? []
  const sessionById = sessions?.byId ?? {}
  // ★ 2026-10-24（用户反馈「右键删除没反应」）：DSH 的归档不会把会话从
  //   `sessions.list` 里拿掉，归档态在 `workspaces.list.archivedSessionIds`。
  //   不过滤的话，点了「归档会话」看起来就跟没删一样。
  const archivedIds = workspaces?.archivedSessionIds ?? []
  const archived = new Set<string>(archivedIds)
  const sessionIds = allSessionIds.filter((id) => !archived.has(id))

  /** 打开会话并把主区拉回对话。 */
  const openSession = (id: string): void => {
    setVfView({ section: 'chat', projectId: null, projectTitle: null, returnSessionId: id })
    props.actions.openSession(id)
  }

  /**
   * 这个项目绑定的会话 id（无效绑定返回 null）。
   *
   * ★ 2026-10-24（用户第二次反馈同一问题后重做）：
   *   绑定只认**权威来源** —— 建项目那一刻的会话，由 vf_project 工具写进 project.settings.dsh_session_id。
   *   以前是"点项目时把当前会话写进去"，可那个"当前会话"常常是空白新会话，
   *   于是「← AI 对话」就跳到空白对话框上 —— 那正是用户说的体验问题。
   *
   *   这里还要挡一层：绑定的会话如果已不在列表（归档/删了），当作没绑定，
   *   绝不能把人带到一个不存在的对话。
   */
  const boundSessionOf = (project: RailProject): string | null => {
    const raw = project.settings?.dsh_session_id
    if (typeof raw !== 'string' || raw === '') return null
    if ((projects ?? []).some((other) => other.id !== project.id && other.settings?.dsh_session_id === raw)) return null
    return sessionIds.includes(raw) ? raw : null
  }

  /** 打开项目面板，并把"这个项目的会话"记进 view。 */
  const openProject = (project: RailProject): void => {
    if (project.id === undefined) return
    // Only a persisted binding identifies a project conversation. Titles are not identities.
    const bound = boundSessionOf(project)
    setVfView({
      section: 'assets',
      projectId: project.id,
      projectTitle: project.title ?? null,
      returnSessionId: bound,
    })
  }

  /** 手动把某个项目绑到"当前正在看的会话"（给历史上没绑定的项目用）。 */
  const bindProjectToCurrent = (project: RailProject): void => {
    if (project.id === undefined) return
    const current = props.actions.currentSessionId?.() ?? null
    if (current === null) {
      notify('没有可绑定的会话：先打开一个 AI 对话再绑')
      return
    }
    const occupied = (projects ?? []).find((other) => other.id !== project.id && other.settings?.dsh_session_id === current)
    if (occupied !== undefined) {
      notify(`当前会话已绑定项目「${occupied.title ?? '未命名项目'}」，请为这个项目打开独立会话`)
      return
    }
    void (async () => {
      const ok = await bindSessionToProject(project.id as string, current)
      notify(ok ? '已绑定到当前会话' : '绑定失败，请重试')
      await reloadProjects()
    })()
  }

  /** 解绑（下次点项目不再跳会话）。 */
  const unbindProject = (project: RailProject): void => {
    if (project.id === undefined) return
    void (async () => {
      const ok = await bindSessionToProject(project.id as string, '')
      notify(ok ? '已解绑' : '解绑失败，请重试')
      await reloadProjects()
    })()
  }

  const askDeleteProject = (project: RailProject): void => {
    if (project.id === undefined) return
    const title = project.title ?? '未命名项目'
    // 真删除前必须二次确认：项目里的资产/分镜/成片会一起没
    // eslint-disable-next-line no-alert
    if (!window.confirm(`删除项目「${title}」？\n\n该项目的资产、分镜、成片记录都会从库里删除，不可恢复。`)) return
    void (async () => {
      const ok = await deleteSidecarProject(project.id as string)
      if (ok) {
        notify(`已删除项目「${title}」`)
        await reloadProjects()
      } else {
        notify('删除失败：sidecar 没有返回成功')
      }
    })()
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, padding: '10px 8px', background: VF.bg1, height: '100%', minHeight: 0, overflowY: 'auto' }}>
      {/* ★ 2026-10-24（用户要求）：会话/项目会越来越多，两段列表都要能滚。
          会话给 38vh、项目给 30vh —— 合计不会把「工作区」挤没，同时都能看到全部。 */}
      <div>
        {/* ★ 2026-10-21（用户第 4 轮）：去掉「会话」右上角那个小「新建」——
            它和顶部的大按钮「新会话」是同一件事，两个入口并排只会让人犹豫点哪个。 */}
        <Section title="会话" />
        {sessionIds.length === 0 && (
          <div style={{ fontSize: 12, color: VF.text3, padding: '4px 9px' }}>还没有会话</div>
        )}
        <ScrollList maxHeight={Math.round(window.innerHeight * 0.42)}>
          {sessionIds.map((id) => {
            const summary = sessionById[id]
            return (
              <Row
                key={id}
                label={summary?.title ?? '未命名会话'}
                meta={summary?.messageCount === undefined ? undefined : `${summary.messageCount} 条`}
                active={view.section === 'chat' && view.projectId === null && summary?.title === undefined ? false : undefined}
                onClick={() => { openSession(id) }}
                onContextMenu={(event) => {
                  event.preventDefault()
                  const items: MenuItem[] = [
                    { label: '打开这个会话', run: () => { openSession(id) } },
                  ]
                  if (props.actions.archiveSession !== undefined) {
                    items.push({
                      label: '归档会话（从列表移除）',
                      danger: true,
                      run: () => {
                        void Promise.resolve(props.actions.archiveSession?.(id))
                          .then(() => { notify('已归档，已从列表移除') })
                          .catch((error: unknown) => {
                            notify(`归档失败：${error instanceof Error ? error.message : String(error)}`)
                          })
                      },
                    })
                  }
                  setMenu({ x: event.clientX, y: event.clientY, items })
                }}
              />
            )
          })}
        </ScrollList>
      </div>

      <div>
        <Section title="工作区" />
        {WORKSPACE_TABS.map((item) => (
          <Row
            key={item.key}
            label={item.label}
            active={view.section === item.key}
            onClick={() => {
              if (item.key === 'chat') {
                setVfView({ section: 'chat', projectId: null, projectTitle: null })
                return
              }
              // Switching an existing project between assets/post keeps its own binding.
              const selected = (projects ?? []).find((project) => project.id === view.projectId)
              setVfView({ section: item.key, returnSessionId: selected === undefined ? null : boundSessionOf(selected) })
            }}
          />
        ))}
      </div>

      <div style={{ minHeight: 0 }}>
        <Section title="项目" />
        {projects === null && <div style={{ fontSize: 12, color: VF.text3, padding: '4px 9px' }}>读取中…</div>}
        {projects !== null && projects.length === 0 && (
          <div style={{ fontSize: 12, color: VF.text3, padding: '4px 9px' }}>还没有项目</div>
        )}
        <ScrollList maxHeight={Math.round(window.innerHeight * 0.3)}>
          {(projects ?? []).map((project) => (
            <Row
              key={project.id ?? project.title}
              label={project.title ?? '未命名'}
              active={project.id !== undefined && project.id === view.projectId}
              onClick={() => {
                // 点项目 = 在右侧打开该项目的详情（进度 / 资产图 / 成片）。
                // ★ 2026-10-24：**不再**在点击时写绑定（那是错的来源）；绑定来自建项那一刻。
                openProject(project)
              }}
              onContextMenu={(event) => {
                event.preventDefault()
                const items: MenuItem[] = [
                  { label: '打开项目', run: () => { openProject(project) } },
                ]
                if (boundSessionOf(project) !== null) {
                  items.push({ label: '解绑会话', run: () => { unbindProject(project) } })
                } else {
                  items.push({ label: '绑定到当前会话', run: () => { bindProjectToCurrent(project) } })
                }
                items.push({ label: '删除项目…', danger: true, run: () => { askDeleteProject(project) } })
                setMenu({ x: event.clientX, y: event.clientY, items })
              }}
            />
          ))}
        </ScrollList>
      </div>

      {menu !== null && <ContextMenu state={menu} onClose={() => { setMenu(null) }} />}
      {toast !== null && (
        <div style={{
          position: 'fixed', left: 16, bottom: 16, zIndex: 10_002,
          padding: '8px 12px', borderRadius: 8, fontSize: 12,
          background: VF.bg2, border: `1px solid ${VF.border1}`, color: VF.text2,
          boxShadow: '0 10px 28px rgba(0,0,0,0.45)',
        }}>
          {toast}
        </div>
      )}
    </div>
  )
}

export { VF as VF_RAIL_TOKENS }
