"""把「关键帧优先」插进 longvideo 的首帧选择（精确按当前文件的缩进与上下文替换）。"""
import shutil
import time
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\longvideo.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + time.strftime('%Y%m%d-%H%M%S')))

old = """        chaining = (strat == STRATEGY_CHAINED) or (anchor_mode == "chained")
        if chaining and prev_last_frame:"""
new = """        chaining = (strat == STRATEGY_CHAINED) or (anchor_mode == "chained")
        if keyframe_file:
            # ★ 关键帧优先：构图与白膜一致；**参考图必须清空** ——
            #   适配器里 first_frame 与 reference_image 互斥
            #   （core/adapters/seedance.py:150-158），带了参考图首帧会降级成
            #   "又一张参考图"，只锁长相不锁构图（这就是白膜与成片对不上的真因）。
            try:
                kw["first_frame"] = to_image_ref(keyframe_file)
                kw["character_reference"] = []
                previz_info.setdefault("applied", {})["keyframe_as_first_frame"] = True
                if keyframe_note:
                    previz_info.setdefault("warnings", []).append(keyframe_note)
            except Exception as _e:  # noqa: BLE001
                logger.warning("关键帧转 URL 失败（按原路径继续）：%s", _e)
        elif chaining and prev_last_frame:"""

if old in text:
    text = text.replace(old, new, 1)
    p.write_text(text, encoding='utf-8')
    print('关键帧优先已接入')
else:
    print('未匹配')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK', p.stat().st_size, 'B')
