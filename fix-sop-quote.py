"""修 SOP 引号（JS 单引号串里不能用双引号）+ 确认/补上 relay_action_hint。"""
from pathlib import Path

# ── ① index.js：把双引号换成中文引号 ────────────────────────────────────
p = Path(r'D:\VideoForge-dsh\deepseek-harness-master\packages\video\vf-video\lib\index.js')
text = p.read_text(encoding='utf-8')
bad = """    '（不要让下一镜重新"走入/转身/拿起"上一镜已经做完的事 —— 那叫时间倒流，'"""
good = """    '（不要让下一镜重新「走入/转身/拿起」上一镜已经做完的事 —— 那叫时间倒流，'"""
if bad in text:
    p.write_text(text.replace(bad, good, 1), encoding='utf-8')
    print('① index.js 引号已修')
else:
    print('① 未匹配（可能已修）')

# ── ② core.js：确保接龙镜有 relay_action_hint ─────────────────────────
core = Path(r'D:\VideoForge-dsh\deepseek-harness-master\packages\video\vf-video\lib\core.js')
ct = core.read_text(encoding='utf-8')
if 'relay_action_hint' in ct:
    print('② relay_action_hint 已在')
else:
    old = '''                compiled["continuity_brief"] = _plan.get("brief", "")'''
    new = '''                compiled["continuity_brief"] = _plan.get("brief", "")
                # ★ 缓解"首帧绑架"（强模型规格）：动作从**当前姿势**开始写清楚，
                #   模型就不会原地扭动。
                compiled["relay_action_hint"] = "从当前姿势继续，动作幅度小而连贯，不要重置人物位置"'''
    if old in ct:
        core.write_text(ct.replace(old, new, 1), encoding='utf-8')
        print('② relay_action_hint 已补')
    else:
        print('② 未匹配 continuity_brief 落点')
        # 退化：在 compile_shot_prompts 的返回里加
        old2 = '''        "character_anchor": "；".join(anchors),'''
        new2 = '''        "character_anchor": "；".join(anchors),
        "relay_action_hint": "",'''
        if old2 in ct:
            core.write_text(ct.replace(old2, new2, 1), encoding='utf-8')
            print('② 已在 compile_shot_prompts 里加占位字段')
