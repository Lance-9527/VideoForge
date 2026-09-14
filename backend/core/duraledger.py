"""时长台账：把「要了多久 / 实际拿到多久」记下来，供规划层使用。

═══════════════════════════════════════════════════════════════════
为什么要有它（这一类病已经栽过三次）
═══════════════════════════════════════════════════════════════════
厂商**接受**请求（`status_code=0`）却给你一个更短的片子，这事在本项目里发生过多次：
  · Hailuo 2.3 / 768P 要 20 秒 → 两段各 10s，实际各 10.13s（这个正常）
  · `video-01` 要 1080P/10s → 回来 **1280×720 / 5.64s**（分辨率、时长**双忽略**）
  · 老版本分镜标称 10s → 实际只给 5.6s，而库里/时间轴/字幕全按 10s 排
后果不是"少一点画面"，而是**成片缺内容**：时间轴按 10 秒排、画面只有 5.6 秒，
要么留黑、要么循环重播（§18.7 那个"卡带感"）。

手写能力表追不上这件事（每个型号、每一档分辨率、每个厂商版本都可能不一样，
而且厂商会**悄悄改**）。所以照 `aspect_ledger.json` 的做法：
**每次真实生成都量一次实际时长并记账**，规划层直接用实测值。

判定规则（保守，宁可不动也不误伤）：
  · 同一 (厂商, 型号, 分辨率) 至少 **2 次**观测才生效；
  · 且观测到的**最大实际时长**明显低于请求值（< 请求 × 0.8）才把那一档判为"发不出"；
  · 只影响"规划用哪些时长档"，**不改写静态能力表**（表是文档，台账是事实）。
═══════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.duraledger")

# 观测到"实际比请求短"到这个比例以下，才认为厂商确实发不出这个时长
SHORTFALL_RATIO = 0.80
MIN_OBSERVATIONS = 2

_CACHE: Optional[Dict[str, Any]] = None


def _data_dir() -> str:
    return os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()


def ledger_path() -> str:
    env = os.environ.get("VIDEOFORGE_DURATION_LEDGER")
    if env:
        return env
    return os.path.join(_data_dir(), "cache", "duration_ledger.json")


def _key(provider: str, model: str, resolution: str) -> str:
    return (f"{str(provider or '').lower()}|{str(model or '')}"
            f"|{str(resolution or '').upper()}")


def load(force: bool = False) -> Dict[str, Any]:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    if os.environ.get("VIDEOFORGE_NO_DURALEDGER"):
        _CACHE = {}
        return _CACHE
    try:
        # ★ 必须用 `utf-8-sig`：实测有人（含我自己用 PowerShell 写的测试夹具）
        #   会把文件写成**带 BOM** 的 UTF-8，而 `json.load(encoding="utf-8")`
        #   遇到 BOM 直接抛 "Unexpected UTF-8 BOM" ——
        #   再被下面的 except 一吞，表现就是"台账明明有内容却完全不起作用"。
        with open(ledger_path(), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        _CACHE = d if isinstance(d, dict) else {}
    except FileNotFoundError:
        _CACHE = {}
    except Exception as e:
        # 不静默：台账读不了是**功能失效**，要能在日志里看见
        logger.warning("时长台账读取失败（本次按静态能力表规划）：%s: %s",
                       type(e).__name__, e)
        _CACHE = {}
    return _CACHE


def record(provider: str, model: str, resolution: str,
           requested: Any, actual: Any) -> Optional[dict]:
    """记一次真实生成：这个组合要了多久、实际给了多久。

    写盘失败不影响生成（台账是加速器，不是关键路径）。
    """
    try:
        rq, ac = float(requested or 0), float(actual or 0)
    except (TypeError, ValueError):
        return None
    if rq <= 0 or ac <= 0:
        return None
    ent: Dict[str, Any] = {}
    try:
        led = dict(load())
        k = _key(provider, model, resolution)
        old = led.get(k) or {}
        obs: List[List[float]] = list(old.get("obs") or [])
        obs.append([round(rq, 2), round(ac, 2)])
        obs = obs[-20:]
        ent = {"obs": obs, "n": len(obs),
               "max_actual": max(o[1] for o in obs),
               "last_at": int(time.time())}
        led[k] = ent
        p = ledger_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(led, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        global _CACHE
        _CACHE = led
    except Exception:
        pass
    return ent


def unreliable_durations(provider: str, model: str, resolution: str,
                         durations: List[int]) -> List[int]:
    """在这一档分辨率下，**实测发不出**的时长档。

    判据用「**这一档的天花板**」而不是"逐档去数观测"：
      同一分辨率下观测 ≥`MIN_OBSERVATIONS` 次，且**历史上拿到过的最长片子**
      都明显短于某个时长档 → 那一档就是发不出的。
    这样更稳，也更贴事实：
      · 海螺 02 / 768P：观测过 6s→5.88、10s→10.13 → 天花板 10.13 →
        10s 档 10 < 10.13/0.8? 不成立 → **保留**（健康，别误伤）。
      · video-01 / 1080P：观测 6s→5.64、10s→5.64 → 天花板 5.64 →
        10s 档 10 < 5.64/0.8=7.05 → **裁掉**；6s 档 6 < 7.05 不成立 → 保留。
    """
    led = load()
    ent = led.get(_key(provider, model, resolution))
    if not ent or int(ent.get("n") or 0) < MIN_OBSERVATIONS:
        return []
    obs = [(float(a), float(b)) for a, b in (ent.get("obs") or [])]
    # 去掉明显异常的观测（实际比请求长很多 —— 多半是量错了）
    obs = [(rq, ac) for rq, ac in obs if ac <= rq * 1.5 + 1.0]
    if len(obs) < MIN_OBSERVATIONS:
        return []
    ceiling = max(ac for _rq, ac in obs)
    return [int(d) for d in (durations or [])
            if float(d) > ceiling / SHORTFALL_RATIO]


def filter_caps(caps: Dict[str, List[int]], provider: str, model: str
                ) -> Tuple[Dict[str, List[int]], List[str]]:
    """按台账裁掉"实测发不出"的时长档，并给出**人话说明**。

    返回 `(新 caps, 说明列表)`。没观测过、或观测正常 → 原样返回（不猜）。
    """
    if not caps:
        return caps, []
    out: Dict[str, List[int]] = {}
    notes: List[str] = []
    for res, durs in caps.items():
        if os.environ.get("VIDEOFORGE_NO_DURALEDGER"):
            out[res] = list(durs)
            continue
        bad = unreliable_durations(provider, model, str(res), list(durs))
        keep = [int(d) for d in durs if int(d) not in bad]
        out[res] = keep or [int(d) for d in durs]
        if bad and keep:
            led = load()
            ent = led.get(_key(provider, model, str(res))) or {}
            mx = ent.get("max_actual")
            notes.append(
                f"「{model}」在 {res} 下**实测**：要 "
                f"{'/'.join(str(b) + 's' for b in bad)} 实际只给约 {mx}s —— "
                f"已按 {'/'.join(str(k) + 's' for k in keep)} 规划"
                f"（要更长请换型号，或接受分成多段）。")
    return out, notes
