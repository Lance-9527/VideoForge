/**
 * 卡片单独预览（开发自检用，不参与 DSH 加载）。
 *
 * 目的：把 `VfProposalCard` 用**真数据形状**渲染出来，和蓝本
 * `D:\VideoForge-review\_audit\ui_fixed_chat.png`（老 VideoForge 的 `.agent-gate`）逐项对照。
 * 加载方式与真实一致：fetch 真实构建产物 `lib/client.js` → `__ModuleLoader__.load` →
 * 执行 factory → 从模块里取 `VfProposalCard`。所以这里看到的样式就是线上样式。
 */
import * as React from 'react'
import { createRoot } from 'react-dom/client'
import * as jsxRuntime from 'react/jsx-runtime'

type LoadedRecord = { id: string; factory: (req: (s: string) => unknown) => any }

const requireShim = (spec: string): unknown => {
  if (spec === 'react') return React
  if (spec === 'react/jsx-runtime') return jsxRuntime
  throw new Error(`harness: 未预期的外部模块 ${spec}`)
}

/** 与蓝本截图同一张卡的字段（stage/title/cost/eta/tweaks/证据 都取自老界面）。 */
const CARD = {
  kind: 'vf',
  card_kind: 'proposal' as const,
  stage: 'compose',
  title: '后期成片',
  why: '把出好的镜头拼成一条完整片子，带转场、可选字幕。未出片的镜头会被跳过并如实告诉你。',
  doing: '把 0 个已有视频的镜头拼成一条成片',
  cost_label: '免费（本地跑，不花钱）',
  cost_yuan: 0,
  eta: '1 ~ 5 分钟',
  tweaks: ['加字幕', '转场全用溶解', '只拼前 3 个镜头', '改文件名'],
  evidence: '场景图 2 张还没出：雨夜街边的便利店外、雨夜街边的便利店内',
  warning: '前置还没做：1080P 成片。现在做可能效果不好，要不要先做它们？',
  can_skip: true,
}

function CardDemo(): React.ReactElement {
  const [log, setLog] = React.useState<string[]>([])
  const [busy, setBusy] = React.useState(false)
  const say = (t: string): void => setLog((v) => [...v, t])

  const actions = {
    onConfirm: (c: any) => {
      setBusy(true)
      say(`→ onConfirm(stage=${c.stage})  =  POST /api/v2/run`)
      window.setTimeout(() => {
        setBusy(false)
        say('（演示）已放回按钮 —— 真实实现里失败也要放回')
      }, 1200)
    },
    onModify: (c: any) => say(`→ onModify(stage=${c.stage})  =  把文字填进 composer`),
    onSkip: (c: any) => say(`→ onSkip(stage=${c.stage})    =  跳过这一步`),
    onTweak: (_c: any, t: string) => say(`→ tweak「${t}」填进 composer`),
  }

  const mod = (window as any).__VF_MODULE
  const Card = mod.VfProposalCard as React.ComponentType<any>
  return React.createElement(
    'div',
    { style: { padding: '18px 22px', maxWidth: 760 } },
    React.createElement(Card, { card: CARD, actions, busy }),
    log.length
      ? React.createElement(
          'pre',
          {
            style: {
              marginTop: 14,
              fontSize: 12,
              color: '#8b93a7',
              whiteSpace: 'pre-wrap',
              fontFamily: 'Consolas, monospace',
            },
          },
          log.join('\n'),
        )
      : null,
  )
}

async function main(): Promise<void> {
  const res = await fetch('../lib/client.js', { cache: 'no-store' })
  if (!res.ok) throw new Error(`取 lib/client.js 失败 HTTP ${res.status}`)
  // eslint-disable-next-line no-new-func
  new Function(await res.text())()
  const record = (window as any).__VF_LOADED as LoadedRecord | undefined
  if (!record) throw new Error('client.js 没有调用 __ModuleLoader__.load')
  ;(window as any).__VF_MODULE = record.factory(requireShim)

  const el = document.getElementById('root')
  if (!el) throw new Error('缺 #root')
  createRoot(el).render(React.createElement(CardDemo))
}

void main().catch((e) => {
  const el = document.getElementById('boot-err')
  if (el) el.textContent = `启动失败：${(e as Error).stack ?? e}`
  console.error(e)
})
