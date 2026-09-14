# -*- coding: utf-8 -*-
"""
VideoForge · 配音选角与逐句落位（Voice Casting & Timed Dubbing）

═══════════════════════════════════════════════════════════════════
要解决的真实问题
═══════════════════════════════════════════════════════════════════
用户原话：
  "语音+字幕——要么不出现要么特别不协调，要适合角色的语音在对应的剧本
   情景中对应的节点产生"

对照成熟项目 MoneyPrinterTurbo 实测结论（已精读其 voice.py 2837 行）：
  - 它是**一条任务一个音色**，全片从头念到尾 —— 没有"角色→音色"的概念
  - 它**没有**按时间节点插话的能力，只能用 `[pause:2s]` 模拟停顿
  - 非 Edge 的 provider 拿不到词边界，字幕靠"按字数比例分配时长"估算

而我们的分镜数据里**本来就带** `start/end/character`，所以能做到 MPT 做不到的：
  ① 每个角色配自己的音色
  ② 每句台词落在剧本里它该出现的那一刻
  ③ 字幕与语音**同源**，不可能对不上
  ④ BGM 在人说话时自动压低（MPT 完全没有 ducking）

═══════════════════════════════════════════════════════════════════
关键工程细节（从 MPT 踩坑里学到的，必须照做）
═══════════════════════════════════════════════════════════════════
1. **只编码一次**。MPT 的 `_tts_with_pauses` 把每段语音解码成
   24kHz/16bit/mono 裸 PCM，按**累计采样数**算偏移，最后一次性编码。
   注释里写得很清楚：这是为了"彻底解决 MP3 编码器 delay/padding 累积
   导致的音画不同步与字幕漂移"。每段各自编码再拼，误差会累加。
   → 我们照做：逐句合成 → 全部解码成 PCM → 在 numpy 缓冲里**按绝对
     采样点**写入 → 最后统一编码。

2. **不要相信 TTS 返回的时长**。MPT 实测 Edge TTS 尾部固定多约 0.88 秒
   （短脚本能占 19%）。必须用 ffprobe 读**真实文件时长**再决定放多长。

3. **字幕不匹配时不要整体放弃**。MPT 在对不齐时干脆不写字幕文件
   —— 那是它"经常没有字幕"的根因。我们每句都有精确时间戳，
     不依赖匹配，天然不会出现这个问题。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from core.dialogue import CHARS_PER_SEC, timeline_lines

logger = logging.getLogger("videoforge.voicecast")

SAMPLE_RATE = 24000          # 统一 24kHz（与 MPT 一致，够用且体积小）
CHANNELS = 1
SAMPLE_WIDTH = 2             # 16bit

# 一句台词的语速允许范围。超出就说明台词长度和时长片不匹配，
# 硬变速会很难听 —— 这时宁可让它稍微溢出，也不要变成"快进音"。
MIN_SPEED = 0.7
MAX_SPEED = 1.6


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


def _run(cmd: List[str], timeout: float = 600.0) -> Tuple[int, str]:
    try:
        from core.proc import run_sync_full
        rc, out, err = run_sync_full(cmd, timeout=timeout)
        return rc, (out or "") + (err or "")
    except Exception:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")


async def probe_duration(path: str) -> float:
    """读**真实**媒体时长（不要用 TTS 返回值 —— 它常带固定尾巴）。"""
    if not path or not os.path.exists(path):
        return 0.0
    try:
        from core.compose import probe_duration as _pd
        return float(await _pd(path) or 0.0)
    except Exception:
        rc, out = _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out or "")
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _pcm_seconds(path: str) -> float:
    """按**解码后的采样点**量长度，而不是 mp3 头里的 `Duration`。

    mp3 头里的时长含 LAME 的 encoder delay / padding：实测同一个文件
    `probe_duration` 报 2.48s、裸 PCM 只有 2.43s，差 50ms。
    对时变速若按头算，就会**系统性地多拉/少拉约 2%** —— 单句看不出来，
    但它会一句一句累积成声画漂移，所以这里按采样点算。
    """
    raw = _decode_pcm_at_speed(path, 1.0)
    return (len(raw) / 2.0 / SAMPLE_RATE) if raw else 0.0


# ═══════════════════════════════════════════════════════════════
# 一、选角：角色 → 音色
# ═══════════════════════════════════════════════════════════════

# ★★★ 2026-09-13 重大修正：池子里**曾经有 10 个根本不存在的 edge 音色**。
#
# 用 `edge_tts.list_voices()` 核对（当时的权威列表）：
#   免费 Edge 端点当时只提供 **8 个 zh-CN 音色**：
#     male  : Yunxia(少年) Yunxi(青年) Yunjian(成年) Yunyang(成年·播音)
#     female: Xiaoyi(少女) Xiaoxiao(青年) liaoning-Xiaobei(成年) shaanxi-Xiaoni(成年)
#   而旧池子里还写着 Yunfeng/Yunhao/Yunze（男）与
#   Xiaochen/Xiaohan/Xiaomo/Xiaorui/Xiaomeng/Xiaoshuang/Xiaozhen（女）——
#   **这 10 个在端点上已经不存在**，一合成就
#   `NoAudioReceived: No audio was received`（重试 3 次也救不回来）。
#
# 后果很严重，而且是"静默"的：被分到这些音色的角色，**那句台词直接没有声音**，
# 只在警告里留一行字。用户听到的就是"有的角色不说话"——
# 正是"配音要角色来发音"这条需求的反面。
#
# 教训：**音色池是一份"引擎能力清单"，不是愿望清单。**
# 里面每一项都必须能在真实端点上合成出声音；定期用
# `tests/test_voice_pool_online.py` 核对（它会真的去列一次音色）。
# 要加新音色：先 `edge_tts.list_voices()` 确认它存在，再加。
_EDGE_POOL_VERIFIED_ON = "2026-09-13"

# 每条 = (voice_id, 性别, 年龄段, 音色气质, 中文说明)
# 年龄段：child / teen / young / adult / mature / senior，用于和角色卡 age 对齐。
# ⚠ 这 8 个音色**只覆盖 teen/young/adult 三段**：免费端点没有儿童/中年/老年音色。
#   这不是配置问题，是端点的能力边界。`cast_plan` 会把这种"降级替代"如实报出来，
#   而不是假装配上了（见 `_band_gap_note`）。
_MALE_POOL = [
    ("edge:zh-CN-YunxiaNeural",   "male", "teen",   "boyish",  "云夏 · 少年男声"),
    ("edge:zh-CN-YunxiNeural",    "male", "young",  "bright",  "云希 · 阳光青年男声"),
    ("edge:zh-CN-YunjianNeural",  "male", "adult",  "sporty",  "云健 · 有力成年男声"),
    ("edge:zh-CN-YunyangNeural",  "male", "adult",  "news",    "云扬 · 端正播音男声"),
]
_FEMALE_POOL = [
    ("edge:zh-CN-XiaoyiNeural",   "female", "teen",  "lively",  "晓伊 · 活泼少女女声"),
    ("edge:zh-CN-XiaoxiaoNeural", "female", "young", "gentle",  "晓晓 · 温柔青年女声"),
    ("edge:zh-CN-liaoning-XiaobeiNeural", "female", "adult", "dialect", "晓北 · 成年女声（东北口音）"),
    ("edge:zh-CN-shaanxi-XiaoniNeural",   "female", "adult", "dialect", "晓妮 · 成年女声（陕西口音）"),
]

# 池子里**没有**的年龄段（免费端点的能力缺口）。选角降级到相邻段时，
# 必须把这件事告诉用户 —— 否则"老年角色听起来是个中年人"会变成一个谜。
_POOL_MISSING_BANDS = ("child", "mature", "senior")

# 已在真实端点上验证过的 edge 音色集合（用于识别角色卡里的"死音色"）。
_EDGE_KNOWN_VOICES = {v for v, *_ in (_MALE_POOL + _FEMALE_POOL)}


# 年龄段池子里没人时的**替代偏好**（先试哪个相邻段）。
# 为什么要显式写死而不是"取最近的"：`teen` 的左右邻居距离都是 1，
# 而实测它抽到了 `child`（晓梦·儿童女声）—— 一个少女用儿童嗓子比用青年嗓子更出戏。
_NEIGHBOUR_PREF = {
    "child":  ["teen", "young"],
    "teen":   ["young", "child"],
    "young":  ["teen", "adult"],
    "adult":  ["young", "mature"],
    "mature": ["adult", "senior"],
    "senior": ["mature", "adult"],
}

# 年龄描述 → 年龄段（与 prosody._age_profile 同一套分档，保证"音色"和"韵律"不打架）
_AGE_BANDS = (
    ("child",  r"(儿童|幼|孩|童|child|kid)"),
    ("teen",   r"(少年|teen|adolesc|late teens|early teens)"),
    ("young",  r"(青年|young|early 20|mid 20|late 20|20s)"),
    ("adult",  r"(中年|middle|30s|40s|30 岁|40 岁|30岁|40岁)"),
    ("mature", r"(50s|50 岁|50岁|年过半百)"),
    ("senior", r"(老年|老|elder|senior|60s|70s|80s|60 岁|70 岁|60岁|70岁)"),
)


def age_band_of(char: Optional[Dict[str, Any]]) -> str:
    """从角色卡读年龄段。读不到返回空串（表示"不确定"，而不是硬猜一个）。"""
    c = char or {}
    blob = " ".join(str(c.get(k) or "") for k in ("age", "description", "body_type"))
    low = blob.lower()
    m = re.search(r"(\d{1,2})", low)
    n = int(m.group(1)) if m else -1
    if n >= 0:
        if n <= 12:
            return "child"
        if n <= 17:
            return "teen"
        if n <= 29:
            return "young"
        if n <= 49:
            return "adult"
        if n <= 59:
            return "mature"
        return "senior"
    for band, pat in _AGE_BANDS:
        if re.search(pat, low):
            return band
    return ""


def _pool_for(gender: str) -> List[tuple]:
    if gender == "female":
        return list(_FEMALE_POOL)
    if gender == "male":
        return list(_MALE_POOL)
    return list(_MALE_POOL) + list(_FEMALE_POOL)


# 年龄段相邻关系（±1 视为"可接受的替代"）。用于池子里某个年龄段没人时的降级顺序。
_BAND_ORDER = ("child", "teen", "young", "adult", "mature", "senior")


def _band_distance(a: str, b: str) -> int:
    if a not in _BAND_ORDER or b not in _BAND_ORDER:
        return 99
    return abs(_BAND_ORDER.index(a) - _BAND_ORDER.index(b))


def pick_pool_voice(gender: str, seed: str, used: Optional[set] = None,
                    *, age_band: str = "") -> str:
    """按**性别 + 年龄段**从音色池里挑一个，优先避开 `used` 里已有的音色。

    ★ 2026-09-13 重写。旧版只有性别一维，于是「40 岁饱经风霜的农夫」和
      「青年魁梧的战士」会拿到同一把嗓子 —— 用户的原话是"声音要跟人物形象符合"。
    ★ 同时修掉两个一致性 bug：
      ① 旧版把"已用音色"排除后再取模，而 `used` 是**按分镜**传进来的 ——
         同一个角色在不同分镜里会被排到不同的嗓子；
      ② 新版的降级顺序必须**先保不撞音、再保年龄段贴合**：
         实测 `caa9904f` 里三个青年男角色（布兰/琼恩/民众）在同一年龄段
         且池子里只有一把 young 男声时会全部撞到 `YunxiNeural` ——
         "两个角色同一把嗓子"比"年龄略有偏差"更出戏，所以优先解撞音。
      降级顺序：① 年龄段吻合且未用 → ② 相邻年龄段且未用 → ③ 未用（任意段）
                → ④ 年龄段吻合（即便撞音）→ ⑤ 池子里任意一把
    """
    pool = _pool_for(gender)
    used = used or set()
    free = [v for v in pool if v[0] not in used]
    _adj = _NEIGHBOUR_PREF.get(age_band) or []
    # ③ 相邻段也被占完时，在**剩下的**里挑"年龄段最近"的，而不是随手挑一个。
    #    实测（2026-09-13）：4 个男角色的片子里，70 岁的老爷被分到了
    #    「云希·阳光青年男声」—— 因为旧代码的第三档就是"任意空闲"，
    #    完全不看年龄。同一把嗓子撞音固然更糟，但"最老的角色配最年轻的声音"
    #    同样是"跟人物形象不符"。所以按年龄段距离排。
    _nearest = []
    if free and age_band:
        _d = min(_band_distance(v[2], age_band) for v in free)
        _nearest = [v for v in free if _band_distance(v[2], age_band) == _d]
    tiers = [
        [v for v in free if v[2] == age_band],
        # 相邻段按**显式偏好**排（不是"距离最近"——teen 的两边距离都是 1，
        # 实测会抽到儿童女声，见 `_NEIGHBOUR_PREF` 的说明）
        *[[v for v in free if v[2] == _b] for _b in _adj],
        _nearest,
        free,
        [v for v in pool if v[2] == age_band],
        pool,
    ]
    for tier in tiers:
        if tier:
            free = tier
            break
    idx = sum(ord(ch) for ch in str(seed or "")) % max(1, len(free))
    return free[idx][0]


def voice_of_character(char: Optional[Dict[str, Any]]) -> str:
    """从角色卡里读它被分配的音色（存在 reference_features.voice_id）。

    ⚠ 实测坑：`voice_id` 里存的可能是**整个音色对象**而不是字符串
    （`{"voice_id": "edge:zh-CN-XiaoxiaoNeural", "voice_name": "...", "provider": "edge"}`）。
    旧代码 `str(v)` 会把它变成字典字面量，最后 TTS 报
    `未注册的 TTS provider：{'voice_id'` —— 台词整句合成失败、那一镜变静音。
    所以这里要把对象拍平。
    """
    if not char:
        return ""
    rf = char.get("reference_features")
    if isinstance(rf, str):
        try:
            rf = json.loads(rf)
        except Exception:
            rf = {}
    if isinstance(rf, dict):
        v = rf.get("voice_id") or rf.get("voice")
        if isinstance(v, dict):                      # ← 拍平嵌套对象
            inner = v.get("voice_id") or v.get("id") or ""
            prov = str(v.get("provider") or "").strip()
            if inner and prov and ":" not in str(inner):
                return f"{prov}:{inner}".strip()
            v = inner
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        if v and str(v).strip():
            return str(v).strip()
    return ""


def gender_of_character(char: Optional[Dict[str, Any]]) -> str:
    """读角色性别（'男'/'女'/male/female 都归一）。读不到返回空串。"""
    if not char:
        return ""
    g = str(char.get("gender") or "").strip().lower()
    if not g:
        rf = char.get("reference_features")
        if isinstance(rf, str):
            try:
                rf = json.loads(rf)
            except Exception:
                rf = {}
        if isinstance(rf, dict):
            g = str(rf.get("gender") or "").strip().lower()
    if not g:
        return ""
    if g.startswith(("m", "男")):
        return "male"
    if g.startswith(("f", "女")):
        return "female"
    return ""


def _name_index(characters: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_name: Dict[str, Dict[str, Any]] = {}
    for c in (characters or []):
        n = str(c.get("name") or "").strip()
        if not n:
            continue
        by_name[n] = c
        rf = c.get("reference_features")
        if isinstance(rf, str):
            try:
                rf = json.loads(rf)
            except Exception:
                rf = {}
        for a in ((rf or {}).get("aliases") or []):
            if str(a).strip():
                by_name.setdefault(str(a).strip(), c)
    return by_name


def _match_character(name: str, by_name: Dict[str, Dict[str, Any]]):
    """精确 → 去掉「·」后的名前段 → 包含关系。"""
    hit = by_name.get(name)
    if hit is not None:
        return hit
    short = name.split("·")[0].strip()
    if short and by_name.get(short):
        return by_name[short]
    for n, c in by_name.items():
        if n and (n in name or name in n or (short and short in n)):
            return c
    return None


# 说话人身份线索 → (性别, 年龄段)。用于"话说的人没建角色卡"的情况。
_ROLE_HINTS: Tuple[Tuple[str, str, str], ...] = (
    # (正则, 性别, 年龄段)
    (r"(领主|国王|陛下|老爷|族长|首领|将军|统领|大臣|长者|父|伯|叔|爷)", "male", "mature"),
    (r"(士兵|战士|侍卫|卫兵|猎人|水手|船夫|农夫|铁匠|车夫|刺客|剑客)", "male", "adult"),
    (r"(少年|男孩|孩子|童子)", "male", "teen"),
    (r"(女王|王后|公主|夫人|太太|母亲|婆婆|老妇)", "female", "mature"),
    (r"(少女|女孩|姑娘|小姐|侍女|婢女|护士|女官)", "female", "young"),
    (r"(孩童|小孩|幼童)", "", "child"),
)


def infer_speaker_profile(name: str) -> Tuple[str, str, str]:
    """从说话人名里推 (性别, 年龄段, 依据)。推不出来返回空串，交给默认音色。"""
    for pat, g, band in _ROLE_HINTS:
        if re.search(pat, name):
            return g, band, f"说话人「{name}」命中身份线索 {pat} → {g or '?'}/{band}"
    return "", "", ""


def cast_voice(text_speaker: str, characters: List[Dict[str, Any]],
               default_voice: str, used: Optional[set] = None,
               plan: Optional[Dict[str, str]] = None) -> str:
    """给一个说话人挑音色：**整片选角表** → 角色卡指定 → 别名/模糊匹配
    → 按性别+年龄分池 → 身份线索 → 默认。

    ★ `plan`（`cast_plan()` 的产物）优先。它保证**同一个角色在全片只用一把嗓子**；
      没有它时，同一个角色在不同分镜里可能被排到不同音色（旧版 `used` 是逐分镜的），
      那正是"角色没有自己的声音"的根源之一。
    """
    name = (text_speaker or "").strip()
    if not name:
        return default_voice
    by_name = _name_index(characters)
    hit = _match_character(name, by_name)

    # ① 整片选角表（含角色卡里显式指定的音色）
    if plan:
        for key in ([name] + ([str(hit.get("name"))] if hit else [])):
            if key and plan.get(key):
                return plan[key]
    if hit is not None:
        v = voice_of_character(hit)
        if v:
            return v
        g = gender_of_character(hit)
        if g:
            return pick_pool_voice(g, name, used, age_band=age_band_of(hit))
    # ② 没匹配到角色卡（真实项目里很常见：说话人写的是"领主A""水手乙"）
    #    → 按**角色名里的身份线索**推性别与年龄，而不是一律回落到默认音色。
    #    实测事故：`caa9904f` 的「领主A」匹配不到「北方领主们」，
    #    于是这句男声台词用了默认音色（晓晓，女声）—— 一听就出戏。
    g, band, _why = infer_speaker_profile(name)
    if g:
        return pick_pool_voice(g, name, used, age_band=band)
    return default_voice


def _plan_key_for(name: str, cast: Optional[Dict[str, str]],
                  by_name: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """把说话人名解析成**整片选角表里的那个键**（简称/全名/别名/包含关系都认）。

    ★★ 为什么必须有这一步（2026-09-14 的真实事故）：
      选角表的键是**角色卡里的全名**「布兰·史塔克」，而剧本/分镜里写的是
      **简称**「布兰」。旧代码在 `dub_shot` 里用 `who not in cast` 判断
      "这是不是卡外角色" —— 简称查不到，于是同一个角色被当成新角色
      **又挑了一把嗓子**；实测「布兰」就此拿到了**默认女声**，
      而无面者拿到了别人的音色。用户听到的就是"还是那个 AI 女音、
      角色之间也不分"。

    返回 "" 表示确实不在选角表里（那才是真正的卡外角色）。
    """
    if not cast:
        return ""
    n = str(name or "").strip()
    if not n:
        return ""
    if n in cast:
        return n
    short = n.split("·")[0].strip()
    for key in cast:
        k_short = str(key).split("·")[0].strip()
        if short and k_short and (k_short == short or k_short == n):
            return key
        if n and (n in str(key) or str(key) in n):
            return key
    try:
        hit = _match_character(n, by_name or {})
    except Exception:
        hit = None
    if hit is not None:
        hn = str(hit.get("name") or "").strip()
        if hn and hn in cast:
            return hn
    return ""


def _voice_health_path() -> str:
    root = os.environ.get("VIDEOFORGE_DATA_DIR") or os.path.expanduser("~")
    d = os.path.join(root, "cache")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return ""
    return os.path.join(d, "voice_health.json")


def load_voice_health() -> Dict[str, Any]:
    """读过"哪些音色真的能出声"的记忆。

    ★ 为什么需要它：各厂商的音色表**都是静态写死的**（edge 那份抄自别的项目），
      里面有一批**合成必然失败**的音色 —— 实测 Edge 端点上
      晓辰/晓涵/晓梦/晓墨/晓睿/晓霜/晓甄 等会返回 `NoAudioReceived`。
      静态表改不动（厂商随时在变），所以让**运行结果自己说话**：
      失败过的音色记下来，以后选角不再选它；成功的也记下来。
      这样"能力清单"是**学出来的**，不是猜出来的。
    """
    p = _voice_health_path()
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def mark_voice_health(voice_id: str, ok: bool, err: str = "") -> None:
    """记录一个音色的实测结果（成功/失败）。失败的不再进选角候选。"""
    if not voice_id:
        return
    p = _voice_health_path()
    if not p:
        return
    try:
        h = load_voice_health()
        h[voice_id] = {"ok": bool(ok), "err": str(err)[:80],
                       "at": datetime.now().isoformat(timespec="seconds")}
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        logger.warning("记录音色健康状态失败：%s", e)


def infer_speaker_from_action(action: str,
                              characters: Optional[List[Dict[str, Any]]] = None,
                              ) -> str:
    """从动作/画面描述里**推断这句台词是谁说的**。

    ★★ 为什么必须要这一步（用户实测反馈「全片一把 AI 女音」）：
      真实分镜的台词常常是**写在 action 的引号里**的，例如
        「李队长蹲在草丛中…同时低声对战士们说：‘兄弟们，沉住气，等他…’」
      `dialogue` 字段是空的，于是解析出来的这句 `character=""` ——
      而 `cast_voice("")` 只能返回**默认音色**。
      所以"一把女音走到底"不是选角没做，是**这些话不知道是谁说的**。

    取值顺序（越靠前越可靠）：
      ① 「<角色名>…说/喊/问/答」这种**紧贴说话动词**的说话人；
      ② 动作里出现过的角色名（取最靠前的那个）；
      ③ 别名（角色卡 aliases）同样参与匹配。
    推不出来返回空 —— **不硬猜**，上层会如实标记"说话人未知"。
    """
    t = str(action or "")
    if not t:
        return ""
    chars = [c for c in (characters or []) if str(c.get("name") or "").strip()]
    names: List[tuple] = []
    for c in chars:
        n = str(c["name"]).strip()
        names.append((n, n))
        rf = c.get("reference_features")
        if isinstance(rf, str):
            try:
                rf = json.loads(rf) or {}
            except Exception:
                rf = {}
        if isinstance(rf, dict):
            for a in (rf.get("aliases") or []):
                a = str(a).strip()
                if a and a != n:
                    names.append((a, n))
    if not names:
        return ""
    best, best_pos = "", 10 ** 9
    # ① 角色名 + 紧随其后的说话动词
    for alias, real in sorted(names, key=lambda x: -len(x[0])):
        for m in re.finditer(re.escape(alias), t):
            tail = t[m.end():m.end() + 14]
            if re.search(r"(说|喊|问|答|道|叫|念|低语|嘟囔|提醒|宣布|回应)", tail):
                if m.start() < best_pos:
                    best_pos, best = m.start(), real
    if best:
        return best
    # ② 动作里出现过的角色名（最靠前者）
    best_pos = 10 ** 9
    for alias, real in sorted(names, key=lambda x: -len(x[0])):
        pos = t.find(alias)
        if 0 <= pos < best_pos:
            best_pos, best = pos, real
    if best:
        return best
    # ③ 兜底：动作**开头**的短名词 + 动词，通常是说话人
    #   实测："村民A指着玉米地，对旁边的村民B说：“真下霜了！…”"
    #   这里 村民A/村民B 不在角色卡里，但它**确实是个人名** ——
    #   认出来就能给它一把独立的嗓子，而不是让全片都落到默认音色。
    m = re.match(r"^\s*([\u4e00-\u9fffA-Za-z]{1,8}?)(?:指着|站|走|坐|抬|低|冷|笑|看|转身|"
                 r"点头|摇头|说|喊|问|答|道|回|开口|盯|皱|伸|握|把|将|从|在|向|对)",
                 t)
    if m:
        cand = m.group(1).strip()
        if len(cand) >= 2:
            return cand
    return ""


def _gender_hint(name: str) -> str:
    """从名字里猜性别（只在角色卡没写性别、又是卡外角色时用）。猜不出返回空。"""
    n = str(name or "")
    if re.search(r"(叔|爷|哥|弟|父|爸|伯|舅|兵|长|军官|首领|侦探|王子|国王|先生|老汉|农夫)", n):
        return "male"
    if re.search(r"(婶|妈|婆|奶|姐|妹|姑娘|娘|女|夫人|小姐|太太|嫂)", n):
        return "female"
    return ""


_CATALOG_CACHE: Dict[str, Any] = {}


async def load_voice_catalog(settings: Optional[dict] = None) -> List[Dict[str, Any]]:
    """拉取**真实的**可用音色目录（各厂商 list_voices），并解析出选角要用的属性。

    ★ 结果做**进程内缓存（10 分钟）**：这个函数会被"每一镜生成""一键配音"
      "出片"反复调用，每次都去问 5 家厂商的接口既慢又没必要。
      缓存键只含"哪些厂商有 Key"，Key 变了自然重新拉。

    ★ 为什么必须走真实清单：用户的话是「不是你那几个固定的 NPC 发音」。
      实测本机可列到 **52 个音色**（中文 44：男 17 / 女 27），
      而本模块里硬编码的池子只有 8 个。硬编码让人以为"端点只有这些"，
      实际上是"我们自己只写了这些"。
    返回每项：`{voice_id, gender, label, provider, verified, age_band, age_inferred, tags}`。
    拉不到（没网/没 Key）返回空列表 —— 调用方退回硬编码池，并如实说明。
    """
    try:
        from core.voice import dispatcher as _vd
        _keys = sorted(k for k, v in ((settings or {}).get("api_keys") or {}).items() if v)
        _ck = "|".join(_keys)
        _c = _CATALOG_CACHE.get(_ck)
        if _c and (time.time() - float(_c.get("at") or 0)) < 600:
            return list(_c.get("cat") or [])
        voices = await _vd.list_voices(settings or {})
        # ★★ 只保留**真的能用**的厂商的音色。
        #   实测踩到：dashscope 没配 Key，但 `list_voices` 照样返回它那 10 个
        #   **静态写死**的音色 —— 选角把「艾莉亚」配成 `dashscope:longjing_v2`，
        #   一合成就失败，再回退默认音色（等于白折腾一次，还差点让角色换嗓子）。
        #   所以先问一遍各厂商"有没有 Key"，没 Key 的直接不进候选。
        _usable = set()
        try:
            for _p in await _vd.list_providers(settings or {}):
                if _p.get("has_api_key"):
                    _usable.add(_p.get("name"))
        except Exception as _e:
            logger.warning("查厂商可用性失败（按全部可用处理）：%s", _e)
            _usable = set()
    except Exception as e:
        logger.warning("拉取音色目录失败（退回内置池）：%s", e)
        return []
    cat: List[Dict[str, Any]] = []
    health = load_voice_health()
    try:
        from core import voicecatalog as _vc
    except Exception:
        return []
    for v in (voices or []):
        vid = str(getattr(v, "voice_id", "") or "")
        if not vid:
            continue
        if not str(getattr(v, "language", "") or "").lower().startswith("zh"):
            continue                       # 中文剧本先用中文音色
        _prov = vid.split(":")[0]
        if _usable and _prov not in _usable:
            continue                       # 这个厂商没配 Key → 用了也合成不出来
        # ★ Edge 只放行**实测过能出声**的那 8 个。
        #   它的静态表里有 14 个音色在端点上已经不存在/不可用
        #   （NoAudioReceived），放进来只会让选角选中一把"发不出声的嗓子"。
        if _prov == "edge" and vid not in _EDGE_KNOWN_VOICES:
            continue
        # ★ 实测失败过的音色不再进候选（运行结果自己说话）
        _h = health.get(vid) or {}
        if _h and not _h.get("ok", True):
            continue
        a = _vc.parse_voice_attrs(getattr(v, "display_name", "") or "",
                                 getattr(v, "gender", "") or "")
        cat.append({"voice_id": vid,
                    "gender": str(getattr(v, "gender", "") or "").lower(),
                    "label": str(getattr(v, "display_name", "") or ""),
                    "provider": _prov,
                    "verified": vid in _EDGE_KNOWN_VOICES,
                    "age_band": a["age_band"], "age_inferred": a["age_inferred"],
                    "tags": a["tags"]})
    try:
        _CATALOG_CACHE[_ck] = {"at": time.time(), "cat": list(cat)}
    except Exception:
        pass
    return cat


def cast_plan(characters: List[Dict[str, Any]], default_voice: str,
              catalog: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """**整片**选角：一次把"每个角色用哪把嗓子"定死。

    返回 `{"voices": {角色名: 音色}, "reasons": {...}, "collisions": [...]}`。

    为什么必须整片一次定：
      · 角色卡没写音色时，音色是按"池子里还有谁没被占"挑的；
        逐分镜挑会让同一个角色在不同分镜里换嗓子（= "角色没有自己的声音"）；
      · 用户要求"声音要跟人物形象符合"，那就得**全局**保证：
        男/女不混、年龄段对得上、两个角色不撞音。

    角色卡里**显式**指定的音色永远优先，不会被覆盖。

    ★★ 2026-09-14：**传入 `catalog`（真实音色目录，见 `load_voice_catalog`）时，
    改走 `voicecatalog.cast_from_catalog`** —— 按角色卡的
    性别/年龄/定位/personality（很多卡直接把"说话语气…"写进去了）打分，
    并在理由里写清命中了什么。传空则退回内置的 8 个音色池（离线兜底）。
    """
    if catalog:
        try:
            from core import voicecatalog as _vc2
            r = _vc2.cast_from_catalog(characters or [], catalog)
            r["default_voice"] = default_voice
            r["catalog_used"] = True
            return r
        except Exception as e:
            logger.warning("按角色卡选角失败（退回内置池）：%s", e)
    from collections import Counter
    plan: Dict[str, str] = {}
    reasons: Dict[str, str] = {}
    used: set = set()
    chars = [c for c in (characters or []) if str(c.get("name") or "").strip()]
    # 先让"已显式指定"的占住位置，避免被自动分配抢掉
    for c in chars:
        n = str(c["name"]).strip()
        v = voice_of_character(c)
        if not v:
            continue
        # ★ edge 音色要先核对"引擎到底提不提供"。角色卡里可能是**历史遗留的
        #   死音色**（旧版池子发出去的 Yunze/Xiaohan 之类），拿着它去合成
        #   就是这个角色**整部片一句声音都没有**。这里让它退回自动选角，
        #   并把原因写出来 —— 换别的厂商（minimax:/siliconflow: 等）不动。
        if v.lower().startswith("edge:") and v not in _EDGE_KNOWN_VOICES:
            reasons[n] = (f"角色卡里的 edge 音色 {v} 已不在引擎音色列表中"
                          f"（2026-09-13 核实）→ 改为按形象自动选角")
            continue
        plan[n] = v
        reasons[n] = "角色卡里已指定音色（保持不变）"
        used.add(v)
    for c in chars:
        n = str(c["name"]).strip()
        if n in plan:
            continue
        g = gender_of_character(c)
        band = age_band_of(c)
        if not g:
            g2, band2, why2 = infer_speaker_profile(n)
            if g2:
                g, band = g2, (band2 or band)
                reasons[n] = why2
        if not g:
            g = "male" if (sum(ord(x) for x in n) % 2 == 0) else "female"
            reasons[n] = f"角色卡没写性别 → 按人名稳定散列取 {g}（可在角色页手改）"
        v = pick_pool_voice(g, n, used, age_band=band)
        plan[n] = v
        used.add(v)
        base = (f"{'男' if g == 'male' else '女'}声 · 年龄段「{band or '未知'}」"
                f" → {_label_of(v)}")
        reasons[n] = (reasons.get(n, "") + "；" if reasons.get(n, "")
                      else "") + base + _band_gap_note(band, v)
    cnt = Counter(plan.values())
    collisions = [{"voice": v, "characters": [k for k, x in plan.items() if x == v]}
                  for v, c2 in cnt.items() if c2 > 1]
    return {"voices": plan, "reasons": reasons, "collisions": collisions,
            "default_voice": default_voice}


def _label_of(voice_id: str) -> str:
    for pool in (_MALE_POOL, _FEMALE_POOL):
        for v, _g, band, style, label in pool:
            if v == voice_id:
                return f"{label}（{band}/{style}）"
    return voice_id


def _band_gap_note(want: str, voice_id: str) -> str:
    """年龄段对不上时如实说明：是"端点根本没有这一段"，还是"这一段被占了"。

    用户要的是"声音跟人物形象符合"。当我们只能用相邻年龄段代替时，
    必须把这件事说出来 —— 否则"老年角色听着像中年人"会变成一个没人解释的谜。
    """
    got = ""
    for pool in (_MALE_POOL, _FEMALE_POOL):
        for v, _g, band, _s, _l in pool:
            if v == voice_id:
                got = band
    if not want or not got or want == got:
        return ""
    if want in _POOL_MISSING_BANDS:
        return (f"；⚠ 免费 Edge 端点**没有「{want}」段音色**"
                f"（{_EDGE_POOL_VERIFIED_ON} 核实的 {len(_MALE_POOL) + len(_FEMALE_POOL)} 个"
                f"中文音色里没有这一段），只能用「{got}」代替 ——"
                f"要真正贴合，请在这个角色上指定一个支持该年龄段的付费音色。")
    return (f"；⚠ 该年龄段的音色已被同片其他角色占用，"
            f"这里只能用剩余里年龄段最近的「{got}」——"
            f"想两个人都贴合，请给其中一个指定付费音色或换引擎。")


# ═══════════════════════════════════════════════════════════════
# 二、逐句合成
# ═══════════════════════════════════════════════════════════════

def _clean_for_tts(text: str) -> str:
    """清掉会让 TTS 念出奇怪东西的标记（时间码、markdown、括号注释）。"""
    t = text or ""
    t = re.sub(r"\[\s*\d+(\.\d+)?\s*[-–~]\s*\d+(\.\d+)?\s*s?\s*\]", "", t)
    t = re.sub(r"[*#`>]+", "", t)
    t = re.sub(r"[（(][^）)]{0,30}(镜头|景别|特写|远景|中景|近景|俯拍|仰拍)[^）)]{0,30}[）)]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def measure_lines_duration(
    lines: List[Dict[str, Any]], characters: Optional[List[Dict[str, Any]]],
    settings: dict, *, default_voice: str = "",
) -> float:
    """实测一组台词**念完到底几秒**（每句用它自己角色的音色量，带内容缓存）。

    ★ 这个函数是"标称时长 vs 真实时长"矛盾的解药：
      分镜里写的 `12s` 是**规划稿**，不是事实。事实是"这段话念完要几秒"。
      MoneyPrinterTurbo 全片不做别的，就是量音频、然后让画面去盖住音频；
      我们把它用在**单个分镜**这一层，于是"分层定 10s、模型只给 5s"
      这个问题从根上消失了 —— 因为台词只要 6.8s 时我们根本不会去要 10s，
      而台词要 13.5s 时我们会规划成两段 10s，而不是硬塞进一段。

    返回 0.0 表示"量不出来"（没台词 / 音色都不可用）——调用方应当退回标称时长，
    绝不能因为量不出来就失败。
    """
    if not lines:
        return 0.0
    dv = (default_voice or "").strip()
    if not dv:
        try:
            dv = str((settings or {}).get("default_voice")
                     or (settings or {}).get("tts_voice") or "").strip()
        except Exception:
            dv = ""
    if not dv:
        dv = "edge:zh-CN-XiaoxiaoNeural"

    cache_root = ""
    try:
        cache_root = str((settings or {}).get("cache_dir") or "")
    except Exception:
        cache_root = ""
    if not cache_root:
        cache_root = os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR") or os.path.expanduser("~"),
            "cache")
    d = os.path.join(cache_root, "durcache")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return 0.0

    from core.voice.base import TTSRequest
    from core.voice import dispatcher as voice_dispatcher

    total = 0.0
    measured_any = False
    for ln in lines:
        text = str(ln.get("text") or "").strip()
        if not text:
            continue
        voice = cast_voice(ln.get("speaker") or ln.get("character") or "",
                           characters or [], dv)
        h = hashlib.md5(f"{voice}|{text}".encode("utf-8")).hexdigest()[:12]
        probe = os.path.join(d, f"{h}.mp3")
        ok = os.path.exists(probe) and os.path.getsize(probe) >= 512
        if not ok:
            try:
                r = await voice_dispatcher.synthesize(
                    TTSRequest(text=text, voice_id=voice,
                               output_path=probe, rate=1.0), settings or {})
                ok = bool(r.success) and os.path.exists(probe)
            except Exception:
                ok = False
            if not ok and voice != dv:
                # 这个音色不可用（没配 key / 音色下线）→ 用默认音色再试一次。
                # 宁可量得略不准，也不要因为一个音色缺失就让整个规划退回标称。
                try:
                    r = await voice_dispatcher.synthesize(
                        TTSRequest(text=text, voice_id=dv,
                                   output_path=probe, rate=1.0), settings or {})
                    ok = bool(r.success) and os.path.exists(probe)
                except Exception:
                    ok = False
        if not ok:
            continue
        dur = float(await probe_duration(probe) or 0.0)
        if dur > 0:
            total += dur
            measured_any = True
    return total if measured_any else 0.0


async def synthesize_line(
    text: str, voice_id: str, settings: dict, out_path: str,
    *, target_span: float = 0.0, emotion: str = "", action: str = "",
    character: Optional[Dict[str, Any]] = None, gender: str = "",
    prosody: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """合成一句台词：**按情绪演**，再按时间片长度做**温和**变速。

    两层语速，必须分清（这是"像机器人"的另一个来源）：
      ① **表演语速**（`prosody.rate`）：愤怒稍快、悲伤更慢 —— 它是"演"出来的，
         所以交给 TTS 引擎（引擎会用韵律模型处理，比后期变速自然得多）；
      ② **对时语速**（`atempo`）：这句比它的时间片长了/短了，才在后期拉一下，
         且只在 [MIN_SPEED, MAX_SPEED] 内；超出就承认"这句放不下"并如实上报，
         让上层去改台词或加长镜头，而不是偷偷压成快进音。

    旧代码把②当成唯一的语速手段、且**完全没把 emotion 送到引擎**
    —— 于是每句话都是同一个平铺直叙的嗓音，只是长短不同。

    ★ 返回值里的两个 speed 必须分清（2026-09-13 踩过坑）：
      · `timelock`  = 算出来的"对时倍数"（**意图**）；
      · `speed`     = 还要交给 `assemble_track` 再拉一次的倍数。
      做法是：只要 atempo **已经烙进文件**，`speed` 就报 1.0。
      否则同一句会被拉两次 —— 实测成片里超时间片 0.7 秒，声画错位。
    """
    t = _clean_for_tts(text)
    if not t:
        return {"ok": False, "error": "空文本"}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    from core.voice.base import TTSRequest
    from core.voice import dispatcher as voice_dispatcher

    # ① 表演韵律：没显式给就现场算（含从文本/动作推断情绪 + 角色基线嗓音）
    pr = dict(prosody or {})
    if not pr:
        try:
            from core.prosody import plan_prosody
            pr = plan_prosody(emotion, t, action, character, gender=gender)
        except Exception as e:
            logger.warning("韵律规划失败（按中性朗读）：%s", e)
            pr = {}
    perf_rate = float(pr.get("rate") or 1.0)

    r = await voice_dispatcher.synthesize(
        TTSRequest(text=t, voice_id=voice_id or "edge:zh-CN-XiaoxiaoNeural",
                   output_path=out_path, rate=perf_rate,
                   volume=float(pr.get("volume") or 1.0),
                   pitch_hz=float(pr.get("pitch_hz") or 0.0),
                   pitch_semitones=float(pr.get("pitch_st") or 0.0),
                   emotion=str(pr.get("emotion") or "")), settings)
    if not r.success or not os.path.exists(out_path):
        # ★ 记下"这把嗓子发不出声" —— 下次选角就不会再选它。
        try:
            mark_voice_health(voice_id, False, r.error or "合成失败")
        except Exception:
            pass
        return {"ok": False, "error": r.error or "合成失败", "text": t}
    try:
        mark_voice_health(voice_id, True)
    except Exception:
        pass

    real = _pcm_seconds(out_path) or await probe_duration(out_path)
    if real <= 0:
        real = max(0.6, len(t) / CHARS_PER_SEC)

    # ② 对时变速（在表演语速之上再拉，只拉"值得拉"的量）
    speed = 1.0
    overflow = 0.0
    fit_applied = False
    if target_span and target_span > 0.2:
        speed = max(MIN_SPEED, min(MAX_SPEED, real / target_span))
        if speed < 0.9 or speed > 1.12:      # 值得调才调，避免无谓重合成
            fitted = out_path + ".fit.mp3"
            rc, err = _run([
                _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", out_path,
                "-filter:a", f"atempo={speed:.4f}", "-c:a", "libmp3lame", "-q:a", "3",
                fitted])
            if rc == 0 and os.path.exists(fitted):
                os.replace(fitted, out_path)
                real = _pcm_seconds(out_path) or await probe_duration(out_path) or real / speed
                fit_applied = True
        overflow = max(0.0, real - target_span)
    # ★★ 交给排轨层的 `speed` 只能是"**还没**烙进文件"的那部分。
    #    已经用 atempo 烙进文件里的必须报 1.0，否则 `assemble_track` 会**再拉一次**——
    #    实测（2026-09-13）：小虎那句已经按 0.748 拉成 2.269s 塞进 2.3s 的时间片，
    #    排轨层又拉一遍 → 成片里变成 3.000s，超出时间片 0.7s，下一句就压上来了。
    #    这是个"看不见"的错位：字幕、落点、单句音频全对，只有混完的成片不对。
    assemble_speed = 1.0 if fit_applied else speed
    if overflow > 0.35:
        assemble_speed = 1.0   # 报给上层的是"建议自己处理"，不是继续硬压
    # 混完之后这一句在时间轴上真正占多长（= 文件长度 / 排轨层还会拉的量）
    placed_duration = real if abs(assemble_speed - 1.0) < 0.01 else real / assemble_speed
    _fb = (r.raw or {}).get("model_fallback") if isinstance(r.raw, dict) else None
    return {
        "ok": True, "path": out_path, "text": t,
        "duration": round(real, 3), "speed": round(assemble_speed, 3),
        "timelock": round(speed, 3), "fit_applied": fit_applied,
        "placed_duration": round(placed_duration, 3),
        "overflow": round(overflow, 3), "emotion": emotion,
        "voice_id": voice_id,
        # ★ 配音模型被厂商拒了、自动退回了备用型号 → 如实带出去（上层要告警）。
        #   不报的话又变成"我选的是 2.8，怎么听起来是老的"这种没人解释的事。
        "model_fallback": _fb,
        "model_used": ((r.raw or {}).get("model_used") if isinstance(r.raw, dict) else None),
        # ★ 把**实际用了什么韵律**如实带出去：上层要能在告警里说清
        #   "这句为什么这么念"，否则又是一条看不见的链路。
        "prosody": pr,
        "perf_rate": perf_rate,
    }


# ═══════════════════════════════════════════════════════════════
# 三、按绝对时间点拼装音轨（只编码一次）
# ═══════════════════════════════════════════════════════════════

def _decode_pcm(path: str) -> Optional[bytes]:
    """把任意音频解码成 24kHz/16bit/mono 裸 PCM。"""
    p = subprocess.run(
        [_ffmpeg(), "-v", "error", "-i", path,
         "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-"],
        capture_output=True)
    return p.stdout if p.returncode == 0 and p.stdout else None


def _decode_pcm_at_speed(path: str, speed: float, skip: float = 0.0) -> Optional[bytes]:
    """解码 + 变速（用 ffmpeg 而不是改采样率 —— 后者会变调）。

    `skip`：从音频开头**跳过**多少秒再摆。用在"画面裁掉了 h 秒死帧"的时候 ——
    画面提前了 h 秒，声音必须同量前移，否则这一镜立刻声画错位。
    """
    af = f"atempo={max(0.5, min(2.0, speed)):.4f}" if abs(speed - 1.0) > 0.01 else "anull"
    cmd = [_ffmpeg(), "-v", "error"]
    if skip and skip > 0.01:
        cmd += ["-ss", f"{float(skip):.4f}"]
    cmd += ["-i", path, "-filter:a", af,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-"]
    p = subprocess.run(cmd, capture_output=True)
    return p.stdout if p.returncode == 0 and p.stdout else None


def assemble_track(placements: List[Dict[str, Any]], total_seconds: float,
                   out_path: str) -> Dict[str, Any]:
    """把若干 (音频, 绝对起始秒) 混成一条音轨。

    ★ **一次性编码**：先在内存里按采样点摆好，最后只编码一次。
      逐段编码再拼接会让 MP3 的 encoder delay/padding 累积，
      几百毫秒的漂移就是这么来的（MPT 注释里明确记录了这个坑）。
    """
    try:
        import numpy as np
    except Exception:
        np = None
    if np is None:
        return {"ok": False, "error": "缺少 numpy，无法精确拼装音轨"}

    total_samples = max(1, int(round(total_seconds * SAMPLE_RATE)))
    buf = np.zeros(total_samples, dtype=np.float32)
    placed: List[Dict[str, Any]] = []
    drift_report: List[str] = []

    for pl in placements:
        raw = _decode_pcm_at_speed(pl["path"], float(pl.get("speed") or 1.0),
                                   float(pl.get("skip") or 0.0))
        if not raw:
            continue
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        start_idx = int(round(float(pl["start"]) * SAMPLE_RATE))
        if start_idx >= total_samples:
            drift_report.append(f"「{pl.get('text','')[:12]}」起点超出片长，已丢弃")
            continue
        end_idx = start_idx + len(arr)
        # 超长的部分不截断，而是让它自然溢出（截断会把话cut断，更难听）；
        # 但整条音轨会按最长的那个延长，由调用方决定要不要再裁。
        if end_idx > len(buf):
            buf = np.concatenate([buf, np.zeros(end_idx - len(buf), dtype=np.float32)])
        buf[start_idx:end_idx] += arr
        placed.append({
            "start": round(float(pl["start"]), 3),
            "end": round(start_idx / SAMPLE_RATE + len(arr) / SAMPLE_RATE, 3),
            "text": pl.get("text", ""), "character": pl.get("character", ""),
            "voice_id": pl.get("voice_id", ""),
            "actual_duration": round(len(arr) / SAMPLE_RATE, 3),
            "target_span": pl.get("span", 0),
            "script_span": pl.get("script_span", pl.get("span", 0)),
            "timelock": pl.get("timelock", 1.0),
            "clamped": bool(pl.get("clamped")),
        })

    if not placed:
        return {"ok": False, "error": "没有任何一句合成成功", "placements": []}

    # 削顶保护：多句重叠相加可能超过 ±1.0
    peak = float(np.max(np.abs(buf))) if buf.size else 0.0
    if peak > 1.0:
        buf = buf / peak
        drift_report.append(f"音轨峰值 {peak:.2f} 超过 1.0，已整体归一化（有台词时间重叠）")

    pcm_path = out_path + ".pcm"
    with open(pcm_path, "wb") as f:
        f.write((np.clip(buf, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
    # 只编码这一次。
    # ★ 编码器必须按**扩展名**选：把 AAC 往 .mp3 里塞会被 ffmpeg 以
    #   "Invalid argument(-22) / no packets" 拒绝（踩过：流水线传的是 voice.mp3）。
    ext = os.path.splitext(out_path)[1].lower()
    if ext == ".mp3":
        acodec = ["-c:a", "libmp3lame", "-q:a", "3"]
    elif ext in (".m4a", ".mp4", ".aac"):
        acodec = ["-c:a", "aac", "-b:a", "192k"]
    elif ext == ".wav":
        acodec = ["-c:a", "pcm_s16le"]
    elif ext == ".opus":
        acodec = ["-c:a", "libopus", "-b:a", "128k"]
    else:
        acodec = ["-c:a", "aac", "-b:a", "192k"]
    rc, err = _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", pcm_path,
        *acodec, "-ar", str(SAMPLE_RATE), out_path], timeout=900)
    try:
        os.remove(pcm_path)
    except Exception:
        pass
    if rc != 0 or not os.path.exists(out_path):
        return {"ok": False, "error": f"编码音轨失败：{(err or '')[-200:]}"}

    return {
        "ok": True, "path": out_path,
        "duration": round(len(buf) / SAMPLE_RATE, 3),
        "placements": placed,
        "warnings": drift_report,
    }


# ═══════════════════════════════════════════════════════════════
# 四、总入口：把整段分镜配音出来
# ═══════════════════════════════════════════════════════════════

async def dub_shot(
    shot: Dict[str, Any], *, characters: List[Dict[str, Any]], settings: dict,
    out_dir: str, default_voice: str = "edge:zh-CN-XiaoxiaoNeural",
    duration: Optional[float] = None, progress=None,
    cast: Optional[Dict[str, str]] = None,
    catalog: Optional[List[Dict[str, Any]]] = None,
    script_sources: Optional[List[Dict[str, Any]]] = None,
    scene_name: str = "",
    shot_order: Optional[int] = None,
    character_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """把一个分镜按台词时间轴配音，返回音轨 + 逐句字幕。

    与旧做法的区别：旧的是"把整段概述从头念到尾"，这里是
    "谁、在第几秒、用什么情绪说什么" —— 全片选角表（`cast`）保证同一个角色
    从头到尾是同一把嗓子，韵律由 `core/prosody.py` 按情绪和角色形象算出来。

    2026-09-13 的三处修正（都对应用户的原话）：
      ① 情绪**真的送到引擎**了（旧代码只把它写进返回值，引擎一个字节都没收到）；
      ② 音色从**整片选角表**来（旧代码按分镜挑，"同一个角色"会换嗓子）；
      ③ 每句带**气口**：台词前留吸气/决定开口的短停顿、台词后留静默尾拍 ——
         这是"说话节奏"能被听出来的地方（参考 dialogue-audio.md 的对白锁）。
    """
    os.makedirs(out_dir, exist_ok=True)
    dur = float(duration or shot.get("duration_seconds") or 5.0)
    lines = timeline_lines(shot.get("layer2_timeline"), dur)

    # ★★ 2026-09-14：**从剧本回填台词** —— 用户实测「第一个镜头没有声音
    #   （剧本是有对话的）」。真实原因是分镜的 `layer2_timeline` 里
    #   **根本没有 dialogue 字段**（AI 写分镜时漏了），而剧本那一场写着对话。
    #   `scriptlines` 只在"说话人确实出现在这一镜里"时才填，绝不按场号硬塞
    #   （这个项目的剧本被改过一稿，硬塞会把琼恩的台词配到布兰的画面上）。
    recovered: Optional[Dict[str, Any]] = None
    if script_sources:
        try:
            from core import scriptlines as _sl
            _inj = _sl.recover_and_inject(
                shot, script_sources, scene_name=scene_name,
                character_names=character_names, order_index=shot_order)
            if _inj.get("added"):
                _tl2 = timeline_lines(_inj.get("timeline"), dur)
                if _tl2:
                    lines = _tl2
                    recovered = {
                        "from": _inj.get("source") or "剧本",
                        "scene": _inj.get("scene_title") or "",
                        "added": _inj.get("added"),
                        "placed": _inj.get("placed") or [],
                        "skipped": _inj.get("skipped") or [],
                    }
            elif _inj.get("reason") or _inj.get("skipped"):
                recovered = {"from": "", "added": 0,
                             "reason": _inj.get("reason") or "",
                             "skipped": _inj.get("skipped") or []}
        except Exception as e:
            logger.warning("从剧本回填台词失败（按原时间轴配音）：%s", e)

    if not lines:
        _extra = ""
        if recovered and recovered.get("skipped"):
            _extra = ("；剧本里那几句没敢填进来：" + "；".join(
                f"「{s.get('text', '')[:10]}」{s.get('reason', '')}"
                for s in recovered["skipped"][:3]))
        elif recovered and recovered.get("reason"):
            _extra = "；" + str(recovered["reason"])
        return {"ok": False, "error": "本分镜没有可念的台词" + _extra,
                "hint": "用「🎬 补全台词」让 AI 按剧情写出真正的对白"
                        "（在分镜页，或 POST /api/projects/{pid}/dialogue/fill）。",
                "lines": [], "subtitles": [], "recovered": recovered}

    # ★★ 剧本时间轴比**实际片段**长时，后面的句子会排到片长之外。
    #   实测：某一镜剧本写 12s、生成出来的片段只有 5.88s，
    #   第 2 句排在 6.5s → 被丢弃，用户听到的是"这一镜只说了半句话"，
    #   而旧告警只有一句"起点超出片长，已丢弃"，**看不出为什么**。
    #   这里提前算清楚：哪几句放不下、为什么，一次说清。
    _out_of_range = []
    for _i, _ln in enumerate(lines):
        try:
            if float(_ln.get("start") or 0) >= dur - 0.05:
                _out_of_range.append(
                    f"第{_i+1}句「{str(_ln.get('text') or '')[:12]}」"
                    f"排在 {float(_ln.get('start') or 0):.1f}s")
        except Exception:
            pass

    chars_by_name = {str(c.get("name") or "").strip(): c for c in (characters or [])}
    _by_name = _name_index(characters)
    placements: List[Dict[str, Any]] = []
    subtitles: List[Dict[str, Any]] = []
    warnings: List[str] = []
    cast_used: Dict[str, str] = {}
    prosody_notes: List[str] = []
    _fb_seen: List[Dict[str, Any]] = []      # 配音型号回退（同一型号只报一次）

    for i, ln in enumerate(lines):
        if progress:
            try:
                progress(0.1 + 0.7 * i / max(1, len(lines)),
                         f"配音 {i+1}/{len(lines)}：{ln['character'] or '旁白'}")
            except Exception:
                pass
        who = ln["character"] or ""
        # ★★ 说话人未知时，先从**动作描述**里推断（真实分镜的台词常写在
        #   action 的引号里，解析出来 character 是空的）。不推的话
        #   `cast_voice("")` 一律落到默认音色 —— 这就是用户听到的
        #   "全片一把 AI 女音"。
        if not who:
            who = infer_speaker_from_action(ln.get("action", ""), characters)
            if who:
                ln["character"] = who
                ln["speaker_inferred"] = True
        # ★★★ 2026-09-14 修掉"同一个角色两个声音 / 又回落到女声"的真根因：
        #   选角表的键是**角色卡里的全名**（「布兰·史塔克」），而剧本/分镜里
        #   写的是**简称**（「布兰」）。旧代码拿简称去查 `who not in cast`，
        #   查不到就把它当"卡外角色"**又挑了一把嗓子** ——
        #   实测就是这一步把「布兰」配成了默认女声（晓晓），
        #   而无面者拿到别人的音色。现在先**归一化到选角表的键**再决定。
        if who:
            _pk = _plan_key_for(who, cast, _by_name)
            if _pk and _pk != who:
                logger.info("说话人「%s」→ 选角表键「%s」", who, _pk)
                who = _pk
                ln["character"] = who
        # ★★ 卡外角色（"村民A""村民B"这种没进角色卡的）也要**各自一把嗓子**，
        #   不能全部落到默认音色 —— 那正是用户说的"一把 AI 女音走到底"。
        #   有了目录就按名字的**身份线索**（刺客/水手→男，少女→女）从目录里
        #   挑一把**没用过的**；没有目录就退回内置池。
        if who and cast and who not in cast and catalog:
            try:
                from core import voicecatalog as _vcc
                _g, _band, _gwhy = infer_speaker_profile(who)
                _g = _g or _gender_hint(who)
                _p = {"name": who,
                      "age": _band,
                      "reference_features": json.dumps(
                          {"gender": _g, "age": _band}, ensure_ascii=False)}
                _r = _vcc.cast_from_catalog([_p], catalog, used=set(cast.values()))
                _nv = (_r.get("voices") or {}).get(who)
                if _nv:
                    cast = dict(cast)
                    cast[who] = _nv
                    logger.info("卡外角色 %s → %s（%s%s）", who, _nv,
                                (_r.get("reasons") or {}).get(who, "")[:60],
                                f"；{_gwhy}" if _gwhy else "")
            except Exception as e:
                logger.warning("卡外角色选音色失败（用默认）：%s", e)
        #   `cast` 可能是 None（没有整片选角表时）—— 必须按空表处理。
        #   踩过：这里直接 `cast.get(...)`，`test_voicecast` / `test_dub_timing`
        #   两个套件当场 `AttributeError: 'NoneType' object has no attribute 'get'`。
        if who and not (cast or {}).get(who) and not catalog:
            try:
                cast = dict(cast or {})
                _g2 = _gender_hint(who) or (infer_speaker_profile(who)[0]) or "male"
                cast[who] = pick_pool_voice(_g2, who, set(cast.values()))
            except Exception:
                pass
        v = cast_voice(who, characters, default_voice, plan=cast)
        cast_used[who or "（旁白）"] = v
        if not who and ln.get("text"):
            warnings.append(
                f"「{str(ln.get('text'))[:14]}…」**没能判断是谁说的**"
                f"（分镜里这句台词没写说话人，动作描述里也没出现角色名）——"
                f"这句用了默认音色。到分镜页给这句补上说话人即可。")
        _hit = chars_by_name.get(who) or _match_character(who, _by_name) if who else None
        wav = os.path.join(out_dir, f"line_{i:03d}.mp3")
        span = float(ln["span"])
        # ★ 真正可用的时间片 = **到下一句开口之前还剩多少**，不是剧本给的那一段。
        #   实测（真实剧本 25 句扫描）：只看自己那一段，会有 3 句的尾巴压到
        #   下一句的起点上（56ms / 97ms / 283ms）—— 两个人抢话，听起来就是
        #   "衔接很突兀"。合成就按这个更短的窗口去对时，才不会压别人。
        script_span = span
        # ★ 先算好这句的**气口**（要留多少吸气/决定开口的停顿），因为"真正能用的
        #   时间片"是 `下一句起点 − 本句起点 − 气口`。之前先合成了再算气口，
        #   于是起点被 lead 往后推、结尾却没人为它让路 —— 实测有 3 句把尾巴
        #   压到下一句的起点上（56/97/283ms），听起来就是两个人抢话。
        _pr = {}
        try:
            from core.prosody import plan_prosody as _plan
            _pr = _plan(ln.get("emotion", ""), ln["text"], ln.get("action", ""),
                        _hit, gender=(gender_of_character(_hit) if _hit else ""))
        except Exception as e:
            logger.warning("韵律规划失败（按中性朗读）：%s", e)
        lead_hint = float(_pr.get("lead") or 0.0)
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt:
            # 再留 0.10s 余量给 atempo 的帧边界与"不值得调"的死区（±12%）
            avail = (float(nxt["start"]) - float(ln["start"]) - lead_hint - 0.10)
            if 0.3 < avail < span:
                span = avail
        res = await synthesize_line(
            ln["text"], v, settings, wav, target_span=span,
            emotion=ln.get("emotion", ""), action=ln.get("action", ""),
            character=_hit, gender=(gender_of_character(_hit) if _hit else ""),
            prosody=_pr)
        if not res.get("ok") and v != default_voice:
            # ★★ 音色失败时**换一把再试一次**，而不是把这句丢掉。
            #   为什么必须有这一层：选角现在会用到**各厂商真实音色目录**里的音色
            #   （44 个中文音色），而其中一部分**没法保证一定可用**——实测 Edge 端点
            #   有一批音色会返回 `NoAudioReceived`。旧行为是只记一条警告、**这句就没了**
            #   （成片里角色突然不说话）。现在回退到默认音色把话说完，
            #   并**如实说明换了嗓子**，不假装还是原来那把。
            _first_err = res.get("error")
            res2 = await synthesize_line(
                ln["text"], default_voice, settings, wav, target_span=span,
                emotion=ln.get("emotion", ""), action=ln.get("action", ""),
                character=_hit, gender=(gender_of_character(_hit) if _hit else ""),
                prosody=_pr)
            if res2.get("ok"):
                warnings.append(
                    f"「{ln['text'][:14]}…」的选定音色（{v}）合成失败，"
                    f"已回退到默认音色 → 这一句的音色与角色卡不一致，"
                    f"建议到「角色」页为 {who or '该角色'} 指定一把可用音色。"
                    f"（原因：{str(_first_err)[:50]}）")
                res = res2
                v = default_voice
                cast_used[who or "（旁白）"] = v
        if not res.get("ok"):
            warnings.append(f"第 {i+1} 句合成失败（{who or '旁白'}）：{res.get('error')}")
            continue
        if res.get("overflow", 0) > 0.35:
            warnings.append(
                f"「{ln['text'][:14]}…」比它的时间片长 {res['overflow']:.1f} 秒"
                f"（{ln['chars']} 字 / {span:.1f} 秒）—— 建议改短台词或加长这一段。")
        _fb = res.get("model_fallback")
        if isinstance(_fb, dict) and _fb.get("from") and _fb not in _fb_seen:
            # ★ 配音型号被厂商拒了、自动退回了备用型号 —— 必须说出来，
            #   否则又变成"我明明选了 2.8，听起来却是老型号"这种没人解释的事。
            _fb_seen.append(_fb)
            warnings.append(
                f"⚙ 配音型号「{_fb.get('from')}」在你账号上不可用，"
                f"这一次**实际用的是「{_fb.get('to')}」**。"
                f"到「设置 → 🎙 配音模型」换成账号可用的型号即可（每个厂商一份下拉）。")
        _pr = res.get("prosody") or _pr
        if _pr:
            from core.prosody import describe as _pdesc
            prosody_notes.append(
                f"{who or '旁白'}「{ln['text'][:12]}…」→ {_pdesc(_pr)}")
        # ★ 气口：台词**前**留吸气/决定开口的停顿，台词**后**留静默尾拍。
        #   放在排轨这一层而不是塞进音频里 —— 这样"演员的呼吸空间"是时间轴上的
        #   真实留白，画面不会被声音压满；也正因为它是留白，两句话不会粘在一起。
        #   但**不能越界**：起点不许为负、总长不许超过这一镜。
        lead = float(_pr.get("lead") or 0.0)
        tail = float(_pr.get("tail") or 0.0)
        start = float(ln["start"]) + lead
        if start + 0.2 > dur:            # 留了气口就放不下了 → 放弃气口，别丢句子
            start = float(ln["start"])
            lead = 0.0
        # ★ 用 `placed_duration`（排轨层混完之后真正的长度），不是文件长度 ——
        #   两者在"没烙 atempo、留给排轨层拉"的情况下不一样，用错了字幕就会
        #   比声音早结束 / 尾拍算错。
        speak_len = float(res.get("placed_duration") or res["duration"])
        speak_end = start + speak_len
        if speak_end + tail > dur:
            tail = max(0.0, dur - speak_end)
        # 压到下一句起点上是"抢话"。大多数情况上面已经把窗口收窄解决了；
        # 剩下的都是**变速上限夹住**的（台词真的比时间片长太多，再压就成了
        # 快进音）—— 这种情况如实报出来，让人去改台词，不偷偷切掉半个字。
        if nxt and speak_end > float(nxt["start"]) + 0.05:
            warnings.append(
                f"「{ln['text'][:14]}…」结尾 {speak_end:.2f}s 压到下一句 "
                f"{float(nxt['start']):.2f}s（抢话 {speak_end - float(nxt['start']):.2f}s）"
                f"—— 台词 {ln['chars']} 字放不进 {span:.1f} 秒，已到变速上限，"
                f"建议改短或把两句的时间片拉开。")
        placements.append({
            "path": wav, "start": start, "span": span,
            "script_span": script_span,
            "timelock": res.get("timelock", 1.0),
            "clamped": abs(float(res.get("timelock") or 1.0) - MIN_SPEED) < 1e-6
            or abs(float(res.get("timelock") or 1.0) - MAX_SPEED) < 1e-6,
            "text": ln["text"], "character": who,
            "voice_id": v, "speed": res.get("speed", 1.0),
        })
        # 字幕与语音**同源**：用实际放下的位置，不是估算
        subtitles.append({
            "start": round(start, 3),
            "end": round(min(dur, start + min(speak_len,
                                              max(span, speak_len))), 3),
            "text": ln["text"], "character": who,
        })

    if not placements:
        return {"ok": False, "error": "全部台词都合成失败",
                "warnings": warnings, "lines": lines, "subtitles": []}
    if _out_of_range:
        warnings.append(
            f"这一镜实际只有 {dur:.1f}s，剧本里有 {len(_out_of_range)} 句排在这个长度之外"
            f"（{'；'.join(_out_of_range)}）—— 它们**没有声音**。"
            f"要么把这一镜重新生成得更长/拆成两镜，要么把这句台词提前或缩短。")

    if progress:
        try:
            progress(0.85, "按时间轴拼装音轨…")
        except Exception:
            pass
    track = assemble_track(placements, dur, os.path.join(out_dir, "dub.m4a"))
    if not track.get("ok"):
        return {"ok": False, "error": track.get("error"), "warnings": warnings,
                "lines": lines, "subtitles": subtitles}

    return {
        "ok": True,
        "path": track["path"],
        "duration": track["duration"],
        "line_count": len(placements),
        "cast": cast_used,
        "prosody_notes": prosody_notes,
        "placements": track["placements"],
        "subtitles": subtitles,
        "warnings": warnings + list(track.get("warnings") or []),
        "lines": lines,
        "recovered": recovered,
    }


def build_srt(subtitles: List[Dict[str, Any]], path: str,
              color_by_speaker: bool = False) -> str:
    """由逐句数据直接生成 SRT。

    因为每句都带精确时间戳，这里**不需要任何匹配/对齐**，
    也就不会出现 MPT 那种"对不齐就整体不写字幕"的情况。
    """
    def ts(sec: float) -> str:
        sec = max(0.0, float(sec))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        if ms == 1000:
            s, ms = s + 1, 0
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines_out: List[str] = []
    for i, s in enumerate(subtitles, 1):
        text = s.get("text") or ""
        who = s.get("character") or ""
        line = f"{who}：{text}" if who and color_by_speaker else text
        lines_out.append(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{line}\n")
    data = "\n".join(lines_out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    return path
