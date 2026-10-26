# -*- coding: utf-8 -*-
"""方法论执行层（2026-10-24）：把「四个反馈」变成链路里的四道门。

用户反馈的四件事（节奏慢 / 15 秒衔接差 / 电影感不足 / 台词 AI 味重），
本质上不是四个后期修补点，而是**每个功能单元的效率不够**：
写台词时不数字数、写分镜时不看切点密度、拼接时不看声音落点、调色时不管片段间漂移。
所以这里不做「事后滤镜」，而是把四把尺子做成**在流程里执行**的门：

    台词门  core/dialogue_natural.director_pass / apply_director_pass
    节奏门  core/gate_pacing（每 3 秒有没有变化 + 景别节奏 + 剪辑层拆切点）
    接缝门  core/gate_seam（声音锚点 + whoosh 焊缝 + 段落级转场）
    电影感门 core/gate_cinematic（全片一套 LUT + 统一颗粒 + 可选画幅）

设计纪律（与 core/seamfix.py 同一套）：
  · **不猜**：量不出来就返回 unknown / 跳过，不编一个好看的数；
  · **可关**：settings.method_gates=false 时全部旁路，出片行为回到原样；
  · **不阻断**：任何一道门异常都只记 warning，绝不让出片失败；
  · **能复核**：门给出的每个动作都写进 compose 的 warnings 和结果字段，
    用户看得到「这次为什么更顺」，而不是只拿到一个文件。

注意：本模块只做**编排与适配**，四把尺子的实现在各自的 gate_* 模块里，
数值口径由各模块的常量注释给出，不在这里再抄一份。
"""
from __future__ import annotations

import logging
import hashlib
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("videoforge.method_gate")

__all__ = [
    "GATE_VERSION",
    "enabled",
    "pacing_for",
    "repair_script",
    "audit_final_script",
    "plan_edit",
    "grade_film",
    "plan_seam_events",
    "summarize",
]

GATE_VERSION = "1.0.0"

#: genre → 默认节奏档（与 core/scriptplan.py 的档位名一致）
_PACE_BY_GENRE = {
    "promo": "promo",
    "drama": "fast_drama",
    "generic": "fast_drama",
}

#: 允许被裁成电影画幅的源画幅下限（竖屏/方形不裁，否则构图会被裁没）
_LANDSCAPE_MIN_RATIO = 1.55

#: 全片统一颗粒强度（0..100 的 ffmpeg noise 尺度；与 seamfix.TEXTURE_GRAIN 对齐）
_GRADE_GRAIN = 8

#: 默认电影画幅；"off" / "" 表示不动画幅
_DEFAULT_ASPECT = "1.85:1"


# ────────────────────────────── 开关 ──────────────────────────────
def _as_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(v)


def enabled(settings: Optional[dict], key: str = "method_gates",
            default: bool = True) -> bool:
    """总开关 / 分开关。`settings` 缺字段时默认开（这就是「提前锁死」）。"""
    if not isinstance(settings, dict):
        return default
    if key != "method_gates" and not _as_bool(settings.get("method_gates"), default):
        return False
    return _as_bool(settings.get(key), default)


def _silent_dialogue_report(script: dict, settings: Optional[dict] = None) -> Optional[dict]:
    """A deliberately silent film has no dialogue to grade, rather than an error."""
    if (str(script.get("dialogue_policy") or "").lower() != "silent"
            and str((settings or {}).get("voiceover_mode") or "").lower() != "off"):
        return None
    scenes = script.get("scenes") or []
    if isinstance(scenes, str):
        scenes = json.loads(scenes)
    if any(scene.get("dialogues") for scene in scenes if isinstance(scene, dict)):
        return None
    return {"_gate": "dialogue", "skipped": True, "reason": "explicit_silent_policy",
            "score": None, "errors": 0, "warnings": 0, "items": [],
            "stats": {"lines": 0, "scenes": len(scenes)}}


def audit_final_script(script: dict, *, settings: Optional[dict] = None,
                       genre: str = "drama", pacing: Optional[str] = None,
                       total: Optional[float] = None, phase: str = "final") -> dict:
    """Audit the actual scenes that will be persisted, without repairing them again.

    The report hashes the complete scene data and records duration/pacing/settings
    so callers can distinguish a pre-fit report from a final persisted report.
    """
    selected_pacing = pacing or pacing_for(settings, genre)
    scenes = script.get("scenes") or []
    scene_json = json.dumps(scenes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report = {"version": GATE_VERSION, "phase": phase, "enabled": enabled(settings),
              "pacing": selected_pacing, "target_total": total, "duration_policy": "exact",
              "scene_sha256": hashlib.sha256(scene_json.encode("utf-8")).hexdigest(),
              "reports": {}}
    contract = script.get("duration_contract") or {}
    if isinstance(contract, dict) and contract:
        rows = json.loads(scenes) if isinstance(scenes, str) else scenes
        rows = [scene for scene in rows if isinstance(scene, dict)] if isinstance(rows, list) else []
        durations = [float(scene.get("duration_seconds") or 0) for scene in rows]
        lower, upper = float(contract.get("min_scene_seconds", 0)), float(contract.get("max_scene_seconds", 15))
        exact = abs(sum(durations) - float(contract.get("target_seconds", 0))) <= 0.05
        count_matches = len(rows) == int(contract.get("scene_count", len(rows)))
        bounds_ok = all(lower <= duration <= upper for duration in durations)
        report["duration_validation"] = {"ok": exact and count_matches and bounds_ok,
                                          "exact_total": exact, "count_matches": count_matches,
                                          "bounds_ok": bounds_ok, "actual_seconds": sum(durations),
                                          "actual_scene_count": len(rows)}
    report["rule_ids"] = ["duration-exact-v1", "director-structure-v1", "dialogue-budget-v1"]
    if enabled(settings, "method_gates_dialogue"):
        from core.dialogue_natural import director_pass
        rows = scenes
        if isinstance(rows, str):
            rows = json.loads(rows)
        rows = [scene for scene in rows if isinstance(scene, dict)] if isinstance(rows, list) else []
        lines = [line for scene in rows for line in (scene.get("dialogues") or [])]
        actual = sum(float(scene.get("duration_seconds") or 0) for scene in rows)
        dialogue = (_silent_dialogue_report(script, settings)
                    or director_pass(lines, genre=genre, scene_seconds=actual or None))
        dialogue["stats"]["scenes"] = len(rows)
        report["reports"]["dialogue"] = dialogue
    if enabled(settings, "method_gates_director"):
        from core.director_gate import audit_script
        report["reports"]["director"] = audit_script(script, pacing=selected_pacing, total=total)
        script["director"] = report["reports"]["director"]
    else:
        script.pop("director", None)
    script["_method_gate_report"] = report
    script["_method_gates"] = summarize(report["reports"])
    director = report["reports"].get("director") or {}
    if director:
        script["_method_gates"].append(
            "导演门：score={}；errors={}；warn={}；pacing={}；目标={}s".format(
                director.get("score"), director.get("errors"), director.get("warnings"),
                selected_pacing, total))
    return script


# ────────────────────────────── 台词门 ──────────────────────────────
def repair_script(script: dict, *, genre: str = "drama", style: str = "auto",
                  settings: Optional[dict] = None
                  ) -> Tuple[dict, Optional[Dict[str, Any]]]:
    """在剧本落库前过一遍台词门：6–18 字 / 3.5–5 字每秒 / 金句 0.5s 气口 / 语气位。

    返回 `(新剧本, 体检报告)`；门关闭或失败时原样返回 `(script, None)`。
    只改 `dialogues` 的 text / tone，不动场次结构与人物关系（改写边界见
    `core/dialogue_natural.apply_director_pass` 的说明）。
    """
    if not enabled(settings, "method_gates_dialogue"):
        return script, None
    silent_report = _silent_dialogue_report(script, settings)
    if silent_report:
        return script, silent_report
    try:
        from core.dialogue_natural import apply_director_pass
        r = apply_director_pass(script or {}, genre=genre, style=style) or {}
        new = r.get("script") if isinstance(r.get("script"), dict) else script
        rep = r.get("report") if isinstance(r.get("report"), dict) else None
        if rep:
            rep = dict(rep)
            rep["_gate"] = "dialogue"
        return new, rep
    except Exception as e:  # noqa: BLE001
        logger.warning("台词门失败（保留原剧本）：%s", e)
        return script, {"_gate": "dialogue", "error": f"{type(e).__name__}: {e}"}


# ────────────────────────────── 节奏门 ──────────────────────────────
def pacing_for(settings: Optional[dict], genre: str = "drama") -> str:
    """节奏档：项目设置优先（用户在分镜页选过就用他的），否则按体裁给默认。"""
    try:
        from core.gate_pacing import PACING_TIERS
    except Exception:  # noqa: BLE001
        PACING_TIERS = {"fast_drama": 2.5, "promo": 3.5, "talking": 5.0, "ambient": 8.0}
    p = str((settings or {}).get("pacing") or "").strip()
    if p in PACING_TIERS:
        return p
    return _PACE_BY_GENRE.get(str(genre or "drama").lower(), "fast_drama")


def plan_edit(clips: Sequence[str], shots: Sequence[dict], work_dir: str, *,
              genre: str = "drama", settings: Optional[dict] = None
              ) -> Optional[Dict[str, Any]]:
    """剪辑层拆切点：把一条 15 秒素材拆成 3~4 个「像不同机位」的切点。

    为什么要这一层：生成模型按 5/10/15 秒出片，而**观众感知的节奏是切点密度**。
    规划层已经把每镜时长压到 3.5 秒，但**已有素材**（尤其用户已经花钱生成的
    15 秒片段）不会自己变短 —— 不拆，成片就还是「两段 15 秒」。

    返回 `{"segments":[...], "report":audit, "cut_seconds":..., "warnings":[...]}`；
    不需要拆 / 出错时返回 None（调用方保持原样，绝不阻断出片）。
    """
    if not enabled(settings, "method_gates_pacing"):
        return None
    if not clips:
        return None
    try:
        from core import gate_pacing as GP
        pacing = pacing_for(settings, genre)
        cut_seconds = float(GP.PACING_TIERS.get(pacing, 3.5))
        # 素材本来就比一个切点短 → 没有可拆的，别为了「过门」硬切碎
        longest = 0.0
        for c in clips:
            try:
                longest = max(longest, float(GP.probe_media(c).get("duration") or 0.0))
            except Exception:  # noqa: BLE001
                continue
        if longest and longest < cut_seconds * 1.6:
            return None

        # ★ 2026-10-24：「非等分拆点」——每镜 layer2_timeline 本来就是内容驱动的
        #   2~5s 时间片，它就是天然切点。以前 compose 丢掉它、按固定 cut_seconds
        #   等分，实测 8 段只有 2 个取值（被 C13/T3 判缺陷）。这里把它交给
        #   gate_pacing.build_cut_plan(timelines=...)；旧版没有该参数时自动回退。
        _timelines: List[Any] = []
        for _i in range(len(clips)):
            _shot = shots[_i] if _i < len(shots) else {}
            _tl = (_shot or {}).get("layer2_timeline")
            if isinstance(_tl, str):
                try:
                    import json as _json
                    _tl = _json.loads(_tl or "[]")
                except Exception:  # noqa: BLE001
                    _tl = []
            _items: List[dict] = []
            if isinstance(_tl, list):
                for _it in _tl:
                    if not isinstance(_it, dict):
                        continue
                    try:
                        _st = float(_it.get("start") or 0.0)
                        _en = float(_it.get("end") or 0.0)
                    except Exception:  # noqa: BLE001
                        continue
                    if _en > _st + 0.05:
                        _items.append({"start": _st, "end": _en,
                                       "intent": str(_it.get("intent") or ""),
                                       "dialogue": _it.get("dialogue")})
            _timelines.append(_items or None)

        try:
            plan = GP.build_cut_plan(list(clips), pacing=pacing,
                                     timelines=_timelines)
        except TypeError:
            # 旧版 gate_pacing（无 timelines 参数）：保持旧行为，不阻断出片
            plan = GP.build_cut_plan(list(clips), pacing=pacing)
        cuts = plan.get("cuts") or []
        if len(cuts) <= len(clips):
            return None

        out_dir = os.path.join(work_dir or ".", "edit_cuts")
        os.makedirs(out_dir, exist_ok=True)
        files = GP.apply_cut_plan(list(clips), plan, out_dir)
        ok_files = [f for f in files if isinstance(f, dict) and f.get("ok") and
                    os.path.exists(str(f.get("path") or ""))]
        if len(ok_files) <= len(clips):
            logger.warning("节奏门：拆切点只落盘 %d/%d，保持原样", len(ok_files), len(cuts))
            return None

        # 按源片段分组，映射回分镜（保留 scene_id / 是否本镜首尾，供转场与地点卡用）
        by_src: Dict[str, List[dict]] = {}
        for f in ok_files:
            by_src.setdefault(str(f.get("src") or ""), []).append(f)
        segments: List[dict] = []
        for i, clip in enumerate(clips):
            group = by_src.get(str(clip)) or []
            group.sort(key=lambda x: float(x.get("in") or 0.0))
            shot = shots[i] if i < len(shots) else {}
            for j, f in enumerate(group):
                segments.append({
                    "path": str(f.get("path")),
                    "shot_index": i,
                    "shot_id": str(shot.get("id") or ""),
                    "scene_id": str(shot.get("scene_id") or ""),
                    "is_head": j == 0,
                    "is_tail": j == len(group) - 1,
                    "duration": float(f.get("duration") or 0.0),
                    "zoom": float(f.get("zoom") or 1.0),
                    "index_in_shot": j,
                })
        if len(segments) <= len(clips):
            return None

        # 节奏体检：拿原分镜表算（不能拿切完的段当输入，那样永远"达标"）
        try:
            audit = GP.audit_shot_plan(list(shots), pacing=pacing)
        except Exception as e:  # noqa: BLE001
            audit = {"error": f"{type(e).__name__}: {e}"}
        warns: List[str] = []
        if len(cuts) and len(segments):
            avg_seg = sum(s["duration"] for s in segments) / len(segments)
            warns.append(
                f"剪辑层拆切点：{len(clips)} 条素材（最长 {longest:.1f}s）→ "
                f"{len(segments)} 段，平均 {avg_seg:.2f}s/段（{pacing} 档目标 {cut_seconds:.1f}s）；"
                f"每段带 1.00→1.12 渐进二次构图，避免同一画面反复重播。")
        if (audit or {}).get("verdict"):
            warns.append("节奏门·原分镜表体检（拆分前）：" + str(audit.get("verdict"))[:160])

        # ★ 2026-10-24：导演门——对**真实切点**做 C13/T3/长短呼吸体检
        _director: Optional[Dict[str, Any]] = None
        _seg_durs = [round(float(x.get("duration") or 0.0), 3) for x in segments]
        _distinct = len({round(d, 2) for d in _seg_durs if d > 0})
        try:
            from core import director_gate as DG
            _director = DG.audit_cuts(
                [{"duration": d, "shot_index": segments[_k].get("shot_index")}
                 for _k, d in enumerate(_seg_durs)], pacing=pacing)
            for _c in (_director.get("checks") or []):
                if _c.get("level") in ("error", "warn"):
                    warns.append("导演门：" + str(_c.get("title") or "") + " — " +
                                 str(_c.get("detail") or "")[:120])
        except Exception as _e:  # noqa: BLE001
            logger.warning("导演门（切点体检）失败（不影响出片）：%s", _e)
        if len(_seg_durs) >= 3 and _distinct < 3:
            warns.append(
                f"导演门：全片切点只有 {_distinct} 个取值（目标 ≥3）——"
                f"内容时间片不足，已尽量保留差异；建议在分镜阶段把时间片长度写开。")
        return {
            "segments": segments,
            "report": audit,
            "director": _director,
            "cut_seconds": cut_seconds,
            "pacing": pacing,
            "warnings": warns,
            "before": {"clips": len(clips), "longest": round(longest, 3),
                       "cuts": 0},
            "after": {"segments": len(segments), "cuts": len(segments) - 1,
                      "avg_segment": round(sum(s["duration"] for s in segments) / len(segments), 3)
                      if segments else 0.0,
                      "distinct_durations": _distinct,
                      "source": plan.get("source_per_clip") or plan.get("note") or ""},
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("节奏门失败（保持原片拼接）：%s", e)
        return {"error": f"{type(e).__name__}: {e}", "segments": []}


# ────────────────────────────── 电影感门 ──────────────────────────────
def _probe_size(path: str) -> Tuple[int, int]:
    """读视频尺寸；读不到返回 (0, 0)（调用方据此跳过画幅处理）。"""
    try:
        from core import gate_cinematic as GC
        info = GC.probe_clip(path) or {}
        for kw in ("width", "w", "out_w"):
            if info.get(kw):
                wk = int(info.get(kw))
                break
        else:
            wk = 0
        for kw in ("height", "h", "out_h"):
            if info.get(kw):
                hk = int(info.get(kw))
                break
        else:
            hk = 0
        if wk and hk:
            return wk, hk
        # 兜底：从 ffmpeg -i 输出里抓 "864x496"
        import re
        import subprocess
        from core.assemble import _ffmpeg
        pr = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
        m = re.search(r",\s*(\d{2,5})x(\d{2,5})[,\s]", (pr.stderr or ""))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Exception:  # noqa: BLE001
        return 0, 0


def grade_film(src: str, work_dir: str, *, clips: Sequence[str],
               genre: str = "drama", settings: Optional[dict] = None
               ) -> Optional[Dict[str, Any]]:
    """对**整片**统一调色（一条 look 用到尾）+ 统一轻颗粒（+ 可选裁电影画幅）。

    为什么在整片上做一次、而不是逐段各调各的：电影感最怕「片段之间色调漂移」。
    逐段调色再接近也会差一点，而观众潜意识里就是靠这个判断「不是同一部片子」。

    返回 `{"path","look","w","h","notes","drift_before","drift_after"}`；
    关闭/失败返回 None（保留原画面）。
    """
    if not enabled(settings, "method_gates_cinematic"):
        return None
    if not src or not os.path.exists(src):
        return None
    try:
        from core import gate_cinematic as GC

        # 1) 先量「现在有多漂」——这是给用户看的证据，不是装饰
        before: Dict[str, Any] = {}
        try:
            before = GC.measure_look(list(clips) if clips else [src])
        except Exception as e:  # noqa: BLE001
            logger.warning("电影感门：调色前测量失败：%s", e)

        # 2) 一套 look（全片共用），曝光只按段微调 ±0.1EV
        plan = GC.plan_look(list(clips) if clips else [src], genre=genre)
        vf = str(plan.get("filter") or GC.build_look_filter(plan) or "")
        if not vf:
            return None
        # 3) 统一颗粒（固定 seed：同片每一帧颗粒一致，不是随机噪点）
        try:
            grain = GC.build_grain_filter(_GRADE_GRAIN)
            if grain:
                vf = vf + "," + grain
        except Exception as e:  # noqa: BLE001
            logger.warning("电影感门：颗粒滤镜不可用（只做调色）：%s", e)

        # 4) 可选电影画幅：只对横屏源做，竖屏/方形不裁（否则构图会被裁没）
        aspect_note = ""
        target_aspect = str((settings or {}).get("cinematic_aspect", _DEFAULT_ASPECT) or "").strip()
        src_w, src_h = _probe_size(src)
        out_w, out_h = src_w, src_h
        if (target_aspect and target_aspect.lower() not in ("off", "none", "0")
                and src_w > 0 and src_h > 0 and (src_w / max(1, src_h)) >= _LANDSCAPE_MIN_RATIO):
            try:
                af = GC.aspect_filter(target_aspect, mode=str(
                    (settings or {}).get("cinematic_aspect_mode", "crop") or "crop"),
                    source_size=(src_w, src_h))
                if af:
                    vf = vf + "," + af
                    geo = GC.aspect_geometry(target_aspect, mode=str(
                        (settings or {}).get("cinematic_aspect_mode", "crop") or "crop"),
                        source_size=(src_w, src_h))
                    # 不同版本返回的键可能不同：能读到就用，读不到就按滤镜自己算
                    for kw in ("out_size", "size", "output_size"):
                        v = geo.get(kw) if isinstance(geo, dict) else None
                        if isinstance(v, (list, tuple)) and len(v) == 2:
                            out_w, out_h = int(v[0]), int(v[1])
                            break
                    aspect_note = f"画幅 {target_aspect}（crop，字幕烧在画幅内）"
            except Exception as e:  # noqa: BLE001
                logger.warning("电影感门：画幅处理跳过：%s", e)

        out = os.path.join(work_dir or ".", "graded.mp4")
        from core.assemble import _ffmpeg  # 复用同一套 ffmpeg 发现逻辑
        cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 10240:
            logger.warning("电影感门：整片调色失败，保留原画面：%s", (p.stderr or "")[-300:])
            return None

        # 5) 画幅是否真的变了：**渲染后探测**（几何推算会因为不同版本返回键名不同
        #    而拿不到尺寸，实测就踩过：滤镜裁成 1280x692，报告里却写 1280x720）。
        try:
            _pw, _ph = _probe_size(out)
            if _pw and _ph:
                out_w, out_h = _pw, _ph
        except Exception:  # noqa: BLE001
            pass

        # 6) 应用后实测：把「同一套 look 落在整片上」的漂移量再量一次
        after: Dict[str, Any] = {}
        try:
            after = GC.measure_look([out])
        except Exception:  # noqa: BLE001
            after = {}
        notes = list(plan.get("notes") or [])
        if aspect_note:
            notes.append(aspect_note)
        return {
            "path": out,
            "look": plan.get("look"),
            "filter": vf,
            "w": out_w, "h": out_h,
            "before": before,
            "after": after,
            "expected_drift_after": plan.get("expected_drift_after"),
            "notes": notes,
            "drift_before": (before or {}).get("drift") or None,
            # ★ 只有 1 个样本时 measure_look 的 drift 必然是 0（没有"段间"可比），
            #   不能拿它冒充"调色后色偏归零"。这里报计划期的推算值，并标明未实测。
            "drift_after": plan.get("expected_drift_after"),
            "drift_after_measured": False,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("电影感门失败（保留原画面）：%s", e)
        return None


# ────────────────────────────── 接缝门 ──────────────────────────────
def plan_seam_events(clips: Sequence[str], cut_times: Sequence[float], work_dir: str,
                     *, settings: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """声音侧的接缝：声锚校正 + 段落级 whoosh 焊缝（+ 段落级转场时长）。

    波导翁的做法：不要靠感觉对齐，找音频里「最猛的那块」上下对齐；拿不准就在
    接缝处叠 0.2~0.4s 音效把接缝「焊住」。这里产出可直接交给
    `core.assemble.mux_film(sfx_events=...)` 的事件表。

    返回 `{"events":[{path,start,gain,kind}], "report":..., "warnings":[...]}`；
    关闭/失败返回 None。
    """
    if not enabled(settings, "method_gates_seam"):
        return None
    if not clips or not cut_times:
        return None
    try:
        from core import gate_seam as GS
        bounds = sorted(float(t) for t in cut_times if float(t) > 0.05)
        if not bounds:
            return None
        anchor = GS.plan_sound_anchored_seams(list(clips), bounds)
        para = GS.plan_paragraph_seams(list(clips), bounds)
        events: List[dict] = []
        for seam in (para.get("seams") or []):
            w = seam.get("whoosh") or {}
            path = str(w.get("path") or "")
            if not path or not os.path.exists(path):
                continue
            try:
                gain = 10.0 ** (float(w.get("mix_gain_db", -6.0)) / 20.0)
            except Exception:  # noqa: BLE001
                gain = 0.5
            events.append({
                "path": path,
                # `cut` = 这处接缝在**拼接时间轴**上的位置；调用方用它把事件
                # 映射到成片时间轴（转场会缩短时间轴，两者不一定相等）。
                "cut": round(float(seam.get("cut") or 0.0), 3),
                "start": round(max(0.0, float(w.get("starts_at") or seam.get("cut") or 0.0)), 3),
                "gain": round(max(0.0, min(1.0, gain)), 3),
                "kind": "whoosh",
            })
        warns: List[str] = []
        if anchor.get("max_abs_delta_before") is not None:
            warns.append(
                "接缝声锚：切点到最近重拍的最大偏差 {:.0f}ms → 校正后 {:.0f}ms"
                "（超过 {:.0f}ms 的用 whoosh 焊住）".format(
                    float(anchor.get("max_abs_delta_before") or 0) * 1000,
                    float(anchor.get("max_abs_delta_after") or 0) * 1000,
                    float(anchor.get("tolerance") or 0.15) * 1000))
        if events:
            warns.append(
                f"接缝焊缝：{len(events)} 处段落边界叠了 whoosh（0.2~0.4s，峰值 -6dBFS），"
                f"另按「声音先于画面」给了 J-cut 提前量 {para.get('counts', {}).get('junction', 0)} 处换场。")
        return {"events": events, "report": {"anchor": anchor, "paragraph": para},
                "warnings": warns}
    except Exception as e:  # noqa: BLE001
        logger.warning("接缝门失败（只保留画面侧接缝处理）：%s", e)
        return None


# ────────────────────────────── 汇总 ──────────────────────────────
def summarize(reports: Dict[str, Any]) -> List[str]:
    """把四道门的结果压成给人看的中文行（进 compose 的 warnings / 结果字段）。"""
    out: List[str] = []
    d = reports.get("dialogue") or {}
    if d.get("skipped"):
        out.append("台词门：按明确的无人声要求跳过台词评分。")
    elif d and not d.get("error"):
        st = d.get("stats") or {}
        out.append(
            "台词门：{} 句检查（score {}，errors {} / warn {}）；"
            "单句 {:.1f} 字、{:.2f} 字/秒，金句 {} 句留了 0.5s 气口".format(
                st.get("lines", len(d.get("items") or [])),
                d.get("score", 0), d.get("errors", 0), d.get("warnings", 0),
                float(st.get("chars_avg") or 0.0), float(st.get("cps_avg") or 0.0),
                int(st.get("golden_sentences") or 0)))
    e = reports.get("edit") or {}
    if e.get("segments"):
        _aft = e.get("after") or {}
        if _aft.get("distinct_durations") is not None:
            out.append("导演门·切点：%s 段 / %s 个取值 / 平均 %.2fs（目标 ≥3 个取值）" % (
                _aft.get("segments"), _aft.get("distinct_durations"),
                float(_aft.get("avg_segment") or 0.0)))
        out.extend(e.get("warnings") or [])
    g = reports.get("cinematic") or {}
    if g.get("path"):
        out.append("电影感门：全片统一 look={}，统一颗粒 {}，{}".format(
            g.get("look"), _GRADE_GRAIN,
            ("画幅 {}x{}".format(g.get("w"), g.get("h")))
            if g.get("w") and g.get("h") else "画幅保持原样"))
        for n in (g.get("notes") or [])[:2]:
            out.append("电影感门：" + str(n)[:160])
    s = reports.get("seam") or {}
    if s.get("warnings"):
        out.extend(s.get("warnings") or [])
    return [x for x in out if x]
