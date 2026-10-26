"""VideoForge 后端两处修正（2026-10-21 第 3 轮，用户第 2、3 条）：

① 旁白：**没有台词就不要自己编旁白**。
   现状 `core/compose.py:narration_for_shot` 在"没有对白"时会退化成
   分镜概述 → 动作描述 → 约束条件，然后拿它去 TTS —— 于是用户看到
   「没写旁白的片子被硬配了一段解说」，而且语速快、音色 AI 感重。
   改法：只认真正的台词/旁白字段；没有就返回空串（上层本来就按空处理）。

② 白膜一致性：把**白膜首帧**作为成片的 `first_frame` 传给视频模型，
   这样成片的构图/机位/景别跟用户在白膜里确认过的一致；
   平台若把「首帧」与「参考图」判为互斥，就让位给首帧（与 longvideo 里
   续接时同一套策略），并把这件事写进返回值，不静默。

只做**纯新增/最小替换**，每处都留 backslash 备份。
"""
import re
import shutil
import time
from pathlib import Path

ROOT = Path(r'D:\VideoForge-dev\backend')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def backup(path: Path) -> None:
    target = path.with_suffix(path.suffix + f'.bak-{STAMP}')
    shutil.copyfile(path, target)
    print('备份 →', target.name)


# ── ① narration_for_shot：删掉三段"兜底编旁白" ───────────────────────────
compose = ROOT / 'core' / 'compose.py'
text = compose.read_text(encoding='utf-8')
old = '''    text = ""
    if dialogues:
        text = " ".join(dialogues)
    if not text:
        text = _clean_narration(shot.get("layer1_overview") or "")
    if not text and actions:
        text = _clean_narration(" ".join(actions))
    if not text:
        text = _clean_narration(shot.get("layer3_constraints") or "")
'''
new = '''    text = ""
    if dialogues:
        text = " ".join(dialogues)
    # ★ 2026-10-21（用户第 2 条）：**没有台词就不配音**。
    #   旧行为会拿「分镜概述 / 动作描述 / 约束」当旁白念出来 ——
    #   用户看到的成片里就多了一段没人写过的解说，而且听起来很 AI。
    #   这里只认真正的台词字段；没有台词就返回空串，让合成器直接跳过这条声轨。
    #   `actions` 仍保留给字幕/分镜展示用，但**不再**喂给 TTS。
'''
if old in text:
    backup(compose)
    compose.write_text(text.replace(old, new), encoding='utf-8')
    print('① narration 兜底已移除')
else:
    print('① 未匹配到目标段落（可能已改过）')


# ── ② longvideo：白膜首帧优先作为 first_frame ───────────────────────────
longvideo = ROOT / 'core' / 'longvideo.py'
text = longvideo.read_text(encoding='utf-8')
old = '''        elif scene_ref:
            kw["first_frame"] = scene_ref
'''
new = '''        elif previz_first_frame:
            # ★ 2026-10-21（用户第 3 条）：白膜和成片**画面对不上**。
            #   原因：白膜只在"平台支持参考视频"时才真的进请求，而这个网关的
            #   seedance 档位不声明视频参考 ⇒ 白膜被降级成一句提示词，成片构图全靠模型编。
            #   改法：白膜渲出来的**首帧图**（用户就是看这张照片确认的）直接当 first_frame，
            #   构图/机位/景别与白膜一致；画面仍由视频模型重绘（不是把白模贴上去）。
            try:
                kw["first_frame"] = to_image_ref(previz_first_frame)
                previz_info.setdefault("applied", {})["first_frame_from_previz"] = True
                previz_info.setdefault("warnings", []).append(
                    "本次成片以**白膜首帧**为首帧（构图/机位跟白膜一致），画面由视频模型重绘。")
            except Exception as _e:  # noqa: BLE001
                logger.warning("白膜首帧转 URL 失败（退回场景图）：%s", _e)
                if scene_ref:
                    kw["first_frame"] = scene_ref
        elif scene_ref:
            kw["first_frame"] = scene_ref
'''
if old in text:
    backup(longvideo)
    text = text.replace(old, new, 1)

    # 计算 previz_first_frame（放在 decide() 之后、循环之前）
    anchor = '''    out_dir = out_dir or os.path.join(os.getcwd(), "data", "outputs",'''
    if anchor in text and 'previz_first_frame =' not in text:
        insert = '''    # ★ 白膜首帧（用户确认过的那张图）：优先拿来当 first_frame，保证构图一致。
    previz_first_frame = ""
    try:
        _pv_path = str(shot.get("previz_path") or "").strip()
        if _pv_path:
            if os.path.isfile(_pv_path):
                previz_first_frame = _pv_path
            else:
                _proj = str(shot.get("project_id") or "")
                for _base in (os.getcwd(), os.path.join(os.getcwd(), "data")):
                    _cand = os.path.join(_base, _pv_path)
                    if os.path.isfile(_cand):
                        previz_first_frame = _cand
                        break
    except Exception as _e:  # noqa: BLE001
        logger.warning("取白膜首帧失败（按常规路径）：%s", _e)

'''
        text = text.replace(anchor, insert + anchor, 1)

    longvideo.write_text(text, encoding='utf-8')
    print('② 白膜首帧优先已接入')
else:
    print('② 未匹配到目标段落（可能已改过）')

# 语法自检
import py_compile
for path in (compose, longvideo):
    py_compile.compile(str(path), doraise=True)
    print('语法 OK', path.name)
