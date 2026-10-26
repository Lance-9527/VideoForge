"""修两个真 bug：
① `shot_size` 按固定顺序取景别（"全景"压过"特写"）→ 改为按**出现位置**取最早的；
② 角色特征提取会切出单字碎片（"左"、"丝"）→ 要求片段长度 ≥3 且必须含关键词。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ① shot_chain.shot_size
sc = ROOT / 'shot_chain.py'
text = sc.read_text(encoding='utf-8')
old = '''def shot_size(text: str) -> str:
    """从镜头描述里取景别（取第一个出现的）。"""
    for size in _SHOT_SIZES:
        if size in (text or ""):
            return size
    return ""'''
new = '''def shot_size(text: str) -> str:
    """从镜头描述里取景别。

    ★ 按**出现位置**取最早的那个，不是按固定顺序 —— 旧写法按顺序找，
      "全景"会永远压过"特写"（两者都出现时），于是同机位连续动作被判成换机位，
      接龙永远不触发（实测踩到）。
    """
    body = text or ""
    best_at = -1
    best = ""
    for size in _SHOT_SIZES:
        at = body.find(size)
        if at >= 0 and (best_at < 0 or at < best_at):
            best_at = at
            best = size
    return best'''
if old in text:
    sc.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('① shot_size 改为按出现位置取')
else:
    print('① 未匹配 shot_size')

# ② shot_prompts 角色特征提取
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')
old_feat = '''    features: List[str] = []
    for keyword in ("疤", "痣", "眼镜", "胡子", "白发", "秃", "辫", "短发", "长发",
                    "黑发", "皱纹", "雀斑", "国字脸", "圆脸", "浓眉", "酒窝", "胡茬"):
        if keyword in desc and len(features) < 3:
            idx = desc.find(keyword)
            piece = desc[max(0, idx - 4): idx + len(keyword) + 2].strip("，。、 ")
            if piece and piece not in features:
                features.append(piece)
    feature = "，".join(features) if features else ""'''
new_feat = '''    features: List[str] = []
    for keyword in ("疤", "痣", "眼镜", "胡子", "白发", "秃", "辫", "短发", "长发",
                    "黑发", "皱纹", "雀斑", "国字脸", "圆脸", "浓眉", "酒窝", "胡茬"):
        if keyword in desc and len(features) < 3:
            idx = desc.find(keyword)
            piece = desc[max(0, idx - 4): idx + len(keyword) + 2].strip("，。、 ")
            # ★ 只要**长度 ≥3 且确实含关键词**的片段：旧写法会切出"左""丝"这种单字碎片
            #   （"豆大的黑痣，左，丝"），塞进提示词反而把模型带偏。
            if len(piece) >= 3 and keyword in piece and piece not in features:
                features.append(piece)
    feature = "，".join(features) if features else ""'''
if old_feat in text:
    sp.write_text(text.replace(old_feat, new_feat, 1), encoding='utf-8')
    print('② 角色特征提取已收紧')
else:
    print('② 未匹配特征提取')

import py_compile
for p in (sc, sp):
    py_compile.compile(str(p), doraise=True)
print('语法 OK')
