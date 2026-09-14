# -*- coding: utf-8 -*-
r"""「模型生成的镜头里，角色到底出不出声」——离线回归。

用户反馈：「通过视频模型生成的视频里面的人物没发出声音」。

实测根因（`tests/probe_shot_audio.py` 可复现）：
  · 视频模型返回的 mp4 **完全没有音轨** —— `caa9904f` 5 个 API 候选
    `Audio:` 一个都没有；
  · 而 `_try_api_video`（530 行）里**没有任何一处**做配音
    （dub / voicecast / TTSRequest / synthesize 出现 **0** 次）；
  · 配音只在**出片（compose）阶段**做 —— 所以成片有声音，但
    **点开这一镜预览是死寂**。观感就是"生成的人物不发声"。

修法：生成完立刻配音（用**整片选角表** + 逐句韵律），把音轨挂进这一镜。

这个套件用**真实素材**（库里那条真的没有音轨的候选片段）验证：
  ① 修之前的事实：那条候选确实没有音轨（否则这个测试的前提就不成立）；
  ② 配音 + 挂轨之后：**有音轨了**，而且语音落在剧本给的时间点上；
  ③ 视频流没被破坏（时长/分辨率不变）—— 挂音轨不能动画面；
  ④ 负对照：没有台词的分镜**不许**被加上旁白（那是 F-AUDIO-POLLUTION）。
"""
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from imageio_ffmpeg import get_ffmpeg_exe                      # noqa: E402

DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge", "data")
os.environ.setdefault("VIDEOFORGE_DATA_DIR", DATA)

from core import compose, voicecast                            # noqa: E402
from core.db import Database                                   # noqa: E402

FF = get_ffmpeg_exe()
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def probe(path):
    p = subprocess.run([FF, "-hide_banner", "-i", path], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    t = p.stderr or ""
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", t)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = re.search(r"Video:.*?,\s*(\d+)x(\d+)", t)
    return {"has_audio": "Audio:" in t, "duration": round(dur, 2),
            "size": f"{v.group(1)}x{v.group(2)}" if v else ""}


def speech_spans(path, noise_db=-40, min_sil=0.15):
    p = subprocess.run([FF, "-hide_banner", "-i", path, "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_sil}",
                        "-f", "null", "-"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    t = p.stderr or ""
    ss = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", t)]
    se = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", t)]
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", t)
    tot = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    spans, cur = [], 0.0
    for a, b in zip(ss, se + [tot]):
        if a > cur + 0.04:
            spans.append((round(cur, 3), round(a, 3)))
        cur = b
    if tot and cur < tot - 0.04:
        spans.append((round(cur, 3), round(tot, 3)))
    return spans, tot


def main() -> int:
    db = Database(os.path.join(DATA, "videoforge.db"))
    con = sqlite3.connect("file:" + os.path.join(DATA, "videoforge.db") + "?mode=ro",
                          uri=True)
    # 找一个"有台词 + 候选片段是 API 生成"的分镜
    target = None
    for pid, title in con.execute("SELECT id,title FROM projects ORDER BY rowid"):
        shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
        for i, sh in enumerate(shots):
            p = compose._selected_candidate_path(sh)
            if not p or not os.path.exists(p):
                continue
            from core.dialogue import timeline_lines
            lines = timeline_lines(sh.get("layer2_timeline"),
                                   float(sh.get("duration_seconds") or 5))
            if lines and "/api/" in p.replace("\\", "/"):
                target = (pid, title, i, sh, p, lines)
                break
        if target:
            break
    if not target:
        print("➖ 跳过：库里找不到「有台词 + API 生成候选」的分镜（无法验证前提）")
        return 0
    pid, title, idx, shot, clip, lines = target
    print(f"真实素材：{pid[:8]} 「{title[:22]}」 第 {idx+1} 镜，"
          f"{len(lines)} 句台词\n     {os.path.basename(clip)}")

    src = probe(clip)
    check("T1 前提成立：这条模型生成的候选片段**本身没有音轨**",
          not src["has_audio"], f"音轨={src['has_audio']} 时长={src['duration']}s")

    tmp = tempfile.mkdtemp(prefix="vf_shotdub_")
    try:
        chars = list(db.list_characters(pid) or [])
        settings = dict(db.get_all_settings() or {})
        dv = str(settings.get("default_voice") or "edge:zh-CN-XiaoxiaoNeural")
        cp = voicecast.cast_plan(chars, dv)
        import asyncio

        dub = asyncio.run(voicecast.dub_shot(
            shot, characters=chars, settings=settings,
            out_dir=os.path.join(tmp, "voice"), default_voice=dv,
            duration=src["duration"], cast=cp.get("voices")))
        check("T2 配音合成成功（按整片选角表 + 逐句韵律）",
              bool(dub.get("ok")) and bool(dub.get("placements")),
              f"ok={dub.get('ok')} 句数={dub.get('line_count')} "
              f"选角={dub.get('cast')}")
        if not dub.get("ok"):
            print("       错误：", dub.get("error"))
            return 1

        voiced = os.path.join(tmp, "voiced.mp4")
        muxed = asyncio.run(compose._mux_clip_audio(clip, dub["path"], voiced,
                                                    src["duration"]))
        check("T3 音轨成功挂进这一镜", bool(muxed), str(muxed))
        if not muxed:
            return 1
        out = probe(voiced)
        check("T4 挂完之后**有音轨了**（预览就能听到对白）",
              out["has_audio"], f"音轨={out['has_audio']}")
        check("T5 画面没被破坏：分辨率不变",
              out["size"] == src["size"], f"{src['size']} → {out['size']}")
        check("T6 画面没被破坏：时长基本不变",
              abs(out["duration"] - src["duration"]) <= 0.35,
              f"{src['duration']}s → {out['duration']}s")

        spans, tot = speech_spans(voiced)
        print(f"       实测有声区间：{spans}")
        # 第 1 句在剧本里给的时间点附近应当有声音
        first = lines[0]
        a, b = float(first["start"]), float(first["end"])
        ov = sum(max(0.0, min(b + 0.6, e) - max(max(0.0, a - 0.3), s))
                 for s, e in spans)
        check("T7 语音落在剧本给的时间点上（不是从头念到尾）",
              ov >= 0.25, f"第1句窗口 {a}-{b}s 内实测有声 {ov:.2f}s")

        # 负对照：没有台词 → 一个字都不加
        bare = dict(shot)
        bare["layer2_timeline"] = [{"start": 0, "end": 5, "action": "他站着不动"}]
        r2 = asyncio.run(voicecast.dub_shot(
            bare, characters=chars, settings=settings,
            out_dir=os.path.join(tmp, "bare"), default_voice=dv,
            duration=5.0, cast=cp.get("voices")))
        check("T8 负对照：没有台词的分镜**不生成旁白**（F-AUDIO-POLLUTION）",
              not r2.get("ok") and "台词" in str(r2.get("error") or ""),
              str(r2.get("error"))[:60])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 接线守卫（这条付费路径没法离线真跑，只能静态守住"接线还在"）──
    #   为什么值得写：这次的 bug 本质就是**代码里根本没有那一步**
    #   （`_try_api_video` 530 行里 dub/voicecast/TTS 出现 0 次）。
    #   有人手滑删掉那几行，靠人眼看是看不出来的，靠这个能看出来。
    src_main = io.open(os.path.join(HERE, "..", "backend", "main.py"),
                       encoding="utf-8").read()
    i_fn = src_main.index("async def _try_api_video")
    j_fn = src_main.index("def _api_error_hint", i_fn)
    fn = src_main[i_fn:j_fn]
    check("T9 接线：`_try_api_video` 接受 with_voice 参数",
          "with_voice: bool" in src_main[i_fn:i_fn + 900])
    check("T10 接线：生成成功路径里真的调了 dub_shot 并挂轨",
          ("voicecast" in fn and "dub_shot" in fn and "_mux_clip_audio" in fn),
          f"dub_shot={'dub_shot' in fn} mux={'_mux_clip_audio' in fn}")
    check("T11 接线：结果里带 voice 信息（界面/告警能看到配没配）",
          '"voice": voice_info' in fn)
    check("T12 接线：调用方把 with_voice 传进来了",
          'with_voice=bool(payload.get("with_voice", True))' in src_main)

    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
