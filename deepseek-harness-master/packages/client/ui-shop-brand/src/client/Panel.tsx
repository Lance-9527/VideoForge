/**
 * 掌柜 AI · 主区面板：电商工作台 / 棚拍。
 *
 * `section === 'chat'` 时返回 null（完全让位给对话流）——
 * 这是 DSH 的 `shell.overlay` 槽位契约：注册一个组件，由它决定显示什么。
 *
 * ★ 布局照抄 VideoForge 的 Panel：overlay 是**固定定位**的一层，
 *   自己盖住主区（left = 侧栏宽，top = 窗口标题栏高）——
 *   不这么写的话内容会挤在左上角，右边一大片空白。
 *
 * ★ 这一层还是**具名指令轮询器的家**（与当前页签无关）：
 *   轮询器以前挂在「棚拍」页签里，用户切到电商页/对话页就没人消费命令了
 *   （sidecar 队列上限 64，满了丢最老的）。现在轮询器住在这一层，
 *   场景执行器由 ShotScene 挂载时注册进来；没有场景时回一条「场景未就绪」。
 *
 * @module
 */
import type { CSSProperties } from 'react'
import { useEffect, useRef } from 'react'
import { useVfView } from './view.ts'
import { EcommercePanel } from './EcommercePanel.tsx'
import { VfShotScene } from './ShotScene.tsx'
import { SectionBoundary } from './SectionBoundary.tsx'
import { startCommandPoller, type CommandRunner } from './scene-commands.ts'
import { sidecarBase } from './api.ts'

/** 掌柜 AI 的深色底。 */
const C = {
  bg: '#0d0d10',
  text: '#f0f0f3',
} as const

/** Electron's non-standard CSS property is absent from React's browser CSS types. */
type DesktopSurfaceStyle = CSSProperties & { WebkitAppRegion?: 'drag' | 'no-drag' }

/** 固定铺满主区的一层。 */
const shellStyle: DesktopSurfaceStyle = {
  position: 'fixed',
  left: 'var(--dsh-windows-sidebar-width, 280px)',
  top: 'var(--dsh-frame-chrome-top, 0px)',
  right: 0,
  bottom: 0,
  zIndex: 35,
  background: C.bg,
  color: C.text,
  pointerEvents: 'auto',
  WebkitAppRegion: 'no-drag',
  fontFamily: 'inherit',
  overflow: 'hidden',
}

/**
 * 主区面板。
 * @param props - 回到对话的回调 + 发给 agent 的回调。
 */
export function ShopPanel(props: {
  readonly onBack: () => void
  readonly onSend?: ((text: string, sessionId: string) => void) | undefined
}) {
  const view = useVfView()
  /**
   * 场景指令执行器（ShotScene 挂载时注册进来）。
   *
   * ★ 用可变的 ref 而不是 state：轮询器每 1.2 秒问一次「现在谁能执行」，
   *   走 state 会让整棵面板树跟着重渲染。
   */
  const sceneRunnerRef = useRef<CommandRunner | null>(null)

  /**
   * 轮询 sidecar 的具名指令队列（与当前页签无关）。
   *
   * ★ 这一层是常驻的：切到电商页/对话页时组件还在（只是返回 null），
   *   轮询照跑 —— agent 的命令不会因为用户换了个页签就没人接。
   */
  useEffect(() => {
    const poller = startCommandPoller({
      // 端口由主进程决定，每轮现取（别把过期地址烤进来）。
      sidecarBase: () => sidecarBase(),
      run: async (tool, args) => {
        const runner = sceneRunnerRef.current
        if (runner === null) {
          throw new Error('场景未就绪：请在「棚拍」页签打开场景编辑器')
        }
        return runner(tool, args)
      },
      onError: (error) => { console.warn('[掌柜 AI] 场景指令轮询失败：', error) },
    })
    return () => { poller.stop() }
  }, [])

  if (view.section === 'chat') { return null }

  if (view.section === 'ecom') {
    return (
      <div style={{ ...shellStyle, display: 'flex', flexDirection: 'column' }}>
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '18px 22px 40px' }}>
          {/* ★ 每段各自包一层错误隔离 —— 一段崩了不该把别的段一起掀掉 */}
          <SectionBoundary label="电商" hint="试试刷新页面，或回「AI 对话」重新发一次方案。">
            <EcommercePanel />
          </SectionBoundary>
        </div>
      </div>
    )
  }

  if (view.section === 'shot') {
    return (
      <div style={{ ...shellStyle, display: 'flex', flexDirection: 'column' }}>
        <div style={{ flex: 1, minHeight: 0, padding: '10px 22px' }}>
          <SectionBoundary label="棚拍" hint="场景没载入成功时，先从左侧「模型」页签点一个场景。">
            <VfShotScene
              sessionId={view.returnSessionId}
              sceneRunnerRef={sceneRunnerRef}
              onSend={props.onSend !== undefined
                ? (text: string) => { props.onSend?.(text, view.returnSessionId ?? '') }
                : undefined}
            />
          </SectionBoundary>
        </div>
      </div>
    )
  }

  return null
}
