# -*- coding: utf-8 -*-
"""验证「每角色不同音色 + 每句落在剧本时间点」真的做到。

这是一个**关键验收**：把两个角色的台词放在不同时间点，
用不同音色念出来，然后用 `silencedetect` 量出绝对有声区间，
证明声音确实出现在指定的秒数上，而不是从头念到尾。

★ 需要联网（Edge TTS），但**零 API 成本**（音色全部用免费的 `edge:`）。
  2026-09-13 修正：此前角色卡写的是付费的 minimax/siliconflow 音色，
  等于每次跑回归都在花钱，与"离线零成本门禁"的承诺矛盾。
"""
import asyncio
import io
import json
import os
import re
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

import sqlite3                                                    # noqa: E402

DB = os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data", "videoforge.db")
conn = sqlite3.connect(DB)
SETTINGS = {}
for k, v in conn.execute("SELECT key, value FROM settings"):
    if v and v[:1] in "[{":
        try:
            SETTINGS[k] = json.loads(v)
            continue
        except Exception:
            pass
    SETTINGS[k] = v

from core.voicecast import dub_shot, build_srt, cast_voice        # noqa: E402
from core.dialogue import normalize_timeline                     # noqa: E402
from imageio_ffmpeg import get_ffmpeg_exe                        # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dub_test")

# 两个角色，各配一个明显不同的音色；台词放在 1s / 4s / 7s 三个节点
SHOT = {
    "order_index": 0,
    "duration_seconds": 9,
    "layer2_timeline": [
        {"start": 0, "end": 1, "action": "雪地里，李连长蹲下画地图",
         "camera": "低角度特写"},
        {"start": 1, "end": 3.5, "action": "李连长抬头看向小虎",
         "dialogue": {"character": "李连长", "text": "小虎，天黑前夺回阵地。",
                      "emotion": "坚定"}},
        {"start": 3.5, "end": 4.2, "action": "小虎挺直身子"},
        {"start": 4.2, "end": 6.5, "action": "小虎请战",
         "dialogue": {"character": "小虎", "text": "连长，让我去！", "emotion": "急切"}},
        {"start": 6.5, "end": 9, "action": "两人望向远方"},
    ],
}

CHARACTERS = [
    # ★ 这里**必须用免费音色**。原来写的是 `minimax:` / `siliconflow:` ——
    #   于是这套"离线回归"每次运行都在**真调付费 TTS**，
    #   而 `run_round_regression.py` 的说明写着"刻意不包含会花钱的套件"。
    #   发现后改成 Edge（免费；仍需联网，但零成本）。
    #   测试要验的是"两个角色拿到两把不同的嗓子"，与厂商无关。
    {"id": "c1", "name": "李连长", "gender": "male",
     "reference_features": json.dumps({"voice_id": "edge:zh-CN-YunjianNeural",
                                       "aliases": ["连长", "老李"]},
                                      ensure_ascii=False)},
    {"id": "c2", "name": "小虎", "gender": "male",
     "reference_features": json.dumps({"voice_id": "edge:zh-CN-YunxiaNeural"},
                                      ensure_ascii=False)},
]


def speech_spans(path: str, noise_db: int = -40, min_sil: float = 0.15):
    """用 `silencedetect` 取**有声区间**（绝对秒）。

    ★ 为什么不用之前那套 `astats + ametadata=print`：它打出来的 `pts_time`
      是**滤镜内部的时间轴**，与实际音轨时间不是一回事 —— 实测据此判定
      "4.2-3.5s 静音"，而 `silencedetect` 明确显示那段时间**有声音**
      （4.71~6.14s 就是小虎那句）。工具用错，结论就反了：
      这条"❌"存在了很久，但因为它只是 `print`、不是断言，一直没被发现。
    """
    ff = get_ffmpeg_exe()
    r = subprocess.run(
        [ff, "-hide_banner", "-i", path, "-af",
         f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    txt = r.stderr or ""
    sil_starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", txt)]
    sil_ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", txt)]
    dur_m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
    total = 0.0
    if dur_m:
        total = int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60 + float(dur_m.group(3))
    # 补上"开头就是静音"与"结尾静音"两段，取补集即为有声区间
    spans = []
    cursor = 0.0
    for s, e in zip(sil_starts, sil_ends + [total]):
        if s > cursor + 0.02:
            spans.append((round(cursor, 3), round(s, 3)))
        cursor = e
    if total and cursor < total - 0.02:
        spans.append((round(cursor, 3), round(total, 3)))
    return spans


def overlap(a, b, spans) -> float:
    """区间 [a,b] 与有声区间的重叠秒数。"""
    return round(sum(max(0.0, min(b, e) - max(a, s)) for s, e in spans), 3)



async def main():
    print("═" * 74)
    print("① 选角：角色 → 音色")
    cast_checks = {}
    for c in CHARACTERS:
        v = cast_voice(c["name"], CHARACTERS, "edge:zh-CN-XiaoxiaoNeural")
        cast_checks[c["name"]] = v
        print(f"   {c['name']:8s} → {v}")
    print(f"   未知角色 '路人' → {cast_voice('路人', CHARACTERS, 'edge:zh-CN-XiaoxiaoNeural')}")
    print(f"   别名 '连长'   → {cast_voice('连长', CHARACTERS, 'edge:zh-CN-XiaoxiaoNeural')}")
    dupes = [k for k in cast_checks if list(cast_checks.values()).count(cast_checks[k]) > 1]
    if dupes:
        print(f"   ❌ 撞音色：{dupes}")
        raise SystemExit(1)
    print(f"   ✅ {len(cast_checks)} 个角色 → {len(set(cast_checks.values()))} 个不同音色，无撞车")

    print("\n" + "═" * 74)
    print("② 逐句合成 + 按绝对时间点落位")
    logs = []

    def prog(p, m):
        logs.append(f"     [{p*100:3.0f}%] {m}")

    res = await dub_shot(SHOT, characters=CHARACTERS, settings=SETTINGS,
                         out_dir=OUT, progress=prog)
    for l in logs:
        print(l)
    print()
    if not res.get("ok"):
        print("   ❌", res.get("error"))
        for w in res.get("warnings") or []:
            print("   ⚠", w)
        return
    print(f"   ✅ 音轨 {res['path']}")
    print(f"      时长 {res['duration']}s · {res['line_count']} 句")
    print(f"      选角结果: {json.dumps(res['cast'], ensure_ascii=False)}")
    print("      每句落点：")
    for p in res["placements"]:
        print(f"        {p['start']:5.2f}s → {p['end']:5.2f}s  "
              f"[{p['character']}] 「{p['text']}」 {p['actual_duration']}s "
              f"speed={p.get('speed',1)}")
    for w in res.get("warnings") or []:
        print("      ⚠", w)

    print("\n" + "═" * 74)
    print("③ 字幕（与语音同源，时间戳直接来自同一份数据）")
    srt = build_srt(res["subtitles"], os.path.join(OUT, "dub.srt"))
    print("   " + open(srt, encoding="utf-8").read().strip().replace("\n", "\n   "))

    print("\n" + "═" * 74)
    print("④ 关键验证：声音是否真的出现在指定秒数（而不是从头念到尾）")
    spans = speech_spans(res["path"])
    print(f"   实测有声区间（silencedetect -40dB/0.15s）：{spans}")
    failures = []

    def check(name, got, want):
        ok = got == want
        if not ok:
            failures.append(name)
        print(f"   {'✅' if ok else '❌'} {name}")
        return ok

    # ① 落点处必须真的有声音：用"与有声区间的重叠秒数"判定，
    #    而不是看某个采样点落在哪 —— 阈值判定要跟区间长度挂钩才稳。
    for p in res["placements"]:
        a, b = float(p["start"]), float(p["end"])
        ov = overlap(a, b, spans)
        need = min(0.25, (b - a) * 0.35)   # 至少覆盖该句时长的 35%（或 0.25s）
        check(f"{a:.2f}-{b:.2f}s 应有 [{p['character']}] 的声音"
              f"（实测重叠 {ov}s / 需 {need:.2f}s）", ov >= need, True)

    # ② 两句之间的空档（无台词的间隙）必须基本安静
    ps = sorted(res["placements"], key=lambda x: x["start"])
    for i in range(len(ps) - 1):
        gap_a, gap_b = float(ps[i]["end"]), float(ps[i + 1]["start"])
        if gap_b - gap_a < 0.6:
            continue
        ov = overlap(gap_a + 0.15, gap_b - 0.15, spans)
        check(f"{gap_a:.2f}-{gap_b:.2f}s 空档应基本静音（实测有声 {ov}s）",
              ov <= 0.25, True)

    # ③ 末句结束之后到音轨结尾必须安静（防止"从头念到尾"式的尾巴）
    tail_a = float(ps[-1]["end"]) + 0.35
    if res["duration"] - tail_a > 0.6:
        ov = overlap(tail_a, float(res["duration"]), spans)
        check(f"{tail_a:.2f}-{res['duration']}s 片尾应静音（实测有声 {ov}s）",
              ov <= 0.25, True)

    # ④ 每句**在混完的成片里**实际占的长度不许超过它的时间片
    #    —— 这条专抓"对时变速被算两次"：单句文件、落点、字幕全对，
    #       只有混完的成片超长，是最难发现的一类错位（实测曾超 0.7s）。
    for p in res["placements"]:
        span = float(p.get("target_span") or 0)
        if span <= 0.2:
            continue
        over = float(p["actual_duration"]) - span
        check(f"{p['character']} 实占 {p['actual_duration']}s ≤ 时间片 {span}s"
              f"（超出 {over:+.3f}s）", over <= 0.10, True)

    print("\n" + "═" * 74)
    if failures:
        print(f"❌ {len(failures)} 项未通过：")
        for f in failures:
            print("   -", f)
        raise SystemExit(1)
    print("✅ 全部通过：台词只出现在自己的时间点，空档与片尾静音")


asyncio.run(main())
