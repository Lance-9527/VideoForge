/**
 * 创作工坊 · 侧栏图标。
 *
 * 单独成 `.tsx`：`index.ts` 只做注册（纯 TS），JSX 必须待在 `.tsx` 里。
 * 几何：跟随侧栏给的 `size`（16/18px）与选中态 `active`，与其它面板图标同规格。
 */
import type { ReactElement } from 'react'

export function VfStudioIcon(props: { size: number; active: boolean }): ReactElement {
  const { size, active } = props
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={active ? 2.2 : 1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {/* 调色板 + 播放箭头：既"创作"又"成片" */}
      <path d="M12 3a9 9 0 1 0 0 18h1.5a2.5 2.5 0 0 0 0-5H13a2 2 0 0 1 0-4h4a4 4 0 0 0 0-8h-5z" />
      <circle cx="7.5" cy="10.5" r="1" fill="currentColor" stroke="none" />
      <circle cx="9.5" cy="6.8" r="1" fill="currentColor" stroke="none" />
      <circle cx="14" cy="6.8" r="1" fill="currentColor" stroke="none" />
    </svg>
  )
}
