/**
 * 掌柜 AI · **工作台视觉规范**（承自 OpenVideoForge 的设计系统）。
 *
 * ## 为什么不是自己另发明一套
 * 掌柜AI 的「棚拍」和 OpenVideoForge 的「白膜工作台」是同一种工具的两个场景来源
 * （程序化白膜 / 真实高斯泼溅）。两套界面长得不一样，用户要学两遍。
 * 所以这里**直接沿用 OpenVideoForge 已经成型的视觉语言**：
 * 暖米色纸面 + 铜金强调 + 细窄图标轨 + 顶部药丸分组 + 底部时间线。
 *
 * ★ 只搬 **token 与控件规格**，不搬类名 —— OpenVideoForge 的类名没有前缀
 *   （.btn / .pill / .sr-panel …，200+ 顶层类名），整份 CSS 进来会与 DSH
 *   与掌柜AI 自己的类名撞车。这里把 token 变成 TS 常量 + 样式工厂函数，
 *   组件用内联 style（与本包「零 CSS、全内联」的既有约定一致）。
 *
 * ## token 出处
 *   D:\KnowledgeVault\projects\cinemap-teardown\product\css\cinemap.css
 *   D:\KnowledgeVault\projects\cinemap-teardown\product\css\inspector.css
 *
 * @module
 */
import type { CSSProperties } from 'react'

/** OpenVideoForge 的色板（:root 变量原值）。 */
export const C = {
  /** 纸面三阶 */
  cream: '#faf7f0',
  cream2: '#f3efe4',
  cream3: '#ebe4d2',
  /** 墨色四阶 */
  ink: '#1a1612',
  ink2: '#3a3228',
  ink3: '#5c5348',
  ink4: '#847967',
  /** 铜金强调 */
  brass: '#b08d57',
  brass2: '#8a6d40',
  brassLight: '#d4b68a',
  /** 辅助语义色 */
  jade: '#6e8a75',
  coral: '#c2634e',
  /** 分隔线 */
  line: '#d8cfbc',
  line2: '#e8e1d0',
  /** 半透明 */
  brassSoft: 'rgba(176, 141, 87, 0.10)',
  brassLine: 'rgba(176, 141, 87, 0.34)',
  scrim: 'rgba(26, 22, 18, 0.42)',
  scrimSoft: 'rgba(250, 247, 240, 0.86)',
  /** 视口里的暗面板（画中画预览、视口底） */
  pane: '#0a0907',
  pane2: '#16120e',
  paneLine: '#2f2820',
  paneInk: '#e9e3d3',
} as const

/** 字号阶（--fs-*）。 */
export const FS = {
  xxs: 11,
  xs: 12,
  sm: 13,
  base: 14,
  md: 16,
  lg: 20,
} as const

/** 圆角阶（--r-*）。 */
export const R = { xs: 2, sm: 4, md: 6, pill: 999 } as const

/** 阴影阶（--shadow-*，纸面版）。 */
export const SHADOW = {
  s1: '0 1px 2px rgba(26, 22, 18, 0.05)',
  s2: '0 6px 20px rgba(26, 22, 18, 0.08)',
  s3: '0 18px 48px rgba(26, 22, 18, 0.14)',
} as const

/** 动效（--t-* / --ease）。 */
export const T = { fast: '0.2s', slow: '0.32s', ease: 'ease' } as const

/** 字体（--f-sans / --f-serif）。DSH 侧已带 Inter，衬线只用于标题。 */
export const FONT = {
  sans: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
  serif: "'Fraunces', 'Source Han Serif SC', Georgia, serif",
} as const

/** 通用按钮（对应 `.btn`）。 */
export function btn(extra?: CSSProperties): CSSProperties {
  return {
    height: 30,
    padding: '0 13px',
    fontFamily: FONT.sans,
    fontSize: FS.xs,
    fontWeight: 500,
    letterSpacing: '0.01em',
    color: C.ink2,
    background: C.cream,
    border: `1px solid ${C.line}`,
    borderRadius: R.sm,
    boxShadow: SHADOW.s1,
    cursor: 'pointer',
    transition: `background-color ${T.fast} ${T.ease}, border-color ${T.fast} ${T.ease}, box-shadow ${T.fast} ${T.ease}, color ${T.fast} ${T.ease}`,
    ...extra,
  }
}

/** 主按钮（对应 `.btn.primary`：墨底米字，hover 转铜金）。 */
export function btnPrimary(extra?: CSSProperties): CSSProperties {
  return btn({
    background: C.ink,
    borderColor: C.ink,
    color: C.cream,
    boxShadow: 'none',
    ...extra,
  })
}

/** 幽灵按钮（对应 `.btn.ghost`）。 */
export function btnGhost(extra?: CSSProperties): CSSProperties {
  return btn({ background: 'transparent', boxShadow: 'none', color: C.ink3, ...extra })
}

/** 药丸按钮（对应 `.pill`，铜金软底 + 铜金描边）。 */
export function pill(active: boolean, extra?: CSSProperties): CSSProperties {
  return {
    height: 28,
    padding: '0 12px',
    borderRadius: R.pill,
    fontSize: FS.xs,
    fontWeight: 500,
    cursor: 'pointer',
    background: active ? C.brassSoft : C.cream,
    border: `1px solid ${active ? C.brassLine : C.line}`,
    color: active ? C.brass2 : C.ink3,
    transition: `background-color ${T.fast} ${T.ease}, color ${T.fast} ${T.ease}`,
    ...extra,
  }
}

/** 输入框（对应 `.sr-num`）。 */
export function field(extra?: CSSProperties): CSSProperties {
  return {
    height: 28,
    padding: '0 8px',
    fontSize: FS.xs,
    color: C.ink,
    background: C.cream,
    border: `1px solid ${C.line}`,
    borderRadius: R.sm,
    boxShadow: SHADOW.s1,
    outline: 'none',
    fontFamily: FONT.sans,
    ...extra,
  }
}

/** 滑块（对应 `.sr-slider`：铜金轨道 + 铜金滑钮）。 */
export const sliderStyle: CSSProperties = { width: '100%', accentColor: C.brass2 }

/** 开关（对应 `.sr-toggle`：铜金轨道 + 米色滑钮）。 */
export function toggle(on: boolean, extra?: CSSProperties): CSSProperties {
  return {
    width: 34,
    height: 18,
    borderRadius: R.pill,
    border: 'none',
    padding: 0,
    cursor: 'pointer',
    position: 'relative',
    background: on ? C.brass : C.line,
    transition: `background-color ${T.fast} ${T.ease}`,
    ...extra,
  }
}

/** 开关滑钮。 */
export function toggleKnob(on: boolean): CSSProperties {
  return {
    position: 'absolute',
    top: 2,
    left: on ? 18 : 2,
    width: 14,
    height: 14,
    borderRadius: R.pill,
    background: C.cream,
    boxShadow: SHADOW.s1,
    transition: `left ${T.fast} ${T.ease}`,
  }
}

/** 页签条（对应 `.sr-tabs` / `.sr-tab`）。 */
export function tabButton(active: boolean, extra?: CSSProperties): CSSProperties {
  return {
    height: 34,
    padding: '0 12px',
    fontSize: FS.xs,
    fontFamily: FONT.sans,
    cursor: 'pointer',
    background: 'transparent',
    border: 'none',
    borderBottom: `2px solid ${active ? C.brass : 'transparent'}`,
    color: active ? C.ink : C.ink4,
    fontWeight: active ? 600 : 400,
    transition: `color ${T.fast} ${T.ease}, border-color ${T.fast} ${T.ease}`,
    ...extra,
  }
}

/** 分组小标题（对应 `.sr-group` / `.sr-label`）。 */
export function groupLabel(extra?: CSSProperties): CSSProperties {
  return {
    fontSize: FS.xxs,
    fontWeight: 600,
    letterSpacing: '0.06em',
    color: C.ink4,
    textTransform: 'uppercase',
    ...extra,
  }
}
