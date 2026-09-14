# -*- coding: utf-8 -*-
r"""端到端回归：**"剧本有对话的第一镜"现在到底会不会出声**（零 API 成本）。

用户实测反馈（2026-09-14）：
    「一键配音在我软件上还是没能体现，我点击后还是单独的那个 AI 女音，
      **且第一个镜头没有声音（剧本是有对话的）**」

这条链路以前在两个地方断掉：
  ① 分镜 `layer2_timeline` 里**没有 dialogue 字段**（AI 写分镜时漏了）→ 没台词可念；
  ② 从剧本回填这件事根本不存在 → 界面只淡淡写一句"这一镜没有台词"。

本套件用**真实项目形状的数据**跑完 `voicecast.dub_shot` + `compose._mux_clip_audio`
整条链，但把**厂商合成**换成本地桩（ffmpeg 生成一小段音频）——
所以它验证的是"流水线会不会出声、每个角色是不是各一把嗓子"，
**不碰任何付费接口**。厂商真实发声由用户点一下就能听到。

锁死：
  · E1 分镜漏写台词时能从剧本回填（且**只填对得上的**）；
  · E2 不同角色分到**不同**音色（这是"全片一把 AI 女音"的直接反例）；
  · E3 每一句 placement 都用的是**该角色自己的**音色；
  · E4 生成的音轨**真的有声音**（不是空文件 / 不是静音轨）；
  · E5 音轨能**真的挂进这一镜**（输出 mp4 同时有视频流和音频流）；
  · E6 剧本台词跟画面对不上时**宁可不出声**，并如实说明原因。
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from imageio_ffmpeg import get_ffmpeg_exe                        # noqa: E402

from core import voicecast                                       # noqa: E402
from core.compose import _mux_clip_audio                         # noqa: E402

PASS, FAIL = [], []
FFMPEG = get_ffmpeg_exe()


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def run(args):
    return subprocess.run([FFMPEG, "-y", "-hide_banner", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)


def make_silent_clip(path, dur=12.0, size="640x360"):
    """造一个**没有音轨**的视频片段 —— 和视频模型返回的 mp4 一样。"""
    r = run(["-f", "lavfi", "-i", f"color=c=gray:s={size}:d={dur}:r=24",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", path])
    return os.path.exists(path) and os.path.getsize(path) > 0


def streams(path):
    """用 ffmpeg 探测文件里有哪些流（返回 stderr 文本）。"""
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    return (p.stderr or "") + (p.stdout or "")


def audio_is_loud(path):
    """用 volumedetect 量一下：音轨是不是**真的有声音**（不是一整段静音）。"""
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-af", "volumedetect",
                        "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    txt = (p.stderr or "")
    mean = None
    for ln in txt.splitlines():
        if "mean_volume:" in ln:
            try:
                mean = float(ln.split("mean_volume:")[1].strip().split()[0])
            except Exception:
                pass
    return mean


# ──────────── 真实项目形状的数据 ────────────

SCRIPT_SOURCES = [
    {"source": "剧本场景", "scene_number": 1, "title": "废墟中的铁王座",
     "location": "君临城 - 铁王座大厅",
     "characters": ["布兰·史塔克", "无面者刺客"],
     "dialogues": [
         {"character": "布兰", "text": "铁王座属于过去，新的时代即将到来。", "emotion": "calm"},
         {"character": "无面者", "text": "谁来统治这片土地？", "emotion": "curious"},
     ]},
]

# 分镜：时间轴**没有** dialogue 字段（这就是"第一镜没声音"的根因）
SHOT = {
    "id": "0145b6c3-6518-4538-b63b-182f4d783416",
    "order_index": 0,
    "duration_seconds": 12,
    "layer1_overview": "君临城铁王座大厅内，一片破败景象。布兰·史塔克坐在轮椅上，缓缓进入大厅。"
                       "无面者刺客站在铁王座旁，眼神警惕而好奇。",
    "layer2_timeline": [
        {"start": 0, "end": 4,
         "action": "布兰坐在轮椅上，缓缓进入铁王座大厅，镜头从背后跟随。",
         "expression": "沉静", "camera": "从背后缓慢跟随"},
        {"start": 4, "end": 8,
         "action": "无面者刺客站在铁王座旁，目光锐利地注视着布兰。",
         "expression": "警惕", "camera": "侧面镜头，缓慢推近"},
        {"start": 8, "end": 12,
         "action": "镜头环绕铁王座，展示厅堂的残破景象。",
         "expression": "坚定", "camera": "环绕镜头"},
    ],
    "layer3_constraints": {"must_appear": ["铁王座", "布兰的轮椅", "无面者"]},
}

CHARACTERS = [
    {"id": "c1", "name": "布兰·史塔克", "age": "少年",
     "reference_features": json.dumps({"gender": "male", "personality": "沉静、克制"})},
    {"id": "c2", "name": "无面者刺客", "age": "成熟",
     "reference_features": json.dumps({"gender": "male", "personality": "阴冷、低沉"})},
]

# 一个小的"真实音色目录"形状（性别/年龄/标签都齐）
CATALOG = [
    {"voice_id": "edge:zh-CN-YunxiNeural", "gender": "male", "label": "云希 少年男声",
     "provider": "edge", "verified": True, "age_band": "young", "age_inferred": False,
     "tags": ["阳光"]},
    {"voice_id": "edge:zh-CN-YunjianNeural", "gender": "male", "label": "云健 成熟男声",
     "provider": "edge", "verified": True, "age_band": "mature", "age_inferred": False,
     "tags": ["沉稳"]},
    {"voice_id": "siliconflow:CosyVoice2:benjamin", "gender": "male",
     "label": "benjamin 低沉男声", "provider": "siliconflow", "verified": False,
     "age_band": "mature", "age_inferred": False, "tags": ["低沉"]},
    {"voice_id": "edge:zh-CN-XiaoxiaoNeural", "gender": "female", "label": "晓晓 女声",
     "provider": "edge", "verified": True, "age_band": "young", "age_inferred": False,
     "tags": []},
]

CALLS = []


def stub_synthesize(monkeypatch_dir):
    """把厂商合成换成**本地 ffmpeg 桩**：真出音频、不花钱。"""
    async def _fake(text, voice_id, settings, out_path, **kw):
        CALLS.append({"text": text, "voice": voice_id, "kw": kw})
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        # 用一个**有声音**的音（440Hz），好让 volumedetect 量得出来
        run(["-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
             "-c:a", "libmp3lame", "-b:a", "128k", out_path])
        return {"ok": True, "path": out_path, "duration": 1.2,
                "placed_duration": 1.2, "timelock": 1.0, "speed": 1.0,
                "overflow": 0.0, "prosody": kw.get("prosody") or {}}
    return _fake


def main():
    print("=" * 70)
    print("端到端：剧本有对话的第一镜 → 出声 + 多角色不同音色（零 API 成本）")
    print("=" * 70)

    orig = voicecast.synthesize_line
    voicecast.synthesize_line = stub_synthesize(None)

    settings = {"default_voice": "edge:zh-CN-XiaoxiaoNeural",
                "tts_api_keys": {"edge": "", "siliconflow": "x"}}
    dv = "edge:zh-CN-XiaoxiaoNeural"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            clip = os.path.join(tmp, "shot.mp4")
            check("E0.1 造出一个**没有音轨**的片段（模拟视频模型产物）",
                  make_silent_clip(clip, 12.0), clip)
            check("E0.2 这个片段确实没有音频流（否则后面的验证不算数）",
                  "Audio:" not in streams(clip), "")

            plan = voicecast.cast_plan(CHARACTERS, dv, catalog=CATALOG)
            cast = plan.get("voices") or {}
            # ── E1 / E2 ──
            CALLS.clear()
            dub = asyncio.run(voicecast.dub_shot(
                SHOT, characters=CHARACTERS, settings=settings,
                out_dir=os.path.join(tmp, "voice"), default_voice=dv,
                duration=12.0, cast=cast, catalog=CATALOG,
                script_sources=SCRIPT_SOURCES,
                scene_name="君临城 - 铁王座大厅", shot_order=0,
                character_names=["布兰·史塔克", "无面者刺客"]))

            check("E1.1 分镜漏写台词 → 配音这次**成功**了", dub.get("ok"), str(dub.get("error")))
            rec = dub.get("recovered") or {}
            check("E1.2 如实标出从剧本回填了几句", rec.get("added") == 2, str(rec))
            check("E1.3 回填来源是**画面一致的那一版**剧本",
                  rec.get("from") == "剧本场景", str(rec.get("from")))
            check("E1.4 这一镜现在有 2 句台词", dub.get("line_count") == 2,
                  str(dub.get("line_count")))

            used = dub.get("cast") or {}
            check("E2.1 两个角色**各自**有一把嗓子",
                  len(used) == 2 and all(used.values()), str(used))
            check("E2.2 两把嗓子不是同一把（全片一把 AI 女音的反例）",
                  len(set(used.values())) == len(used), str(used))
            check("E2.3 两个角色都没落到默认女声上",
                  all(v != dv for v in used.values()), f"{used} / 默认 {dv}")

            # ── E3 逐句用的是角色自己的音色 ──
            pl = dub.get("placements") or []
            check("E3.1 两句都排上了轨", len(pl) == 2, str(len(pl)))
            by_text = {p["text"]: p for p in pl}
            b_voice = (by_text.get("铁王座属于过去，新的时代即将到来。") or {}).get("voice_id")
            w_voice = (by_text.get("谁来统治这片土地？") or {}).get("voice_id")
            # ★ 键名会被**规范化成角色卡里的全名**（剧本写"布兰"、卡里是"布兰·史塔克"）：
            #   这正是修掉"简称查不到选角表 → 又给同一角色挑一把嗓子"的那一步。
            check("E3.2 布兰那句用的是布兰的音色",
                  b_voice and b_voice == used.get("布兰·史塔克"),
                  f"{b_voice} vs {used.get('布兰·史塔克')}")
            check("E3.3 无面者那句用的是无面者的音色",
                  w_voice and w_voice == used.get("无面者刺客"),
                  f"{w_voice} vs {used.get('无面者刺客')}")
            check("E3.5 说话人已归一化到角色卡里的名字",
                  set(used.keys()) == {"布兰·史塔克", "无面者刺客"}, str(list(used.keys())))
            check("E3.4 桩收到的每次调用都带了韵律（情绪送到引擎那条链）",
                  all("prosody" in c["kw"] for c in CALLS) and len(CALLS) == 2,
                  str([c["voice"] for c in CALLS]))
            check("E3.6 没有一句台词被重复念（回填只补缺、不重念）",
                  len({p["text"] for p in pl}) == len(pl),
                  str([p["text"][:10] for p in pl]))

            # ── E4 音轨真的有声音 ──
            track = dub.get("path") or ""
            check("E4.1 音轨文件生成了", bool(track) and os.path.exists(track), track)
            check("E4.2 音轨里有音频流", "Audio:" in streams(track))
            mean_db = audio_is_loud(track)
            check("E4.3 音轨**不是静音**（量到真实电平）",
                  mean_db is not None and mean_db > -50,
                  f"mean_volume={mean_db} dB")

            # ── E5 真的挂进这一镜 ──
            out = os.path.join(tmp, "shot_voiced.mp4")
            muxed = asyncio.run(_mux_clip_audio(clip, track, out, 12.0))
            check("E5.1 挂音轨成功", bool(muxed), str(muxed))
            st = streams(out)
            check("E5.2 成片**同时**有视频流和音频流（预览不再是静音）",
                  "Video:" in st and "Audio:" in st,
                  " / ".join([l.strip() for l in st.splitlines()
                              if "Stream #" in l])[:120])
            out_mean = audio_is_loud(out)
            check("E5.3 挂完以后**声音还在**（不是只剩一条空轨）",
                  out_mean is not None and out_mean > -50, f"mean_volume={out_mean} dB")

            # ── E6 对不上就宁可不出声 ──
            wrong = [{"source": "剧本细纲", "scene_number": 1, "title": "铁王座前的对峙",
                      "location": "君临城，铁王座大厅",
                      "characters": ["琼恩·雪诺", "瑟曦·兰尼斯特"],
                      "dialogues": [{"character": "琼恩", "text": "这一切该结束了，瑟曦。",
                                     "emotion": "determined"}]}]
            d2 = asyncio.run(voicecast.dub_shot(
                SHOT, characters=CHARACTERS, settings=settings,
                out_dir=os.path.join(tmp, "voice2"), default_voice=dv,
                duration=12.0, cast=cast, catalog=CATALOG,
                script_sources=wrong, scene_name="君临城 - 铁王座大厅",
                shot_order=0, character_names=["布兰·史塔克", "无面者刺客"]))
            check("E6.1 剧本跟画面不一致 → **不硬塞**，这一镜不出声",
                  (not d2.get("ok")), str(d2.get("error"))[:80])
            check("E6.2 原因里写清了没敢填、以及是谁的台词",
                  ("不硬塞" in str(d2.get("error")))
                  or ("琼恩" in str(d2.get("error"))), str(d2.get("error"))[:160])
    finally:
        voicecast.synthesize_line = orig

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
