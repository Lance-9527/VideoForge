/**
 * 掌柜 AI · 主区错误隔离（Error Boundary）。
 *
 * ★ 为什么必须有：DSH 的 slot 机制是「一个 slot entry 抛错 → 整个 slot 失效」。
 *   实测过：棚拍段里一个 `mark.distance.toFixed` 在旧数据上报错，
 *   结果 **电商段和棚拍段一起消失**，整个应用只剩对话流。
 *
 *   这跟 openvideoforge 的差别很大 —— 它是纯静态页面，一段坏了别的段照样能用。
 *   掌柜 AI 是桌面应用，一段崩了全崩 = 用户什么都干不了。
 *
 * 所以每个段各自包一层：这段炸了，那段显示「这段出错了 + 原因」，
 * 别的段照常用 ✓
 *
 * @module
 */
import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  /** 这一段的名字（出错时显示）。 */
  readonly label: string
  /** 出错时给用户的额外提示。 */
  readonly hint?: string
  readonly children: ReactNode
}

interface State {
  readonly error: Error | null
}

/**
 * 捕获子树渲染错误并降级显示，不让它掀掉整个主区。
 */
export class SectionBoundary extends Component<Props, State> {
  override state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[掌柜 AI] ${this.props.label} 段出错：`, error, info.componentStack)
  }

  override render(): ReactNode {
    const { error } = this.state
    if (error === null) { return this.props.children }
    return (
      <div style={{ padding: '28px 32px', color: '#e8eaed', fontFamily: 'inherit' }}>
        <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 8 }}>
          ⚠ 「{this.props.label}」这一段出错了
        </div>
        <div style={{ fontSize: 13, color: '#ff9a9a', marginBottom: 10, wordBreak: 'break-word' }}>
          {error.message}
        </div>
        {this.props.hint !== undefined ? (
          <div style={{ fontSize: 12, color: '#9aa0a6', marginBottom: 10 }}>{this.props.hint}</div>
        ) : null}
        <button
          type="button"
          onClick={() => { this.setState({ error: null }) }}
          style={{
            font: 'inherit', fontSize: 13, padding: '6px 14px', borderRadius: 8,
            border: '1px solid rgba(255,255,255,0.22)', background: 'rgba(255,255,255,0.06)',
            color: '#e8eaed', cursor: 'pointer',
          }}
        >
          重试这一段
        </button>
        <div style={{ fontSize: 12, color: '#6b7076', marginTop: 12 }}>
          别的段（AI 对话 / 电商 / 棚拍）不受影响，可以继续用。
        </div>
      </div>
    )
  }
}
