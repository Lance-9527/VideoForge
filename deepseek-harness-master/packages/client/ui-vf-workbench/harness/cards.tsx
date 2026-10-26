/**
 * 三种卡片一起预览（开发自检用）。
 *
 * 页面里刻意放一条**假的工具轨迹行**，模拟 DSH 对话里的位置关系：
 *   工具行 → 提案卡 / 进度卡 / 审批卡 / 产物卡
 * 媒体用占位文件（真机上指向 sidecar `/api/media?path=…` 与 `/api/projects/{pid}/final`），
 * 因为截图时 sidecar 没起。**不内联字节**这条没有变。
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

const getM = (): any => (window as any).__VF_MODULE || {}

const PROPOSAL = {
  kind: 'vf',
  card_kind: 'proposal',
  stage: 'script',
  title: '剧本',
  why: '剧本决定了故事、场次和人物，后面分镜是按场次拆的。这一步用你的想法生成大纲；你自己有剧本的话也可以直接贴进来导入。',
  doing: '用「雨夜便利店」这个想法生成 2 场戏的大纲',
  cost_label: '很少的钱（一次文字模型调用）',
  cost_yuan: null,
  eta: '30 秒 ~ 2 分钟',
  tweaks: ['换个题材', '改结局', '加一个角色', '缩短到 3 场戏'],
  evidence: '（还没有可展示的证据）',
  can_skip: true,
}

const PROGRESS = {
  kind: 'vf',
  card_kind: 'progress',
  stage: 'looks',
  title: '定妆照 / 场景图',
  why: '正在出图，期间可以继续输入，也可以取消。',
  have: 2,
  total: 5,
}

const APPROVAL = {
  kind: 'vf',
  card_kind: 'approval',
  stage: 'draft',
  title: '480P 小样',
  why: '用便宜档位出一版小样，确认「脸对不对、动作对不对」。小样不满意就驳回，别急着出成片 —— 成片贵得多。',
  cost_label: '按条计费（视频模型，明显花钱）',
  cost_yuan: null,
  cost_floor_yuan: 8.4,
  eta: '每条 2 ~ 8 分钟',
  evidence: '0/2 个镜头出过样片',
  can_skip: true,
}

const ARTIFACTS = {
  kind: 'vf',
  card_kind: 'artifacts',
  title: '后期成片',
  media: [
    { kind: 'video', url: 'assets/final.mp4', name: 'final.mp4', tag: '▶ 视频' },
    { kind: 'image', url: 'assets/look.png', name: '定妆照 · 老板娘.png', tag: '🖼 图片' },
    { kind: 'image', url: 'assets/final-thumb.png', name: '白膜帧 · 第1镜.png', tag: '🖼 图片' },
  ],
  script_text:
    '【雨夜便利店】\n场1 便利店外 · 夜 · 外\n雨下得很密。霓虹灯牌在积水里碎成一片红色。\n（她推门进来，门上的风铃响了一声）\n老板娘：又是这个点。',
  shots: [
    { index: 1, text: '雨夜街边的便利店外空无一人，暖白灯光从玻璃窗内透出，雨水沿玻璃缓慢滑落，在积水的路面倒映出破碎光影。' },
    { index: 2, text: '雨夜街边的便利店内空无一人，镜头贴着玻璃门从街边缓慢移入，门外的雨声被玻璃隔成沉闷连续的白噪音。' },
  ],
}

function ToolRow(props: { name: string; done?: boolean }): React.ReactElement {
  return React.createElement(
    'div',
    {
      style: {
        display: 'flex', alignItems: 'center', gap: 8, margin: '0 0 8px',
        fontSize: 12, color: '#8b93a7',
      },
    },
    React.createElement('span', { style: { color: '#7ac130' } }, props.done ? '✓' : '•'),
    React.createElement('span', { style: { color: '#cfd6e4' } }, props.name),
    React.createElement('span', { style: { opacity: 0.6 } }, props.done ? '完成' : '运行中'),
  )
}

function Section(props: { title: string; children: React.ReactNode }): React.ReactElement {
  return React.createElement(
    'div',
    { style: { marginBottom: 26 } },
    React.createElement(
      'div',
      { style: { color: '#6f7789', fontSize: 11, margin: '0 0 6px', letterSpacing: '.5px' } },
      props.title,
    ),
    props.children,
  )
}

function Demo(): React.ReactElement {
  const M = getM()
  const [log, setLog] = React.useState<string[]>([])
  const say = (t: string): void => setLog((v) => [...v, t])
  const actions = {
    onConfirm: (c: any) => say(`onConfirm(stage=${c.stage}) → POST /api/v2/run`),
    onModify: (c: any) => say(`onModify(stage=${c.stage}) → 填 composer`),
    onSkip: (c: any) => say(`onSkip(stage=${c.stage}) → skip 接口`),
    onTweak: (_c: any, t: string) => say(`tweak「${t}」→ 填 composer`),
  }

  return React.createElement(
    'div',
    { style: { maxWidth: 860, margin: '0 auto', padding: '20px 24px 60px' } },
    React.createElement(Section, { title: '① 提案卡（工具 result）' },
      React.createElement(ToolRow, { name: 'vf_script', done: true }),
      React.createElement(M.VfProposalCard, { card: PROPOSAL, actions })),
    React.createElement(Section, { title: '② 进度卡（工具 start）' },
      React.createElement(ToolRow, { name: 'vf_looks' }),
      React.createElement(M.VfProgressCard, { card: PROGRESS, onCancel: () => say('取消 → job_kill / /api/v2/jobs/{id}/cancel') })),
    React.createElement(Section, { title: '③ 审批卡（花钱那一步）' },
      React.createElement(ToolRow, { name: 'vf_draft' }),
      React.createElement(M.VfApprovalCard, { card: APPROVAL, onApprove: () => say('onApprove → 原生 ctx.approval 批了才跑'), onSkip: () => say('onSkip') })),
    React.createElement(Section, { title: '④ 产物卡（.msg-media 同款）' },
      React.createElement(ToolRow, { name: 'vf_compose', done: true }),
      React.createElement(M.VfArtifactsCard, { card: ARTIFACTS })),
    log.length
      ? React.createElement('pre', {
          style: { marginTop: 8, fontSize: 12, color: '#8b93a7', whiteSpace: 'pre-wrap', fontFamily: 'Consolas, monospace' },
        }, log.join('\n'))
      : null,
  )
}

async function main(): Promise<void> {
  const res = await fetch('../lib/client.js', { cache: 'no-store' })
  if (!res.ok) throw new Error(`取 lib/client.js 失败 HTTP ${res.status}`)
  new Function(await res.text())()
  const record = (window as any).__VF_LOADED as LoadedRecord | undefined
  if (!record) throw new Error('client.js 没有调用 __ModuleLoader__.load')
  ;(window as any).__VF_MODULE = record.factory(requireShim)
  const el = document.getElementById('root')
  if (!el) throw new Error('缺 #root')
  createRoot(el).render(React.createElement(Demo))
}

void main().catch((e) => {
  const el = document.getElementById('boot-err')
  if (el) el.textContent = `启动失败：${(e as Error).stack ?? e}`
})
