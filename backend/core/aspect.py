"""画幅守卫（aspect guard）：**不要相信厂商会按你要的画幅出片。**

═══════════════════════════════════════════════════════════════════
真实事故（2026-09-13，花钱买来的）
═══════════════════════════════════════════════════════════════════
项目「生成权力的游戏最终版大结局」分镜画幅是 16:9。为了让一个 20 秒的镜头
少调几次模型，规划层把分辨率从 `1080P` 降到 `768P`（因为 1080P 只给 6s、
768P 给 10s）—— 省下两次调用。

结果：MiniMax 海螺 `MiniMax-Hailuo-2.3` 在 `768P` 下返回的是 **768×768（1:1）**，
而适配器 payload 里**根本没有画幅字段**（只有 model/prompt/duration/resolution），
所以画幅是厂商按分辨率隐含决定的，我们全程无感。

后果链条：
  1. 库里这个分镜还写着 16:9 → 界面、剧本、运镜描述全都按 16:9 写的；
  2. 成片归一化时把 768×768 裁成 16:9 → **左右各丢 44% 画面**，
     构图、人物位置、运镜方向全部作废；
  3. 没有任何一层报警 —— 因为从来没人**量过回来的片到底多大**。

本模块的立场：
  · 请求的画幅是**意图**，量到的画幅才是**事实**；
  · 每个外部生成的片段都必须过 `guard_clip()`，与意图不符就**明确失败**，
    并给出可执行的替代分辨率，而不是"静默裁掉 44% 画面"。
═══════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("videoforge.aspect")

# 画幅偏差容忍度（相对值）。0.06 相当于 16:9 里允许 ±1.7% 的浮点/编码误差，
# 但 16:9(1.778) vs 16:10(1.600) 偏差 0.111 → 判为不符。
ASPECT_TOL = 0.06

# 偏差超过这个值就算"结构性不符"（裁切会毁构图），例如 16:9 → 1:1 偏差 0.4375。
ASPECT_FATAL = 0.20

# 常见画幅名 → 宽高比
_NAMED: Dict[str, float] = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "21:9": 21 / 9,
    "1:1": 1.0,
    "2.35:1": 2.35,
}

# ── 各厂商「分辨率 → 实际出片画幅」实测表 ──────────────────────────
# 只写**实测过**的；没实测的一律不写（宁可说不知道，也不猜）。
# ⚠ 画幅是 **(厂商, 型号, 分辨率)** 的函数，**不是**全局常数 ——
#   同是"768P"：海螺 2.3 出一条 768×768（1:1），海螺 02 出 1366×768（16:9）。
#   所以键必须带型号，绝不能拿一个型号的实测去套另一个型号。
RES_ASPECT: Dict[Tuple[str, str], Dict[str, str]] = {
    ("hailuo", "MiniMax-Hailuo-2.3"): {"768P": "1:1", "1080P": "16:9"},
    ("hailuo", "MiniMax-Hailuo-02"): {"768P": "16:9", "1080P": "16:9"},
    # video-01 两档都实测过，画幅都是 16:9（而且都是 1280×720 ——
    # 它**忽略分辨率**，见 model_catalog 里那三条样本）
    ("hailuo", "video-01"): {"768P": "16:9", "1080P": "16:9"},
}
RES_ASPECT_EVIDENCE: Dict[Tuple[str, str], str] = {
    ("hailuo", "MiniMax-Hailuo-2.3"):
        "2026-09-13 实测：768P/10s → 768x768；1080P/6s → 1920x1080（3 次样本一致）；"
        "传 aspect_ratio 被忽略；1080P 不能要 10s（厂商报 combination 不合法）",
    ("hailuo", "MiniMax-Hailuo-02"):
        "2026-09-13 实测：768P/6s → 1366x768；1080P/6s → 1920x1080",
    ("hailuo", "video-01"):
        "2026-09-13 实测：1080P/6s、1080P/10s、768P/10s **三者都** → 1280x720 且 5.64s"
        "（说明 video-01 同时忽略分辨率与时长）",
}


def _ffmpeg() -> str:
    try:
        from core.proc import ffmpeg_path  # type: ignore
        p = ffmpeg_path()
        if p:
            return p
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


# ═══════════════════════════════════════════════════════════════
# 纯函数（可单测，不碰磁盘）
# ═══════════════════════════════════════════════════════════════

def normalize_ratio(spec: Any, default: str = "16:9") -> str:
    """把画幅写法统一成**厂商 API 认识的** `W:H`。

    ★★ 2026-09-14 真实事故（用户："我用了 seedance…生成的视频并没有按我的剧本"）：
      我们在这里把 `16:9` 用 `.replace(":", "x")` 变成了 **`16x9`**，然后
      既写进提示词后缀（`--ratio 16x9`）、又写进 `parameters.ratio`（`"16x9"`）。
      方舟不认这个值 → 按默认出了 **960x960（1:1）** → 我们的画幅守卫
      判定"画幅不符：会裁掉 44% 画面" → **把这条已经付费生成好的视频丢掉**，
      退回本地合成（一张图的运镜）。用户看到的就是：
      场景在、**该出现的人物不在**、而且完全没按剧本演。
      （更讽刺的是：我早先的探针脚本 `probe_ark_which.py` 用的却是正确的
        `--ratio 16:9` —— 产品代码和探针代码不一致，谁也没去对。）
    """
    t = str(spec or "").strip()
    if not t:
        return default
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:x×X/]\s*(\d+(?:\.\d+)?)\s*$", t)
    if not m:
        return t
    a, b = m.group(1), m.group(2)
    if a.endswith(".0"):
        a = a[:-2]
    if b.endswith(".0"):
        b = b[:-2]
    return f"{a}:{b}"


def parse_aspect(spec: Any) -> Optional[float]:
    """'16:9' → 1.777…；认不出来的写法返回 None（含 'other'）。"""
    if spec is None:
        return None
    if isinstance(spec, (int, float)):
        return float(spec) if spec > 0 else None
    t = str(spec).strip()
    if t in _NAMED:
        return _NAMED[t]
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:x×/]\s*(\d+(?:\.\d+)?)\s*$", t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return a / b if a > 0 and b > 0 else None
    return None


def ratio_of(w: Any, h: Any) -> float:
    try:
        w, h = float(w), float(h)
    except (TypeError, ValueError):
        return 0.0
    return (w / h) if w > 0 and h > 0 else 0.0


def classify(w: Any, h: Any) -> str:
    """把实际尺寸归到最近的标准画幅名；差得太远就如实写 `other:WxH`。"""
    r = ratio_of(w, h)
    if r <= 0:
        return "unknown"
    best, bestd = None, 1e9
    for name, v in _NAMED.items():
        d = abs(r - v) / v
        if d < bestd:
            best, bestd = name, d
    if best is not None and bestd <= ASPECT_TOL:
        return best
    return f"other:{int(w)}x{int(h)}"


def deviation(w: Any, h: Any, want: Any) -> float:
    """实际画幅相对目标画幅的**相对偏差**（0 = 完全一致）。"""
    wr = parse_aspect(want)
    r = ratio_of(w, h)
    if not wr or r <= 0:
        return 1.0
    return abs(r - wr) / wr


def is_match(w: Any, h: Any, want: Any, tol: float = ASPECT_TOL) -> bool:
    return deviation(w, h, want) <= tol


def crop_loss(w: Any, h: Any, want: Any) -> float:
    """把 WxH 铺满目标画幅（`force_original_aspect_ratio=increase` + 居中裁切）
    会丢掉**多少比例的画面**。0.44 = 丢掉 44%。

    这就是"画幅不符"的真实代价，也是本模块存在的理由。
    """
    wr = parse_aspect(want)
    r = ratio_of(w, h)
    if not wr or r <= 0:
        return 0.0
    if r > wr:
        # 太宽 → 裁左右
        return 1.0 - (wr / r)
    if r < wr:
        # 太高 → 裁上下
        return 1.0 - (r / wr)
    return 0.0


def _data_dir() -> str:
    return os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()


def ledger_path() -> str:
    """实测台账文件的位置（可用 `VIDEOFORGE_ASPECT_LEDGER` 覆盖，测试用）。"""
    env = os.environ.get("VIDEOFORGE_ASPECT_LEDGER")
    if env:
        return env
    return os.path.join(_data_dir(), "cache", "aspect_ledger.json")


_LEDGER_CACHE: Optional[Dict[str, Any]] = None


def load_ledger(force: bool = False) -> Dict[str, Any]:
    """读"本机实测台账"：`厂商|型号|分辨率 → 实际画幅`。

    为什么要有台账：手写实测表只能覆盖我亲自试过的组合，而用户的模型列表
    远不止这些。每次真实生成都量一次尺寸并记账，**下次规划就能直接用事实**，
    不用等我把表补全 —— 也不会拿没验证过的映射去拦用户。
    """
    global _LEDGER_CACHE
    if _LEDGER_CACHE is not None and not force:
        return _LEDGER_CACHE
    try:
        # ★ 用 `utf-8-sig`：手工/脚本写的台账可能带 BOM，
        #   而 `json.load(encoding="utf-8")` 遇到 BOM 会抛
        #   "Unexpected UTF-8 BOM" —— 再被 except 一吞就成了"台账静默失效"
        #   （时长台账那边实测踩过同款）。
        with open(ledger_path(), "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        _LEDGER_CACHE = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        _LEDGER_CACHE = {}
    except Exception as e:
        logger.warning("画幅台账读取失败（本次只用实测表）：%s: %s",
                       type(e).__name__, e)
        _LEDGER_CACHE = {}
    return _LEDGER_CACHE


def _ledger_key(provider: str, model: str, resolution: str) -> str:
    return f"{str(provider or '').lower()}|{str(model or '')}|{str(resolution or '').upper()}"


def record_measurement(provider: str, model: str, resolution: str,
                       w: Any, h: Any) -> Optional[str]:
    """记一次真实测量：这个 (厂商, 型号, 分辨率) 实际出了多大、什么画幅。

    返回量到的画幅名（量不出尺寸返回 None，不记空账）。
    写盘失败**不能影响生成** —— 台账是加速器，不是关键路径。
    """
    if not provider or not model or not resolution:
        return None
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    asp = classify(w, h)
    try:
        led = dict(load_ledger())
        k = _ledger_key(provider, model, resolution)
        old = led.get(k) or {}
        led[k] = {"aspect": asp, "w": w, "h": h,
                  "n": int(old.get("n") or 0) + 1,
                  "at": int(__import__("time").time())}
        p = ledger_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(led, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        global _LEDGER_CACHE
        _LEDGER_CACHE = led
    except Exception:
        pass
    return asp


def observed_aspect(provider: str, model: str, resolution: str) -> Optional[str]:
    """本机实测台账里的画幅（比手写表更可信，因为是在这台机器上真跑出来的）。"""
    led = load_ledger()
    ent = led.get(_ledger_key(provider, model, resolution)) or {}
    return ent.get("aspect") or None


# ── 「首帧说了算」的型号实测名单 ────────────────────────────────────
# 实测模型（2026-09-13，同一型号/同一 1080P/同一提示词，只换首帧）：
#   海螺 MiniMax-Hailuo-2.3：方形首帧 → 1080x1080；16:9 首帧 → 1920x1080
# 由此得到一条能同时解释全部历史数据的规则：
#   **短边 = 分辨率高度；画幅 = 首帧画幅**（没有首帧时才用该型号的默认画幅）。
# 这条规则很值钱：它意味着"有首帧的镜头，降分辨率不会改变画幅"，
# 于是长镜头可以安心走便宜的 768P/10s 而不用担心变成方形。
# 名单**必须逐个实测后**才能加 —— 没验证过的型号一律不享受这个豁免。
FIRST_FRAME_GOVERNS: Dict[Tuple[str, str], str] = {
    ("hailuo", "MiniMax-Hailuo-2.3"):
        "2026-09-13 实测：同型号同 1080P，方形首帧→1080x1080，16:9 首帧→1920x1080",
}


def first_frame_governs(provider: str, model: str) -> bool:
    """这个型号在有首帧时，画幅是否**由首帧决定**（而不是由分辨率决定）？

    只有实测过的型号返回 True；其余一律 False（保守：宁可多花一次调用，
    也不赌一个没验证过的行为）。
    """
    return (str(provider or "").lower(), str(model or "")) in FIRST_FRAME_GOVERNS


def expected_aspect(provider: str, model: str, resolution: str) -> Optional[str]:
    """这个 (厂商, 型号, 分辨率) 会出什么画幅？

    优先级：**本机实测台账 > 手写实测表 > 不知道（None）**。
    没实测过一律返回 None —— 宁可说不知道，也不猜一个画幅去拦用户。
    ⚠ 表里的值都是**无首帧（文生视频）**时实测的；有首帧时画幅由首帧决定
      （见 `FIRST_FRAME_GOVERNS`），此时应先 `conform_image()` 再请求。
    """
    got = observed_aspect(provider, model, resolution)
    if got:
        return got
    table = RES_ASPECT.get((str(provider or "").lower(), str(model or "")))
    if not table:
        return None
    return table.get(str(resolution or "").upper())


def resolutions_matching_aspect(provider: str, model: str, want: Any,
                                caps: Optional[Dict[str, Any]] = None) -> list:
    """挑出画幅与 want 相符的分辨率（手写表 + 本机台账，按 caps 的顺序）。

    没有实测依据时返回 []：宁可不给建议，也不给一个没验证过的分辨率建议。
    """
    wr = parse_aspect(want)
    if not wr:
        return []
    table = RES_ASPECT.get((str(provider or "").lower(), str(model or ""))) or {}
    if caps:
        pool = list(caps.keys())
    else:
        # 没给 caps 时，手写表 + 本机台账里出现过的分辨率都算候选
        pool = list(table.keys())
        prefix = f"{str(provider or '').lower()}|{str(model or '')}|"
        for k in load_ledger():
            if k.startswith(prefix):
                r = k[len(prefix):]
                if r not in pool:
                    pool.append(r)
    out = []
    for res in pool:
        got = expected_aspect(provider, model, res)
        if not got:
            continue
        v = parse_aspect(got)
        if v and abs(v - wr) / wr <= ASPECT_TOL:
            out.append(res)
    return out


def resolution_conflicts_aspect(provider: str, model: str, resolution: str,
                                want: Any) -> Optional[str]:
    """这个分辨率会不会出一张画幅不符的片？会 → 返回实际画幅名，否则 None。"""
    asp = expected_aspect(provider, model, resolution)
    if not asp:
        return None
    v, wr = parse_aspect(asp), parse_aspect(want)
    if not v or not wr:
        return None
    if abs(v - wr) / wr > ASPECT_TOL:
        return asp
    return None


# ═══════════════════════════════════════════════════════════════
# 实测（碰磁盘）
# ═══════════════════════════════════════════════════════════════

def probe_size(path: str, timeout: int = 120) -> Tuple[int, int]:
    """量视频/图片的**真实**像素尺寸。读不出来返回 (0, 0) —— 不猜。"""
    if not path or not os.path.exists(path):
        return (0, 0)
    try:
        p = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception:
        return (0, 0)
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", p.stderr or "")
    if not m:
        m = re.search(r"Stream #0:0.*?,\s*(\d{2,5})x(\d{2,5})", p.stderr or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def conform_image(src: str, want_aspect: Any, dst: str = "", *,
                  max_long: int = 1280, tol: float = ASPECT_TOL) -> str:
    """把首帧图**裁成**目标画幅；本来就是这个画幅就原样返回（不重编码）。

    ★★ 为什么必须做这一步（第二个真实事故）：
      项目里 10 张场景参考图全是 **1024×1024（1:1）**，而分镜画幅是 16:9。
      生成时场景图被当 `first_frame_image` 传给海螺，海螺**跟随首帧画幅**出片
      —— 于是同一模型同一分辨率，有的镜头出 1280×720（16:9，没传首帧）、
      有的出 720×720 / 768×768 / 1080×1080（1:1，传了方形首帧）。
      这不是"模型不听话"，是**我们喂了一张方图**。

    做法：cover 裁切（等比放大到盖满目标画幅再居中裁），不拉伸、不留黑边。
    代价（裁掉多少画面）由 `crop_loss()` 如实算出，由调用方决定要不要提示用户。
    """
    if not src or not os.path.exists(src):
        return src
    wr = parse_aspect(want_aspect)
    if not wr:
        return src
    w, h = probe_size(src)
    if not w or not h:
        return src
    if deviation(w, h, want_aspect) <= tol:
        return src                      # 已经对了，不要白白重编码

    if wr >= 1.0:                        # 横画幅：以高为准
        out_h = int(min(h, max_long / wr))
        out_w = int(round(out_h * wr))
    else:                                # 竖画幅：以宽为准
        out_w = int(min(w, max_long))
        out_h = int(round(out_w / wr))
    out_w -= out_w % 2                   # yuv420 要求偶数
    out_h -= out_h % 2
    if out_w < 16 or out_h < 16:
        return src

    target = dst or os.path.join(
        os.path.dirname(src) or ".",
        f"{os.path.splitext(os.path.basename(src))[0]}_conform_"
        f"{str(want_aspect).replace(':', 'x')}.jpg")
    try:
        p = subprocess.run(
            [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
             "-vf", f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                    f"crop={out_w}:{out_h}",
             "-frames:v", "1", "-q:v", "2", target],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        if os.path.exists(target) and os.path.getsize(target) > 1024:
            return target
        _ = p
    except Exception:
        pass
    return src


def guard_clip(path: str, want_aspect: Any, *, provider: str = "", model: str = "",
               resolution: str = "", tol: float = ASPECT_TOL,
               record: bool = True) -> Dict[str, Any]:
    """验一个刚下载/刚生成的片段：它的画幅和我们要的是不是一个。
    返回（**所有字段都可直接展示给用户**，不含猜测）：
      ok          画幅相符（或量不出来 —— 那不该当成"通过"，见 measured 字段）
      measured    是否真的量到了尺寸
      w/h/actual  实测宽高与画幅名
      deviation   相对偏差
      crop_loss   若强行铺满成片画幅会丢多少画面
      severity    ok | warn | fatal | unknown
      message     中文说明（可直接进告警列表）
      suggest     画幅相符的替代分辨率（仅来自实测；可能为空）

    量到尺寸时会顺手记进实测台账（`record=False` 可关，测试用），
    这样"这个模型在这个分辨率下出什么画幅"下回就不用再猜。
    """
    w, h = probe_size(path)
    if not w or not h:
        return {"ok": True, "measured": False, "w": 0, "h": 0,
                "actual": "unknown", "want": str(want_aspect or ""),
                "deviation": 0.0, "crop_loss": 0.0, "severity": "unknown",
                "message": f"量不出这个片段的尺寸（{os.path.basename(path)}）——"
                           f"无法确认画幅是否与分镜一致",
                "suggest": []}
    actual = classify(w, h)
    if record:
        try:
            record_measurement(provider, model, resolution, w, h)
        except Exception:
            pass
    loss = crop_loss(w, h, want_aspect)
    dev = deviation(w, h, want_aspect)
    suggest = resolutions_matching_aspect(provider, model, want_aspect)
    ok = dev <= tol
    if ok:
        sev, msg = "ok", ""
    else:
        sev = "fatal" if dev > ASPECT_FATAL else "warn"
        msg = (f"画幅不符：分镜要 {want_aspect}，实际出的是 {w}x{h}（{actual}）；"
               f"铺满成片会裁掉 {loss * 100:.0f}% 的画面")
        if resolution:
            msg += f"（请求分辨率 {resolution}）"
        if suggest:
            msg += f"。该模型画幅相符的分辨率：{'、'.join(suggest)}"
        else:
            msg += "。该模型的分辨率→画幅映射尚未实测，无法自动给出替代分辨率"
    return {"ok": ok, "measured": True, "w": w, "h": h, "actual": actual,
            "want": str(want_aspect or ""), "deviation": round(dev, 4),
            "crop_loss": round(loss, 4), "severity": sev,
            "message": msg, "suggest": suggest}
