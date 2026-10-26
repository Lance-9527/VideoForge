/**
 * 本地渲染夹具（只用于开发自检，不参与 DSH 加载）。
 *
 * 为什么要有它：这个面板必须能"拿真数据渲染出来"才算数。DSH 里加载它需要在
 * profile / bundle 的 cordis patch 里登记一行（不在本包写范围内），所以先在
 * 浏览器里用同一个 `lib/client.js` 走一遍真实的模块加载契约
 * （`window.__ModuleLoader__.load({id, factory})`）+ 真实 sidecar 数据，
 * 把界面跑出来截图，证明 UI 本身是对的。
 *
 * 加载方式刻意用「fetch + eval」而不是静态 import：真实 DSH 也是把 bundle 当
 * 脚本注入的，这样夹具不会因为打包器的 tree-shaking 把副作用丢掉而失真。
 */
import * as React from 'react'
import { createRoot } from 'react-dom/client'
import * as jsxRuntime from 'react/jsx-runtime'

type LoadedRecord = { id: string; factory: (req: (s: string) => unknown) => any }

const requireShim = (spec: string): unknown => {
  if (spec === 'react') return React
  if (spec === 'react/jsx-runtime') return jsxRuntime
  if (spec === 'react-dom') return { createRoot }
  throw new Error(`harness: 未预期的外部模块 ${spec}`)
}

function bootError(msg: string): void {
  const el = document.getElementById('boot-err')
  if (el) el.textContent = msg
  console.error(msg)
}

async function main(): Promise<void> {
  // 1) 注入真实构建产物（和浏览器里 <script> 加载完全一样的执行方式）
  const res = await fetch('../lib/client.js', { cache: 'no-store' })
  if (!res.ok) throw new Error(`harness: 取 lib/client.js 失败 HTTP ${res.status}`)
  const src = await res.text()
  // eslint-disable-next-line no-new-func
  new Function(src)()

  const record = (window as any).__VF_LOADED as LoadedRecord | undefined
  if (!record) throw new Error('harness: client.js 没有调用 __ModuleLoader__.load（构建产物不对？）')

  // 2) 按 DSH 的契约执行 factory，拿到插件模块
  const mod = record.factory(requireShim)
  const target = mod.VfWorkbench ?? mod.default
  if (typeof target !== 'function') throw new Error('harness: bundle 没有导出 VfWorkbench')

  // 3) 挂载
  const el = document.getElementById('root')
  if (!el) throw new Error('harness: 缺 #root')
  createRoot(el).render(React.createElement(target))
}

void main().catch(e => bootError(`启动失败：${(e as Error).stack ?? e}`))
