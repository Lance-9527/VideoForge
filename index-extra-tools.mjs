import { readFileSync, writeFileSync } from 'node:fs'

const p = 'D:/VideoForge-dsh/deepseek-harness-master/packages/video/vf-video/lib/index.js'
let text = readFileSync(p, 'utf8')

// ① 引入 makeExtraToolSpecs
if (!text.includes('makeExtraToolSpecs')) {
  text = text.replace(
    "import { makeToolSpecs, stageMeta, DEFAULT_BASE_URL, DEFAULT_HTTP_TIMEOUT_MS } from './core.js';",
    "import { makeExtraToolSpecs, makeToolSpecs, stageMeta, DEFAULT_BASE_URL, DEFAULT_HTTP_TIMEOUT_MS } from './core.js';",
  )
}

// ② 注册两个额外工具（放在 vf_project 之后）
const anchor = "  wrap(specs.vfProject, '@vf/vf-video: vf_project');"
const added = anchor + `
  // ★ 2026-10-22：提示词读写 + skill 库 —— 用户要求"提示词要能过一遍、能改"，
  //   以及"VideoForge 要能学习（吸收风格手册）"。
  const extraSpecs = makeExtraToolSpecs({ fetchJson });
  wrap(extraSpecs.vfPrompts, '@vf/vf-video: vf_prompts');
  wrap(extraSpecs.vfSkill, '@vf/vf-video: vf_skill');`
if (!text.includes('extraSpecs.vfPrompts')) {
  text = text.replace(anchor, added, 1)
}

// ③ SOP 里补两条硬规则
const sopAnchor = "    '· **剧本必须带 idea**：vf_script 的 idea = 用户想拍什么（原话）；空 idea 会被后端直接判失败。',"
const sopAdded = sopAnchor + `
    '· **出片前把分镜提示词给用户过一遍**：分镜做完后调用 vf_prompts(action=list)，',
    '  把每一镜的 video_prompt（画面/镜头/动作）和 audio_prompt（台词/环境音）用 Markdown 列出来，',
    '  然后问「这版提示词要不要改？确认后我再进白膜/出片」。用户说要改哪一镜，就用',
    '  vf_prompts(action=set, shot_index=N, video_prompt=...) 改；改完再列一次给他看。',
    '· 用户提到某种风格（邵氏武侠 / 短剧爆款 / 竖屏剧）时，先 vf_skill(action=list) 看有没有对应手册，',
    '  有就 vf_skill(action=use, name=...) 读进来，再按它写剧本或分镜。',`
if (!text.includes('vf_prompts(action=list)')) {
  text = text.replace(sopAnchor, sopAdded, 1)
}

writeFileSync(p, text)
console.log('index.js 已接入 vf_prompts / vf_skill 与 SOP 规则')
