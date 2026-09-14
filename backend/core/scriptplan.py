# -*- coding: utf-8 -*-
"""VideoForge · 剧本时长强制归一化（"我选了 5 秒，结果给我 60 秒"）

★★ 用户实测反馈（2026-09-14）：
    「主页这块的总时长可以选择了，比如我选的 5 秒，但我生成剧本的时候发现
      没按我的要求来，居然给了 60 秒」

查真实数据后确认，这不是"AI 不听话"这么简单，而是**两处独立的断点**：

  ① **选择根本没存下来**。主页 `createFromHero` 把 5 秒发给了这一次生成请求，
     但**没有写进项目设置** `default_total_duration`。而剧本页的「💡 想法生成大纲 /
     重新生成」走的是 `settings.default_total_duration || 60` ——
     实测项目 `37f40d84` 的 settings 里**没有这个键**，于是任何一次重新生成
     都变成 60 秒。用户看到的 5 场 × 12 秒 = 60 秒，正是 `total=60` 的产物。

  ② **时长只是"请求"，没有强制**。`ScriptService.generate_script` 把
     "总时长：N 秒 / 各场秒数之和必须等于 N 秒"写进提示词就结束了 ——
     模型吐回 5×12 秒，我们就照单全收。**提示词不是保证**（这正是参考库里
     `F-*` 那套失败清单反复强调的事）。

所以本模块做**服务端强制**：场次数由**我们**决定，每场秒数由**我们**分配，
总和必须等于用户要的秒数；做不到的（例如 5 秒放不下 5 场）**如实报告被裁掉的场次**，
绝不静默改数。

纯函数，不碰数据库、不碰网络。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional


logger = logging.getLogger("videoforge.scriptplan")


# 一场戏短于 3 秒连一句台词都说不完（中文约 4 字/秒），所以这是硬下限。
MIN_SCENE_SECONDS = 3
# 短视频的常见节奏：一场 10-15 秒（也是 VideoForge 一直以来的默认值）。
PREFERRED_SCENE_SECONDS = 12
# 单次剧本生成真能吐完 JSON 的场次上限（原实现也是 24）。
SCENE_CAP = 24


def scene_count_for(total: int, *, min_scene: int = MIN_SCENE_SECONDS,
                    preferred: int = PREFERRED_SCENE_SECONDS,
                    cap: int = SCENE_CAP) -> int:
    """**我们**决定这个时长该写几场戏（不把这个决定权交给 LLM）。

    例：5 秒 → 1 场；60 秒 → 5 场；90 秒 → 8 场；30 分钟 → 24 场（封顶）。
    """
    try:
        total = int(total)
    except Exception:
        total = 0
    total = max(int(min_scene), total)
    n = int(round(total / float(max(1, preferred))))
    n = max(1, n)
    # 每场不能短于下限：5 秒最多 1 场，7 秒最多 2 场
    n = min(n, max(1, total // int(min_scene)))
    return max(1, min(int(cap), n))


def distribute(total: int, n: int) -> List[int]:
    """把 `total` 秒**整数**分摊到 n 场，和恰好等于 total（余数给前几场）。"""
    total, n = int(total), max(1, int(n))
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def _dur(v: Any) -> Optional[int]:
    try:
        f = float(v)
    except Exception:
        return None
    if f <= 0:
        return None
    return int(round(f))


def fit_scenes(scenes: Optional[List[Dict[str, Any]]], total: int,
               *, min_scene: int = MIN_SCENE_SECONDS,
               preferred: int = PREFERRED_SCENE_SECONDS,
               cap: int = SCENE_CAP) -> Dict[str, Any]:
    """把 LLM 给的场次**强制**排成"总时长 = 目标时长"，并如实报告改了什么。

    返回：
      `{"scenes": [...], "target", "actual", "scene_count", "planned_scene_count",
        "dropped": [{"title","duration_seconds","reason"}], "changed": bool,
        "before_total": n, "note": "..."}`

    规则：
      · 场次数上限 = `scene_count_for(total)`（5 秒 → 1 场）；
      · 超出的场次**不静默丢**：列进 `dropped` 并给出原因（时长放不下）；
      · 每场秒数按目标重新分配，和 = 目标；LLM 原来写的秒数只作为"它的意图"记在
        `before` 里，方便人对照；
      · LLM 给的场次**少于**规划时**不硬造**场次（造出来的场次没有内容），
        只把时长摊到现有的场次上，并在 `note` 里说明。
    """
    src = [s for s in (scenes or []) if isinstance(s, dict)]
    total = int(total)
    want_n = scene_count_for(total, min_scene=min_scene, preferred=preferred, cap=cap)
    before_total = 0
    for s in src:
        d = _dur(s.get("duration_seconds"))
        if d:
            before_total += d

    kept = [dict(s) for s in src[:want_n]]
    dropped_src = src[want_n:]
    n = len(kept)
    notes: List[str] = []

    if n == 0:
        return {"scenes": [], "target": total, "actual": 0, "scene_count": 0,
                "planned_scene_count": want_n,
                "dropped": [], "changed": False, "before_total": before_total,
                "note": "剧本没有场次，无法按时长重排。"}

    durs = distribute(total, n)
    for i, s in enumerate(kept):
        before = _dur(s.get("duration_seconds"))
        if before is not None and before != durs[i]:
            s["duration_seconds_before_fit"] = before
        s["duration_seconds"] = durs[i]

    dropped = []
    for s in dropped_src:
        dropped.append({"title": str(s.get("title") or f"第{s.get('scene_number')}场"),
                        "duration_seconds": _dur(s.get("duration_seconds")),
                        "reason": (f"总时长只有 {total} 秒，按每场至少 {min_scene} 秒"
                                   f"最多容得下 {want_n} 场，这一场没排进来")})

    if dropped:
        notes.append(
            f"你要的是 {total} 秒，最多排 {want_n} 场（每场至少 {min_scene} 秒）；"
            f"AI 写了 {len(src)} 场，已只保留前 {n} 场、并把秒数改成 "
            f"{'/'.join(str(d) for d in durs)} 秒；"
            f"被裁掉的 {len(dropped)} 场："
            + "、".join(f"「{d['title']}」" for d in dropped[:6])
            + ("…" if len(dropped) > 6 else "")
            + "。想保留它们就把总时长调大，或让 AI 减少场次重新生成。")
    if n < want_n:
        notes.append(
            f"AI 只写了 {n} 场（按时长本可以排 {want_n} 场）—— 没有凭空造场次，"
            f"而是把 {total} 秒摊到现有场次上（每场 "
            f"{'/'.join(str(d) for d in durs)} 秒）。")
    if not notes:
        notes.append(f"已按 {total} 秒重排：{n} 场，每场 "
                     f"{'/'.join(str(d) for d in durs)} 秒（合计 {sum(durs)} 秒）。")

    changed = bool(dropped) or any(
        _dur(src[i].get("duration_seconds")) != durs[i] for i in range(n))
    return {"scenes": kept, "target": total, "actual": sum(durs),
            "scene_count": n, "planned_scene_count": want_n,
            "dropped": dropped, "changed": changed, "before_total": before_total,
            "note": " ".join(notes)}


def duration_brief(fit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """给接口/界面用的精简版（不把整份场景再传一遍）。"""
    f = fit or {}
    return {"target": f.get("target"), "actual": f.get("actual"),
            "scene_count": f.get("scene_count"),
            "planned_scene_count": f.get("planned_scene_count"),
            "before_total": f.get("before_total"),
            "dropped": f.get("dropped") or [], "changed": bool(f.get("changed")),
            "note": f.get("note") or "",
            "ok": (f.get("target") is not None and f.get("actual") == f.get("target"))}
