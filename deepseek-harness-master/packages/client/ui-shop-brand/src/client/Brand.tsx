/**
 * 掌柜 AI 品牌件：侧栏 logo 与字标。
 *
 * 名字取「掌柜」——电商里掌柜就是**当铺的人**：懂货、懂规矩、还能亲自上架拍摄。
 * 做成 agent 应用正好对上：它替你查平台规格、审文案合规、摆场景、拍成片。
 *
 * ★ logo 是内联 SVG（不依赖运行时文件，也不用另出图），
 *   造型与应用图标**同源**：黑底 + 银色金属算盘 + 胶片打孔横杆 + 底部橙光。
 *   —— 和 VideoForge 的「金属 V + 胶片齿孔 + 底部火光」是同一套视觉语言，
 *   一眼能看出是一家，但主体符号（算盘 / V）不会混淆。
 *
 * @module
 */

/** 金属暗部。 */
const METAL_DARK = '#7d7f8c'
/** 底部火光橙。 */
const EMBER = '#f2842c'

/**
 * 掌柜 AI 的 logo。
 * @param props - 渲染边长（px），默认 24。
 */
export function ShopLogo({ size = 24 }: { size?: number }) {
  const frame = size * 0.88
  const offset = (size - frame) / 2
  const bar = size * 0.055
  const bead = size * 0.085
  const barYs = [size * 0.36, size * 0.52, size * 0.68]
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      role="img"
      aria-label="掌柜 AI"
      style={{ display: 'block', flex: 'none' }}
    >
      {/* 底：纯黑 + 底部火光（VideoForge 同款） */}
      <defs>
        <radialGradient id="shop-ember" cx="50%" cy="100%" r="55%">
          <stop offset="0%" stopColor={EMBER} stopOpacity="0.85" />
          <stop offset="60%" stopColor={EMBER} stopOpacity="0.18" />
          <stop offset="100%" stopColor={EMBER} stopOpacity="0" />
        </radialGradient>
        <linearGradient id="shop-metal" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#f2f3f7" />
          <stop offset="45%" stopColor={METAL_DARK} />
          <stop offset="60%" stopColor="#e6e8ef" />
          <stop offset="100%" stopColor="#9a9ca8" />
        </linearGradient>
      </defs>
      <rect x={offset} y={offset} width={frame} height={frame} rx={size * 0.22} fill="#000000" />
      <rect x={offset} y={offset} width={frame} height={frame} rx={size * 0.22} fill="url(#shop-ember)" />

      {/* 算盘框 */}
      <rect
        x={size * 0.17}
        y={size * 0.22}
        width={size * 0.66}
        height={size * 0.56}
        rx={size * 0.07}
        fill="#14151a"
        stroke="url(#shop-metal)"
        strokeWidth={size * 0.07}
      />

      {/* 三根胶片横杆（两端打孔 —— VideoForge 的齿孔语汇） */}
      {barYs.map((y, index) => {
        const shift = index % 2 === 0 ? -size * 0.15 : size * 0.15
        return (
          <g key={y}>
            <line
              x1={size * 0.2}
              y1={y}
              x2={size * 0.8}
              y2={y}
              stroke="#6d6f7c"
              strokeWidth={bar}
              strokeLinecap="round"
            />
            {[size * 0.245, size * 0.755].map((x) => (
              <rect
                key={x}
                x={x - size * 0.028}
                y={y - size * 0.028}
                width={size * 0.056}
                height={size * 0.056}
                rx={size * 0.012}
                fill="#d9dbe3"
              />
            ))}
            {[0, 1, 2].map((k) => (
              <circle
                key={k}
                cx={size * 0.5 + shift + (k - 1) * size * 0.19}
                cy={y}
                r={bead}
                fill="url(#shop-metal)"
                stroke="#3c3d46"
                strokeWidth={size * 0.02}
              />
            ))}
          </g>
        )
      })}
    </svg>
  )
}

/** 侧栏字标。 */
export function ShopName() {
  return (
    <span style={{ fontWeight: 700, letterSpacing: '-0.01em', fontSize: 15, color: 'inherit' }}>掌柜 AI</span>
  )
}
