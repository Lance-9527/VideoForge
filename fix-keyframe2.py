"""两处修正 + 重做关键帧（只喂白膜灰模一张图，让构图没有竞争）。

① `core/shot_prompts.py`：角色回退分支也要按"声音角色"分流（旁白不该进画面主体）。
② `core/keyframe.py`：关键帧默认**只喂白膜首帧**（构图唯一来源），
   场景/角色靠提示词描述 —— 上一版喂了三张图，图像模型跟的是写实场景图，构图跑偏。
   需要严格锁脸时才把角色图作为第二张（可开关）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① shot_prompts：回退分支分流 ────────────────────────────────────────
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')
old = """    if not cast and not voice_only and len(characters) == 1:
        cast = list(characters.values())"""
new = """    if not cast and not voice_only and len(characters) == 1:
        # ★ 回退分支也要分流：只有一张角色卡时，若它是"只出声不出镜"的角色（旁白等），
        #   绝不能当成画面主体（否则模型会把旁白画成一个人站在街上）。
        only = list(characters.values())[0]
        if is_voice_only(only):
            voice_only.append(only)
        else:
            cast.append(only)"""
if old in text:
    shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))
    sp.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('① 回退分支已分流')
else:
    print('① 未匹配')

# ── ② keyframe：默认只喂白膜 ───────────────────────────────────────────
kf = ROOT / 'keyframe.py'
text = kf.read_text(encoding='utf-8')
old_sig = """async def build_keyframe(
    db: Any,
    shot: Dict[str, Any],
    settings: Dict[str, Any],
    *,
    output_dir: str,
    prompt: str = "",
) -> Dict[str, Any]:"""
new_sig = """async def build_keyframe(
    db: Any,
    shot: Dict[str, Any],
    settings: Dict[str, Any],
    *,
    output_dir: str,
    prompt: str = "",
    use_extra_refs: bool = False,
) -> Dict[str, Any]:"""
if old_sig in text:
    text = text.replace(old_sig, new_sig, 1)
    text = text.replace(
        """    refs = _collect_refs(db, shot)
    if not refs:
        return {"ok": False, "error": "这个镜头没有场景图/角色图参考，跳过关键帧"}""",
        """    refs = _collect_refs(db, shot)
    # ★ 默认**只喂白膜首帧**：构图必须只有一个来源。
    #   上一版把写实场景图一起喂进去，图像模型跟的是那张照片（构图跑偏），
    #   场景/人物信息应该走**提示词**（我们已经把场景锚定与角色锚定写进去了）。
    if not use_extra_refs:
        refs = []""",
        1,
    )
    text = text.replace(
        "            reference_images=[previz] + refs[:2],",
        "            reference_images=[previz] + refs[:2],",
    )
    shutil.copyfile(kf, kf.with_suffix(kf.suffix + '.bak-' + STAMP))
    kf.write_text(text, encoding='utf-8')
    print('② 关键帧默认只喂白膜')
else:
    print('② 未匹配 keyframe 签名')

import py_compile
for path in (sp, kf):
    py_compile.compile(str(path), doraise=True)
    print('语法 OK', path.name)
