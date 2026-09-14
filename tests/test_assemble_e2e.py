# -*- coding: utf-8 -*-
"""端到端验证「成片装配」：转场 + 逐句配音 + 时间轴映射 + BGM避让 + 响度归一化。

用合成片段（颜色+编号清晰可辨）而不是真实生成视频 —— 这样每一秒是什么画面、
哪一秒该有谁的声音，都是可验证的，不受模型随机性干扰。
"""
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

from imageio_ffmpeg import get_ffmpeg_exe                        # noqa: E402
FF = get_ffmpeg_exe()
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_assemble_test")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)

from core.continuity import chain_plan, final_timeline, remap_lines_to_film  # noqa: E402
from core.voicecast import assemble_track, build_srt, synthesize_line       # noqa: E402
from core.assemble import concat_with_transitions, mux_film, normalize_picture  # noqa: E402
from core.dialogue import timeline_lines                                     # noqa: E402
import sqlite3                                                               # noqa: E402

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

W, H = 640, 360
FPS = 30


def make_clip(path, color, label, dur, with_audio=False):
    """造一段可辨识的测试画面：纯色 + 大号场次编号。"""
    draw = (f"drawtext=text='{label}':fontsize=64:fontcolor=white:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.4:boxborderw=12")
    if with_audio:
        # 有音轨版本：两路 lavfi 输入 + 统一滤镜，避免 -vf 与多输入冲突
        cmd = [FF, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:d={dur}:r={FPS}",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
               "-filter_complex", f"[0:v]{draw}[v]",
               "-map", "[v]", "-map", "1:a",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-shortest", path]
    else:
        cmd = [FF, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:d={dur}:r={FPS}",
               "-vf", draw, "-an",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(f"造片段失败 {label}: {r.stderr[-400:]}")


def probe(path):
    r = subprocess.run([FF, "-hide_banner", "-i", path], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
    d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0
    return round(d, 2), ("Audio:" in (r.stderr or ""))


def window_db(path, start, dur):
    p = subprocess.run([FF, "-hide_banner", "-nostats", "-ss", f"{start:.3f}",
                        "-t", f"{dur:.3f}", "-i", path, "-af", "volumedetect",
                        "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", p.stderr or "")
    return float(m.group(1)) if m else -999.0


# ── 剧本：一个 3 镜头的小片段，转场需求各不相同 ──
SCENES = {
    "s1": {"id": "s1", "name": "战壕", "time_of_day": "day", "lighting": "overcast",
           "reference_features": json.dumps({"slots": {"color_tone": "冷灰"}})},
    "s2": {"id": "s2", "name": "指挥部", "time_of_day": "night", "lighting": "candle"},
}
SHOTS = [
    {"id": "sh1", "order_index": 0, "scene_id": "s1", "duration_seconds": 6,
     "model_provider": "hailuo", "model_name": "I2V-01",
     "layer2_timeline": [
         {"start": 0, "end": 1, "action": "雪地全景"},
         {"start": 1, "end": 3.5, "action": "李连长抬头",
          "dialogue": {"character": "李连长", "text": "小虎，天黑前夺回阵地。", "emotion": "坚定"}},
         {"start": 3.5, "end": 6, "action": "两人望向远方"}]},
    {"id": "sh2", "order_index": 1, "scene_id": "s1", "duration_seconds": 6,
     "model_provider": "hailuo", "model_name": "I2V-01",
     "layer2_timeline": [
         {"start": 0, "end": 1, "action": "小虎特写"},
         {"start": 1, "end": 3.2, "action": "小虎请战",
          "dialogue": {"character": "小虎", "text": "连长，让我去！", "emotion": "急切"}},
         {"start": 3.2, "end": 6, "action": "两人握手"}]},
    {"id": "sh3", "order_index": 2, "scene_id": "s2", "duration_seconds": 6,
     "model_provider": "hailuo", "model_name": "I2V-01",
     "layer2_timeline": [
         {"start": 0, "end": 1.5, "action": "油灯下的地图"},
         {"start": 1.5, "end": 4, "action": "李连长指着地图",
          "dialogue": {"character": "李连长", "text": "这里，就是突破口。", "emotion": "沉稳"}},
         {"start": 4, "end": 6, "action": "镜头拉远"}]},
]
CHARACTERS = [
    {"id": "c1", "name": "李连长", "gender": "male",
     "reference_features": json.dumps({"voice_id": "minimax:male-qn-jingying"},
                                      ensure_ascii=False)},
    {"id": "c2", "name": "小虎", "gender": "male",
     "reference_features": json.dumps({"voice_id": "edge:zh-CN-YunxiNeural"},
                                      ensure_ascii=False)},
]


async def main():
    print("═" * 76)
    print("① 造 3 段测试画面（6s 各，色彩+编号可辨识）")
    raw = []
    for i, (sh, col, lbl) in enumerate(zip(SHOTS, ["0x2b4a6b", "0x4a2b6b", "0x6b4a2b"],
                                           ["SHOT 1", "SHOT 2", "SHOT 3"])):
        p = os.path.join(WORK, f"raw{i+1}.mp4")
        make_clip(p, col, lbl, 6.0)
        d, a = probe(p)
        raw.append(p)
        print(f"   raw{i+1}.mp4  {d}s  音轨={a}")
    # 故意让第 2 段带音轨、第 1/3 段没有 —— 验证"缺音轨也能拼"
    p2 = os.path.join(WORK, "raw2a.mp4")
    make_clip(p2, "0x4a2b6b", "SHOT 2", 6.0, with_audio=True)
    raw[1] = p2
    print(f"   （把第 2 段换成带音轨版本，验证混合拼接）")

    print("\n" + "═" * 76)
    print("② 规范化为统一规格、**去掉音轨**")
    norm = []
    for i, p in enumerate(raw):
        d = os.path.join(WORK, f"norm{i+1}.mp4")
        await normalize_picture(p, d, W, H, FPS)
        dd, aa = probe(d)
        norm.append(d)
        print(f"   norm{i+1}.mp4  {dd}s  音轨={aa}  ← 应该是 False")

    print("\n" + "═" * 76)
    print("③ 串联计划 + 转场选择")
    plan = chain_plan(SHOTS, scenes=SCENES,
                      selected_video_of=lambda s: raw[SHOTS.index(s)])
    for i, p in enumerate(plan):
        t = p["transition"]
        u = p["use_last_frame_of"]
        print(f"   {p['shot_id']}: 转场={t['type']:5s}({t['seconds']}s) [{t['level']:6s}] "
              f"接上一镜尾帧={'是' if u else '否'}  {t.get('why','')}")

    print("\n" + "═" * 76)
    print("④ 按逐切点转场拼接画面（无音轨）")
    pic = os.path.join(WORK, "picture.mp4")
    r = await concat_with_transitions(norm, pic, [p["transition"] for p in plan],
                                      w=W, h=H, fps=FPS)
    pd, pa = probe(pic)
    print(f"   模式={r['mode']}  各段={r['durations']}  切点削减={r['cuts']}")
    print(f"   画面轨 {pd}s  音轨={pa}")
    expect = sum(6.0 for _ in SHOTS) - sum(r["cuts"])
    print(f"   期望总长 {expect:.2f}s → 实际 {pd}s  {'✅' if abs(pd-expect)<0.5 else '❌'}")

    print("\n" + "═" * 76)
    print("⑤ 时间轴映射：算出每个分镜在成片里的真实起点")
    tl = final_timeline(SHOTS, plan)
    print(f"   成片总长 {tl['total']}s（被转场吃掉 {tl['timeline_shrink']}s）")
    for s in tl["shots"]:
        print(f"     {s['shot_id']}: 成片 {s['film_start']:5.2f}-{s['film_end']:5.2f}s"
              f"  转场进 {s['transition_in']}({s['overlap_in']}s)")

    print("\n" + "═" * 76)
    print("⑥ 逐句配音（每角色不同音色），落到**映射后的成片时间**")
    placements, subs = [], []
    for sh in SHOTS:
        lines = timeline_lines(sh["layer2_timeline"], sh["duration_seconds"])
        mapped = remap_lines_to_film(sh["id"], lines, tl)
        for j, ln in enumerate(mapped):
            v = "minimax:male-qn-jingying" if ln["character"] == "李连长" \
                else "edge:zh-CN-YunxiNeural"
            out = os.path.join(WORK, f"dub_{sh['id']}_{j}.mp3")
            rr = await synthesize_line(ln["text"], v, SETTINGS, out,
                                       target_span=ln["span"], emotion=ln.get("emotion", ""))
            if not rr.get("ok"):
                print(f"   ❌ {ln['text'][:16]} {rr.get('error')}")
                continue
            placements.append({"path": out, "start": ln["start"], "span": ln["span"],
                               "text": ln["text"], "character": ln["character"],
                               "voice_id": v, "speed": rr.get("speed", 1.0)})
            subs.append({"start": ln["start"],
                         "end": round(ln["start"] + rr["duration"], 3),
                         "text": ln["text"], "character": ln["character"]})
            print(f"     {ln['character']:6s} 分镜内{ln['local_start']:.1f}s → "
                  f"成片 {ln['start']:.2f}s  「{ln['text']}」")

    print("\n" + "═" * 76)
    print("⑦ 一次性编码整条音轨 + 生成字幕")
    voice = os.path.join(WORK, "voice.m4a")
    tr = assemble_track(placements, tl["total"], voice)
    print(f"   音轨 {tr['duration']}s  {'✅' if tr['ok'] else '❌ '+str(tr.get('error'))}")
    srt = build_srt(subs, os.path.join(WORK, "sub.srt"))
    print("   字幕:")
    print("     " + open(srt, encoding="utf-8").read().strip().replace("\n", "\n     "))

    print("\n" + "═" * 76)
    print("⑧ 合轨：BGM 避让人声 + 响度归一化 + 输出侧 -t 钳制")
    bgm = os.path.join(WORK, "bgm.mp3")
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=220:duration=20", "-c:a", "libmp3lame", bgm],
                   capture_output=True)
    final = os.path.join(WORK, "final.mp4")
    mr = await mux_film(pic, voice, final, bgm=bgm, bgm_volume=0.18,
                        duck=True, loudness_norm=True, total_duration=tl["total"])
    fd, fa = probe(final)
    print(f"   ✅ {final}")
    print(f"   时长 {fd}s（期望 {tl['total']}s）音轨={fa} ducking={mr.get('ducked')} "
          f"响度归一化={mr.get('normalized')}")
    for w in mr.get("warnings") or []:
        print("   ⚠", w)

    print("\n" + "═" * 76)
    print("⑨ 关键验收：每句台词是否落在**成片**里该出现的时刻")
    ok = True
    for i, s in enumerate(subs):
        db = window_db(final, s["start"], min(1.2, s["end"] - s["start"]))
        hit = db > -45
        ok = ok and hit
        print(f"   {'✅' if hit else '❌'} {s['start']:5.2f}s [{s['character']}] "
              f"「{s['text'][:14]}」 {db:.1f} dB")
    # 反向：没有任何台词的区间应该只有 BGM（很轻），不该有人声量级
    quiet_t = [0.2, 4.5]
    for t in quiet_t:
        db = window_db(final, t, 0.6)
        print(f"   ·  {t:.1f}s（无台词，只应有 BGM）{db:.1f} dB")
    print()
    print("   " + ("✅ 所有台词都落在正确的时间点上" if ok else "❌ 有台词没落对"))
    print(f"   产物目录: {WORK}")


asyncio.run(main())
