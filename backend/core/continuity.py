# -*- coding: utf-8 -*-
"""
VideoForge · 镜头衔接与连贯性（Continuity）

═══════════════════════════════════════════════════════════════════
要解决的真实问题
═══════════════════════════════════════════════════════════════════
用户原话：
  "最关键的是后期成片上！！！以上步骤如果都做好了，下一步就是不同视频
   之间的衔接问题！这个不解决，整个软件的设定都没意义！视频是需要有观众的，
   前言不搭后语或者情景转化衔接一眼AI，这种情况没用户看的！！！"

═══ 参考两个成熟项目后的结论（重要，避免走错路）═══
精读 Pavo 逆向文档（3781 行）+ MPT 源码（video.py 68KB）后确认：

  - **MPT**：转场实现列在它自己的"未验证"清单里；它是 stock 素材检索路线，
    片段之间本来就是不同素材，谈不上连贯性设计。**无可借鉴**。
  - **Pavo**：有「首帧关键帧」范式（`Segment.keyframe_url` + 没关键帧直接
    raise ValueError）+ 运镜/景别词汇表 + 分镜重排序端点；
    但**尾帧串联、转场设计、镜头连贯性规则（轴线/180度/match cut）全都没有**。

也就是说：**"衔接"这件事两个参考项目都没解决**，必须自己设计。
好在原理是清楚的 —— 影视剪辑上"看不出来是AI"靠的是三件事：

  1. **画面连续**：下一个镜头从上一个镜头的画面里长出来
     → 用「上一镜的尾帧」当「下一镜的首帧」（image-to-video），
       这是花钱最少、效果最直接的一招，也是本模块的核心。
  2. **状态连续**：人物穿着/光线/时间/天气不能跳变
     → 把上一镜的"状态"（时段、光位、服装、情绪）带进下一镜的提示词。
  3. **节奏连续**：不能每个镜头都硬切
     → 按"同一场景内 / 跨场景但同时间 / 跨场景且跳时间"分三档，
       分别用 硬切 / 短溶解 / 长溶解+黑场，而不是一律 fade。

本模块只做**计算与素材提取**，不调模型 —— 所以在没有付费 Key 的情况下
也能完整测试。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.continuity")


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


def _as_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    return v if isinstance(v, list) else []


# ═══════════════════════════════════════════════════════════════
# 一、首/尾帧提取（衔接的原材料）
# ═══════════════════════════════════════════════════════════════

def _grab(src: str, out: str, at_end: bool) -> str:
    if not src or not os.path.exists(src):
        return ""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # 尾帧取"倒数第 0.1 秒"而不是最后一帧：很多编码最后一帧是重复帧或黑帧
    args = ([_ffmpeg(), "-y", "-sseof", "-0.1"] if at_end else [_ffmpeg(), "-y"])
    cmd = args + ["-hide_banner", "-loglevel", "error", "-i", src,
                  "-frames:v", "1", "-q:v", "2", out]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception as e:
        logger.warning("抽帧失败 %s: %s", src, e)
    return ""


def extract_last_frame(video_path: str, out_path: str) -> str:
    """取视频的**尾帧** —— 它将成为下一个镜头的首帧。"""
    return _grab(video_path, out_path, at_end=True)


def extract_first_frame(video_path: str, out_path: str) -> str:
    return _grab(video_path, out_path, at_end=False)


def supports_first_frame(provider: str, model: str) -> bool:
    """这个模型能不能接受"首帧图"（决定能否做尾帧串联）。

    优先问适配器（它最清楚自己的入参），失败再查能力目录。
    """
    try:
        from core.adapters import get_adapter
        ad = get_adapter(provider, api_key="x")
        return bool(getattr(ad, "supports_first_frame", False))
    except Exception:
        pass
    try:
        from core.model_catalog import VIDEO_CATALOG
        entry = VIDEO_CATALOG.get(provider) or {}
        m = next((x for x in (entry.get("models") or []) if x.get("id") == model), None)
        if m:
            return m.get("mode") in ("i2v", "both") or "图生视频" in (m.get("tags") or [])
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════
# 二、状态延续：把上一镜"带得走"的东西带到下一镜
# ═══════════════════════════════════════════════════════════════

# 会随剧情推进而变化的"状态"字段（与"身份"字段相对）
_STATE_KEYS = ("time_of_day", "lighting", "weather", "color_tone", "atmosphere")


def shot_state(shot: Dict[str, Any], scene: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """抽出一个镜头的"可延续状态"。

    身份（角色长相/服装）来自角色卡，是**不变**的；
    状态（时段/光位/天气/色调）是**随镜头变**的。
    把这两者分开，才谈得上"同一个角色在不同光影下仍是同一个人"。
    """
    sc = scene or {}
    ssl = _as_dict(sc.get("reference_features")).get("slots") or {}
    st = {
        "scene_id": shot.get("scene_id") or "",
        "scene_name": sc.get("name") or "",
        "time_of_day": sc.get("time_of_day") or "",
        "lighting": sc.get("lighting") or "",
        "weather": sc.get("weather") or "",
        "color_tone": ssl.get("color_tone") or "",
        "atmosphere": ssl.get("atmosphere") or "",
        "characters": [c for c in _as_list(shot.get("character_ids"))],
    }
    return {k: v for k, v in st.items() if v not in ("", None, [])}


def continuity_brief(prev_shot: Dict[str, Any], cur_shot: Dict[str, Any],
                     prev_scene: Optional[Dict[str, Any]],
                     cur_scene: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """算出"上一镜 → 这一镜"要不要保持连续、以及保持什么。

    返回：
      {
        same_scene: bool,        # 同一场景内的切镜 → 必须连续
        gap_seconds: float,      # 剧本里的时间跳跃（用 start 差近似）
        level: "tight"|"loose"|"break",
        carry: {...},            # 要带过去的状态
        notes: [...]             # 给提示词用的"连贯性指令"
      }
    """
    ps, cs = shot_state(prev_shot, prev_scene), shot_state(cur_shot, cur_scene)
    same_scene = bool(ps.get("scene_id")) and ps.get("scene_id") == cs.get("scene_id")

    # 时间跳跃：两个镜头在同一场景里，中间隔的镜头越多，越可能是"时间流逝"
    try:
        gap = float(cur_shot.get("order_index") or 0) - float(prev_shot.get("order_index") or 0)
    except (TypeError, ValueError):
        gap = 1.0

    level = "tight" if same_scene else "loose"
    notes: List[str] = []
    carry: Dict[str, Any] = {}

    if same_scene:
        # 同一场景：光线/色调/天气必须完全不变，否则观众一眼看出是拼的
        for k in ("time_of_day", "lighting", "weather", "color_tone", "atmosphere"):
            if ps.get(k):
                carry[k] = ps[k]
        notes.append("与上一个镜头是同一场景，光线、天气、色调必须**完全一致**，"
                     "只改变机位与被摄主体的动作。")
    else:
        # 跨场景：保留全片统一的东西（色调/氛围基调），时段与光位按新场景来
        for k in ("color_tone",):
            if ps.get(k) and cs.get(k):
                notes.append(f"场景切换，但全片色调基调保持（{cs.get(k)}）。")
        notes.append("场景发生变化，可以用转场交代空间转换。")

    return {
        "same_scene": same_scene,
        "gap_seconds": gap,
        "level": level,
        "carry": carry,
        "notes": notes,
        "prev_state": ps,
        "cur_state": cs,
    }


# ═══════════════════════════════════════════════════════════════
# 三、转场选择：不是所有切点都该用同一种
# ═══════════════════════════════════════════════════════════════

# 三档转场。ffmpeg 的 xfade 支持很多种，这里只用经得起看的三种。
TRANSITIONS = {
    # 同一场景内的镜头切换：硬切最自然（真实电影绝大多数是硬切）
    "tight": {"type": "none", "seconds": 0.0,
              "why": "同一场景内切镜，硬切最像真实拍摄"},
    # 跨场景但剧情连续：溶解交代"换地方了"，同时把接缝跳变摊到看不出来
    #
    # ★★ 0.9s 是**实测标定**出来的，不是拍脑袋（`tests/calibrate_seam_transition.py`，
    #    2026-09-13，项目 caa9904f 的 4 个真实跨场景接缝）：
    #      硬切       4.70× / 6.50× / 11.40× / 5.75×（自身运动 p99 的倍数）—— 一眼就跳
    #      0.2s       3.04× / 4.05× /  7.23× / 3.55×
    #      0.35s(旧)  1.40× / 1.57× /  2.85× / 1.51×   ← 全部高于 1.25× 阈值
    #      0.7s       1.27× / 1.27× /  2.50× / 1.47×
    #      0.9s       0.98× / 0.79× /  1.66× / 1.37×   ← **四个接缝上全都优于 0.7 与 1.2**
    #      1.2s       1.48× / 0.89× /  1.80× / 1.39×
    #    两个反直觉但重要的结论：
    #      ① **硬切在跨场景处是灾难**（4.7～11.4 倍）—— "真实电影都硬切"这条经验
    #         只适用于**同一场景内**的切镜，跨场景必须给观众一个过渡。
    #      ② **溶解不是越长越好**：曲线非单调（0.7 反而比 0.5 差、1.2 比 0.9 差），
    #         因为窗口内的最大跳变还取决于"叠化期间内容自己动得多快"。
    #         所以正确做法是取"最短达到阈值"的那档，而不是一路加长。
    "loose": {"type": "fade", "seconds": 0.9,
              "why": "换场景，用 0.9s 溶解交代空间转换（实测可把接缝压到自身运动量级）"},
    # 时间/地点大跳：更长一点，给观众一个呼吸
    "break": {"type": "fade", "seconds": 1.1,
              "why": "时间或地点跳跃，用长溶解划断"},
}


def chain_decision(prev_shot: Optional[Dict[str, Any]],
                   cur_shot: Optional[Dict[str, Any]],
                   *, prev_has_video: bool,
                   model_supports_first_frame: bool) -> Dict[str, Any]:
    """要不要把上一镜的尾帧当这一镜的首帧（**决策**，不碰文件/DB）。

    ★ 为什么把它抽成纯函数：这个判断原来是塞在 `main.py` 的一个闭包里，
      根本没法单独测 —— 而它是"多镜头看起来像不像一镜到底"的**总开关**。
      线上 `d5815c1e`（1 模型 / 1 场景 / 5 镜）接缝仍是自身运动尺度的 12~13 倍，
      说明这些镜头是各自独立生成的；而"该不该串"是纯逻辑，必须先能测。

    规则（按优先级）：
      1. 没有上一镜（全片第一镜）→ 不串
      2. **换了场景** → 不串。这一条原先漏了：文档写着"同场景优先"，
         代码里却根本没判场景。跨场景硬串会把白天战壕的尾帧当成夜晚宫殿的起点，
         模型被锚在错误的地点上，出来的画面既不像新场景也不像旧场景，**比不串更糟**。
         换场景本来就该硬切（观众理解"换地方了"），不需要连续性。
      3. 上一镜还没有视频 → 不串（并说明，而不是静默跳过）
      4. 当前模型不支持首帧输入 → 不串
      5. 否则 → 串

    只要两边场景 id **都已知且不同**才拦；有一边为空（老数据没挂场景）时按"未知"放过，
    保持原有行为 —— 宁可少拦，不要因为缺字段把该串的也拦掉。
    """
    out: Dict[str, Any] = {"chain": False, "why": "", "skipped_reason": ""}
    if not prev_shot:
        out["why"] = "全片第一个镜头，没有可承接的画面"
        out["skipped_reason"] = "first_shot"
        return out
    _ps = str((prev_shot or {}).get("scene_id") or "")
    _cs = str((cur_shot or {}).get("scene_id") or "")
    if _ps and _cs and _ps != _cs:
        out["why"] = ("换了场景（上一个镜头在别的场景），不接尾帧 —— "
                      "新场景应该从自己的场景图开始，硬切才符合观感")
        out["skipped_reason"] = "scene_changed"
        return out
    if not prev_has_video:
        out["why"] = "上一个镜头还没有生成视频，无法接尾帧"
        out["skipped_reason"] = "prev_no_video"
        return out
    if not model_supports_first_frame:
        p = (cur_shot or {}).get("model_provider") or ""
        m = (cur_shot or {}).get("model_name") or ""
        out["why"] = f"当前模型（{p}/{m}）不支持首帧输入，无法接尾帧"
        out["skipped_reason"] = "model_no_first_frame"
        return out
    pidx = (prev_shot.get("order_index") or 0) + 1
    out["chain"] = True
    out["why"] = f"接上一镜（第 {pidx} 个）的尾帧，画面从这里继续"
    return out


def pick_transition(prev_shot: Optional[Dict[str, Any]], cur_shot: Dict[str, Any],
                    prev_scene: Optional[Dict[str, Any]] = None,
                    cur_scene: Optional[Dict[str, Any]] = None,
                    base: str = "fade",
                    scene_change: str = "cut") -> Dict[str, Any]:
    """给一个切点挑转场。

    旧实现是"一律 fade 0.5 秒"——这就是"情景转化衔接一眼AI"的一部分：
    真实影片里同一场戏的镜头之间**几乎都是硬切**，处处溶解反而假。
    """
    if prev_shot is None:
        return {"type": "none", "seconds": 0.0, "why": "全片第一个镜头，不需要转场",
                "level": "start"}
    b = continuity_brief(prev_shot, cur_shot, prev_scene, cur_scene)
    level = b["level"]
    # 时间点/场景名完全不同 → 视为大跳
    if not b["same_scene"]:
        pn = str(b["prev_state"].get("scene_name") or "")
        cn = str(b["cur_state"].get("scene_name") or "")
        pt = str(b["prev_state"].get("time_of_day") or "")
        ct = str(b["cur_state"].get("time_of_day") or "")
        if (pn and cn and pn != cn and pt and ct and pt != ct) or b["gap_seconds"] > 1:
            level = "break"
    t = dict(TRANSITIONS.get(level, TRANSITIONS["loose"]))
    t["level"] = level
    if base == "none":
        t = {"type": "none", "seconds": 0.0, "level": level,
             "why": "用户选择了硬切"}
    # ★ 跨场景默认**硬切**（2026-09-13 用户看过两版成片后定的）。
    #   两种都成立，所以都要能表达出来：
    #     · 硬切（默认）：没有任何一帧是"混出来"的，而且"换场切一刀"是最基本的
    #       电影语法。代价是成片级 `spike_ratio` 会变大（实测 10.6）——
    #       那是**指标把"切"当成跳变**，不是缺陷（见 `film_report` 的语境说明）。
    #     · 溶解（scene_change="dissolve"）：单帧跳变小（0.79～1.66×），
    #       但混合期间是两张不相干画面叠出来的（中点细节能量 0.69～0.85×，画面偏软）。
    #   注意：显式选硬切时必须带 `respect`，否则会被 `assemble` 的自适应摊薄层
    #   按指标改回溶解 —— 用户的显式选择不许被静默覆盖。
    #
    # ★★ 2026-09-13 用户追加要求："有的衔接很突兀（突然特别亮、突然特别暗）……
    #    你可以设定转场或者给个毫秒级别的黑屏+字幕说明下一个情景发生地点都行"。
    #    于是这一层只负责给出**语义基线**（硬切），
    #    "这处换场要不要升级成黑场+地点卡"由 `assemble` 最后那一层**按实测亮度跳变**
    #    决定（`seamfix.choose_scene_treatment`）。这样语义与测量各管各的，
    #    也保证旧行为（scene_change="cut"）逐字不变。
    if scene_change not in ("dissolve", "dip") and not b["same_scene"]:
        t = {"type": "none", "seconds": 0.0, "level": level, "respect": True,
             "why": "换场景用硬切（不叠加两张不相干的画面；换场硬切是基本电影语法）"}
    if scene_change == "dip" and not b["same_scene"]:
        t = {"type": "none", "seconds": 0.0, "level": level, "respect": True,
             "why": "换场景：交给换场处理层用黑场+地点字幕卡隔开"}
    return t


# ═══════════════════════════════════════════════════════════════
# 四、串联计划：哪些镜头要接上一镜的尾帧
# ═══════════════════════════════════════════════════════════════

def chain_plan(shots: List[Dict[str, Any]], *,
               scenes: Optional[Dict[str, Dict[str, Any]]] = None,
               selected_video_of=None,
               scene_change: str = "cut") -> List[Dict[str, Any]]:
    """为整片算出一份"串联计划"。

    对每个镜头给出：
      - `use_last_frame_of`: 应该拿哪个镜头的尾帧当自己的首帧（没有则 None）
      - `transition`: 与前一个镜头之间用什么转场
      - `notes`: 连贯性指令（拼进提示词）

    规则（按重要性）：
      1. 同场景内的相邻镜头 → **一定要接尾帧**，这是"同一场戏"的观感来源
      2. 跨场景但上一镜和这一镜都有可用视频 → 也接（让空间转换有视觉延续）
      3. 第一个镜头、或上一镜没有可用视频 → 不接
    """
    scenes = scenes or {}
    out: List[Dict[str, Any]] = []
    prev = None
    for sh in shots:
        sc = scenes.get(sh.get("scene_id")) if sh.get("scene_id") else None
        psc = scenes.get(prev.get("scene_id")) if (prev and prev.get("scene_id")) else None
        trans = pick_transition(prev, sh, psc, sc, scene_change=scene_change)
        brief = continuity_brief(prev, sh, psc, sc) if prev else {
            "same_scene": False, "level": "start", "carry": {}, "notes": [],
            "gap_seconds": 0}

        use_prev = None
        if prev is not None:
            prev_video = ""
            if selected_video_of:
                try:
                    prev_video = selected_video_of(prev) or ""
                except Exception:
                    prev_video = ""
            provider = sh.get("model_provider") or ""
            model = sh.get("model_name") or ""
            can_first = supports_first_frame(provider, model)
            # 同场景内必接；跨场景也接（如果模型支持）
            if prev_video and can_first:
                use_prev = {"from_shot_id": prev.get("id"), "video": prev_video}
            elif prev_video and not can_first and brief.get("same_scene"):
                brief.setdefault("notes", []).append(
                    f"当前模型（{provider}/{model}）不支持首帧输入，无法用尾帧串联；"
                    f"建议切换到支持「图生视频」的版本，同一场戏的镜头才不会跳。")

        out.append({
            "shot_id": sh.get("id"),
            "order_index": sh.get("order_index"),
            "use_last_frame_of": use_prev,
            "transition": trans,
            "same_scene_as_prev": bool(brief.get("same_scene")),
            "carry": brief.get("carry") or {},
            "notes": brief.get("notes") or [],
        })
        prev = sh
    return out


def chain_summary(plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    """给前端/日志用的汇总"""
    chained = sum(1 for x in plan if x.get("use_last_frame_of"))
    by_level: Dict[str, int] = {}
    for x in plan:
        lv = (x.get("transition") or {}).get("level") or "?"
        by_level[lv] = by_level.get(lv, 0) + 1
    return {
        "shot_count": len(plan),
        "chained_count": chained,
        "chain_ratio": round(chained / len(plan), 3) if plan else 0.0,
        "transitions": by_level,
    }


# ═══════════════════════════════════════════════════════════════
# 五、成片时间轴映射（转场会"吃掉"时长，必须显式补偿）
# ═══════════════════════════════════════════════════════════════
#
# 这是一个**很容易被忽略、后果很严重**的点：
#   用 xfade 交叉溶解拼接 n 段、每段转场 t 秒时，
#   成片总时长 = Σdur − (n−1)×t   —— 每接一次就少 t 秒。
#   而语音/字幕是按"分镜内的相对时间"写好的
#   （"李连长在第 3 秒说话"指的是**这个分镜的第 3 秒**）。
#   如果不去补偿，从第二个镜头开始，所有台词都会**整体提前**，
#   越到后面偏得越多 —— 用户看到的就是"语音和画面对不上、特别不协调"。
#
# 所以有转场时，必须先把"每段在成片里的起点"算出来，
# 再把分镜内的相对时间映射到成片时间轴。

def final_timeline(shots: List[Dict[str, Any]],
                   plan: Optional[List[Dict[str, Any]]] = None,
                   durations: Optional[List[float]] = None) -> Dict[str, Any]:
    """算出每个分镜在**成片**里的绝对时间区间。

    返回：
      {
        shots: [{shot_id, order, film_start, film_end, local_duration,
                 transition_in, overlap_in}],
        total: float,             # 成片总时长（已扣掉转场重叠）
        timeline_shrink: float,   # 因为转场被"吃掉"的总时长
        map_local(shot_id, t)    # 把分镜内相对秒映射成成片绝对秒
      }
    """
    plan = plan or []
    plan_by_id = {p.get("shot_id"): p for p in plan if p.get("shot_id")}
    out: List[Dict[str, Any]] = []
    cursor = 0.0
    shrink = 0.0
    for i, sh in enumerate(shots):
        d = float((durations[i] if durations and i < len(durations) else None)
                  or sh.get("duration_seconds") or 5.0)
        p = plan_by_id.get(sh.get("id")) or (plan[i] if i < len(plan) else {}) or {}
        trans = (p.get("transition") or {})
        # 这一镜与前镜的重叠量（第一个镜头没有）
        overlap = float(trans.get("seconds") or 0.0) if i > 0 else 0.0
        overlap = max(0.0, min(overlap, d * 0.5))      # 不允许吃掉超过半段
        start = max(0.0, cursor - overlap)
        end = start + d
        if i > 0:
            shrink += overlap
        out.append({
            "shot_id": sh.get("id"),
            "order": sh.get("order_index", i),
            "index": i,
            "film_start": round(start, 3),
            "film_end": round(end, 3),
            "local_duration": round(d, 3),
            "transition_in": trans.get("type") or "none",
            "overlap_in": round(overlap, 3),
        })
        cursor = end

    total = out[-1]["film_end"] if out else 0.0
    by_id = {s["shot_id"]: s for s in out}

    def map_local(shot_id: str, t: float) -> float:
        """分镜内 t 秒 → 成片绝对秒。找不到该分镜时原样返回。"""
        s = by_id.get(shot_id)
        if not s:
            return float(t)
        return round(s["film_start"] + float(t), 3)

    return {
        "shots": out,
        "total": round(total, 3),
        "timeline_shrink": round(shrink, 3),
        "map_local": map_local,
    }


def remap_lines_to_film(shot_id: str, lines: List[Dict[str, Any]],
                        tl: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把某个分镜的台词/字幕从"分镜内相对时间"平移到"成片绝对时间"。

    这是语音与字幕能跟着转场一起对齐的关键一步。
    """
    f = tl.get("map_local") if isinstance(tl, dict) else None
    if not callable(f):
        return list(lines or [])
    out = []
    for ln in (lines or []):
        x = dict(ln)
        x["shot_id"] = shot_id
        x["local_start"] = ln.get("start")
        x["local_end"] = ln.get("end")
        x["start"] = f(shot_id, ln.get("start") or 0.0)
        x["end"] = f(shot_id, ln.get("end") or 0.0)
        out.append(x)
    return out
