# -*- coding: utf-8 -*-
"""验证①「角色能随场景/光影角度变化而变化，但仍是同一个人」。

做法：**同一个角色卡**，放进两个场景（白天战壕 / 夜晚指挥部），
分别构造（a）出图提示词（b）视频提示词，然后逐项核对：

  必须**逐字不变**（身份）：脸型/肤色/眼型/发型/辨识特征/体型/服装/识别锚点
  必须**跟着场景变**（状态）：时段、光位、天气、色调、环境

如果身份字段有任何一处变了 → 角色会漂移；如果光影没变 → 那就是"换个背景贴同一个人"。
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

from core.assets import build_portrait_prompt, _character_slots      # noqa: E402
from core.videoprompt import build_video_prompt, character_signature  # noqa: E402

# ── 一个结构化到位的角色（模拟「从剧本提取 + 细化设定」的产物）──
LLM_CHAR = {
    "name": "李连长", "role": "protagonist", "gender": "male", "age": "40 岁上下",
    "face_shape": "方脸，颧骨明显，下颌线硬朗",
    "skin": "偏黄，风吹日晒的粗糙感，额头有细纹",
    "eyes": "细长眼，眼神锐利，眼窝略深",
    "hair": "短寸黑发，两鬓已花白，胡茬未刮净",
    "distinctive": "左眉骨一道旧疤",
    "height_build": "中等身高，精瘦，肩背挺直",
    "costume_top": "洗得发白的灰蓝色粗布对襟上衣，肘部有补丁",
    "costume_bottom": "深灰粗布长裤，裤脚扎进绑腿",
    "costume_shoes": "黑色旧布鞋，鞋面有补丁",
    "costume_accessories": ["布制绑腿", "帆布挎包"],
    "signature": "左眉骨的旧疤 + 洗白的灰蓝对襟上衣",
    "props": ["旧驳壳枪"],
}
slots = _character_slots(LLM_CHAR)
CARD = {
    "id": "c1", "name": "李连长", "age": "40 岁上下", "gender": "male",
    "description": "", "costume_main": "", "props": ["旧驳壳枪"],
    "reference_features": json.dumps({"slots": slots}, ensure_ascii=False),
}

# ── 两个光影完全不同的场景 ──
SCENES = {
    "day": {
        "name": "战壕", "time_of_day": "day", "weather": "snowy", "lighting": "overcast",
        "reference_features": json.dumps({"slots": {
            "terrain": "碎石土路，两侧枯草与残雪",
            "architecture": "半塌的土坯战壕墙",
            "color_tone": "冷灰蓝为主，几乎没有暖色",
        }}, ensure_ascii=False),
    },
    "night": {
        "name": "指挥部", "time_of_day": "night", "weather": "clear",
        "lighting": "candle",
        "reference_features": json.dumps({"slots": {
            "terrain": "夯土地面，铺着旧草席",
            "architecture": "低矮的土木屋内，木梁裸露",
            "color_tone": "暖橙为主，烛光把四周压成深褐",
        }}, ensure_ascii=False),
    },
}
SHOTS = {
    "day": {"order_index": 0, "duration_seconds": 10,
            "layer1_overview": "李连长在战壕里蹲下",
            "layer2_timeline": [{"start": 0, "end": 10, "action": "李连长蹲下画地图",
                                 "expression": "眉头微皱", "camera": "低角度特写"}]},
    "night": {"order_index": 2, "duration_seconds": 10,
              "layer1_overview": "李连长在指挥部指着地图",
              "layer2_timeline": [{"start": 0, "end": 10, "action": "李连长指着地图",
                                   "expression": "神情沉稳", "camera": "过肩中景"}]},
}

IDENTITY_KEYS = ["face_shape", "skin", "eyes", "hair", "distinctive", "height_build",
                 "costume_top", "costume_bottom", "costume_shoes", "signature"]
STATE_HINTS = ["白天", "阴天", "雪", "夜晚", "烛光", "冷灰蓝", "暖橙",
               "碎石土路", "夯土地面", "土坯战壕墙", "土木屋"]

print("═" * 78)
print("① 身份字段：两个场景下必须**逐字相同**")
sig_day = character_signature(CARD)
p_day = build_portrait_prompt(CARD)
p_night = build_portrait_prompt(CARD)      # 出图提示词与场景无关（设定图）
print(f"   角色签名长度 {len(sig_day)} 字")
same = sig_day == character_signature(CARD)
print(f"   签名稳定: {'✅ 是' if same else '❌ 否'}")
for k in IDENTITY_KEYS:
    v = slots.get(k)
    in_sig = bool(v) and (str(v) in sig_day)
    print(f"     {'✅' if in_sig else '❌'} {k:18s} 出现在签名里: {str(v)[:30]}")

print("\n" + "═" * 78)
print("② 视频提示词：两个场景下身份必须一致、光影必须不同")
built = {}
for key, sc in SCENES.items():
    r = build_video_prompt(SHOTS[key], scene=sc, characters=[CARD], style="cinematic",
                           provider="hailuo", duration=10, aspect_ratio="16:9",
                           resolution="768P", layers={"l1": True, "l2": True, "l3": True})
    built[key] = r.get("prompt") or r.get("text") or ""
    print(f"\n   ── {key}（{sc['name']}/{sc['time_of_day']}）──")
    print("   " + built[key].replace("\n", "\n   "))

print("\n" + "═" * 78)
print("③ 逐项判定")
day, night = built["day"], built["night"]
print("   身份（必须两边都有，且逐字相同）：")
allid = True
for k in IDENTITY_KEYS:
    v = slots.get(k)
    if not v:
        continue
    a, b = str(v) in day, str(v) in night
    okk = a and b
    allid = allid and okk
    print(f"     {'✅' if okk else '❌'} {k:18s} 白天={a} 夜晚={b}")
sig_in_both = sig_day in day and sig_day in night
print(f"     {'✅' if sig_in_both else '❌'} 完整签名在两条提示词里都逐字出现")
allid = allid and sig_in_both

print("\n   状态（必须随场景改变）：")
for h in STATE_HINTS:
    print(f"     · 「{h}」 白天={h in day}  夜晚={h in night}")
day_only = [h for h in STATE_HINTS if h in day and h not in night]
night_only = [h for h in STATE_HINTS if h in night and h not in day]
print(f"     白天独有: {day_only}")
print(f"     夜晚独有: {night_only}")
states_differ = bool(day_only) and bool(night_only) and day != night

print("\n" + "═" * 78)
print("④ 结论")
print(f"   身份逐字不变      : {'✅' if allid else '❌'}")
print(f"   光影随场景改变    : {'✅' if states_differ else '❌'}")
print(f"   两条提示词不相同  : {'✅' if day != night else '❌'}")
print()
if allid and states_differ:
    print("   ✅ 「同一个人，在不同的光影与场景下」成立")
    print("      —— 身份来自角色卡的固定槽位（逐字复用），")
    print("         光影/环境来自场景卡（随时段与光位变化）。")
else:
    print("   ❌ 未达成")
