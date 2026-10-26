"""把接龙信息暴露到接口并显示在卡片上：
① main.py 的 /prompts 响应加 chain_mode / relay_first_frame / continuity_brief
② core.js 的 buildGateCard：shots_prompts 每项带上 chain_mode
③ VfCard.tsx：接龙的镜显示「🔗 接上一镜」徽标 + 连续性快照
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')

# ── ① main.py ───────────────────────────────────────────────────────────
mp = Path(r'D:\VideoForge-dev\backend\main.py')
text = mp.read_text(encoding='utf-8')
old = """                "character_anchor": prompts.get("character_anchor") or "",
                "scene_anchor": prompts.get("scene_anchor") or "","""
new = """                "character_anchor": prompts.get("character_anchor") or "",
                "scene_anchor": prompts.get("scene_anchor") or "",
                # ★ 跨镜接龙信息：这一镜是不是接着上一镜的尾帧出的
                "chain_mode": prompts.get("chain_mode") or "independent",
                "relay_first_frame": prompts.get("relay_first_frame") or "",
                "continuity_brief": prompts.get("continuity_brief") or "","""
if old in text and 'chain_mode' not in text:
    shutil.copyfile(mp, mp.with_suffix(mp.suffix + '.bak-' + STAMP))
    mp.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('① main.py 已暴露接龙字段')
else:
    print('① main.py 已存在或未匹配')

# ── ② core.js ──────────────────────────────────────────────────────────
core = Path(r'D:\VideoForge-dsh\deepseek-harness-master\packages\video\vf-video\lib\core.js')
text = core.read_text(encoding='utf-8')
old = """        .map((s) => ({
          index: s.index,
          video_prompt: String(s.video_prompt || ''),
          negative_prompt: String(s.negative_prompt || ''),
          audio_prompt: String(s.audio_prompt || ''),
        }));"""
new = """        .map((s) => ({
          index: s.index,
          video_prompt: String(s.video_prompt || ''),
          negative_prompt: String(s.negative_prompt || ''),
          audio_prompt: String(s.audio_prompt || ''),
          // ★ 这一镜是不是"接上一镜的尾帧"出的（跨镜连贯）
          chain_mode: String(s.chain_mode || 'independent'),
          continuity_brief: String(s.continuity_brief || ''),
        }));"""
if old in text:
    core.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('② core.js 卡片载荷已带 chain_mode')
else:
    print('② core.js 未匹配')

# ── ③ VfCard.tsx ───────────────────────────────────────────────────────
card = Path(r'D:\VideoForge-dsh\deepseek-harness-master\packages\client\ui-vf-cards\src\client\VfCard.tsx')
text = card.read_text(encoding='utf-8')
old_type = """  shots_prompts?: Array<{ index?: number, video_prompt?: string, negative_prompt?: string, audio_prompt?: string }>"""
new_type = """  shots_prompts?: Array<{
    index?: number
    video_prompt?: string
    negative_prompt?: string
    audio_prompt?: string
    /** 'relay' = 这一镜接着上一镜的尾帧出的（跨镜连贯） */
    chain_mode?: string
    /** 接龙时携带的"上一镜结束状态"快照 */
    continuity_brief?: string
  }>"""
if old_type in text:
    text = text.replace(old_type, new_type, 1)
else:
    print('   （类型未匹配，跳过）')

old_ui = """                <summary>第 {(shot.index ?? i) + 1} 镜</summary>"""
new_ui = """                <summary>
                  第 {(shot.index ?? i) + 1} 镜
                  {shot.chain_mode === 'relay' ? <span style={{ marginLeft: 6, color: 'var(--accent,#7ac130)' }}>🔗 接上一镜</span> : null}
                </summary>"""
if old_ui in text:
    text = text.replace(old_ui, new_ui, 1)
else:
    print('   （UI 未匹配，跳过）')

old_body = """                  <div className="gate-prompt-label">视频生成提示词</div>"""
new_body = """                  {shot.continuity_brief ? (
                    <div className="gate-prompt-label" style={{ color: 'var(--accent,#7ac130)' }}>接龙状态</div>
                  ) : null}
                  {shot.continuity_brief ? (
                    <div className="gate-prompt-text" style={{ fontSize: 12, opacity: 0.85 }}>{shot.continuity_brief}</div>
                  ) : null}
                  <div className="gate-prompt-label">视频生成提示词</div>"""
if old_body in text:
    text = text.replace(old_body, new_body, 1)
else:
    print('   （brief UI 未匹配，跳过）')

card.write_text(text, encoding='utf-8')
print('③ VfCard.tsx 已显示接龙徽标与接龙状态')

import py_compile
py_compile.compile(str(mp), doraise=True)
print('语法 OK (main.py)')
