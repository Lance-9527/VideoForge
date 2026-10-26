"""把「写实关键帧」接进出片链路（core/longvideo.py）。

改动两处：
  ① 生成前：白膜首帧 + 角色/场景图 → 写实关键帧（缓存到 outputs/<pid>/keyframe/<shot>.png）
  ② 生成时：有关键帧就用它当 first_frame，并且**不带参考图**
     —— 因为适配器里 first_frame 与 reference_image 互斥（seedance.py:150-158），
        带参考图会让首帧降级成"又一张参考图"，构图就锁不住了。
"""
import shutil
import time
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\longvideo.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + time.strftime('%Y%m%d-%H%M%S')))

# ── ① 在生成循环之前算/建关键帧 ─────────────────────────────────────────
anchor = """    # ★ 复用上次已成功的段（省钱的关键：抽到第 7 段发现第 2 段不行，只重跑第 2 段）"""
block = '''    # ★★ 2026-10-22：写实关键帧（白膜 → 关键帧 → 成片）
    #   实测依据：适配器里 first_frame 与 reference_image **互斥**
    #   （core/adapters/seedance.py:150-158）——有参考图时首帧会被降级成"又一张参考图"，
    #   只锁长相不锁构图；没有参考图时才以 role=first_frame 发出，**构图 100% 跟白膜一致**
    #   （对照实验：去掉参考图后，成片首帧 = 白膜首帧）。
    #   但白膜是灰模，直接当首帧 → 成片也是灰模。
    #   ⇒ 中间加一步：用图像模型把白膜**重绘成写实关键帧**（构图不变、长相来自参考图），
    #   再拿关键帧当首帧出片。只花一张图的钱，且关键帧可缓存复用。
    keyframe_file = ""
    keyframe_note = ""
    try:
        from core import keyframe as _kf
        _root = str((settings or {}).get("output_dir") or os.path.join(os.getcwd(), "data", "outputs"))
        keyframe_file = _kf.existing_keyframe(_root, shot)
        if not keyframe_file and str(shot.get("previz_path") or ""):
            _built = await _kf.build_keyframe(db, shot, settings or {}, output_dir=_root)
            if _built.get("ok"):
                keyframe_file = str(_built.get("path") or "")
                keyframe_note = "本次成片用**写实关键帧**当首帧（构图取自白膜，画面与人物已重绘为写实）"
            else:
                keyframe_note = f"关键帧没生成（{_built.get('error')}），退回场景图当首帧"
    except Exception as _e:  # noqa: BLE001
        logger.warning("关键帧环节失败（按原路径继续）：%s", _e)

'''
if 'keyframe_file' not in text:
    text = text.replace(anchor, block + anchor, 1)
    print('① 关键帧构建已插入')

# ── ② 生成时优先用关键帧，且不带参考图 ─────────────────────────────────
old = """        if chaining and prev_last_frame:
            kw["first_frame"] = to_image_ref(prev_last_frame)
            kw["character_reference"] = []      # 互斥，让位给首帧
        elif previz_first_frame:"""
new = """        if keyframe_file:
            # ★ 关键帧优先：构图与白膜一致；**参考图必须清空** ——
            #   适配器里 first_frame 与 reference_image 互斥，带了参考图首帧就降级了。
            kw["first_frame"] = to_image_ref(keyframe_file)
            kw["character_reference"] = []
            previz_info.setdefault("applied", {})["keyframe_as_first_frame"] = True
            if keyframe_note:
                previz_info.setdefault("warnings", []).append(keyframe_note)
        elif chaining and prev_last_frame:
            kw["first_frame"] = to_image_ref(prev_last_frame)
            kw["character_reference"] = []      # 互斥，让位给首帧
        elif previz_first_frame:"""
if old in text:
    text = text.replace(old, new, 1)
    print('② 关键帧作为首帧已接入')
else:
    print('② 未匹配（可能已改过）')

p.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK', p.stat().st_size, 'B')
