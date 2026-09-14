# -*- coding: utf-8 -*-
"""验证「固定槽位」真的让同一角色在不同镜头拿到逐字相同的外形描述。"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

from core.assets import (build_portrait_prompt, CHARACTER_SCHEMA, _character_slots,
                         _join_slots, _FACE_SLOTS, _COSTUME_SLOTS)          # noqa: E402
from core.videoprompt import character_signature, build_video_prompt        # noqa: E402

print("═" * 72)
print("① 槽位定义齐全性")
need_face = {"face_shape", "skin", "eyes", "hair", "distinctive", "height_build"}
need_cost = {"costume_top", "costume_bottom", "costume_shoes",
             "costume_accessories", "costume_palette"}
print("  面部槽位:", sorted(need_face), "→", "全在" if need_face <= set(CHARACTER_SCHEMA) else "缺")
print("  服装槽位:", sorted(need_cost), "→", "全在" if need_cost <= set(CHARACTER_SCHEMA) else "缺")
print("  锚点字段: signature / prop_detail →",
      "在" if {"signature", "prop_detail"} <= set(CHARACTER_SCHEMA) else "缺")

# 模拟 LLM 返回值
LLM_CHAR = {
    "name": "李队长", "aliases": ["老李"], "role": "protagonist",
    "gender": "male", "age": "40 岁上下",
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
    "costume_palette": ["灰蓝", "深灰", "黑"],
    "signature": "左眉骨的旧疤 + 洗白的灰蓝对襟上衣",
    "prop_detail": "一把木质柄的旧驳壳枪，枪身有划痕",
    "personality": "沉稳寡言，说一不二",
    "relationships": "王护士的上级",
    "first_appearance": 1,
    "props": ["旧驳壳枪"],
}
slots = _character_slots(LLM_CHAR)
print(f"\n② 槽位提取：{len(slots)} 项")
for k, v in slots.items():
    print(f"     {k:22s} = {v}")

face = _join_slots(LLM_CHAR, _FACE_SLOTS)
cost = _join_slots(LLM_CHAR, _COSTUME_SLOTS)
print("\n③ 合成的自然语言（兼容旧界面）")
print("   外貌:", face)
print("   服装:", cost)

char_card = {
    "name": "李队长", "age": "40 岁上下", "gender": "male", "role": "protagonist",
    "description": face, "costume_main": cost, "props": ["旧驳壳枪"],
    "reference_features": json.dumps({"slots": slots}, ensure_ascii=False),
}
p = build_portrait_prompt(char_card)
print("\n④ 出图提示词（角色设定图）")
print("   ", p)

print("\n" + "═" * 72)
print("⑤ 关键验证：同一角色在 3 个不同镜头里的外形签名必须**逐字相同**")
sig = character_signature(char_card)
print("   签名:", sig)
same = True
for i, shot in enumerate([
    {"order_index": 0, "layer1_overview": "李队长在雪地里艰难前行",
     "duration_seconds": 10, "layer2_timeline": [
         {"start": 0, "end": 5, "action": "李队长裹紧外套迈步", "expression": "坚毅",
          "camera": "中景跟拍"}]},
    {"order_index": 1, "layer1_overview": "李队长发现冻僵的蛇",
     "duration_seconds": 10, "layer2_timeline": [
         {"start": 0, "end": 5, "action": "李队长蹲下拾起蛇", "expression": "迟疑",
          "camera": "特写"}]},
    {"order_index": 2, "layer1_overview": "蛇突然袭击",
     "duration_seconds": 10, "layer2_timeline": [
         {"start": 0, "end": 5, "action": "李队长猛地甩手", "expression": "惊怒",
          "camera": "手持近景"}]},
], 1):
    r = build_video_prompt(
        shot, scene={"name": "寒冬森林小径", "description": "碎石土路，两侧枯草与残雪"},
        characters=[char_card], style="cinematic", provider="hailuo",
        duration=10, aspect_ratio="16:9", resolution="768P",
        layers={"l1": True, "l2": True, "l3": True})
    prompt = r.get("prompt") or r.get("text") or ""
    ok = sig in prompt
    same = same and ok
    print(f"   镜头{i}: 提示词 {len(prompt)} 字 · 含固定签名={ok}")
    if i == 1:
        print("\n   ── 镜头 2 的完整提示词 ──")
        print("   " + prompt.replace("\n", "\n   "))

print("\n⑥ 结论:", "✅ 三个镜头拿到逐字相同的外形描述，角色不会漂移"
      if same else "❌ 有镜头没带上固定签名")
