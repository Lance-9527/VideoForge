"""把「关键帧构建」代码块插到 generate_long_video 里（用稳定锚点：for seg in plan:）。"""
import shutil
import time
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\longvideo.py')
text = p.read_text(encoding='utf-8')

if 'keyframe_file = ""' in text:
    print('已存在构建块')
    raise SystemExit(0)

block = '''    # ★★ 2026-10-22：写实关键帧（AI 预演图 → 当首帧出片）
    #   实测依据：适配器里 first_frame 与 reference_image **互斥**
    #   （core/adapters/seedance.py:150-158）——有参考图时首帧被降级成"又一张参考图"，
    #   只锁长相不锁构图；没有参考图时才以 role=first_frame 发出，**构图 100% 跟首帧一致**
    #   （对照实验：去掉参考图后，成片首帧 = 给它的首帧图）。
    #   所以这里的做法是：**先生成一张写实关键帧**（用户确认的那张图），
    #   出片时它当 first_frame 且不带参考图 ⇒ 用户看到的构图 = 成片的构图。
    keyframe_file = ""
    keyframe_note = ""
    try:
        from core import keyframe as _kf
        _root = str((settings or {}).get("output_dir") or os.path.join(os.getcwd(), "data", "outputs"))
        keyframe_file = _kf.existing_keyframe(_root, shot)
        if not keyframe_file:
            from core import shot_prompts as _sp
            _comp = _sp.stored_prompts(shot)
            _video = str(_comp.get("video_prompt") or "")
            _head = _video
            for _mark in ("镜头：", "动作：", "这一镜要讲清楚的是："):
                if _mark in _head:
                    _head = _head.split(_mark)[0]
            _built = await _kf.build_keyframe(
                db, shot, settings or {}, output_dir=_root,
                prompt=_kf.KEYFRAME_PROMPT + "\\n" + _head.strip(),
                use_extra_refs=True)
            if _built.get("ok"):
                keyframe_file = str(_built.get("path") or "")
                keyframe_note = "本次成片以**写实关键帧**为首帧（构图/机位与用户确认的那张图一致）"
            else:
                keyframe_note = f"关键帧没生成（{_built.get('error')}），退回场景图当首帧"
    except Exception as _e:  # noqa: BLE001
        logger.warning("关键帧环节失败（按原路径继续）：%s", _e)

'''

anchor = '    for seg in plan:'
if anchor not in text:
    raise SystemExit('锚点 for seg in plan: 没找到')
text = text.replace(anchor, block + anchor, 1)
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + time.strftime('%Y%m%d-%H%M%S')))
p.write_text(text, encoding='utf-8')

check = p.read_text(encoding='utf-8')
print('构建块已插入:', 'keyframe_file = ""' in check)
import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
