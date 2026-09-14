# -*- coding: utf-8 -*-
"""音频主轴规划测试 —— 治"分层定 10s 但最终只能生成 5s"这个病。

用户原话：
    "用户的大部分视频模型不支持5秒以上的视频生成，里面提到的分镜时长
     根本达不到，比如分层定10s但最终只能生成5s，
     且生成的视频跟实际描述的完全不相干或者只有一小部分！"

医学诊断（不是猜测，是实测出来的）：
  ① 时长矛盾 = 拿**规划稿数字**（10s）去要求模型，而模型只卖 5/6/10s；
     且真正的约束不是"这段话我写了 10s"，而是"这段话念完只要 4.2s"。
  ② 内容不相干 = 长分镜被切成 n 段，每段却用**同一段提示词**，
     于是每段重演同一件事；或者段长 `total//n` 不在合法时长集合里被静默截断。

这个测试覆盖两件事：
  A. `measure_lines_duration` 真的量出了音频长度（不是估算）
  B. 把量出来的长度喂给 `plan_shot`，规划结果**永远盖得住音频**、
     且**每一段都是模型真实合法的时长**、**每一段都有内容**

运行：python tests/test_audio_master_plan.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def mk_shot(nominal: float, lines, scene="破败的城门口，黄昏"):
    """造一个分镜：nominal 是**规划稿**上写的秒数。"""
    total = float(nominal)
    n = max(1, len(lines))
    step = total / n
    tl = []
    for i, (who, text) in enumerate(lines):
        tl.append({
            "index": i,
            "start": round(i * step, 2),
            "end": round((i + 1) * step, 2),
            "camera": "中景",
            "action": f"第{i + 1}个动作",
            "dialogue": {"character": who, "text": text, "emotion": "平静"},
        })
    return {
        "id": "shot-test", "project_id": "proj-test",
        "duration_seconds": nominal,
        "layer1_overview": scene,
        "layer2_timeline": json.dumps(tl, ensure_ascii=False),
        "resolution": "768p", "aspect_ratio": "16:9",
    }


MODEL_HAILUO_768 = {
    "id": "MiniMax-Hailuo-02", "label": "海螺 02",
    "caps": {"768P": [6, 10], "1080P": [6]},
    "mode": "t2v",
}
# 模拟用户说的"大部分模型只支持 5 秒"：只卖 5s，别的都不给。
MODEL_ONLY5 = {
    "id": "fake-5s-only", "label": "只会 5 秒的模型",
    "caps": {"720P": [5]},
    "mode": "t2v",
}


def real_hailuo_entry():
    """从**真正发布的** model_catalog 里取海螺 02，避免测试和产品能力表脱节。"""
    try:
        from core.model_catalog import VIDEO_CATALOG
        for m in (VIDEO_CATALOG.get("hailuo") or {}).get("models") or []:
            if m.get("id") == "MiniMax-Hailuo-02":
                return m
    except Exception as e:
        print(f"  (读真实能力表失败，用内置副本：{e})")
    return MODEL_HAILUO_768


async def part_a_measure():
    print("\n" + "=" * 72)
    print("A. 实测音频长度（不是估算）")
    print("=" * 72)

    from core.voicecast import measure_lines_duration

    tmp = tempfile.mkdtemp(prefix="vf_dur_")
    settings = {"cache_dir": tmp, "default_voice": "edge:zh-CN-XiaoxiaoNeural"}
    chars = [
        {"name": "布兰", "gender": "male",
         "reference_features": json.dumps({"voice_id": "edge:zh-CN-YunxiNeural"})},
        {"name": "艾莉亚", "gender": "female", "reference_features": ""},
    ]
    from core.dialogue import timeline_lines

    shot_s = mk_shot(10, [("布兰", "我看见了。")])
    shot_l = mk_shot(10, [("布兰", "我看见了三条龙从东边的海面上飞过来，"
                                 "它们的翅膀遮住了整片天空，然后一切都被火焰吞没了。")])

    d_s = await measure_lines_duration(
        timeline_lines(shot_s["layer2_timeline"], 10), chars, settings)
    d_l = await measure_lines_duration(
        timeline_lines(shot_l["layer2_timeline"], 10), chars, settings)

    print(f"  短句实测 = {d_s:.2f}s    长句实测 = {d_l:.2f}s")
    check("A1 短句量出了正数时长", d_s > 0.2, f"{d_s:.2f}s")
    check("A2 长句量出了正数时长", d_l > 0.2, f"{d_l:.2f}s")
    check("A3 长句 > 短句（量的是内容，不是常数）", d_l > d_s * 1.8,
          f"{d_l:.2f} vs {d_s:.2f}")

    # 缓存：同样的输入再量一次，值必须一样（且不该重新合成）
    d_s2 = await measure_lines_duration(
        timeline_lines(shot_s["layer2_timeline"], 10), chars, settings)
    check("A4 第二次量同一句结果一致（缓存命中）", abs(d_s2 - d_s) < 0.02,
          f"{d_s:.2f} -> {d_s2:.2f}")

    # 换了角色音色 → 结果可能变（证明确实用的是角色自己的音色）
    chars2 = [{"name": "布兰", "gender": "female",
               "reference_features": json.dumps(
                   {"voice_id": "edge:zh-CN-XiaoyiNeural"})}]
    d_s3 = await measure_lines_duration(
        timeline_lines(shot_s["layer2_timeline"], 10), chars2, settings)
    print(f"  换音色后 = {d_s3:.2f}s")
    check("A5 换音色后仍然量得出来", d_s3 > 0.2, f"{d_s3:.2f}s")

    # 没台词 → 0.0，且不能抛异常
    empty = mk_shot(10, [("", "")])
    tl = json.loads(empty["layer2_timeline"])
    for it in tl:
        it["dialogue"] = {"character": "", "text": ""}
    empty["layer2_timeline"] = json.dumps(tl, ensure_ascii=False)
    d_e = await measure_lines_duration(
        timeline_lines(empty["layer2_timeline"], 10), chars, settings)
    check("A6 没台词返回 0.0（不抛异常）", d_e == 0.0, f"{d_e}")
    return d_s, d_l


async def part_b_plan():
    print("\n" + "=" * 72)
    print("B. 音频主轴规划：盖得住、合法、每段都有内容")
    print("=" * 72)

    from core.shotplan import plan_shot, allowed_durations
    H = real_hailuo_entry()
    print(f"  用真实能力表：{H.get('id')} caps={H.get('caps')}")
    print(f"    768P 合法时长 = {allowed_durations(H, '768p')}")
    print(f"    1080P 合法时长 = {allowed_durations(H, '1080p')}")

    shot = mk_shot(10, [("布兰", "我看见了。")])

    # ── 用户报的那个 case：分层写 10s，但模型只给 5s ──
    plan = plan_shot(shot, model_entry=MODEL_ONLY5, resolution="720p",
                     want_duration=10, audio_seconds=4.2)
    segs = plan["segments"]
    total = sum(int(s["duration"]) for s in segs)
    print(f"  标称10s + 音频4.2s + 只会5s的模型 -> 总长 {total}s，"
          f"{len(segs)} 段 {[s['duration'] for s in segs]}")
    check("B1 每段都是模型合法时长(5s)", all(int(s["duration"]) == 5 for s in segs),
          str([s["duration"] for s in segs]))
    check("B2 总长盖得住音频 4.2s", total >= 4.2, f"{total}s")
    check("B3 不为 4.2s 的音频付两次调用", len(segs) == 1, f"{len(segs)} 段")

    # ── 关键：时长贴近音频，而不是盲目贴标称 ──
    print("\n  音频主轴 vs 标称时长（海螺 768p：合法 6/10s）")
    rows = []
    for audio in (0.0, 3.0, 5.0, 6.8, 9.0, 13.5, 20.0, 26.0):
        p = plan_shot(shot, model_entry=H, resolution="768p",
                      want_duration=12, audio_seconds=audio)
        ss = p["segments"]
        t = sum(int(x["duration"]) for x in ss)
        rows.append((audio, t, [int(x["duration"]) for x in ss], p["duration_source"]))
        print(f"    音频 {audio:5.1f}s -> 总长 {t:3d}s  "
              f"{len(ss)}段 {[int(x['duration']) for x in ss]}  ({p['duration_source']})")

    legal = {6, 10}
    check("B4 所有段长都在合法集合 {6,10} 内",
          all(d in legal for _, _, ds, _ in rows for d in ds),
          str(sorted({d for _, _, ds, _ in rows for d in ds})))
    covered = [(a, t) for a, t, _, _ in rows if a > 0]
    check("B5 每一档总长都盖得住音频",
          all(t >= a for a, t in covered),
          str([(a, t) for a, t in covered if t < a] or "全部盖住"))
    # 不能再出现"音频只要 6.8s 却拆成 6+6=12s"那种白花钱
    r68 = [r for r in rows if abs(r[0] - 6.8) < 0.01][0]
    check("B6 音频 6.8s -> 一次 10s 调用（旧逻辑是 6+6 两次）",
          r68[2] == [10], str(r68[2]))
    r3 = [r for r in rows if abs(r[0] - 3.0) < 0.01][0]
    check("B7 音频 3.0s -> 一次 6s 调用", r3[2] == [6], str(r3[2]))
    check("B8 音频>0 时 duration_source 标注了配音实测",
          all("配音" in r[3] or "台词" in r[3] for r in rows if r[0] > 0.2),
          str({r[3] for r in rows if r[0] > 0.2}))
    check("B9 无音频时不动时间轴（用标称）",
          [r for r in rows if r[0] == 0.0][0][1] == 12,
          str([r for r in rows if r[0] == 0.0][0]))

    # ── 每段都有内容，且各段内容**不同** ──
    print("\n  分段内容切片（4 句台词、标称 26s）")
    shot4 = mk_shot(26, [("布兰", "第一句台词内容在这里。"),
                         ("艾莉亚", "第二句台词内容在这里。"),
                         ("布兰", "第三句台词内容在这里。"),
                         ("艾莉亚", "第四句台词内容在这里。")])
    p4 = plan_shot(shot4, model_entry=H, resolution="768p",
                   want_duration=26, audio_seconds=24.0)
    segs4 = p4["segments"]
    print(f"    {len(segs4)} 段 {[int(s['duration']) for s in segs4]}")
    for i, s in enumerate(segs4):
        acts = [a.get("action") for a in (s.get("timeline") or [])]
        print(f"      段{i} [{s['start']}-{s['end']}s] 动作={acts}")
    check("B10 每段都非空（没有白花钱的空段）",
          all(len(s.get("timeline") or []) > 0 for s in segs4),
          str([len(s.get("timeline") or []) for s in segs4]))
    check("B11 段与段的时间线不重叠（各演各的，不重演同一件事）",
          all(segs4[i]["end"] <= segs4[i + 1]["start"] + 0.01
              for i in range(len(segs4) - 1)),
          str([(s["start"], s["end"]) for s in segs4]))
    check("B12 段时长单调不增（先满后尾）" ,
          [int(s["duration"]) for s in segs4] ==
          sorted([int(s["duration"]) for s in segs4], reverse=True),
          str([int(s["duration"]) for s in segs4]))

    # ★ 段间"重演同一件事"是用户投诉里"画面跟描述不相干/只有一小部分"的成因之一：
    #   一个动作若同时落进相邻两段，第 2 段开头会把第 1 段演过的动作再演一遍。
    seen: dict = {}
    dupes = []
    for i, s in enumerate(segs4):
        for a in (s.get("timeline") or []):
            key = str(a.get("action") or "")
            if not key:
                continue
            if key in seen:
                dupes.append((key, seen[key], i))
            else:
                seen[key] = i
    check("B17 同一个动作不会出现在两段里（不重演）", not dupes, str(dupes))

    # 台词同理：同一句台词不能在两段里各配一次音
    lseen: dict = {}
    ldup = []
    for i, s in enumerate(segs4):
        for ln in (s.get("lines") or []):
            t = str(ln.get("text") or "")
            if not t:
                continue
            if t in lseen:
                ldup.append((t[:14], lseen[t], i))
            else:
                lseen[t] = i
    check("B18 同一句台词不会在两段里各念一遍", not ldup, str(ldup))

    # 所有动作加起来必须覆盖原时间线的全部动作（不能因为归属判定丢内容）
    orig_actions = {str(x.get("action") or "") for x in json.loads(
        shot4["layer2_timeline"]) if x.get("action")}
    seg_actions = {str(a.get("action") or "") for s in segs4
                   for a in (s.get("timeline") or []) if a.get("action")}
    missing = orig_actions - seg_actions
    check("B19 没有动作在切片时被丢掉", not missing, str(missing))

    # ── 1080p 只允许 6s、音频 9s：应该**降一档到 768P 一次出完**，而不是 6+6 两次 ──
    p1080 = plan_shot(shot, model_entry=H, resolution="1080p",
                      want_duration=10, audio_seconds=9.0)
    s1080 = p1080["segments"]
    t1080 = sum(int(s["duration"]) for s in s1080)
    print(f"\n  1080p(只允许6s) + 音频9s -> 总长 {t1080}s "
          f"res={p1080['resolution']} {[int(s['duration']) for s in s1080]}")
    check("B13 自动降档到 768P、一次出完（省一次调用、省一份钱）",
          p1080["resolution"] == "768P" and [int(s["duration"]) for s in s1080] == [10],
          f"res={p1080['resolution']} segs={[int(s['duration']) for s in s1080]}")
    check("B14 总长盖得住 9s", t1080 >= 9.0, f"{t1080}s")
    # 降档必须**说清楚**，不能偷偷换分辨率；而且说的次数必须是真的
    _adj = [a for a in p1080["adjustments"] if "改用" in a]
    print(f"    降档说明: {_adj[0] if _adj else '(缺失)'}")
    check("B14b 降档有明确说明（不偷偷换）",
          any("改用" in a and "768P" in a for a in p1080["adjustments"]), str(_adj))
    check("B14d 说明里的次数是真的（1080p 确实要 2 次）",
          bool(_adj) and "要拆 2 次" in _adj[0],
          _adj[0] if _adj else "")

    # 用户明确锁死 1080p 时（allow_resolution_downgrade=False）才走 6+6
    p_lock = plan_shot(shot, model_entry=H, resolution="1080p",
                       want_duration=10, audio_seconds=9.0,
                       allow_resolution_downgrade=False)
    slock = p_lock["segments"]
    print(f"  锁死 1080p -> res={p_lock['resolution']} "
          f"{[int(s['duration']) for s in slock]}")
    check("B14c 锁死分辨率时退化为 6+6（仍然盖得住）",
          p_lock["resolution"] == "1080p"
          and all(int(s["duration"]) == 6 for s in slock)
          and sum(int(s["duration"]) for s in slock) >= 9.0,
          f"{[int(s['duration']) for s in slock]}")

    # ── 提示词切片：每段的提示词必须不同 ──
    print("\n  每段提示词是否真的不同（『画面跟描述不相干』的直接病因）")
    from core.videoprompt import build_video_prompt
    prompts = []
    for s in segs4:
        sshot = dict(shot4)
        sshot["layer2_timeline"] = json.dumps(s["timeline"], ensure_ascii=False)
        sshot["duration_seconds"] = int(s["duration"])
        vp = build_video_prompt(sshot, scene=None, characters=None, art={},
                                style="cinematic", provider="minimax",
                                duration=int(s["duration"]), aspect_ratio="16:9",
                                resolution="768p")
        prompts.append(vp["prompt"])
    uniq = len(set(prompts))
    print(f"    {len(prompts)} 段 -> {uniq} 个不同提示词")
    for i, p in enumerate(prompts):
        print(f"      段{i}: {p[:88]}...")
    check("B15 段数>1 时提示词必须互不相同", uniq == len(prompts),
          f"{uniq}/{len(prompts)}")

    # ── 动作顺序必须是时间顺序 ──
    print("\n  动作顺序（旧 bug：按字符串长度排序 -> 时间倒流）")
    shotseq = mk_shot(12, [])
    shotseq["layer2_timeline"] = json.dumps([
        {"index": 0, "start": 0, "end": 4, "camera": "中景", "action": "他推开门",
         "dialogue": {"character": "", "text": ""}},
        {"index": 1, "start": 4, "end": 8, "camera": "近景",
         "action": "他缓缓走到桌前坐下并抬头看向窗外那片正在崩塌的城墙",
         "dialogue": {"character": "", "text": ""}},
        {"index": 2, "start": 8, "end": 12, "camera": "特写", "action": "他笑了",
         "dialogue": {"character": "", "text": ""}},
    ], ensure_ascii=False)
    vp = build_video_prompt(shotseq, scene=None, characters=None, art={},
                            style="cinematic", provider="minimax", duration=12,
                            aspect_ratio="16:9", resolution="768p")
    pr = vp["prompt"]
    i_open, i_sit, i_smile = pr.find("推开门"), pr.find("坐下"), pr.find("笑了")
    print(f"    推开门@{i_open}  坐下@{i_sit}  笑了@{i_smile}")
    print(f"    {pr[:160]}...")
    check("B16 动作按时间顺序出现（推开门 -> 坐下 -> 笑了）",
          -1 < i_open < i_sit < i_smile,
          f"{i_open},{i_sit},{i_smile}")

    # ★ 动作是**带句号的整句**，直接用"，随后"拼会写出 "姿态。，随后…"
    #   这种双重标点。模型读到混乱的断句，生成质量会掉 —— 而提示词的
    #   标点质量本来就该是我们自己保证的。
    shot_dot = mk_shot(12, [])
    shot_dot["layer2_timeline"] = json.dumps([
        {"index": 0, "start": 0, "end": 4, "camera": "中景",
         "action": "布兰坐在轮椅上，缓缓进入铁王座大厅。",
         "dialogue": {"character": "", "text": ""}},
        {"index": 1, "start": 4, "end": 8, "camera": "中景",
         "action": "无面者刺客站在铁王座旁，目光锐利地注视着布兰。",
         "dialogue": {"character": "", "text": ""}},
        {"index": 2, "start": 8, "end": 12, "camera": "中景",
         "action": "布兰抬头看向铁王座，无面者保持警惕姿态。",
         "dialogue": {"character": "", "text": ""}},
    ], ensure_ascii=False)
    pd = build_video_prompt(shot_dot, scene=None, characters=None, art={},
                            style="cinematic", provider="minimax", duration=12,
                            aspect_ratio="16:9", resolution="768p")["prompt"]
    print(f"\n  拼接后的画面内容：{pd[:150]}...")
    check("B20 动作拼接不会写出『。，随后』这种双重标点",
          "。，随后" not in pd and "。，" not in pd and "。。" not in pd,
          "有双重标点" if ("。，" in pd or "。。" in pd) else "干净")
    check("B21 三句动作仍然全部保留、顺序不变",
          pd.find("布兰坐在轮椅上") < pd.find("无面者刺客") < pd.find("布兰抬头看向"),
          "ok")


async def part_c_model_lookup():
    print("\n" + "=" * 72)
    print("C. 模型查表 + 降档说明的正确性（这两处各有一个真 bug）")
    print("=" * 72)

    from core.model_catalog import find_video_model, VIDEO_CATALOG
    from core.shotplan import plan_shot, _resolution_candidates, allowed_durations

    # ── bug ①：跨厂商查表 ──
    #   线上分镜表里真的有 provider=hailuo 而 model_name=kling-1.6 的记录。
    #   只按 provider 查 → 查不到 → 退回"最保守 5 秒" → 本来能一次出 10 秒的
    #   镜头被压成 5+5 两次调用，多花钱还掉画质。
    hit = find_video_model("kling-1.6", "hailuo")
    print(f"  kling-1.6 @provider=hailuo -> {hit.get('id')} "
          f"(来自 {hit.get('found_under')}) caps={hit.get('caps')}")
    check("C1 provider 与 model_name 不一致时仍能查到能力表",
          bool(hit) and hit.get("id") == "kling-1.6", str(hit.get("id")))
    check("C2 跨厂商查到的是真能力（kling-1.6 有 720P/1080P）",
          set((hit.get("caps") or {}).keys()) == {"720P", "1080P"},
          str(list((hit.get("caps") or {}).keys())))
    check("C3 认不出来的模型如实返回空（不瞎猜）",
          find_video_model("this-model-does-not-exist", "hailuo") == {},
          "ok")
    check("C4 空名字返回空", find_video_model("", "hailuo") == {}, "ok")
    check("C5 忽略大小写也能查到",
          (find_video_model("minimax-hailuo-02", "hailuo") or {}).get("id")
          == "MiniMax-Hailuo-02", "ok")
    # 全表每个模型都要能被 find_video_model 找到（保证这张表自洽）
    missing = []
    for prov, conf in VIDEO_CATALOG.items():
        for m in (conf.get("models") or []):
            f = find_video_model(str(m.get("id")), prov)
            if not f or not f.get("caps"):
                missing.append(f"{prov}/{m.get('id')}")
    check("C6 全表模型都能查到且都带能力矩阵", not missing, str(missing))

    # ── bug ②：降档说明 ──
    #   分镜里存的分辨率是小写 "1080p"，能力表里是大写 "1080P"，
    #   直接字符串比较 != 会**误报降档**，写出"改用 1080P（少一份钱）"这种
    #   分辨率根本没变的假话。
    H = real_hailuo_entry()
    shot = mk_shot(10, [("布兰", "我看见了。")])
    # 1080P 一次就能出完 6s → 不该出现任何"改用/降档"字样
    p = plan_shot(shot, model_entry=H, resolution="1080p", want_duration=10,
                  audio_seconds=5.5)
    downgrade = [a for a in p["adjustments"] if "改用" in a or "没有" in a]
    print(f"  1080p 一次能出完 -> res={p['resolution']} "
          f"segs={[int(s['duration']) for s in p['segments']]} "
          f"降档说明={downgrade}")
    check("C7 分辨率没变时**不准**出现降档说明", not downgrade, str(downgrade))

    # 真的降档时：次数必须是真的，且必须提到新分辨率
    p2 = plan_shot(shot, model_entry=H, resolution="1080p", want_duration=10,
                   audio_seconds=9.0)
    dg = [a for a in p2["adjustments"] if "改用" in a]
    print(f"  真降档 -> res={p2['resolution']} {dg[0] if dg else '(缺失)'}")
    check("C8 真降档时必须说明", bool(dg), str(dg))
    check("C9 说明里的旧次数是真的（1080p 确实要 2 次）",
          bool(dg) and "要拆 2 次" in dg[0], dg[0] if dg else "")
    check("C10 说明里不会出现『改用 1080p』这种没变的分辨率",
          bool(dg) and "改用 768P" in dg[0], dg[0] if dg else "")

    # 模型根本没有用户选的分辨率时，措辞要换（不能说"省了钱"）
    W = {"id": "wanx2.1-t2v-turbo", "label": "万相 Turbo",
         "caps": {"480P": [5], "720P": [5]}, "mode": "t2v"}
    p3 = plan_shot(shot, model_entry=W, resolution="1080p", want_duration=12)
    dg3 = [a for a in p3["adjustments"] if "没有" in a]
    print(f"  模型没有 1080P -> {dg3[0] if dg3 else '(缺失)'}")
    check("C11 模型没有该分辨率时如实说『没有』，不吹省了钱",
          bool(dg3) and "少一份钱" not in dg3[0] and "少 1 次" not in dg3[0],
          dg3[0] if dg3 else "")

    # ── 次数相同时**不许**动用户选的画质 ──
    #   I2V-01 三个分辨率都只有 [6]。want=5 时三者都是 1 次调用，
    #   这时绝不能"顺手"把 512P 升成 1080P —— 画质上去了，账单也上去了。
    I2V = find_video_model("I2V-01", "hailuo")
    p4 = plan_shot(shot, model_entry=I2V, resolution="512P", want_duration=5)
    print(f"  I2V-01 512P want=5 -> res={p4['resolution']} "
          f"segs={[int(s['duration']) for s in p4['segments']]}")
    check("C13 次数相同时保留用户选的分辨率（不乱升级加价）",
          p4["resolution"].upper() == "512P", p4["resolution"])
    check("C14 没有省钱时不编『少一份钱』的理由",
          not any("少一份钱" in a for a in p4["adjustments"]),
          str(p4["adjustments"]))

    # 用户选的分辨率模型真的没有 → 才允许换，且必须说明
    p5 = plan_shot(shot, model_entry=I2V, resolution="720p", want_duration=6)
    print(f"  I2V-01 720p(模型没有) want=6 -> res={p5['resolution']}")
    check("C15 模型没有该分辨率时可以换，但必须说明",
          p5["resolution"].upper() != "720P"
          and any("没有" in a for a in p5["adjustments"]),
          str(p5["adjustments"]))

    # ── 目标函数：调用次数优先 ──
    #   对每个模型、每个目标秒数，规划出来的段数必须是**所有分辨率里最少的**。
    print("\n  调用次数是不是真的最省（逐模型穷举对照）")
    worse = []
    for prov, conf in VIDEO_CATALOG.items():
        for m in (conf.get("models") or []):
            caps = m.get("caps") or {}
            if not caps:
                continue
            for res in caps:
                for want in (5, 6, 7, 8, 10, 12, 15, 20):
                    res_list = _resolution_candidates(m, res)
                    got = plan_shot(shot, model_entry=m, resolution=res,
                                    want_duration=want,
                                    allow_resolution_downgrade=True)
                    n_got = len(got["segments"])
                    # 穷举所有候选分辨率，看有没有更少的
                    best = None
                    for r in res_list:
                        d = allowed_durations(m, r)
                        if not d:
                            continue
                        # 该分辨率下最少要几段才盖得住 want
                        for n in range(1, 7):
                            if any(base * n >= want for base in d):
                                best = n if best is None else min(best, n)
                                break
                    if best and n_got > best:
                        worse.append(f"{prov}/{m.get('id')}@{res} want={want} "
                                     f"got={n_got} best={best}")
    check("C12 任何模型/分辨率/目标时长下，段数都不多于理论最优",
          not worse, str(worse[:6]))
    return


async def main():
    d_s, d_l = await part_a_measure()
    await part_b_plan()
    await part_c_model_lookup()
    print("\n" + "=" * 72)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  [x] " + f)
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
