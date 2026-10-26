import { defineConfig } from 'tsdown'

/** 四种卡片一起预览（IIFE 单入口，另起一次构建）。 */
export default defineConfig({
  entry: { cards: 'cards.tsx' },
  outDir: 'dist',
  format: ['iife'],
  platform: 'browser',
  target: 'es2022',
  dts: false,
  sourcemap: false,
  clean: false,
  inlineDynamicImports: true,
  external: [],
  noExternal: [/^react($|\/)/, /^react-dom($|\/)/, /^scheduler($|\/)/],
})
