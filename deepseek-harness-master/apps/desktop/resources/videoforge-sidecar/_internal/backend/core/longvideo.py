"""
VideoForge · 长视频分段生成

════════════════════════════════════════════════════════════════════════
为什么需要这个模块
════════════════════════════════════════════════════════════════════════
用户的原话是"目前受视频模型 API 限制（1-12s 到顶了）"。**这个前提是错的**：

  · 即梦/Seedance 2.5 —— 单次 4~30s，且官方有超长模式
  · 可灵 3.0        —— 单次 3~15s，`multi_shot` 一次调用可出 6 个镜头
  · Runway `wan3`/`seedance2_5` —— 2~30s

但**分钟级（1~5 分钟）确实没有任何模型能一次出**。所以真正的结论不是
"一段一段硬拼"，而是分三档（来源见 docs/AGENT-改造方案.md 第 6 节）：

  A. `single`   —— 目标时长 ≤ 模型上限：**一次调用出完，不切**。
                   这是最容易被忽略的一档：很多人明明只要 12 秒，
                   却按 5 秒切成 3 段去拼，白白多两次接缝。

  B. `chained`  —— 目标时长 ≤ 上限 × CHAIN_MAX_SEGMENTS（默认 3）：
                   首尾帧续接。上一段的**最后一帧**当下一段的首帧
                   （Seedance 有官方钩子 `return_last_frame`，能给就不必抽帧）。

  C. `anchored` —— 更长：**按分镜切点断开，每段重新用参考图锚定身份**。
                   为什么不用 B 一路续到底：每一段都是有损重建，
                   研究报告实测 12 段之后主体与色调必然漂移
                   （"退化累积"）。在镜头切点断开重锚，退化不会跨镜累积。

`anchored` 里要不要续接由调用方决定（`anchor_mode`）：
  · `shot`   —— 默认。段与段是**不同的分镜**，本来就该是硬切，
                续接反而会强行把两场戏缝成一个长镜头。直接锚定。
  · `chained`—— 同一场戏内为了动作连贯而续接，但每段仍带参考图。
                属于折中，用于"一镜到底"式的段落。

════════════════════════════════════════════════════════════════════════
排片的硬约束（踩过的坑，别改）
════════════════════════════════════════════════════════════════════════
1. **每段时长必须落在 [min_duration, max_duration] 内**。模型对 duration
   是强校验（Seedance 写在 body 顶层，可灵在 settings 里），给个 3 秒而模型
   最小 4 秒 → 直接被平台拒，白跑一趟。所以均分之后要往合法区间里收。
2. **不能平均分成一堆 2 秒的碎段**。碎段接缝多、每段还有各自的片头沉降
   （seamfix 就是为了处理这个）。宁可段数少、每段长。
3. **各段时长之和要等于目标时长**（最后一段吸收余数），否则成片长度不对，
   用户按 60 秒排的字幕/配音全错位。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("videoforge.longvideo")

# ── 策略常量 ────────────────────────────────────────────────────────────

STRATEGY_SINGLE = "single"      # 一次出完
STRATEGY_CHAINED = "chained"    # 首尾帧续接
STRATEGY_ANCHORED = "anchored"  # 分镜切点重锚

# 续接最多几段。研究结论：超过这个数就进入"退化累积"区间，收益变负。
CHAIN_MAX_SEGMENTS = 3

# 单段秒数下限。低于这个值接缝成本 > 收益，不如减少段数。
MIN_USEFUL_SEGMENT = 4.0


# ──────────────────────── 排片 ────────────────────────


def choose_strategy(total_duration: float, max_duration: float) -> str:
    """按目标时长与模型上限挑策略。纯函数，便于单测。"""
    if total_duration <= 0 or max_duration <= 0:
        return STRATEGY_SINGLE
    if total_duration <= max_duration:
        return STRATEGY_SINGLE
    n = _segment_count(total_duration, max_duration)
    return STRATEGY_CHAINED if n <= CHAIN_MAX_SEGMENTS else STRATEGY_ANCHORED


def _segment_count(total_duration: float, max_duration: float) -> int:
    """要几段。段数尽可能少（每多一段就多一个接缝、多一次调用）。"""
    if max_duration <= 0:
        return 1
    import math
    return max(1, int(math.ceil(total_duration / max_duration)))


def plan_segments(total_duration: float, min_duration: float, max_duration: float,
                  strategy: str, *, tolerance: float = 0.05) -> List[Dict[str, Any]]:
    """把总时长排成若干段，返回 [{index, start, duration}]。

    规则：
      · 段数取"用满上限时所需的最少段数"，再检查平均时长会不会掉到
        MIN_USEFUL_SEGMENT 以下 —— 会的话就减少段数（宁可超一点上限，
        由调用方决定是拒还是降级，也不能排出一堆碎段）。
      · 各段时长之和 == total_duration（最后一段吸收余数）。
      · 每段都夹在 [min_duration, max_duration] 内；实在夹不住就返回 [],
        让调用方拿到明确失败原因，而不是发一个平台会拒的请求出去。
    """
    if total_duration <= 0:
        return []
    if min_duration <= 0:
        min_duration = 1.0
    if max_duration < min_duration:
        return []

    if strategy == STRATEGY_SINGLE or total_duration <= max_duration:
        d = total_duration
        if d < min_duration - tolerance:
            # 比模型下限还短 —— 只能按模型下限生成，然后由后期裁到目标时长。
            # 这不是错误（短于下限的请求平台会拒，所以只能这样）。
            return [{"index": 0, "start": 0.0, "duration": float(min_duration),
                     "clamped": True, "reason": f"目标 {d:g}s 短于模型下限 {min_duration:g}s，按 {min_duration:g}s 生成后裁切"}]
        return [{"index": 0, "start": 0.0, "duration": float(min(max_duration, d))}]

    n = _segment_count(total_duration, max_duration)
    # 想减段数（段少了接缝少），但**不能减到单段超上限** —— 那会让每段请求
    # 都非法。所以这里两个条件同时满足才减：
    #   · 当前平均段长还不到 MIN_USEFUL_SEGMENT（碎段，该减）
    #   · 减一段之后平均段长仍然 ≤ max_duration（减了还合法）
    # 20s / 上限 3s 就是"减不动"的例子：7 段 × 2.86s 已经是唯一合法解，
    # 再减单段就超 3s 了。这种情况只能出碎段，并记一条日志说明。
    while (n > 1
           and (total_duration / n) < MIN_USEFUL_SEGMENT
           and (total_duration / (n - 1)) <= max_duration):
        n -= 1

    avg = total_duration / n
    if avg < MIN_USEFUL_SEGMENT:
        logger.info("排片无法避免碎段：总 %.1fs / 上限 %.1fs → 必须 %d 段，平均仅 %.2fs"
                    "（模型上限对这段目标时长来说太小）",
                    total_duration, max_duration, n, avg)

    if avg > max_duration + tolerance:
        # 段数被压到太少，单段会超上限 —— 只能加回去
        n = _segment_count(total_duration, max_duration)
        avg = total_duration / n

    if avg < min_duration - tolerance or avg > max_duration + tolerance:
        logger.warning("排片失败：总 %.1fs，%d 段，平均 %.2fs，合法区间 [%.1f, %.1f]",
                       total_duration, n, avg, min_duration, max_duration)
        return []

    out: List[Dict[str, Any]] = []
    remaining = float(total_duration)
    start = 0.0
    for i in range(n):
        if i == n - 1:
            dur = remaining                      # 最后一段吸收余数，保证总长精确
        else:
            dur = round(total_duration / n, 3)
            # 别让某一段掉到下限以下
            if dur < min_duration:
                dur = float(min_duration)
            if dur > max_duration:
                dur = float(max_duration)
            if remaining - dur < min_duration:
                dur = max(min_duration, remaining - min_duration)
        out.append({"index": i, "start": round(start, 3), "duration": round(dur, 3)})
        start += dur
        remaining -= dur

    # 数值兜底：浮点累积可能差几毫秒
    drift = round(total_duration - sum(s["duration"] for s in out), 3)
    if abs(drift) > 1e-6 and out:
        out[-1]["duration"] = round(out[-1]["duration"] + drift, 3)
    return out


# ──────────────────────── 分段说明文字 ────────────────────────


def segment_note(index: int, total: int, duration: float, start: float) -> str:
    """给单段的补充说明，追加到 prompt 末尾。

    写清楚"这是第几段、覆盖哪段时间"，模型才不会把整场戏在 10 秒里演完。
    """
    if total <= 1:
        return ""
    return (
        f"\n\n【分段提示】这是同一个镜头的第 {index + 1}/{total} 段，"
        f"覆盖第 {start:.1f}–{start + duration:.1f} 秒。"
        f"只演这 {duration:.1f} 秒里的内容，"
        f"不要把整场戏压缩演完，也不要在结尾加收束/黑场。"
    )


# ──────────────────────── 抽尾帧 ────────────────────────


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


def grab_last_frame(video_path: str, out_path: str, *, timeout: float = 180.0) -> str:
    """抽出视频最后一帧存成 jpg，返回路径；失败返回空串。

    为什么要自己抽：Seedance 有官方 `return_last_frame` 钩子（能拿就用那个），
    但可灵、万相、Runway 有的不给，而续接又必须要这一帧。本地抽帧免费且确定。

    `-sseof -0.1` 从"距结尾 0.1 秒"处定位，比 `-sseof -1` 更贴近真正的最后一帧。
    """
    if not video_path or not os.path.exists(video_path):
        return ""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(), "-y", "-v", "error",
        "-sseof", "-0.1", "-i", video_path,
        "-vsync", "0", "-q:v", "2", "-frames:v", "1",
        out_path,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if p.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        # 退一步：有些封装 `-sseof` 定位不到，直接取第一帧（至少不空手）
        cmd2 = [_ffmpeg(), "-y", "-v", "error", "-i", video_path,
                "-q:v", "2", "-frames:v", "1", out_path]
        p2 = subprocess.run(cmd2, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
        if p2.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            logger.info("抽尾帧回退到首帧：%s", video_path)
            return out_path
        # ★ 这里只记 debug：断点续接是**可选**能力，抽不到就退化成不续接，
        #   不该在正常流程里刷 warning（之前每次失败打两行，一个 12 段的片子
        #   就是 24 行噪声，把真正的错误淹掉了）。
        logger.debug("抽尾帧失败 rc=%s err=%s", p.returncode, (p.stderr or "")[:200])
        return ""
    except Exception as e:
        logger.debug("抽尾帧异常 %s: %s", video_path, e)
        return ""


# ──────────────────────── 主流程 ────────────────────────


def _validate_plan(plan: List[Dict[str, Any]], total_duration: float,
                   min_duration: float, max_duration: float) -> Optional[str]:
    """排片自检。宁可在这里拒绝，也不要发一个平台注定拒绝的请求。"""
    if not plan:
        return (f"无法把 {total_duration:g} 秒排成合法分段"
                f"（模型单段范围 {min_duration:g}–{max_duration:g} 秒）")
    for s in plan:
        if s["duration"] < min_duration - 1e-6 and not s.get("clamped"):
            return f"第 {s['index']+1} 段只有 {s['duration']:g} 秒，低于模型下限 {min_duration:g} 秒"
        if s["duration"] > max_duration + 1e-6 and not s.get("clamped"):
            return f"第 {s['index']+1} 段有 {s['duration']:g} 秒，超过模型上限 {max_duration:g} 秒"
    # 有 clamped 段时总长**故意**不等于目标（按模型下限生成、后期再裁），
    # 所以只在没有 clamped 的常规路径上校验总长守恒。
    if not any(s.get("clamped") for s in plan):
        ssum = sum(s["duration"] for s in plan)
        if abs(ssum - total_duration) > 0.1:
            return f"分段总长 {ssum:g} 秒与目标 {total_duration:g} 秒不符"
    return None


async def generate_long_video(
    shot: Dict[str, Any],
    adapter,
    *,
    db=None,
    out_dir: str = "",
    strategy: str = "",
    anchor_mode: str = "shot",
    max_reference_images: int = 4,
    negative_prompt: str = "",
    prior_segments: Optional[List[Dict[str, Any]]] = None,
    progress: Optional[Callable[[float, str], None]] = None,
    dry_run: bool = False,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把一个长镜头切成若干段生成。

    Args:
        shot: 分镜 dict（要有 id / project_id / duration_seconds / aspect_ratio /
              resolution / layer1_overview / layer2_timeline / layer3_constraints /
              character_ids）
        adapter: 已构造好的 VideoAdapter 实例
        db: Database，用来取角色/场景参考图；给了才能锚定一致性
        out_dir: 片段落地目录
        strategy: 强制指定策略；空则自动
        anchor_mode: `shot`（默认，段间硬切不续接）或 `chained`（续接）
        prior_segments: 上次生成的分段记录，用于**只重跑没成的段**
        progress: 进度回调 (0~1, 文案)
        dry_run: 只排片不真调 API（用于自检/给用户预览）
        settings: 全部设置（**必须传**）。白膜要作为参考视频发出去时，
                  平台只吃**公网 URL**，而"怎么拿到公网 URL"完全由设置决定
                  （`public_base_url` / 命名隧道 / 临时隧道）。
                  不传的话 `previz_ref.decide` 只能看到空设置 ——
                  用户配好的命名隧道/固定域名**一个都用不上**，
                  白膜会静默降级成"只把运镜写进提示词"。

    Returns:
        {
          ok, strategy, reason, plan, segments:[{index,start,duration,path,status,...}],
          note
        }
    """
    from core.orchestrator import _compose_full_prompt
    from core.adapters import to_image_ref

    def tick(p: float, msg: str) -> None:
        if progress:
            try:
                progress(p, msg)
            except Exception:
                pass

    shot_id = shot.get("id") or "shot"
    total = float(shot.get("duration_seconds") or 0)
    max_dur = float(getattr(adapter, "max_duration", 10) or 10)
    min_dur = float(getattr(adapter, "min_duration", 1) or 1)

    strat = strategy or choose_strategy(total, max_dur)
    plan = plan_segments(total, min_dur, max_dur, strat)
    bad = _validate_plan(plan, total, min_dur, max_dur)
    if bad:
        return {"ok": False, "error": bad, "strategy": strat, "plan": plan, "segments": []}

    logger.info("长视频排片：%s 共 %.1fs → %s，%d 段 %s", shot_id, total, strat, len(plan),
                [s["duration"] for s in plan])

    if dry_run:
        return {"ok": True, "strategy": strat, "plan": plan, "segments": [], "dry_run": True,
                "note": f"{total:g}s 用 {strat} 策略切 {len(plan)} 段"}

    # ── 参考素材（每段都要带，这是"重锚"的核心）──
    char_refs: List[str] = []
    if db is not None:
        for cid in (shot.get("character_ids") or []):
            try:
                c = db.get_character(cid)
            except Exception:
                c = None
            if c and c.get("reference_image_path"):
                ref = to_image_ref(c["reference_image_path"])
                if ref:
                    char_refs.append(ref)
    scene_ref = None
    if db is not None and shot.get("scene_id"):
        try:
            s = db.get_scene(shot["scene_id"])
        except Exception:
            s = None
        if s and s.get("reference_image_path"):
            scene_ref = to_image_ref(s["reference_image_path"])

    base_prompt = _compose_full_prompt(shot)
    if not base_prompt.strip():
        base_prompt = shot.get("layer1_overview") or ""

    # ★★ 白膜注入。分段生成时白膜尤其重要：段与段之间本来最容易"运动断掉"，
    #   白膜提供的**时间节奏**正是把这个接上用的。
    #   （在此之前 longvideo 完全没提 previz —— 白膜渲了也用不上。）
    previz_note = ""
    previz_info: Dict[str, Any] = {}
    try:
        from core import previz_ref as _pr
        _ref = _pr.decide(shot, getattr(adapter, "name", "") or "",
                          settings=settings or {})
        previz_info = _ref.to_dict()
        if _ref.usable and _ref.video_ref:
            previz_note = _ref.prompt_note
        elif _ref.prompt_note:
            # 平台不支持视频参考 → 退化成把运镜写进 prompt（也要说明是降级）
            previz_note = _ref.prompt_note
        previz_info["segments_planned"] = len(plan)
        if _ref.usable and _ref.video_ref:
            logger.info("[%s] 长视频分段：白膜将作为参考素材（mode=%s）",
                        shot_id, _ref.mode)
    except Exception as e:
        logger.warning("长视频白膜决策失败（按不带白膜继续）：%s", e)

    # ★ 白膜首帧（用户确认过的那张图）：优先拿来当 first_frame，保证构图一致。
    previz_first_frame = ""
    try:
        _pv_path = str(shot.get("previz_path") or "").strip()
        if _pv_path:
            if os.path.isfile(_pv_path):
                previz_first_frame = _pv_path
            else:
                _proj = str(shot.get("project_id") or "")
                for _base in (os.getcwd(), os.path.join(os.getcwd(), "data")):
                    _cand = os.path.join(_base, _pv_path)
                    if os.path.isfile(_cand):
                        previz_first_frame = _cand
                        break
    except Exception as _e:  # noqa: BLE001
        logger.warning("取白膜首帧失败（按常规路径）：%s", _e)

    out_dir = out_dir or os.path.join(os.getcwd(), "data", "outputs",
                                      str(shot.get("project_id") or "p"), str(shot_id))
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    from core.reference_delivery import requires_reference_delivery
    _strict_reference = requires_reference_delivery(shot, settings,
        getattr(adapter, "name", "") or shot.get("model_provider") or "",
        (getattr(adapter, "config", {}) or {}).get("model_name") or shot.get("model_name") or "")
    # ── 复用上次已成功的段（省钱的关键：抽到第 7 段发现第 2 段不行，只重跑第 2 段）──
    reuse: Dict[int, Dict[str, Any]] = {}
    for old in (prior_segments or []):
        try:
            idx, odur = int(old.get("index", -1)), float(old.get("duration") or 0)
        except Exception:
            continue
        p = old.get("path") or ""
        if old.get("status") == "ok" and p and os.path.exists(p) and abs(odur - plan[idx]["duration"]) < 0.05:
            reuse[idx] = old
    if reuse:
        logger.info("%s 复用 %d 个已生成的段（只重跑其余）", shot_id, len(reuse))

    segments: List[Dict[str, Any]] = []
    prev_last_frame = ""          # 上一段尾帧（续接用）
    errors: List[str] = []

    # ★★ 2026-10-22：写实关键帧（AI 预演图 → 当首帧出片）
    #   实测依据：适配器里 first_frame 与 reference_image **互斥**
    #   （core/adapters/seedance.py:150-158）——有参考图时首帧被降级成"又一张参考图"，
    #   只锁长相不锁构图；没有参考图时才以 role=first_frame 发出，**构图 100% 跟首帧一致**
    #   （对照实验：去掉参考图后，成片首帧 = 给它的首帧图）。
    #   所以这里的做法是：**先生成一张写实关键帧**（用户确认的那张图），
    #   出片时它当 first_frame 且不带参考图 ⇒ 用户看到的构图 = 成片的构图。
    keyframe_file = ""
    keyframe_note = ""
    if not _strict_reference:
        try:
            from core import keyframe as _kf
            _root = str((settings or {}).get("output_dir") or os.path.join(os.getcwd(), "data", "outputs"))
            keyframe_file = _kf.existing_keyframe(_root, shot)
            if not keyframe_file:
                from core import shot_prompts as _sp
                _comp = _sp.stored_prompts(shot)
                _video = str(_comp.get("video_prompt") or "")
                _head = _video
                for _mark in ("镜头：", "动作：", "这一镜要讲清楚的是："):
                    if _mark in _head:
                        _head = _head.split(_mark)[0]
                _built = await _kf.build_keyframe(
                    db, shot, settings or {}, output_dir=_root,
                    prompt=_kf.KEYFRAME_PROMPT + "\n" + _head.strip(),
                    use_extra_refs=True)
                if _built.get("ok"):
                    keyframe_file = str(_built.get("path") or "")
                    keyframe_note = "本次成片以**写实关键帧**为首帧（构图/机位与用户确认的那张图一致）"
                else:
                    keyframe_note = f"关键帧没生成（{_built.get('error')}），退回场景图当首帧"
        except Exception as _e:  # noqa: BLE001
            logger.warning("关键帧环节失败（按原路径继续）：%s", _e)

    for seg in plan:
        i = seg["index"]
        tick(0.02 + 0.9 * i / max(1, len(plan)),
             f"第 {i + 1}/{len(plan)} 段（{seg['duration']:g}s）")

        if i in reuse:
            old = reuse[i]
            if _strict_reference:
                from core.reference_delivery import finalize_reference_candidate
                from core.previz_ref import white_model_video
                _source = dict((old.get("delivery") or {}).get("source_metadata") or {})
                if not _source:
                    _source = {"provider": old.get("provider"), "model": old.get("model"), "task_id": old.get("task_id")}
                _review = await finalize_reference_candidate(str(old["path"]), white_model_video(shot), float(seg["duration"]),
                    reference_start=float(seg.get("start") or 0), aspect_ratio=shot.get("aspect_ratio") or "16:9",
                    silent=not bool(old.get("with_audio")), ffmpeg_path=(settings or {}).get("ffmpeg_path") or "",
                    source_metadata=_source)
                if not _review.get("ok"):
                    segments.append({**old, "status": "rejected", "reused": True, "delivery": _review})
                    errors.append(_review.get("error") or "旧分段一致性未通过")
                    break
                old = {**old, "path": _review["path"], "delivery": _review}
            segments.append({**old, "status": "ok", "reused": True})
            prev_last_frame = old.get("last_frame") or ""
            continue

        prompt = base_prompt + previz_note + segment_note(i, len(plan), seg["duration"], seg["start"])

        kw: Dict[str, Any] = dict(
            prompt=prompt,
            duration=int(round(seg["duration"])),
            aspect_ratio=shot.get("aspect_ratio") or "16:9",
            resolution=shot.get("resolution") or "1080p",
            character_reference=char_refs[:max(1, max_reference_images)],
            negative_prompt=negative_prompt,
        )
        # 续接：上一段尾帧当这一段首帧。★ 但只有在"允许续接"时才给 ——
        #   Seedance/万相/可灵 的首尾帧模式与参考图模式**互斥**，
        #   两个都给会被平台拒或悄悄忽略掉其中一个（实测 Seedance 就是后者）。
        chaining = (strat == STRATEGY_CHAINED) or (anchor_mode == "chained")
        _relay_first = ""
        try:
            from core import shot_prompts as _sp
            _chain_prompts = _sp.stored_prompts(shot)
            if str(_chain_prompts.get("chain_mode") or "") == "relay" and _chain_prompts.get("relay_first_frame"):
                _relay_first = str(_chain_prompts["relay_first_frame"])
        except Exception:  # noqa: BLE001
            _relay_first = ""
        if _relay_first:
            # ★ 跨镜接龙：上一镜尾帧当这一镜首帧 ⇒ 像素级连续（位置不会跳）。
            #   代价：首帧与参考图互斥，所以这一镜**不能**带角色/场景参考图，
            #   身份靠提示词里的角色锚定句 + 负面词锁。
            try:
                kw["first_frame"] = to_image_ref(_relay_first)
                kw["character_reference"] = []
                previz_info.setdefault("applied", {})["relay_first_frame"] = True
                previz_info.setdefault("warnings", []).append(
                    "本镜与上一镜**像素级接龙**（上一镜尾帧当首帧，位置不会跳）。")
            except Exception as _e:  # noqa: BLE001
                logger.warning("接龙首帧转 URL 失败（按独立镜继续）：%s", _e)
        elif keyframe_file:
            # ★ 关键帧优先：构图与白膜一致；**参考图必须清空** ——
            #   适配器里 first_frame 与 reference_image 互斥
            #   （core/adapters/seedance.py:150-158），带了参考图首帧会降级成
            #   "又一张参考图"，只锁长相不锁构图（这就是白膜与成片对不上的真因）。
            try:
                kw["first_frame"] = to_image_ref(keyframe_file)
                kw["character_reference"] = []
                previz_info.setdefault("applied", {})["keyframe_as_first_frame"] = True
                if keyframe_note:
                    previz_info.setdefault("warnings", []).append(keyframe_note)
            except Exception as _e:  # noqa: BLE001
                logger.warning("关键帧转 URL 失败（按原路径继续）：%s", _e)
        elif chaining and prev_last_frame:
            # ★ 必须过 to_image_ref：`prev_last_frame` 是**本地文件路径**
            #   （`grab_last_frame` 的产物），而平台只吃 http(s) URL 或
            #   base64 data URI。直接传路径过去会被平台拒（400）——
            #   可灵适配器那边独立发现了同一个问题并自己兜了一层，
            #   但兜在调用方才是对的：别的适配器不一定有这层保护。
            kw["first_frame"] = to_image_ref(prev_last_frame)
            kw["character_reference"] = []      # 互斥，让位给首帧
        elif previz_first_frame:
            # ★ 2026-10-21（用户第 3 条）：白膜和成片**画面对不上**。
            #   原因：白膜只在"平台支持参考视频"时才真的进请求，而这个网关的
            #   seedance 档位不声明视频参考 ⇒ 白膜被降级成一句提示词，成片构图全靠模型编。
            #   改法：白膜渲出来的**首帧图**（用户就是看这张照片确认的）直接当 first_frame，
            #   构图/机位/景别与白膜一致；画面仍由视频模型重绘（不是把白模贴上去）。
            try:
                kw["first_frame"] = to_image_ref(previz_first_frame)
                previz_info.setdefault("applied", {})["first_frame_from_previz"] = True
                previz_info.setdefault("warnings", []).append(
                    "本次成片以**白膜首帧**为首帧（构图/机位跟白膜一致），画面由视频模型重绘。")
            except Exception as _e:  # noqa: BLE001
                logger.warning("白膜首帧转 URL 失败（退回场景图）：%s", _e)
                if scene_ref:
                    kw["first_frame"] = scene_ref
        elif scene_ref:
            kw["first_frame"] = scene_ref

        from core.adapters import VideoGenRequest
        req = VideoGenRequest(**kw)

        # 白膜作为参考素材附加（只在上面 decide() 判定可用时才真的加）。
        # ★ 放在**校验之前**：加了参考视频可能让请求变得平台不支持
        #   （比如"参考素材与首帧互斥"），必须让 validate_request 看到
        #   最终形态并有机会拦住，而不是发出去被平台拒。
        if _ref is not None and _ref.usable and _ref.video_ref:
            try:
                _did = _pr.apply_to_request(req, _ref)
                previz_info.setdefault("applied", {})
                previz_info["applied"].update(_did or {})
                if (_did or {}).get("dropped_first_frame"):
                    previz_info.setdefault("warnings", []).append(
                        "为了让白膜作为参考素材发出去，本次没有使用首帧图"
                        "（平台把「首帧」和「参考素材」列为互斥场景）。")
                if not previz_info["applied"].get("added_video_ref"):
                    previz_info.setdefault("warnings", []).append(
                        "白膜本应作为参考素材发出，但**没有真的挂上视频参考** —— "
                        "本次只靠提示词里的运镜说明约束（模型仍可能自己编运镜）。")
            except Exception as e:
                logger.warning("第 %d 段白膜注入失败（按不带白膜继续）：%s", i + 1, e)

        try:
            verr = adapter.validate_request(req)
        except Exception as e:
            verr = f"{type(e).__name__}: {e}"
        if verr:
            segments.append({"index": i, "start": seg["start"], "duration": seg["duration"],
                             "status": "failed", "error": f"参数校验未过：{verr}"})
            errors.append(f"第{i+1}段：{verr}")
            continue

        from core.previz_ref import require_reference_quality
        _reference_quality = require_reference_quality(shot, getattr(adapter, "name", "") or shot.get("model_provider") or "",
                                  (getattr(adapter, "config", {}) or {}).get("model_name") or shot.get("model_name") or "", settings)
        if _reference_quality is not None and not getattr(req, "video_url", None):
            raise ValueError("白膜质量通过但参考视频未实际加入请求，已停止付费生成")

        from core.reference_delivery import prepare_reference_spend, record_reference_spend
        _spend = prepare_reference_spend(db, shot, req.duration, req.resolution,
            provider=getattr(adapter, "name", "") or shot.get("model_provider") or "",
            model_name=(getattr(adapter, "config", {}) or {}).get("model_name") or shot.get("model_name") or "")
        try:
            res = await adapter.generate(req)
        except Exception as e:
            logger.exception("第 %d 段生成异常", i + 1)
            segments.append({"index": i, "start": seg["start"], "duration": seg["duration"],
                             "status": "failed", "error": f"{type(e).__name__}: {e}"})
            errors.append(f"第{i+1}段：{type(e).__name__}: {e}")
            continue

        if not res or not getattr(res, "success", False):
            msg = (getattr(res, "error", "") or "未知错误") if res else "适配器返回空"
            segments.append({"index": i, "start": seg["start"], "duration": seg["duration"],
                             "status": "failed", "error": msg})
            errors.append(f"第{i+1}段：{msg}")
            continue

        _charge = record_reference_spend(db, shot, _spend, res.task_id or "")
        local = os.path.join(out_dir, f"seg_{i + 1:02d}_{__import__('uuid').uuid4().hex}.raw.mp4")
        try:
            await adapter.download_to_local(res.video_url, local)
        except Exception as e:
            segments.append({"index": i, "start": seg["start"], "duration": seg["duration"],
                             "status": "failed", "error": f"下载失败：{e}",
                             "video_url": res.video_url, "task_id": res.task_id})
            errors.append(f"第{i+1}段下载失败：{e}")
            continue

        _delivery = None
        if _reference_quality is not None:
            from core.reference_delivery import finalize_reference_candidate
            from core.previz_ref import white_model_video
            _delivery = await finalize_reference_candidate(local, white_model_video(shot), float(seg["duration"]),
                reference_start=float(seg.get("start") or 0), aspect_ratio=shot.get("aspect_ratio") or "16:9",
                silent=not getattr(req, "with_audio", False),
                ffmpeg_path=(settings or {}).get("ffmpeg_path") or "",
                source_metadata={"provider": getattr(adapter, "name", "") or shot.get("model_provider"),
                    "model": (getattr(adapter, "config", {}) or {}).get("model_name") or shot.get("model_name"),
                    "task_id": res.task_id or "", "charge": _charge})
            if not _delivery.get("ok"):
                segments.append({"index": i, "start": seg["start"], "duration": seg["duration"],
                    "status": "rejected", "path": local, "task_id": res.task_id,
                    "is_selected": False, "delivery": _delivery})
                errors.append(_delivery.get("error") or "白膜一致性未通过")
                break  # 不合格不继续付费生成后续段、不换本地素材冒充。
            local = _delivery["path"]

        # 尾帧：优先用平台给的（Seedance `return_last_frame`，不用本地抽）
        last_frame = getattr(res, "last_frame_url", "") or ""
        if not last_frame:
            lf = os.path.join(out_dir, f"seg_{i + 1:02d}_last.jpg")
            last_frame = grab_last_frame(local, lf) or ""
        if not last_frame:
            # 抽不到尾帧 → 下一段接不上。这是**可接受的降级**（退化成硬切），
            # 但必须说清楚，否则用户只会看到"续接模式没生效"却不知道为什么。
            logger.info("第 %d 段抽不到尾帧，下一段将退化为硬切（不续接）", i + 1)

        segments.append({
            "index": i, "start": seg["start"], "duration": seg["duration"],
            "status": "ok", "path": local, "video_url": res.video_url,
            "task_id": res.task_id, "last_frame": last_frame,
            "actual_duration": (_delivery or {}).get("duration") or res.duration_seconds,
            "delivery": _delivery or {}, "provider": getattr(adapter, "name", "") or shot.get("model_provider"),
            "model": (getattr(adapter, "config", {}) or {}).get("model_name") or shot.get("model_name"),
        })
        # ★ 把尾帧交给下一段当首帧。少了这一行，"续接模式"就只是算了个
        #   chaining 布尔值却永远拿不到帧 —— 静默失效，最难查的那类 bug。
        prev_last_frame = last_frame

    # ★ 2026-10-22：把**最后一段的尾帧**写进本镜的提示词载荷（prompts.last_frame）。
    #   为什么必须有：平台的 last_frame_url **不落地**（只有平台没给时才本地抽帧），
    #   于是"上一镜尾帧"在本地根本找不到 ⇒ 跨镜接龙拿不到首帧，接龙永远触发不了。
    #   写 URL 或本地路径都行（平台吃 http(s) URL 与 base64 data URI）。
    try:
        _last = ""
        for _s in reversed(segments):
            if _s.get("status") == "ok" and _s.get("last_frame"):
                _last = str(_s["last_frame"])
                break
        if _last:
            from core import shot_prompts as _sp
            _prompts = _sp.stored_prompts(shot)
            if not isinstance(_prompts, dict):
                _prompts = {}
            _prompts["last_frame"] = _last
            db.update_shot(str(shot.get("id") or ""), layer3_constraints=json.dumps(
                _sp.attach_prompts(shot, _prompts), ensure_ascii=False))
    except Exception as _e:  # noqa: BLE001
        logger.warning("持久化尾帧失败（不影响本镜）：%s", _e)

    ok_count = [s for s in segments if s.get("status") == "ok"]
    result = {
        "ok": len(ok_count) == len(plan) and bool(plan),
        "shot_id": shot_id,
        "strategy": strat,
        "anchor_mode": anchor_mode,
        "plan": plan,
        "segments": segments,
        "clips": [s["path"] for s in ok_count if s.get("path")],
        "errors": errors[:5],
        "reused": sum(1 for s in segments if s.get("reused")),
        "previz": previz_info,
    }
    if not result["ok"]:
        result["error"] = "；".join(errors[:3]) if errors else "有分段未生成成功"
    tick(1.0, "分段完成")
    return result
