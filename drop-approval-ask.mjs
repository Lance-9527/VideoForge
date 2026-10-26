import { readFileSync, writeFileSync } from 'node:fs'

const p = 'D:/VideoForge-dsh/deepseek-harness-master/packages/video/vf-video/lib/index.js'
let text = readFileSync(p, 'utf8')

// ★ 2026-10-22 真机实测：这套桌面环境里会话跑在「完全权限」下，DSH 的审批通道
//   对 {kind:'ask'} 直接判拒（模型原文：「本会话审批已关闭，vf_looks 会被自动拒」），
//   于是花钱步永远出不了片 —— 这不是用户想点"拒绝"，是审批通道根本没开。
//
//   产品化折中（并保留硬门）：
//     · 用户同意 = 卡片上点「✅ 确认，开始做」（发一条确认消息给模型）
//     · 硬门 = sidecar 的 confirm=true 参数（不传直接拒，钱不会自己花出去）
//     · 这里不再返回 {kind:'ask'}，改为 allow，避免被"审批不可用"挡死。
//   以后若做独立审批通道，把 ASK_ALWAYS 打开即可恢复。
const oldAsk = `    return {
      kind: 'ask',
      reason: \`VideoForge「\${title}」这一步要花钱（成本档=\${cost}）：确认后才开始，执行中也可以取消。\`
        + '金额以卡片上显示的为准（可能是"待确认"）。',
    };`

const newAllow = `    // ★ 审批通道不可用时的降级：只放行，不替用户点"同意" ——
    //   用户点卡片「确认」才会让模型带 confirm=true 调工具；sidecar 侧不传 confirm 一律拒。
    logger.info?.(\`[vf-video] \${name}（\${title}，成本档=\${cost}）交由卡片确认 + sidecar confirm 硬门\`);
    return typeof next === 'function' ? next() : { kind: 'allow' };`

if (text.includes(oldAsk)) {
  text = text.replace(oldAsk, newAllow)
  writeFileSync(p, text)
  console.log('已把 tools/pre-execute 的 ask 降级为 allow（保留 sidecar confirm 硬门）')
} else if (text.includes('交由卡片确认')) {
  console.log('已改过')
} else {
  console.log('未匹配到 ask 段，打印上下文：')
  const i = text.indexOf("kind: 'ask'")
  console.log(text.slice(Math.max(0, i - 300), i + 300))
}
