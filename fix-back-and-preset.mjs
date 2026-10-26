import { readFileSync, writeFileSync } from 'node:fs'

const root = 'D:/VideoForge-dsh/deepseek-harness-master'

// ── ① 关掉「标准模式」下拉（ui-agent-preset 整包）─────────────────────
const patchPath = `${root}/packages/video/vf-video/cordis.patch.yml`
let patch = readFileSync(patchPath, 'utf8')
if (!patch.includes('ui-agent-preset')) {
  patch += `
# ⑧ 去掉「标准模式」下拉（2026-10-22 用户要求：这个功能没必要）。
#
#   那个 chip 是 \`ui-agent-preset\` 注册在 \`conversation.hero.agentPreset\` 的
#   Agent 预设选择器（标准/PTC/极简/创造）。VideoForge 的流程由 9 步 SOP 固定，
#   预设模式对用户是纯噪音，所以整包关掉：chip、会话头部的那枚、以及"插件"里的
#   "创建插件"入口和设置里的「Agent 预设」页会一起消失。
- id: ui-agent-preset
  disabled: true
`
  writeFileSync(patchPath, patch)
  console.log('① patch 已禁用 ui-agent-preset')
} else {
  console.log('① 已存在')
}

// ── ② 修「← AI 对话」返回：uiWorkspace 必须从注入的作用域拿 ─────────────
const idxPath = `${root}/packages/client/ui-vf-brand/src/client/index.tsx`
let idx = readFileSync(idxPath, 'utf8')

if (!idx.includes('uiWorkspaceRef')) {
  idx = idx.replace(
    `  const backToConversation = (): void => {
    const target = vfViewSnapshot().returnSessionId
    setVfView({ section: 'chat', projectId: null, projectTitle: null })
    if (target === null) return
    const workspace = (ctx as unknown as { uiWorkspace?: { openSession: (id: string) => void } }).uiWorkspace
    workspace?.openSession(target)
  }`,
    `  // ★ 2026-10-22 第二次修：上一版从 **root ctx 直接摸 uiWorkspace** —— 它是
  //   注入作用域里的服务，root 上取不到（undefined），于是只剩"收起面板"，
  //   用户看到的就是"点了没反应"。现在改成在 ctx.inject 里拿引用。
  let uiWorkspaceRef: { openSession: (id: string) => void } | undefined
  let sessionsRef: { list?: { getSnapshot: () => { ids?: string[] } } } | undefined
  ctx.inject(['uiWorkspace', 'sessions'], (scope) => {
    uiWorkspaceRef = (scope as unknown as { uiWorkspace: { openSession: (id: string) => void } }).uiWorkspace
    sessionsRef = scope.sessions as unknown as typeof sessionsRef
  })

  const backToConversation = (): void => {
    // 优先回到"进面板时正在看的那个会话"；没记到就退回最近会话。
    const remembered = vfViewSnapshot().returnSessionId
    const fallback = sessionsRef?.list?.getSnapshot()?.ids?.[0] ?? null
    const target = remembered ?? fallback
    setVfView({ section: 'chat', projectId: null, projectTitle: null })
    if (target === null) {
      console.warn('[vf-brand] 返回对话：没有可回到的会话 id')
      return
    }
    if (uiWorkspaceRef === undefined) {
      console.warn('[vf-brand] 返回对话：uiWorkspace 未就绪，只收起了面板')
      return
    }
    uiWorkspaceRef.openSession(target)
  }`,
  )
  writeFileSync(idxPath, idx)
  console.log('② index.tsx 返回逻辑改为注入引用')
} else {
  console.log('② 已存在')
}
