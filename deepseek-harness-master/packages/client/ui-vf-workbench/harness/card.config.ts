import { defineConfig } from 'tsdown'

/**
 * 卡片预览单独一份配置（IIFE 不支持多入口，所以和 harness 分成两次构建）。
 */
export default defineConfig({
  entry: { card: 'card.tsx' },
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
