/** VideoForge 品牌件：侧栏 logo 与字标。
 *
 * 图来自用户提供的 logo（V + 胶片孔 + 火花），生成脚本见
 * `D:\VideoForge-dsh\brand\make-brand.py`；产物已内联成 data URL（见 `./mark.ts`），
 * 这样 client bundle 自带品牌图，不依赖运行时文件。
 *
 * @module
 */
import { VF_MARK_DATA_URL } from './mark.ts'

/**
 * VideoForge 的 logo（白瓦片 + V 图形）。
 *
 * 为什么是白瓦片：原 logo 是深色金属 V，直接放在暗色侧栏上几乎看不见；
 * 白瓦片既保留原图观感，又能在暗底上立住（与安装包图标同一张图）。
 *
 * @param props - 渲染边长（px），默认 24。
 */
export function VfLogo({ size = 24 }: { size?: number }) {
  return (
    <img
      src={VF_MARK_DATA_URL}
      width={size}
      height={size}
      alt="VideoForge"
      style={{ display: 'block', borderRadius: Math.round(size * 0.24), flex: 'none' }}
    />
  )
}

/**
 * 侧栏字标。
 *
 * 只放字标：`sidebar.brand.name` 的槽位很窄，塞 slogan 会把品牌行挤成两行。
 */
export function VfName() {
  return (
    <span style={{ fontWeight: 700, letterSpacing: '-0.01em', fontSize: 15, color: 'inherit' }}>VideoForge</span>
  )
}
