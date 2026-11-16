/**
 * 掌柜 AI · 左栏：AI 对话 / 电商 / 棚拍 三段 + 会话列表。
 *
 * 与 VideoForge 的左栏相比**故意做窄**：这个应用只有电商与棚拍两个业务面，
 * 所以不带「资产 / 后期 / 画布 / 项目」——那些是 VideoForge 的事，不该出现在这里。
 *
 * 数据源全是 DSH 自己的服务（会话列表 / 工作区），不另造状态。
 *
 * @module
 */
import { useCallback, useEffect, useState } from 'react'
import type { MouseEvent as ReactMouseEvent, ReactNode } from 'react'

import { setVfView, useVfView } from './view.ts'

/** 掌柜 AI 的配色（暖铜 + 墨蓝，与 VideoForge 的荧光绿区分开）。 */
const SHOP = {
  bg1: '#111114',
  bg2: '#18181c',
  border1: '#2a2a30',
  text1: '#f0f0f3',
  text2: '#b8b8c0',
  text3: '#7a7a85',
  accent: '#e8b062',
  accentSoft: 'rgba(232, 176, 98, 0.14)',
  accentBorder: 'rgba(232, 176, 98, 0.38)',
  radiusMd: 10,
  radiusFull: 999,
} as const

/** 左栏三段。 */
const WORKSPACE_TABS = [
  { key: 'chat', label: 'AI 对话' },
  { key: 'ecom', label: '电商' },
  { key: 'shot', label: '棚拍' },
] as const

/** Store shape the services expose (`getSnapshot` + `subscribe`). */
interface ReadableStore<T> {
  getSnapshot: () => T
  subscribe?: (listener: () => void) => () => void
}

/** One listed session summary. */
interface RailSession {
  readonly id?: string
  readonly title?: string
  readonly messageCount?: number
}

/** Sessions service surface (`@deepseek-ai/dsh-api-session-controller`). */
interface SessionsService {
  readonly list: ReadableStore<{ readonly ids?: readonly string[]; readonly byId?: Record<string, RailSession> }>
}

/** Actions the rail drives — DSH's own, none invented here. */
export interface RailActions {
  openSession: (sessionId: string) => void
  startSession: () => void
  /** DSH's archive action (DSH has no hard-delete client API). */
  archiveSession?: (sessionId: string) => void
  /** Session the shell is currently showing. */
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

/** 一个可滚动的列表容器。 */
function ScrollList({ children, maxHeight }: { children: ReactNode; maxHeight: number }) {
  return (
    <div style={{ maxHeight, overflowY: 'auto', overflowX: 'hidden', paddingRight: 2 }}>
      {children}
    </div>
  )
}

/** One session row. */
function Row({ label, meta, active, onClick, onContextMenu }: {
  label: string
  meta?: string
  active?: boolean
  onClick: () => void
  onContextMenu?: (event: ReactMouseEvent) => void
}) {
  return (
    <div
      onClick={onClick}
      onContextMenu={onContextMenu}
      style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 9px', borderRadius: SHOP.radiusMd,
        cursor: 'pointer', color: active ? SHOP.text1 : SHOP.text2,
        background: active ? SHOP.accentSoft : 'transparent',
        fontSize: 12.5,
      }}
    >
      <span style={{
        flex: 'none', width: 6, height: 6, borderRadius: '50%',
        background: active ? SHOP.accent : SHOP.border1,
      }} />
      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {label}
      </span>
      {meta !== undefined && meta !== ''
        ? <span style={{ flex: 'none', color: SHOP.text3, fontSize: 11 }}>{meta}</span>
        : null}
    </div>
  )
}

/**
 * 掌柜 AI 左栏。
 * @param props - DSH 服务与动作。
 */
export function ShopRail(props: {
  sessions: SessionsService
  actions: RailActions
}) {
  const view = useVfView()
  const sessions = useStore(props.sessions.list)
  const [menu, setMenu] = useState<MenuState | null>(null)

  const ids = sessions?.ids ?? []
  const byId = sessions?.byId ?? {}

  const openMenu = useCallback((event: ReactMouseEvent, sessionId: string): void => {
    event.preventDefault()
    setMenu({
      x: event.clientX,
      y: event.clientY,
      items: [
        { label: '打开这个会话', run: () => { props.actions.openSession(sessionId) } },
        ...(props.actions.archiveSession !== undefined
          ? [{ label: '归档会话（从列表移除）', run: () => { props.actions.archiveSession?.(sessionId) } }]
          : []),
      ],
    })
  }, [props.actions])

  useEffect(() => {
    if (menu === null) return
    const close = (): void => { setMenu(null) }
    window.addEventListener('click', close)
    window.addEventListener('contextmenu', close)
    return () => {
      window.removeEventListener('click', close)
      window.removeEventListener('contextmenu', close)
    }
  }, [menu])

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', background: SHOP.bg1, color: SHOP.text2 }}>
      {/* 段切换 */}
      <div style={{ padding: 10, display: 'flex', flexDirection: 'column', gap: 3 }}>
        {WORKSPACE_TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => { setVfView({ section: item.key, projectId: null, projectTitle: null }) }}
            style={{
              textAlign: 'left', padding: '7px 10px', borderRadius: SHOP.radiusMd,
              border: `1px solid ${view.section === item.key ? SHOP.accentBorder : 'transparent'}`,
              background: view.section === item.key ? SHOP.accentSoft : 'transparent',
              color: view.section === item.key ? SHOP.text1 : SHOP.text2,
              cursor: 'pointer', fontSize: 13, fontWeight: view.section === item.key ? 600 : 400,
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div style={{ height: 1, background: SHOP.border1, flex: 'none' }} />

      {/* 会话 */}
      <div style={{ padding: 10, display: 'flex', flexDirection: 'column', gap: 6, minHeight: 0, flex: 1 }}>
        <button
          type="button"
          onClick={() => { props.actions.startSession() }}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, width: '100%',
            padding: '8px 10px', borderRadius: SHOP.radiusMd,
            border: `1px solid ${SHOP.border1}`, background: SHOP.bg2,
            color: SHOP.text1, cursor: 'pointer', fontSize: 13,
          }}
        >
          <span style={{ fontSize: 15, lineHeight: 1 }}>＋</span>
          新会话
        </button>

        <div style={{ fontSize: 11, color: SHOP.text3, paddingLeft: 2 }}>会话</div>
        <ScrollList maxHeight={999}>
          {ids.length === 0
            ? <div style={{ fontSize: 11.5, color: SHOP.text3, padding: '6px 2px' }}>还没有会话</div>
            : ids.map((id) => {
              const session = byId[id]
              const title = session?.title ?? '新会话'
              return (
                <Row
                  key={id}
                  label={title}
                  {...(session?.messageCount !== undefined && session.messageCount > 0
                    ? { meta: String(session.messageCount) }
                    : {})}
                  active={props.actions.currentSessionId?.() === id}
                  onClick={() => { props.actions.openSession(id) }}
                  onContextMenu={(event) => { openMenu(event, id) }}
                />
              )
            })}
        </ScrollList>
      </div>

      {menu !== null && (
        <div
          style={{
            position: 'fixed', left: menu.x, top: menu.y, zIndex: 60,
            background: SHOP.bg2, border: `1px solid ${SHOP.border1}`, borderRadius: SHOP.radiusMd,
            padding: 4, minWidth: 150, boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
          }}
          onClick={(event) => { event.stopPropagation() }}
        >
          {menu.items.map((item) => (
            <button
              key={item.label}
              type="button"
              onClick={() => { item.run(); setMenu(null) }}
              style={{
                display: 'block', width: '100%', textAlign: 'left',
                padding: '6px 9px', borderRadius: 6, border: 'none', background: 'transparent',
                color: item.danger === true ? '#ff6b6b' : SHOP.text2, cursor: 'pointer', fontSize: 12.5,
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
