# -*- coding: utf-8 -*-
"""验证：分段用的是**模型真实合法时长**，且每段拿到**自己那几秒的提示词**。

对照旧实现的三处错：
  ① 用适配器标量上限 → 应改为模型能力矩阵（分辨率→时长集合）
  ② 段长随意（total//n）→ 应全部落在合法集合里
  ③ 每段同一提示词 → 应各段不同、且只描述自己那几秒
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

from core.shotplan import plan_shot, plan_summary, allowed_durations   # noqa: E402
from core.videoprompt import build_video_prompt                        # noqa: E402
from core.model_catalog import VIDEO_CATALOG                           # noqa: E402

# 一个 12 秒、有 4 个时间片的分镜（模拟用户真实数据）
SHOT = {
    "id": "s1", "order_index": 0, "duration_seconds": 12, "resolution": "1080P",
    "layer1_overview": "李连长在战壕里向小虎交代任务",
    "layer2_timeline": json.dumps([
        {"start": 0, "end": 3, "action": "李连长蹲下，用树枝在泥地上画地图",
         "camera": "低角度特写", "expression": "眉头微皱"},
        {"start": 3, "end": 6, "action": "李连长抬头看向小虎",
         "camera": "过肩中景", "expression": "目光坚定",
         "dialogue": {"character": "李连长", "text": "天黑前必须夺回阵地。", "emotion": "坚定"}},
        {"start": 6, "end": 9, "action": "小虎挺直身子认真听",
         "camera": "正面特写", "expression": "眼神发亮"},
        {"start": 9, "end": 12, "action": "小虎点头，转身跑出战壕",
         "camera": "侧面中景", "expression": "神情坚决",
         "dialogue": {"character": "小虎", "text": "连长，让我去！", "emotion": "急切"}},
    ], ensure_ascii=False),
}

CHAR = {"id": "c1", "name": "李连长", "age": "40 岁上下", "gender": "male",
        "reference_features": json.dumps({"slots": {
            "face_shape": "方脸，颧骨明显", "hair": "短寸黑发，两鬓花白",
            "costume_top": "洗得发白的灰蓝粗布对襟上衣",
            "signature": "左眉骨旧疤"}}, ensure_ascii=False)}

MODELS = [("hailuo", "MiniMax-Hailuo-02"),      # 1080P 只给 6s，768P 给 6/10s
          ("hailuo", "video-01"),               # 全组合 6/10
          ("kling", "kling-1.6"),               # 5/10
          ("wanx", "wanx2.1-t2v-turbo")]        # 只给 5

for prov, mid in MODELS:
    entry = next((m for m in (VIDEO_CATALOG.get(prov) or {}).get("models", [])
                  if m["id"] == mid), {})
    print("=" * 90)
    print(f"模型 {prov}/{mid}")
    print(f"   能力矩阵: {entry.get('caps')}")
    p = plan_shot(SHOT, model_entry=entry, resolution="1080P")
    allowed = p.get("allowed") or []
    print(f"   规划: {p['requested']}s → 实际 {p['effective']}s @ {p['resolution']}"
          f" · {p['segment_count']} 段 · 合法时长={allowed}")
    print(f"   摘要: {plan_summary(p)}")
    for a in (p.get("adjustments") or []):
        print("   调整:", a.replace("<b>", "").replace("</b>", ""))
    for w in (p.get("warnings") or []):
        print("   警告:", str(w)[:100])

    # ① 每段时长都必须合法
    durs = [s["duration"] for s in p["segments"]]
    ok1 = all((not allowed) or (d in allowed) for d in durs)
    print(f"   {'✅' if ok1 else '❌'} 每段时长都在合法集合里: {durs} ⊆ {allowed or '(未知)'}")

    # ② 每段提示词必须不同，且只描述自己那几秒
    prompts = []
    for s in p["segments"]:
        seg_shot = dict(SHOT)
        seg_shot["layer2_timeline"] = json.dumps(s["timeline"], ensure_ascii=False)
        seg_shot["duration_seconds"] = s["duration"]
        vp = build_video_prompt(seg_shot, scene={"name": "战壕"}, characters=[CHAR],
                                style="cinematic", provider=prov, duration=s["duration"],
                                aspect_ratio="16:9", resolution=p["resolution"],
                                layers={"l1": True, "l2": True, "l3": True})
        prompts.append(vp["prompt"])
    uniq = len(set(prompts)) == len(prompts)
    print(f"   {'✅' if uniq else '❌'} 每段提示词互不相同（{len(set(prompts))}/{len(prompts)} 唯一）")
    for i, (s, txt) in enumerate(zip(p["segments"], prompts), 1):
        print(f"      段{i} [{s['start']}-{s['end']}s {s['duration']}s] "
              f"台词{s['line_count']}条 · 提示词 {len(txt)} 字")
        print(f"        {txt[:130]}")
    # ③ 分段后每段应当带上"自己那几秒"的内容（不重复上一段）
    if len(prompts) > 1:
        # 取每段提示词里的动作词，检查有没有串台
        acts = [s["timeline"][0].get("action", "")[:8] for s in p["segments"]]
        print(f"   各段起始动作: {acts}")

print("\n" + "=" * 90)
print("对照：旧实现会怎样")
for prov, mid in MODELS[:1]:
    entry = next((m for m in (VIDEO_CATALOG.get(prov) or {}).get("models", [])
                  if m["id"] == mid), {})
    print(f"   模型 {mid} 在 1080P 下合法时长只有 {allowed_durations(entry, '1080P')}，"
          f"而旧代码用 adapter.max_duration（标量，通常是 10）当上限 → 会请求 10 秒 → 被厂商拒绝")
