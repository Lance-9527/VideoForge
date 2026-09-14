# -*- coding: utf-8 -*-
r"""离线、零成本、确定性的"配音对时"回归（不联网、不调付费 TTS）。

为什么要有这一套
────────────────
2026-09-13 发现一个**只有混完成片才看得见**的错位：

    `synthesize_line` 用 atempo 把一句拉进它的时间片之后，
    把"对时倍数"（例如 0.748）当成"还要再拉一次的量"返回；
    `assemble_track` 于是**又拉了一遍** → 2.3 秒的时间片里塞进 3.0 秒的声音，
    超出 0.7 秒，下一句直接被压上。

单句音频、落点、字幕**全都是对的**，所以单看任何一项都发现不了。
它被抓到，是因为有人去量"混完之后这句实际占多长"。

这一套就是把这个量固化成断言：
    ① `synthesize_line` 返回的 `placed_duration`（它说会占多长）
    ② `assemble_track` 混完后报的 `actual_duration`（它真的占多长）
两者必须一致 —— 一旦有人再把"已经烙进文件的变速"往下传，这里立刻红。

用 `core.voice.dispatcher.synthesize` 的桩替掉真引擎：写一段定时长的正弦波。
这样时长完全可控，且**一个字节都不外发**。
"""
import asyncio
import io
import os
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from imageio_ffmpeg import get_ffmpeg_exe                      # noqa: E402

from core import voicecast                                     # noqa: E402
from core.voice import dispatcher as voice_dispatcher          # noqa: E402
from core.voice.base import TTSResult                          # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))


# ── 桩引擎：按文本里标注的秒数写一段正弦波（时长可知、可控、零成本）──
_RAW_SECONDS = {}


async def fake_synthesize(req, settings=None):
    secs = float(_RAW_SECONDS.get(req.text, 1.0))
    os.makedirs(os.path.dirname(req.output_path), exist_ok=True)
    rc = subprocess.run(
        [get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=24000:duration={secs:.4f}",
         "-c:a", "libmp3lame", "-q:a", "3", req.output_path],
        capture_output=True)
    if rc.returncode != 0:
        return TTSResult(success=False, voice_id=req.voice_id, error="桩引擎写文件失败")
    return TTSResult(success=True, voice_id=req.voice_id, audio_path=req.output_path)


def make_shot(lines, dur):
    return {
        "order_index": 0,
        "duration_seconds": dur,
        "layer2_timeline": lines,
    }


def line(start, end, who, text, emotion="中性"):
    return {"start": start, "end": end, "action": "",
            "dialogue": {"character": who, "text": text, "emotion": emotion}}


CHARACTERS = [
    {"id": "c1", "name": "甲", "gender": "male",
     "reference_features": '{"voice_id": "edge:zh-CN-YunxiNeural"}'},
    {"id": "c2", "name": "乙", "gender": "female",
     "reference_features": '{"voice_id": "edge:zh-CN-XiaoxiaoNeural"}'},
]


async def main() -> int:
    real = voice_dispatcher.synthesize
    voice_dispatcher.synthesize = fake_synthesize
    try:
        await run_cases()
        await run_real_scripts()
    finally:
        voice_dispatcher.synthesize = real

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("配音对时：落点 / 时间片 / 实际占用 三者一致，变速没有被算两次")
    return 0


async def run_real_scripts():
    """拿**真实项目**的剧本时间轴跑一遍（声音用桩，不花钱）。

    为什么还要这一层：上面三个用例是手挑的极端值。真实剧本里，一句台词
    到底比它的时间片长还是短、会不会正好落进"不值得调"的死区，只有拿真数据
    扫一遍才知道。桩的时长按"文本长度 × 固定系数"生成，系数覆盖 0.6~1.7，
    两个方向和死区都会被走到。
    """
    import hashlib
    import sqlite3

    db_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge",
                           "data", "videoforge.db")
    if not os.path.exists(db_path):
        check("真实剧本扫描（找不到数据库，跳过）", True)
        return

    from core.db import Database
    from core.dialogue import timeline_lines

    con = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    projects = [r[0] for r in con.execute("SELECT id FROM projects ORDER BY rowid")]
    con.close()
    db = Database(db_path)

    scratch = tempfile.mkdtemp(prefix="vf_dub_real_")
    n_lines = 0
    n_over = 0
    worst = []
    n_overlap = 0
    overlap_eg = []
    n_clamped_overlap = 0

    for pid in projects:
        try:
            chars = list(db.list_characters(pid) or [])
            shots = sorted(db.list_shots(pid) or [],
                           key=lambda s: s.get("order_index") or 0)
        except Exception:
            continue
        for sh in shots:
            dur = float(sh.get("duration_seconds") or 5)
            lines = timeline_lines(sh.get("layer2_timeline"), dur)
            if not lines:
                continue
            _RAW_SECONDS.clear()
            for ln in lines:
                span = float(ln["span"]) or 1.0
                h = int(hashlib.sha256(ln["text"].encode("utf-8")).hexdigest()[:6], 16)
                factor = 0.6 + (h % 1100) / 1000.0        # 0.6 ~ 1.7
                _RAW_SECONDS[ln["text"]] = max(0.4, span * factor)
            out = os.path.join(scratch, f"{pid[:8]}_{sh.get('id','')[:8]}")
            try:
                d = await voicecast.dub_shot(sh, characters=chars,
                                             settings={"tts_provider": "edge"},
                                             out_dir=out)
            except Exception as e:
                check(f"{pid[:8]} 分镜配音异常", False, str(e)[:120])
                continue
            if not d.get("ok"):
                continue
            for i, p in enumerate(d["placements"]):
                n_lines += 1
                span = float(p.get("target_span") or 0)
                actual = float(p["actual_duration"])
                if span > 0.2 and actual > span + 0.10:
                    n_over += 1
                    worst.append(f"{pid[:8]} 「{p['text'][:14]}」 {actual:.2f}s > {span:.2f}s")
                # 更不能压到下一句的起点上 —— 两个人抢话是最刺耳的"错位"。
                # 例外：被变速上限夹住的句子（台词本身比时间片长太多，
                # 再压就是快进音了），这类如实计数、单独报，不算回归失败。
                nxt = d["placements"][i + 1] if i + 1 < len(d["placements"]) else None
                if nxt and float(p["end"]) > float(nxt["start"]) + 0.05:
                    if p.get("clamped"):
                        n_clamped_overlap += 1
                    else:
                        n_overlap += 1
                        overlap_eg.append(
                            f"{pid[:8]} 「{p['text'][:10]}」 结尾 {p['end']}s "
                            f"压过下一句 {nxt['start']}s")

    check(f"真实剧本扫描：{n_lines} 句台词，混完无一句超时间片（>0.10s）",
          n_over == 0,
          f"超出 {n_over} 句" + ("；例：" + "；".join(worst[:3]) if worst else ""))
    check(f"真实剧本扫描：{n_lines} 句之间没有抢话（不压下一句起点）",
          n_overlap == 0,
          f"压线 {n_overlap} 处"
          + (f"；另有 {n_clamped_overlap} 处是变速上限夹住（台词确实放不下，已单独告警）"
             if n_clamped_overlap else "")
          + ("；例：" + "；".join(overlap_eg[:3]) if overlap_eg else ""))
    check(f"真实剧本扫描：{n_lines} 句都量过（样本量非平凡）", n_lines >= 5,
          f"样本 {n_lines} 句")


async def run_cases():
    scratch = tempfile.mkdtemp(prefix="vf_dub_timing_")
    settings = {"tts_provider": "edge"}

    # 三个刻意构造的场景：
    #   A 原声比时间片**长很多** → 需要压缩（atempo > 1.12，会被烙进文件）
    #   B 原声比时间片**短很多** → 需要拉长（atempo < 0.9，会被烙进文件）
    #   C 原声与时间片**只差一点点** → 落在死区里，不在合成期变速，
    #     留给排轨层拉一次（这一条最容易写成"拉两次"）
    cases = [
        ("A 原声偏长（0.9s 原声 → 2.0s 时间片）", "甲", "A句测试文本。", 0.9, 2.0),
        ("B 原声偏短（3.0s 原声 → 2.0s 时间片）", "乙", "B句测试文本。", 3.0, 2.0),
        ("C 只有微小偏差（2.06s 原声 → 2.0s 时间片）", "甲", "C句测试文本。", 2.06, 2.0),
    ]

    for title, who, text, raw_secs, span in cases:
        print(f"\n=== {title} ===")
        _RAW_SECONDS.clear()
        _RAW_SECONDS[text] = raw_secs
        out = os.path.join(scratch, who)
        os.makedirs(out, exist_ok=True)

        res = await voicecast.synthesize_line(
            text, "edge:zh-CN-YunxiNeural", settings,
            os.path.join(out, "line_000.mp3"), target_span=span,
            character=CHARACTERS[0], gender="male")

        if not res.get("ok"):
            check(f"{title} 合成成功", False, str(res.get("error")))
            continue

        print(f"    原声 {raw_secs}s · 时间片 {span}s · timelock={res['timelock']} "
              f"fit_applied={res['fit_applied']} 交给排轨={res['speed']} "
              f"声明实占={res['placed_duration']}s")

        # ① 声明"混完会占多长"必须与真实混完一致（这一条就是防重复变速的）
        shot = make_shot([line(0.0, span, who, text)], max(6.0, span + 2))
        dub = await voicecast.dub_shot(shot, characters=CHARACTERS, settings=settings,
                                       out_dir=out)
        if not dub.get("ok"):
            check(f"{title} dub_shot 成功", False, str(dub.get("error")))
            continue
        pl = dub["placements"][0]
        actual = float(pl["actual_duration"])
        declared = float(res["placed_duration"])
        check(f"{title} 声明实占 == 混完实占",
              abs(actual - declared) <= 0.05,
              f"声明 {declared}s / 实际 {actual}s（差 {actual - declared:+.3f}s）")

        # ② 混完的实占不许超过时间片（0.1s 容差给 atempo 的帧边界）
        check(f"{title} 实占未超时间片",
              actual <= span + 0.10,
              f"{actual}s ≤ {span}s + 0.10")

        # ③ 也不能短得离谱：说明这句根本没被对时（除非被 MIN_SPEED 夹住）
        clamped = abs(res["timelock"] - voicecast.MIN_SPEED) < 1e-6 or \
            abs(res["timelock"] - voicecast.MAX_SPEED) < 1e-6
        floor = min(span, raw_secs) * 0.75 if not clamped else 0.5 * span
        check(f"{title} 实占不应过短", actual >= floor - 0.10,
              f"{actual}s ≥ {floor:.2f}s" + ("（已触变速上限，放宽）" if clamped else ""))

        # ④ 落点必须还在剧本给的位置上（不能因为变速被挪走）
        check(f"{title} 起点仍在剧本位置",
              abs(float(pl["start"]) - 0.0) <= 0.35, f"start={pl['start']}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
