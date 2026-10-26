"""关键帧素材收口：只喂**场景图**（+ 出镜角色图），声音角色不喂。

并把 build 的调用改成 use_extra_refs=True（场景图进参考 → 画面更贴近用户要的场景；
关键是**它只作为"长什么样"的参考**，最终构图由用户确认这张图决定）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')

# ① keyframe.py：_collect_refs 排除声音角色
kf = Path(r'D:\VideoForge-dev\backend\core\keyframe.py')
text = kf.read_text(encoding='utf-8')
old = """    for cid in (shot.get("character_ids") or []):
        try:
            char = db.get_character(str(cid))
        except Exception:  # noqa: BLE001
            char = None
        if char and char.get("reference_image_path"):
            refs.append(str(char["reference_image_path"]))
    return refs"""
new = """    for cid in (shot.get("character_ids") or []):
        try:
            char = db.get_character(str(cid))
        except Exception:  # noqa: BLE001
            char = None
        if not char or not char.get("reference_image_path"):
            continue
        # ★ 只出声不出镜的角色（旁白等）**不能**当视觉参考 ——
        #   实测：把旁白的灰色人偶图喂进去，图像模型会真的画一个人站在街上。
        try:
            from core.shot_prompts import is_voice_only
            if is_voice_only(char):
                continue
        except Exception:  # noqa: BLE001
            pass
        refs.append(str(char["reference_image_path"]))
    return refs"""
if old in text:
    shutil.copyfile(kf, kf.with_suffix(kf.suffix + '.bak-' + STAMP))
    kf.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('① _collect_refs 已排除声音角色')
else:
    print('① 未匹配')

# 返回值里的 refs 也改成真实输入
text = kf.read_text(encoding='utf-8')
text = text.replace(
    '    return {"ok": True, "path": target, "refs": [previz] + refs[:2]}',
    '    return {"ok": True, "path": target, "refs": (refs[:2] if use_extra_refs else [])}',
)
kf.write_text(text, encoding='utf-8')

# ② build-keyframe.py：用场景参考（use_extra_refs=True）
bp = Path(r'D:\VideoForge-dsh\build-keyframe.py')
b = bp.read_text(encoding='utf-8')
b = b.replace(
    "    result = await kf.build_keyframe(db, shot, settings, output_dir=ROOT, prompt=prompt)",
    "    result = await kf.build_keyframe(db, shot, settings, output_dir=ROOT, prompt=prompt, use_extra_refs=True)",
)
bp.write_text(b, encoding='utf-8')
print('② build-keyframe.py 已改为带场景参考')

import py_compile
py_compile.compile(str(kf), doraise=True)
print('语法 OK')
