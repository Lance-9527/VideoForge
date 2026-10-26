/** VideoForge 左栏视图状态 —— Rail 与主区面板共用的极小 store。
 *
 * 为什么不用服务：这是**纯前端视图状态**（现在看哪一段、选中哪个项目），
 * 进了 DSH 的服务体系反而要动 core；用 module 级 store + useSyncExternalStore
 * 够用，且不会与宿主的会话/工作区状态打架。
 *
 * @module
 */
import { useSyncExternalStore } from 'react'

/** 左栏三段的标识（与旧版 VideoForge 一致）。 */
export type VfSection = 'chat' | 'assets' | 'post'

/** 当前视图：哪一段 + 选中的项目。 */
export interface VfView {
  /** 主区显示哪一段。 */
  readonly section: VfSection
  /** 选中项目 id（`assets` / `post` 段使用；`chat` 段忽略）。 */
  readonly projectId: string | null
  /** 选中项目标题（面板标题用，省一次请求）。 */
  readonly projectTitle: string | null
  /** 「← AI 对话」要回到哪个会话 —— 进面板之前用户正在看的那个。 */
  readonly returnSessionId: string | null
}

const listeners = new Set<() => void>()
let state: VfView = { section: 'chat', projectId: null, projectTitle: null, returnSessionId: null }

/** 当前快照（`useSyncExternalStore` 的 getSnapshot）。 */
export function vfViewSnapshot(): VfView {
  return state
}

/** 订阅视图变化。 */
export function subscribeVfView(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** 更新视图（浅合并）。 */
export function setVfView(patch: Partial<VfView>): void {
  const next: VfView = { ...state, ...patch }
  if (next.section === state.section && next.projectId === state.projectId
    && next.projectTitle === state.projectTitle && next.returnSessionId === state.returnSessionId) return
  state = next
  for (const listener of listeners) listener()
}

/** React 侧读取当前视图。 */
export function useVfView(): VfView {
  return useSyncExternalStore(subscribeVfView, vfViewSnapshot, vfViewSnapshot)
}
