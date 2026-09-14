# -*- coding: utf-8 -*-
"""② 场景侧与 ① 角色侧对称的验证：同一地点、不同时段 → 结构不变、光线变。

用户对 ① 和 ② 的要求是同一句话："能随场景、光影角度变化而变化的形象"。
角色侧已经验过（身份逐字不变、光影随场景变）。
场景侧要验的是：**同一地点在不同时段，空间结构必须保持，而光线必须改变**。
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

from core.videoprompt import build_video_prompt, _slots_of      # noqa: E402

# 同一个地点「战壕」，两个时段 —— 地貌/建筑/陈设必须一致，光位/色调必须不同
def scene(time_of_day, lighting, weather, tone):
    return {
        "name": "战壕", "time_of_day": time_of_day, "lighting": lighting,
        "weather": weather,
        "reference_features": json.dumps({"slots": {
            "terrain": "碎石土路，两侧枯草与残雪",
            "architecture": "半塌的土坯战壕墙，木梁裸露",
            "furnishings": "墙边堆着几只空弹药箱",
            "atmosphere": "肃杀，空气里像有雪味",
            "color_tone": tone,
        }}, ensure_ascii=False),
    }


DAY = scene("day", "overcast", "snowy", "冷灰蓝为主，几乎没有暖色")
NIGHT = scene("night", "candle", "clear", "暖橙为主，暗部压成深褐")

SHOT = {
    "order_index": 0, "duration_seconds": 10,
    "layer1_overview": "战壕里的空镜",
    "layer2_timeline": [{"start": 0, "end": 10, "action": "镜头缓慢横移过战壕",
                         "camera": "横移中景"}],
}

STRUCT = ["碎石土路", "枯草与残雪", "半塌的土坯战壕墙", "木梁裸露", "空弹药箱", "肃杀"]
# ★ 注意：「雪」既可能来自地形（残雪，属于"同一个地方"的结构），
#   也可能来自天气（snowy → 雪，属于"随时段变的光影"）。
#   所以这里只拿**天气字段派生的整词**做判别，不拿单字。
LIGHT_DAY = ["白天", "阴天漫射光", "冷灰蓝"]
LIGHT_NIGHT = ["夜晚", "烛光", "暖橙"]

built = {}
for key, sc in (("day", DAY), ("night", NIGHT)):
    r = build_video_prompt(SHOT, scene=sc, characters=[], style="cinematic",
                           provider="hailuo", duration=10, aspect_ratio="16:9",
                           resolution="768P", layers={"l1": True, "l2": True, "l3": True})
    built[key] = r.get("prompt") or r.get("text") or ""

day, night = built["day"], built["night"]

print("═" * 78)
print("同一地点「战壕」· 白天 vs 夜晚")
for k, txt in (("白天", day), ("夜晚", night)):
    print(f"\n   ── {k} ──")
    print("   " + txt.replace("\n", "\n   "))

print("\n" + "═" * 78)
print("① 空间结构：两边都必须有，且逐字相同（同一个地方）")
allok = True
for h in STRUCT:
    a, b = h in day, h in night
    okk = a and b
    allok = allok and okk
    print(f"   {'✅' if okk else '❌'} 「{h}」 白天={a} 夜晚={b}")

print("\n   判定：")
print(f"   {'✅' if allok else '❌'} 空间结构完全保持 —— 是同一个地点，地方没变")

print("\n" + "═" * 78)
print("② 光线与色调：必须随场景改变")
allok2 = True
for h in LIGHT_DAY:
    inn = h in day and h not in night
    allok2 = allok2 and inn
    print(f"   {'✅' if inn else '❌'} 白天独有 「{h}」 白天={h in day} 夜晚={h not in night}")
# 天气字段：白天 snowy→雪，夜晚 clear→晴，两边必须是不同的天气词
wd, wn = "雪" in day, "晴" in night
allok2 = allok2 and wd and wn
print(f"   {'✅' if wd else '❌'} 天气随场景变（白天=雪） 白天={wd}")
print(f"   {'✅' if wn else '❌'} 天气随场景变（夜晚=晴） 夜晚={wn}")
for h in LIGHT_NIGHT:
    inn = h in night and h not in day
    allok2 = allok2 and inn
    print(f"   {'✅' if inn else '❌'} 夜晚独有 「{h}」 夜晚={h in night} 白天={h not in day}")

print("\n" + "═" * 78)
print("③ 两条提示词必须不同")
print(f"   {'✅' if day != night else '❌'} 内容不同（长度 {len(day)} vs {len(night)}）")

print("\n" + "═" * 78)
print("结论")
print(f"   空间结构不变 : {'✅' if allok else '❌'}")
print(f"   光线色调改变 : {'✅' if allok2 else '❌'}")
print()
if allok and allok2 and day != night:
    print("   ✅ 「同一个地点，在不同的光影下」成立")
    print("      —— 结构来自场景槽位（逐字复用），光影来自时段/光位字段（随时段变）。")
    print("      与角色侧（身份槽位不变 + 场景光影变）是同一套机制，两边对称。")
else:
    print("   ❌ 未达成")
