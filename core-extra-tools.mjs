import { readFileSync, writeFileSync } from 'node:fs'

const core = 'D:/VideoForge-dsh/deepseek-harness-master/packages/video/vf-video/lib/core.js'
let text = readFileSync(core, 'utf8')

// ── ① 卡片里带「分镜提示词」清单（供用户过一遍）─────────────────────────
const gateAnchor = `  // ★ 不再往 card.note 里塞「不确认就不会扣费」`
const promptsFetch = `  // ★ 2026-10-22（用户第 5 条）：让用户能"过一遍提示词"。
  //   在即将进入白膜/出片之前（previz / draft / final），把这几个镜头的三段提示词
  //   塞进卡片；用户可以直接说要改哪一条，模型用 vf_prompts(set) 改。
  try {
    if (['previz', 'draft', 'final'].includes(meta.key)) {
      const list = await fetchJson(\`/api/projects/\${encodeURIComponent(pid)}/prompts\`, readOpts);
      const shots = Array.isArray(list && list.shots) ? list.shots : [];
      const brief = shots
        .filter((s) => s && s.compiled)
        .map((s) => ({
          index: s.index,
          video_prompt: String(s.video_prompt || ''),
          negative_prompt: String(s.negative_prompt || ''),
          audio_prompt: String(s.audio_prompt || ''),
        }));
      if (brief.length > 0) card.shots_prompts = brief;
    }
  } catch { /* 没编译过 / 读不到 → 卡片就不带提示词 */ }
`
if (!text.includes('card.shots_prompts')) {
  text = text.replace(gateAnchor, promptsFetch + '  ' + gateAnchor, 1)
  console.log('① 卡片加 shots_prompts')
} else {
  console.log('① 已存在')
}

// ── ② 追加两个工具：vf_prompts / vf_skill ──────────────────────────────
const extra = `
/**
 * 额外工具：提示词读写（vf_prompts）与 skill 库（vf_skill）。
 *
 * 为什么单独成两个工具而不是塞进阶段工具：
 *   · 提示词是"出片前最后一关"，用户要能看能改，改完下次生成立刻生效；
 *   · skill 是"知识"，正文可能几万字，必须**按需拉取**（先 list 再 use），
 *     否则一次塞进上下文就把窗口吃满。
 *
 * @param {object} deps 与 makeToolSpecs 相同的 fetchJson
 * @returns {{vfPrompts: object, vfSkill: object}}
 */
export function makeExtraToolSpecs(deps) {
  const { fetchJson } = deps;
  const nullOut = (v) => v;

  const vfPrompts = {
    name: 'vf_prompts',
    description:
      '[vf-video] 读写分镜提示词（三段式：video_prompt / negative_prompt / audio_prompt）。'
      + 'action=list 列出每镜提示词；action=set 改某一镜（用户说"改第 N 镜"时用）。'
      + '**出片前要把提示词给用户过一遍**：先说清每镜拍什么，再问要不要改。',
    parameters: {
      action: { type: 'string', description: 'list | set' },
      project_id: { type: 'string', description: '项目 id' },
      shot_index: { type: 'number', description: 'set 时：第几镜（从 0 开始，与 list 返回的 index 一致）' },
      video_prompt: { type: 'string', description: 'set 时：新的视频生成提示词' },
      negative_prompt: { type: 'string', description: 'set 时：新的负面提示词' },
      audio_prompt: { type: 'string', description: 'set 时：新的音色/配音提示词' },
    },
    async execute(args) {
      const action = String((args && args.action) || 'list');
      const pid = String((args && args.project_id) || '').trim();
      if (pid === '') return { ok: false, error: '缺少 project_id' };
      const base = \`/api/projects/\${encodeURIComponent(pid)}/prompts\`;
      if (action === 'list') {
        const data = await fetchJson(base);
        const shots = Array.isArray(data && data.shots) ? data.shots : [];
        return { ok: true, count: shots.length, shots };
      }
      if (action === 'set') {
        const listed = await fetchJson(base);
        const shots = Array.isArray(listed && listed.shots) ? listed.shots : [];
        const idx = Number(args && args.shot_index);
        const target = shots.find((s) => Number(s.index) === idx) || shots[idx];
        if (!target) return { ok: false, error: \`没有第 \${idx} 镜（项目里共 \${shots.length} 镜）\` };
        const payload = {};
        for (const key of ['video_prompt', 'negative_prompt', 'audio_prompt']) {
          const value = String((args && args[key]) || '').trim();
          if (value !== '') payload[key] = value;
        }
        if (Object.keys(payload).length === 0) return { ok: false, error: '没有要给的新内容（video_prompt/negative_prompt/audio_prompt 至少给一个）' };
        const res = await fetchJson(\`\${base}/\${encodeURIComponent(String(target.shot_id))}\`, {
          method: 'PUT',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(payload),
        });
        return { ok: true, shot_index: idx, updated: res && res.updated, next: '下一次出片就会用新提示词' };
      }
      return { ok: false, error: \`不认识的 action：\${action}（只支持 list / set）\` };
    },
    output: nullOut,
  };

  const vfSkill = {
    name: 'vf_skill',
    description:
      '[vf-video] 读 skill 知识库（风格手册/短剧公式等）。'
      + 'action=list 看有哪些；action=use 读某一本正文（可能几万字，**只在需要时拉**）。'
      + '用户提到某种风格（邵氏/武侠/短剧爆款）时，先 use 对应 skill 再写剧本或分镜。',
    parameters: {
      action: { type: 'string', description: 'list | use' },
      name: { type: 'string', description: 'use 时：skill 名（从 list 里拿）' },
    },
    async execute(args) {
      const action = String((args && args.action) || 'list');
      if (action === 'list') {
        const data = await fetchJson('/api/skills');
        return { ok: true, skills: (data && data.skills) || [] };
      }
      if (action === 'use') {
        const name = String((args && args.name) || '').trim();
        if (name === '') return { ok: false, error: '缺少 name' };
        const data = await fetchJson(\`/api/skills/\${encodeURIComponent(name)}\`);
        const body = String((data && data.body) || '');
        return { ok: true, name, chars: body.length, body: body.slice(0, 20000) };
      }
      return { ok: false, error: \`不认识的 action：\${action}\` };
    },
    output: nullOut,
  };

  return { vfPrompts, vfSkill };
}
`

if (!text.includes('export function makeExtraToolSpecs')) {
  text += '\n' + extra
  console.log('② 追加 makeExtraToolSpecs')
} else {
  console.log('② 已存在')
}

writeFileSync(core, text)
console.log('core.js 大小', text.length)
