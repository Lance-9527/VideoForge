# -*- coding: utf-8 -*-
"""
VideoForge · 音效垫底（环境声床 + 音效床）
==========================================

用户原话（2026-10-23）：
    ①「音色太烂了！你可以用我中转站里面的最强的语音模型配啊 / 人物感情不对 / 我这个是产品宣传片」
    ②「你只需要在现在视频（去人物声音）基础上重新附上你重新做好的音频」
    ③「再就是全程最好除了台词部分，设计音效，不冷场」

这个模块解决 ③：**整条片子除了台词之外，还得有环境声/气氛声垫着**。
不铺底的成片，专业听感上就是"干声对着灰底"，一停台词就死寂 —— 用户说的"冷场"。

## 为什么不调 API

音效/音乐生成类接口（Suno、MusicGen、MiniMax music）在这台机器上
**一个都没配 Key**，而且按秒/按次收费。用户只说了"别冷场"，
不是要一首原创配乐。所以这里走**本地程序化合成**：

    · 底噪：ffmpeg `anoisesrc`（白/粉/棕噪）—— 三种噪声的频谱不同，
      正好对应"空气声 / 室内 / 街道低频"三种听感；
    · 律动：`tremolo` 做慢速起伏（风声、人流、空调），频率 0.1~2.5Hz；
    · 空间：`aecho` 给走廊/楼道一点混响尾巴；
    · 电压：`sine` + `lowpass` 做 50~70Hz 的室内嗡鸣（机房/空调/变压器）。

**零费用、零依赖、离线可用**，渲染一条 30 秒的床约 0.3 秒。

## 诚实边界

· 这是**气氛垫**，不是"能听出是什么"的拟音（脚步、开门、水滴这些要真素材）。
  想要真实音效请走 `/api/projects/{pid}/outputs/mix-bgm` 上传自己的音频，
  或者把素材放到 `data/sfx/` 下由本模块自动按场次铺（见 `_user_sfx_files`）。
· 默认音量压得很低（-18dB 左右）+ 人声侧链避让，**保证台词清楚**。
  它存在的意义是"没有死寂"，不是"听出配乐"。

## 配方怎么选

按**场次类型/关键词**选：室内 / 走廊 / 街道 / 野外 / 雨 / 夜 / 楼顶。
项目里多个场次 → 按镜头时长加权，挑占比最大的那一种（一条片子一种底噪更稳，
频繁换底噪反而假）。用户可以在场次文字里写"雨"来强制要雨声。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 一种底噪 = 几层 lavfi 源 + 一层滤镜链
#   keys: noise(白/粉/棕) / hp / lp(Hz) / trem(Hz,depth) / hum(Hz,vol) / echo / vol
RECIPES: Dict[str, Dict[str, Any]] = {
    "room":     dict(noise="pink",   lp=900,  trem=(0.09, 0.30), hum=(50, 0.020), vol=0.55),
    "corridor": dict(noise="pink",   lp=700,  trem=(0.07, 0.25), echo=(0.7, 0.5, "80|160", "0.30|0.20"), vol=0.50),
    "street":   dict(noise="brown",  hp=80, lp=1400, trem=(0.17, 0.35), hum=(70, 0.030), vol=0.55),
    "field":    dict(noise="pink",   hp=220, lp=2000, trem=(0.11, 0.75), vol=0.55),
    "rain":     dict(noise="white",  hp=1200, lp=6000, trem=(2.50, 0.35), vol=0.42),
    "night":    dict(noise="brown",  hp=60, lp=520,  trem=(0.06, 0.30), hum=(50, 0.025), vol=0.55),
    "roof":     dict(noise="brown",  hp=100, lp=800, trem=(0.15, 0.55), vol=0.52),
    "studio":   dict(noise="pink",   lp=1200, trem=(0.05, 0.15), vol=0.40),
}

#: 关键词 → 配方（先匹配先赢；顺序有意义：雨 > 夜 > 街 > 野 > 走廊 > 室内）
KEYWORDS: List[Tuple[Tuple[str, ...], str]] = [
    (("雨", "暴雨", "小雨", "rain", "淋雨", "wet"), "rain"),
    (("楼顶", "天台", "roof", " rooftop"), "roof"),
    (("走廊", "楼道", "隧道", "电梯", "corridor", "hallway"), "corridor"),
    (("街", "马路", "集市", "广场", "market", "street", "road"), "street"),
    (("野", "田", "雪", "山", "林", "河", "field", "snow", "forest", "outdoor", "mountain"), "field"),
    (("夜", "深夜", "凌晨", "night"), "night"),
    (("棚", "影棚", "发布会", "产品", "studio", "stage", "展台"), "studio"),
]

DEFAULT_KIND = "room"


def _ffmpeg() -> str:
    try:
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess
        return get_ffmpeg_for_postprocess()
    except Exception:  # noqa: BLE001
        try:
            from core.proc import ffmpeg_path  # type: ignore
            return ffmpeg_path()
        except Exception:  # noqa: BLE001
            return "ffmpeg"


def kind_from_text(text: str) -> Optional[str]:
    """从一段中文/英文描述里猜场景类型（找不到返回 None）。"""
    t = (text or "").lower()
    if not t:
        return None
    for words, kind in KEYWORDS:
        for w in words:
            if w and w.strip() in t:
                return kind
    return None


def pick_kind(texts: List[str]) -> Tuple[str, Dict[str, int]]:
    """按出现次数挑主配方。返回 (kind, 计数表)。"""
    tally: Dict[str, int] = {}
    for t in texts:
        k = kind_from_text(t)
        if k:
            tally[k] = tally.get(k, 0) + 1
    if not tally:
        return DEFAULT_KIND, {}
    # 并列时按 KEYWORDS 的优先级（rain/roof/corridor/street/field/night/studio）
    order = [k for _w, k in KEYWORDS]
    best = max(tally.items(), key=lambda kv: (kv[1], -order.index(kv[0]) if kv[0] in order else -99))
    return best[0], tally


def _build_filter(kind: str) -> str:
    """底噪这一路的滤镜链（频谱整形 → 律动 → 空间 → 电平）。"""
    r = RECIPES.get(kind) or RECIPES[DEFAULT_KIND]
    chain = []
    if r.get("hp"):
        chain.append("highpass=f=%d" % int(r["hp"]))
    if r.get("lp"):
        chain.append("lowpass=f=%d" % int(r["lp"]))
    if r.get("trem"):
        f, d = r["trem"]
        chain.append("tremolo=f=%.2f:d=%.2f" % (max(0.1, float(f)), float(d)))
    if r.get("echo"):
        i, o, dl, dec = r["echo"]
        chain.append("aecho=%.2f:%.2f:%s:%s" % (i, o, dl, dec))
    chain.append("volume=%.3f" % float(r.get("vol", 0.5)))
    return ",".join(chain)


async def _run(cmd: List[str], timeout: float = 180.0) -> Tuple[int, str]:
    def _call():
        pr = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return pr.returncode, (pr.stdout or "") + (pr.stderr or "")
    return await asyncio.get_event_loop().run_in_executor(None, _call)


async def build_bed(kind: str, seconds: float, out_path: str, *,
                    intensity: float = 1.0) -> Dict[str, Any]:
    """程序化生成一条环境声床（m4a）。返回 {ok, path, kind, seconds, reason}。"""
    kind = kind if kind in RECIPES else DEFAULT_KIND
    seconds = max(2.0, min(float(seconds or 0) or 30.0, 600.0))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    r = RECIPES[kind]
    seed = int(hashlib.sha1(kind.encode()).hexdigest()[:6], 16) % 99991
    ff = _ffmpeg()

    ins: List[str] = []
    parts: List[str] = []
    # ① 底噪
    ins += ["-f", "lavfi", "-i",
            "anoisesrc=color=%s:duration=%.3f:amplitude=0.6:seed=%d"
            % (r.get("noise", "pink"), seconds, seed)]
    parts.append("[0:a]" + _build_filter(kind) + "[bed]")
    labels = ["[bed]"]
    # ② 室内嗡鸣（可选）：50/70Hz 低频
    if r.get("hum"):
        hz, v = r["hum"]
        ins += ["-f", "lavfi", "-i", "sine=frequency=%d:duration=%.3f" % (int(hz), seconds)]
        parts.append("[1:a]lowpass=f=140,volume=%.3f[hum]" % (float(v) * 8.0))
        labels.append("[hum]")
    # ③ 合成 → 立体声 → 轻微响度统一
    src = "".join(labels)
    if len(labels) > 1:
        parts.append("%samix=inputs=%d:duration=first:normalize=0[raw]" % (src, len(labels)))
    else:
        parts.append("%sanull[raw]" % src)
    parts.append("[raw]aresample=48000,aformat=channel_layouts=stereo,"
                 "volume=%.3f,"
                 # ★ 把床本身统一到 -30 LUFS：这样"混的时候按 1.0 直接叠"就是
                 #   比台词（成片 -16 LUFS）低 14dB 的垫底声 —— 有存在感但不抢话。
                 #   不统一电平的话，不同配方（雨比室内响 10dB）会导致
                 #   「有的片子音效听得见、有的完全听不见」。
                 "loudnorm=I=-30:TP=-8:LRA=7,"
                 "afade=t=in:st=0:d=1.2,"
                 "afade=t=out:st=%.2f:d=2.0[out]"
                 % (max(0.05, float(intensity)), max(0.0, seconds - 2.2)))

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"] + ins + [
        "-filter_complex", ";".join(parts), "-map", "[out]",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-t", "%.3f" % seconds, out_path,
    ]
    try:
        rc, err = await _run(cmd)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": kind, "reason": "%s: %s" % (type(e).__name__, e)}
    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 512:
        return {"ok": False, "kind": kind,
                "reason": ("ffmpeg 合成底噪失败：" + (err or "")[-300:])}
    return {"ok": True, "path": out_path, "kind": kind, "seconds": seconds,
            "recipe": {k: v for k, v in r.items() if k != "echo"}}


def _user_sfx_files(settings: Dict[str, Any], kind: str) -> List[str]:
    """用户自己放进 `data/sfx/<kind>/` 的素材（有就优先用真实素材）。

    这是"程序化合成"的逃生门：想要真实雨声/街道声，把 wav/mp3 丢进去即可，
    不需要改代码、不需要 API Key。
    """
    try:
        root = str((settings or {}).get("data_dir") or "").strip()
        if not root:
            root = os.path.join(os.environ.get("VIDEOFORGE_DATA_DIR") or ".", "")
        base = os.path.join(root, "sfx", kind)
        if not os.path.isdir(base):
            return []
        out = []
        for n in sorted(os.listdir(base)):
            if os.path.splitext(n)[1].lower() in (".wav", ".mp3", ".m4a", ".ogg", ".flac"):
                out.append(os.path.join(base, n))
        return out
    except Exception:  # noqa: BLE001
        return []


async def bed_for_project(db, project_id: str, seconds: float, out_dir: str, *,
                          settings: Optional[Dict[str, Any]] = None,
                          intensity: float = 1.0) -> Dict[str, Any]:
    """给一个项目挑配方 + 生成底噪床。失败**不抛**（返回 ok=False，调用方跳过）。"""
    settings = settings or {}
    texts: List[str] = []
    try:
        for s in (db.list_shots(project_id) or []):
            for k in ("layer1_overview", "previz_notes", "camera"):
                v = s.get(k)
                if isinstance(v, str) and v:
                    texts.append(v)
            tl = s.get("layer2_timeline")
            if tl:
                texts.append(str(tl))
        for sc in (db.list_scenes(project_id) or []):
            for k in ("name", "description", "location"):
                v = sc.get(k)
                if isinstance(v, str) and v:
                    texts.append(v)
    except Exception as e:  # noqa: BLE001
        logger.warning("读项目场次失败（音效床将用默认配方）：%s", e)

    kind, tally = pick_kind(texts)
    user_files = _user_sfx_files(settings, kind)
    if user_files:
        # 用用户素材：按顺序拼到够长（够长就行，混流时还会 stream_loop）
        target = max(seconds, 8.0)
        listfile = os.path.join(out_dir, "sfx_list.txt")
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(listfile, "w", encoding="utf-8") as f:
                for p in user_files:
                    f.write("file '%s'\n" % p.replace("\\", "/"))
            ff = _ffmpeg()
            outp = os.path.join(out_dir, "ambience.m4a")
            cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "concat", "-safe", "0", "-i", listfile,
                   "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                   "-t", "%.3f" % target, outp]
            rc, err = await _run(cmd)
            if rc == 0 and os.path.exists(outp):
                return {"ok": True, "path": outp, "kind": kind, "source": "user",
                        "files": len(user_files), "tally": tally}
        except Exception as e:  # noqa: BLE001
            logger.warning("用户音效素材拼接失败，回退程序化合成：%s", e)

    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "ambience.m4a")
    r = await build_bed(kind, max(8.0, min(float(seconds or 30.0) + 2.0, 600.0)), out,
                        intensity=intensity)
    r["source"] = "synth"
    r["tally"] = tally
    return r
