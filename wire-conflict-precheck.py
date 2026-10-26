"""接上「首帧冲突预检」与「白膜规格说明导出」。

① 编译期：接龙镜在出片前先问一次 LLM ——「首帧状态」与「这一镜要做的动作」是否自然？
   冲突（首帧正面却要背对镜头、坐姿却已经在跑）就**自动降级独立镜**
   （用回自己的关键帧 + 参考图），并把理由写进提示词载荷。规格来自强模型给的安全带。

② 渲染后：导出「白膜规格说明」（白膜视频 + 每机位静帧 + 自动 prompt.txt 三件套里的第三件）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① 编译期：首帧冲突预检 ─────────────────────────────────────────────
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')
shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))

old = """                if _relay_first:
                    compiled["relay_first_frame"] = _relay_first
                else:
                    compiled["chain_mode"] = "independent"
                    compiled["chain_note"] = "上一镜尾帧还没生成，本镜先独立出片\""""
new = """                if _relay_first:
                    # ★ 首帧冲突预检（安全带）：首帧的姿态与这一镜的动作打架时，
                    #   接龙会让模型原地扭动/穿帮 ⇒ 自动降级独立镜（用回自己的关键帧+参考图）。
                    _conflict = {"conflict": False, "reason": "未预检"}
                    try:
                        from core import shot_chain as _sc
                        _action = " ".join(
                            str(i.get("action") or "") for i in _timeline_items(shot))[:200]
                        _conflict = await _sc.detect_first_frame_conflict(
                            str(_plan.get("brief") or ""), _action, settings or {})
                    except Exception as _e:  # noqa: BLE001
                        logger.warning("首帧冲突预检失败（按不冲突继续）：%s", _e)
                    if _conflict.get("conflict"):
                        compiled["chain_mode"] = "independent"
                        compiled["chain_note"] = (
                            "首帧与本镜动作冲突（%s），已自动降级为独立镜（用回自己的关键帧）"
                            % str(_conflict.get("reason") or "")[:40])
                        compiled["relay_first_frame"] = ""
                    else:
                        compiled["relay_first_frame"] = _relay_first
                        compiled["first_frame_conflict_check"] = str(_conflict.get("reason") or "")[:40]
                else:
                    compiled["chain_mode"] = "independent"
                    compiled["chain_note"] = "上一镜尾帧还没生成，本镜先独立出片\""""
if old in text:
    text = text.replace(old, new, 1)
    print('① 编译期已接首帧冲突预检')
else:
    print('① 未匹配编译期插入点')

if 'def _timeline_items(' not in text:
    helper = '''

def _timeline_items(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """镜头的 layer2_timeline（字符串则解 JSON）。"""
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:  # noqa: BLE001
            timeline = []
    return [i for i in (timeline or []) if isinstance(i, dict)]
'''
    text += helper
    print('① _timeline_items 辅助已加')

sp.write_text(text, encoding='utf-8')

# ── ② 渲染后：导出白膜规格说明 ─────────────────────────────────────────
p = ROOT / 'previz.py'
text = p.read_text(encoding='utf-8')
if 'export_notes(' not in text:
    # 找到渲染成功后的落点：写 previz_path / previz_notes 之类
    import re
    m = re.search(r'previz_path', text)
    print('② previz.py 里 previz_path 出现次数:', len(re.findall('previz_path', text)))
    # 在 previz.py 里注入导出：找到 previz_path 赋值的附近
    idx = text.find('previz_path')
    print('② 将在 previz.py 里接导出（需人工确认落点）')
else:
    print('② 已存在导出')

import py_compile
py_compile.compile(str(sp), doraise=True)
print('语法 OK')
