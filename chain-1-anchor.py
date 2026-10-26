"""把「跨镜接龙」接进编译与出片（照强模型给的规格）。

改动：
  ① core/shot_prompts.py
     - 角色锚定句按「身份→外貌(3特征)→服装→状态」重写（30-60字；接龙时外貌扩到 20-25字）
     - negative_prompt 追加「保持角色外貌完全一致，严禁更换发型、发色、衣服、配饰、疤痕位置」
     - compile_project_prompts：逐镜判定接龙 → 写 prompts.chain_mode / relay_source /
       relay_first_frame / continuity_brief；连续接龙满 2 镜强制回独立镜（重校准）
  ② core/longvideo.py
     - 生成时：prompts.relay_first_frame 优先于关键帧；接龙时清空参考图
     - 生成后：把最后一段的尾帧存到 outputs/<pid>/lastframe/<shot_id>.jpg（供下一镜接龙）
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① 角色锚定句重写 ───────────────────────────────────────────────────
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')

old_anchor = '''    # 从描述里挑一个稳定识别特征（发型/疤痕/眼镜/肤色…），只取第一个分句里的关键词
    feature = ""
    for keyword in ("疤", "痣", "眼镜", "胡子", "白发", "秃", "辫", "短发", "长发", "黑发", "皱纹", "雀斑"):
        if keyword in desc:
            # 取该关键词前后 6 字作为特征短语
            idx = desc.find(keyword)
            feature = desc[max(0, idx - 4): idx + len(keyword) + 2].strip("，。、 ")
            break'''
new_anchor = '''    # ★ 2026-10-22（按强模型规格重写）：外貌特征要**给 3 个**，不是 1 个。
    #   规格：身份 → 外貌(3个特征) → 服装 → 状态，30-60 字；
    #   接龙（丢参考图）时外貌要扩到 20-25 字。
    features: List[str] = []
    for keyword in ("疤", "痣", "眼镜", "胡子", "白发", "秃", "辫", "短发", "长发",
                    "黑发", "皱纹", "雀斑", "国字脸", "圆脸", "浓眉", "酒窝", "胡茬"):
        if keyword in desc and len(features) < 3:
            idx = desc.find(keyword)
            piece = desc[max(0, idx - 4): idx + len(keyword) + 2].strip("，。、 ")
            if piece and piece not in features:
                features.append(piece)
    feature = "，".join(features) if features else ""'''
if old_anchor in text:
    text = text.replace(old_anchor, new_anchor, 1)
    print('① 角色锚定句：外貌特征改为 3 个')
else:
    print('① 未匹配角色锚定句')

# 接龙模式：外貌扩写 + 负面补充
old_parts = '''    parts = [f"{name}"]
    if feature:
        parts.append(feature)'''
new_parts = '''    parts = [f"{name}"]
    if feature:
        parts.append(feature)'''
if old_parts not in text:
    print('   （parts 段落未变，无需处理）')

# negative_prompt 追加"保持外貌一致"
old_neg = '''NEGATIVE_BASE = (
    "文字、字幕、水印、招牌字样；多余人物、路人；畸形手指、多指；"
    "穿模、肢体扭曲；夸张大幅动作；镜头切换、变焦、抖动；人物出画或被裁切；面部变形"
)'''
new_neg = '''NEGATIVE_BASE = (
    "文字、字幕、水印、招牌字样；多余人物、路人；畸形手指、多指；"
    "穿模、肢体扭曲；夸张大幅动作；镜头切换、变焦、抖动；人物出画或被裁切；面部变形；"
    # ★ 2026-10-22（强模型规格）：接龙模式丢掉了参考图，身份只能靠文字锁，
    #   所以负面词必须显式禁止"换脸/换装"。
    "更换发型、发色、衣服、配饰、疤痕位置；角色年龄体型突变"
)'''
if old_neg in text:
    text = text.replace(old_neg, new_neg, 1)
    print('① negative_prompt 已加"禁止换脸换装"')
else:
    print('① 未匹配 NEGATIVE_BASE')

sp.write_text(text, encoding='utf-8')
print('① shot_prompts.py 已更新')
