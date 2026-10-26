import { globSync } from 'node:fs'
import { defineConfig } from 'tsdown'
import { typertPlugin } from './packages/typert/generator/lib/types/tsdown-plugin.js'
import { WORKSPACE_TYPERT_FACES } from './scripts/typert-faces.ts'
import { workspacePackageGlobs } from './scripts/workspace-package-globs.ts'

function isBuildFaceClient(value: unknown): boolean {
  if (value === undefined || value === 'host') return false
  if (value === 'client') return true
  throw new Error(`tsdown: --env.DSH_BUILD_FACE must be host or client, received ${String(value)}`)
}

/**
 * The ordinary workspace build consumes JavaScript emitted by the Host
 * TypeScript project and runs Typert. The Client pass selects packages that
 * declare a browser bundle and lets their package-local configs emit both
 * their Node loader entry and browser artifact. `apps/desktop` bundles after
 * this pass (root package.json `build:lib:host`): its main bundle inlines
 * workspace devDependencies from their lib/ output, and tsdown builds
 * workspace members concurrently without ordering them.
 */

/**
 * VideoForge fork: our bundle ships hand-written ESM JavaScript
 * (lib/index.js + lib/core.js, ported from the validated dsh-plugin prototype),
 * so it does not take the 'tsc emits lib/types, then tsdown bundles it' path.
 *
 * This workspace pass demands an entry lib/types/{index,startup}.js from every
 * member; without the exclusion the Host bundle stage fails with
 *   [@deepseek-ai/dsh-vf-video] Cannot find entry: ["lib/types/{index,startup}.js"]
 * which aborts build:official and therefore packaging (measured 2026-10-21).
 *
 * The exclusion is build-only: release:pack still packs the package as a family
 * member, because release family discovery uses its own glob list in
 * scripts/release/families.ts, independent of this one.
 */
const PREBUILT_PACKAGES = [
  // VideoForge fork: hand-written ESM client bundle (no lib/types entry).
  'packages/client/ui-vf-workbench',
  'packages/video/vf-video',
  // VideoForge fork: vendored upstream dsh-zcode-browser-use@0.9.0 (侧栏自有浏览器).
  // 它的 index.js / lib/*.js 都是上游**已构建**的 ESM，没有 src/ 也没有 lib/types；
  // 不排除掉，Host bundle 门会以「Cannot find entry: lib/types/{index,startup}.js」
  // 整段失败（2026-10-26 实测）。client bundle 走它自带的 lib/client.js。
  'packages/browser-use/dsh-zcode-browser-use',
]

export default defineConfig(({ env }) => {
  const client = isBuildFaceClient(env?.DSH_BUILD_FACE)
  const packages = workspacePackageGlobs(globSync(['vendor/*/package.json', 'packages/*/*/package.json'], { cwd: import.meta.dirname }))
    .filter(directory => !PREBUILT_PACKAGES.includes(directory))
  return {
    workspace: [...packages, 'apps/cli', ...client ? [] : ['apps/desktop-host']],
    entry: client ? '' : ['lib/types/{index,startup}.js'],
    outDir: 'lib',
    format: ['esm'],
    platform: 'node',
    target: 'es2024',
    fixedExtension: false,
    dts: false,
    clean: false,
    plugins: client ? [] : [typertPlugin({ mode: 'workspace', faces: WORKSPACE_TYPERT_FACES })],
  }
})
