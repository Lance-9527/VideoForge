"""成片**声音**链路的回归测试。

═══════════════════════════════════════════════════════════════════
真实事故（2026-09-13）
═══════════════════════════════════════════════════════════════════
用户要"检验成片"，我一探才发现成片 **只有一条视频流、根本没有音轨**：

  · `voicecast.dub_shot`（按角色逐句配音）**写好了却从来没有被任何地方调用**；
  · 走"已生成视频"的分镜在合成时 `ci.audio=""`，那一镜注定静音；
  · 更隐蔽的是架构层：`assemble.normalize_picture` **故意 `-an` 丢掉音轨**
    （"音频最后一次性挂上"），而 `compose` **从来没建过那条轨** ——
    两次 `mux_film(...)` 传的都是 `audio=""`；
  · 而 `mux_film` 在"既没配音也没 BGM"时还会用 `-an` **把画面自带的音轨也丢一次**。

于是"每一环看起来都对"，成片却是哑的。这个测试盯的就是这几处。

用法：python tests/test_film_audio.py
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), "backend")
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL: list = []


def ck(cond: bool, msg: str, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {msg}" + (f" — {detail}" if detail else ""))
    else:
        FAIL.append(msg)
        print(f"  ❌ {msg}" + (f" — {detail}" if detail else ""))


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def make_video_with_tone(path: str, seconds: float = 2.0) -> bool:
    """造一段**带真实声音**的视频（800Hz 正弦）。"""
    p = subprocess.run(
        [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=24:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=800:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-shortest", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    return os.path.exists(path) and os.path.getsize(path) > 1024


def streams(path: str) -> dict:
    p = subprocess.run([ffmpeg(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    err = p.stderr or ""
    vid = re.search(r"Video:.*?,\s*(\d+)x(\d+)", err)
    aud = re.search(r"Audio:\s*([a-z0-9]+).*?(\d+) Hz,\s*([a-z]+)", err)
    return {"video": bool(vid), "audio": bool(aud),
            "acodec": aud.group(1) if aud else "",
            "hz": int(aud.group(2)) if aud else 0}


def mean_volume(path: str) -> float:
    """返回 mean_volume(dB)。**-91 就是数字静音**。"""
    p = subprocess.run([ffmpeg(), "-hide_banner", "-i", path,
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", p.stderr or "")
    return float(m.group(1)) if m else -91.0


def read_src(*parts: str) -> str:
    with io.open(os.path.join(os.path.dirname(HERE), *parts), encoding="utf-8") as f:
        return f.read()


def main() -> None:
    import asyncio

    print("═" * 74)
    print("① 死代码回归：`dub_shot` 必须真的被 compose 调用")
    print("═" * 74)
    compose_src = read_src("backend", "core", "compose.py")
    ck("dub_shot" in compose_src,
       "compose 里出现了 dub_shot（不再是从没被调用的死代码）",
       "这一条挡的是『功能写了却没接线』")
    ck("voice_track" in compose_src,
       "compose 会按绝对时间混出一条配音轨（voice_track）")
    ck(re.search(r"mux_film\(\s*current\s*,\s*voice_track", compose_src) is not None
       or "voice_track, normd" in compose_src or "voice_track, mixed" in compose_src,
       "mux_film 收到的是 voice_track（不再是空字符串）")

    print()
    print("═" * 74)
    print("② mux_film：没有独立配音文件时，**必须保留画面自带的音轨**")
    print("═" * 74)
    tmp = tempfile.mkdtemp(prefix="vf_filmaudio_")
    src = os.path.join(tmp, "tone.mp4")
    if not make_video_with_tone(src):
        ck(False, "造测试视频失败", "ffmpeg 不可用？")
    else:
        from core.assemble import mux_film
        out = os.path.join(tmp, "norm.mp4")
        r = asyncio.run(mux_film(src, "", out, bgm="", duck=False,
                                 loudness_norm=True, total_duration=2.0))
        st = streams(out)
        ck(bool(r.get("ok")) and os.path.exists(out), "mux_film 跑通并产出文件")
        ck(st["audio"], "输出**仍有音轨**（旧代码这一步用 -an 把音轨丢了）",
           f"codec={st['acodec']} {st['hz']}Hz")
        ck(st["video"], "画面流也在（没有为了让音频通过而丢画面）")
        mv = mean_volume(out)
        ck(mv > -60, "音轨不是数字静音", f"mean_volume={mv} dB")

    print()
    print("═" * 74)
    print("③ 配音轨：按**绝对时间**摆放（这是「声画同步」的根据）")
    print("═" * 74)
    from core.voicecast import assemble_track
    line = os.path.join(tmp, "line.m4a")
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=600:duration=0.5",
                    "-c:a", "aac", line], capture_output=True, timeout=180)
    if os.path.exists(line):
        tr = assemble_track([{"path": line, "start": 1.0, "span": 0.5,
                              "speed": 1.0, "text": "测试", "character": "甲"}], 3.0,
                            os.path.join(tmp, "track.m4a"))
        ck(bool(tr.get("ok")), "assemble_track 跑通", f"时长={tr.get('duration')}")
        if tr.get("ok"):
            # 只测前 0.8s（应该没有声音）与 1.0~1.5s（应该有声音）
            head = os.path.join(tmp, "head.m4a")
            subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                            "-i", tr["path"], "-t", "0.8", head],
                           capture_output=True, timeout=180)
            body = os.path.join(tmp, "body.m4a")
            subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", "1.0", "-t", "0.4", "-i", tr["path"], body],
                           capture_output=True, timeout=180)
            ck(mean_volume(head) < mean_volume(body) - 20,
               "起点 1.0s 的音频**确实落在 1.0s**（前半段静、后半段响）",
               f"前 {mean_volume(head)} dB < 后 {mean_volume(body)} dB")

    print()
    print("═" * 74)
    print("④ 角色音色：嵌套对象要拍平，不同角色要拿到不同嗓子")
    print("═" * 74)
    from core.voicecast import voice_of_character, cast_voice, gender_of_character
    nested = {"name": "珊莎", "reference_features": json.dumps(
        {"voice_id": {"voice_id": "edge:zh-CN-XiaoxiaoNeural",
                      "voice_name": "晓晓", "provider": "edge"}})}
    ck(voice_of_character(nested) == "edge:zh-CN-XiaoxiaoNeural",
       "嵌套的音色对象被拍平成 id（旧代码 str(dict) 会让 TTS 报"
       "『未注册的 TTS provider：{voice_id』）",
       voice_of_character(nested))
    plain = {"name": "珊莎", "reference_features": json.dumps(
        {"voice_id": "edge:zh-CN-XiaoxiaoNeural"})}
    ck(voice_of_character(plain) == "edge:zh-CN-XiaoxiaoNeural",
       "字符串形式的音色照旧可用")
    male = {"name": "琼恩", "gender": "male", "reference_features": "{}"}
    female = {"name": "艾莉亚", "gender": "female", "reference_features": "{}"}
    ck(gender_of_character(male) == "male" and gender_of_character(female) == "female",
       "性别字段被正确归一")
    v_m = cast_voice("琼恩", [male, female], "edge:zh-CN-XiaoxiaoNeural")
    v_f = cast_voice("艾莉亚", [male, female], "edge:zh-CN-XiaoxiaoNeural")
    ck(v_m != v_f, "男女角色拿到**不同**音色", f"{v_m} vs {v_f}")
    ck("Yun" in v_m or "yun" in v_m, "男角色落到男声池", v_m)
    v_m2 = cast_voice("另一个男角", [male, female,
                                {"name": "另一个男角", "gender": "男",
                                 "reference_features": "{}"}],
                      "edge:zh-CN-XiaoxiaoNeural", used={v_m})
    ck(v_m2 != v_m, "同一镜内第二个男角色不会撞音", f"{v_m} → {v_m2}")
    ck("Yun" in v_m2 or "yun" in v_m2,
       "第二个男角色仍然落在男声池（不会因为避撞就塞一个女声）", v_m2)

    print()
    print("═" * 74)
    print("⑤ 配音轨的 skip：画面裁掉 h 秒死帧时，声音必须**同量前移**")
    print("═" * 74)
    # 造一段"前 0.5s 静音 + 后 1.0s 有声音"的素材：
    # 带 skip 摆放时开头应该是**有声音**的，不带 skip 时开头是静的。
    src2 = os.path.join(tmp, "head_silence.m4a")
    subprocess.run(
        [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000:d=0.5",
         "-f", "lavfi", "-i", "sine=frequency=700:duration=1.0",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[a]",
         "-c:a", "aac", src2], capture_output=True, timeout=180)
    if os.path.exists(src2):
        from core.voicecast import assemble_track
        t_no = assemble_track([{"path": src2, "start": 0.0, "span": 1.5, "speed": 1.0,
                                "text": "", "character": ""}], 1.5,
                              os.path.join(tmp, "t_noskip.m4a"))
        t_sk = assemble_track([{"path": src2, "start": 0.0, "span": 1.5, "speed": 1.0,
                                "skip": 0.5, "text": "", "character": ""}], 1.5,
                              os.path.join(tmp, "t_skip.m4a"))
        ck(bool(t_no.get("ok")) and bool(t_sk.get("ok")), "两条对照音轨都合成成功")

        def head_db(p: str, sec: float = 0.4, tag: str = "x") -> float:
            # ⚠ 输出必须带 ffmpeg 认得的扩展名（`.m4a`）：写成 `p+".head"` 时
            #   它选不出封装器、产物根本不存在，于是"两次都读到 -91dB" ——
            #   测试自己骗了自己（第一次就是这么错的）。
            out = os.path.join(tmp, f"head_{tag}.m4a")
            subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                            "-i", p, "-t", str(sec), "-c:a", "aac", out],
                           capture_output=True, timeout=180)
            return mean_volume(out)

        a_no, a_sk = head_db(t_no["path"], tag="no"), head_db(t_sk["path"], tag="sk")
        ck(a_sk > a_no + 20,
           "带 skip 的版本开头**有声音**、不带的开头是静的（即 skip 真的前移了音频）",
           f"skip 版 {a_sk}dB vs 无 skip 版 {a_no}dB")

    print()
    print("═" * 74)
    print("⑥ 长字幕要拆成可读短句，但**时间锚点不许动**")
    print("═" * 74)
    from core.subtitle import resplit_long_cues
    long_cue = [{"start": 0.1, "end": 5.725,
                 "text": "在寒冬的森林小径中，一位农夫裹紧外套，艰难地行走着。"},
                {"start": 6.2, "end": 7.0, "text": "短句。"}]
    rs = resplit_long_cues(long_cue)
    ck(len(rs) >= 4, "长条被拆成多条（一条糊满整镜是观感回归）", f"{len(rs)} 条")
    ck(abs(rs[0]["start"] - 0.1) < 0.01 and abs(rs[-1]["end"] - 7.0) < 0.01,
       "父条的时间锚点保持不变（句首句尾仍对得上人声）",
       f"首 {rs[0]['start']} / 末 {rs[-1]['end']}")
    ck(all(len(str(x["text"])) <= 20 for x in rs), "每一片都不长",
       str([len(str(x["text"])) for x in rs]))
    ck(any(x["text"] == "短句。" for x in rs), "本来就短的那条**原样保留**")
    times = [(x["start"], x["end"]) for x in rs]
    ck(all(times[i][1] <= times[i + 1][0] + 0.001 for i in range(len(times) - 1)),
       "拆出来的各片时间不重叠", str(times))

    print()
    print("═" * 74)
    print(f"结果: {PASS} 通过 / {len(FAIL)} 失败")
    print("═" * 74)
    for f in FAIL:
        print("   ❌", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
