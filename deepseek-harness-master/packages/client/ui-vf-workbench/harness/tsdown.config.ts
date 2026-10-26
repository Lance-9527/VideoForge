import { defineConfig } from 'tsdown'

export default defineConfig({
  entry: { harness: 'entry.tsx' },
  outDir: 'dist',
  format: ['iife'],
  platform: 'browser',
  target: 'es2022',
  dts: false,
  sourcemap: false,
  clean: false,
  inlineDynamicImports: true,
  // React 是本包声明的 peerDependency，tsdown 默认把它当外部依赖；
  // 夹具是单文件自包含页面，必须把 React 打进来。
  external: [],
  noExternal: [/^react($|\/)/, /^react-dom($|\/)/, /^scheduler($|\/)/],
})
