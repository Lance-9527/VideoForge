# -*- coding: utf-8 -*-
"""
VideoForge · 分镜分段规划（Shot Plan）

═══════════════════════════════════════════════════════════════════
要解决的真实问题
═══════════════════════════════════════════════════════════════════
用户原话：
  "用户的大部分视频模型不支持 5 秒以上的视频生成，里面提到的分镜时长根本
   达不到，比如分层定 10s 但最终只能生成 5s"

线上实测就是这样：分镜写着 `duration_seconds = 12`，而它选的模型
`doubao-seedance-1-0-pro` 只支持 5s / 10s。结果只有两种，都很糟：
  - 适配器报错 → 用户看到"生成失败"
  - 适配器自己截断 → 用户拿到 5 秒，但**剧本里 12 秒的内容塞不进去**，
    于是"生成的视频跟实际描述的完全不相干或者只有一小部分"

根因：**没有人拿"模型到底能出多长"去反向约束分镜**。
`model_catalog.py` 里明明有完整的能力矩阵（每个分辨率支持哪些时长），
但这个信息从来没有参与过分镜规划。

═══════════════════════════════════════════════════════════════════
本模块做什么
═══════════════════════════════════════════════════════════════════
`plan_shot()` 把「剧本想要的时长/内容」和「模型真实能力」对齐，产出一份
**可执行的拍摄计划**：

  1. 想要的时长不合法 → 找一组**合法时长**把它拆成 N 段（N 尽量小，省钱）
  2. 把分镜的时间线（动作+台词）**按段切开**，每段只带自己那几秒的内容
     → 每段的提示词因此是具体、对得上画面的，而不是把 12 秒的概述塞给 5 秒的模型
  3. 标出哪些段需要**首尾帧串联**（交给 continuity 模块接手）
  4. 把"做了什么调整"如实告诉用户（不偷偷改）

这个模块是纯函数：不调模型、不写库，只做计算 —— 所以能被单测充分覆盖。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from core.dialogue import timeline_lines, normalize_timeline

logger = logging.getLogger("videoforge.shotplan")

# 单次生成最多拆几段。超过这个数，费用与失败率都不可接受，
# 宁可如实告诉用户"这个模型出不了这么长"，让他换模型或缩短。
MAX_SEGMENTS = 6

# 模型不支持的时长太离谱时，允许的兜底时长
FALLBACK_DURATION = 5

# 分辨率从高到低。判定"降一档"、比较新旧分辨率都要用同一张表，
# 之前散在两个函数里各写一遍，改一处漏一处。
RES_ORDER = ["4K", "2K", "1080P", "768P", "720P", "512P", "480P"]


def _res_rank(res: str) -> int:
    """分辨率高低排名（越小越清晰）。表外的值排到最后。"""
    r = str(res or "").upper()
    return RES_ORDER.index(r) if r in RES_ORDER else len(RES_ORDER)


def _resolution_candidates(model_entry: Dict[str, Any], prefer: str) -> List[str]:
    """这个模型**所有可用**的分辨率，从 prefer 开始、由高到低。

    为什么要把所有分辨率都当候选：用户反复强调"太贵了"。
    同一段内容，1080P 拆 2 次 = 两份钱，768P 一次出完 = 一份钱，
    后者对他明显更划算 —— 该比较的是**花几次钱**，不是"分辨率越高越好"。
    """
    caps = _as_dict(model_entry.get("caps")) if model_entry else {}
    if not caps:
        return [prefer] if prefer else []
    # 统一成大写再排序，避免 "768p"/"768P" 被当成两个分辨率
    known = sorted({str(k).upper() for k in caps},
                   key=lambda r: _res_rank(r))
    p = str(prefer or "").upper()
    head = [r for r in known if _res_rank(r) >= _res_rank(p)]
    tail = [r for r in known if _res_rank(r) < _res_rank(p)]
    return (head or known) + tail


def _aspect_safe_candidates(cands: List[str], provider: str, model_id: str,
                            want_aspect: str, first_frame_governs: bool = False
                            ) -> Tuple[List[str], List[str], List[str], List[str]]:
    """在候选分辨率里**剔掉画幅会变的那些**，并如实说明剔了谁、为什么。

    ★★ 真实事故（2026-09-13，花了钱才发现的）：
      分镜画幅 16:9、要 20 秒。为省一次调用，规划器把分辨率从 1080P 降到
      768P（1080P 只给 6s、768P 给 10s）。海螺 `MiniMax-Hailuo-2.3` 在 768P 下
      返回的是 **768×768（1:1）**，而适配器 payload 里没有画幅字段 ——
      画幅是厂商按分辨率隐含决定的。结果成片归一化时**静默裁掉 44% 画面**，
      分镜写的构图、人物站位、运镜方向全部作废。

    所以"降分辨率省钱"这条优化必须带一个硬约束：**降档不能跨越画幅**。
    只有厂商实测表里"画幅不变"的分辨率才是合法候选。

    返回 `(可用候选, adjustments, warnings, 被剔除的分辨率)`：
      · 厂商画幅映射**没实测过** → 原样返回（宁可不拦，也不凭猜拦）
      · 实测过且**全都不符** → 原样返回 + 冲突说明交给上层决定（不制造死路）
    """
    if not cands:
        return cands, [], [], []
    if first_frame_governs:
        # 该型号有首帧时**画幅由首帧决定**（已实测），而降档只改变短边尺寸。
        # 我们的管线会把首帧裁成目标画幅（`aspect.conform_image`），
        # 所以这里不必为了画幅牺牲"少花一次调用"这个更重要的目标。
        return cands, [], [], []
    try:
        from core import aspect as _asp
    except Exception:
        return cands, [], [], []
    wr = _asp.parse_aspect(want_aspect)
    if not wr:
        return cands, [], [], []

    conflicts: List[Tuple[str, str]] = []      # (分辨率, 实际画幅)
    keep: List[str] = []
    for c in cands:
        bad = _asp.resolution_conflicts_aspect(provider, model_id, c, want_aspect)
        if bad:
            conflicts.append((c, bad))
        else:
            keep.append(c)

    if not conflicts:
        return cands, [], [], []
    if not keep:
        # 全都冲突 → 拦下来就是死路一条（用户会生成不出任何东西）。
        # 如实报冲突，让上层用"实测 + 明确失败"的方式处理，而不是在这里假装没事。
        notes = [f"{r} 实测出的是 {a}" for r, a in conflicts]
        return cands, [], [
            f"⚠ {want_aspect} 分镜下，该模型实测的可用分辨率**全都不是这个画幅**"
            f"（{'；'.join(notes)}）。已保留原分辨率，但成片会裁掉大量画面 —— "
            f"建议换一个画幅相符的模型/分辨率。"
        ], []
    notes = [f"{r} 实测出的是 {a}" for r, a in conflicts]
    return keep, [
        f"为了让这个镜头少花一次调用，曾考虑降分辨率；但该模型在 "
        f"{'；'.join(notes)}，与分镜画幅 {want_aspect} 不符（成片会裁掉画面），"
        f"已排除，改用 {'、'.join(keep)} 规划。"
    ], [], [r for r, _ in conflicts]


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


def allowed_durations(model_entry: Dict[str, Any], resolution: str) -> List[int]:
    """取某模型在某分辨率下**允许的时长集合**（升序）。

    能力表缺失时返回空列表 —— 调用方据此走"未知能力"的保守分支，
    而不是假装知道。
    """
    caps = _as_dict(model_entry.get("caps")) if model_entry else {}
    if not caps:
        return []
    res = str(resolution or "").upper()
    # 精确匹配优先；否则取"最接近且不高于"的分辨率（避免 1080p/1080P 大小写坑）
    if res not in caps:
        cands = [r for r in caps if str(r).upper() == res]
        if cands:
            res = cands[0]
        else:
            want = _res_rank(res)
            lower = [r for r in caps if _res_rank(r) <= want]
            res = max(lower, key=_res_rank) if lower else sorted(caps.keys())[0]
    durs = caps.get(res) or []
    out = sorted({int(d) for d in durs if isinstance(d, (int, float, str)) and str(d).isdigit()})
    return out


def pick_resolution_for_duration(model_entry: Dict[str, Any], want_seconds: int,
                                 prefer: str = "1080P") -> Tuple[str, List[int]]:
    """想要一个长时长时，找一个**能一次出完**的分辨率。

    为什么需要：同一模型常常"高分辨率只能出短、低分辨率能出长"
    （海螺 Hailuo-02：1080P 只给 6s，768P 给 6/10s）。
    与其拆成两段多花一次钱，不如降一档分辨率一次出完 —— 但必须让用户知情。
    """
    caps = _as_dict(model_entry.get("caps")) if model_entry else {}
    if not caps:
        return prefer, []
    prefer = str(prefer or "1080P").upper()
    best = None
    for res in _resolution_candidates(model_entry, prefer):   # 从高到低找
        durs = allowed_durations(model_entry, res)
        if want_seconds in durs:
            return res, durs
        if best is None and durs:
            best = (res, durs)
    return best if best else (prefer, [])


def _plan_counts_for(allowed: List[int], want: int) -> Tuple[Optional[List[int]], str]:
    """给定一个「合法时长集合」，用**最少段数**覆盖 want 秒。

    返回 `(counts, kind)`：
      - `"exact"`      want 本身就是合法时长 → 一段搞定
      - `"split"`      多段相加正好等于 want
      - `"overshoot"`  凑不出精确值，向上取一个能覆盖的（多出来的成片时裁掉）
      - `"impossible"` 这个模型在这个分辨率下出不了（allowed 为空）

    目标顺序是 **① 段数最少 ② 总时长最短**，不是"先找精确拆分"。
    这一点很反直觉但很值钱，实测两个例子：
      - sora-2 合法 [5,10,20]、要 15s：精确拆分是 10+5（**两次**调用），
        而直接要一段 20s 只要**一次**调用（多出的 5 秒成片时裁掉）。
      - luma ray-2 合法 [5,9]、要 15s：精确拆分是 5+5+5（**三次**），
        而 9+9=18 只要**两次**。
    这些模型都是"按次/按时长档位"计费的，段数就是账单。
    依据 MPT 的注释伦理：生成多了可以在合成时裁掉，生成少了内容就缺一块。
    """
    if not allowed or want <= 0:
        return None, "impossible"
    durs = sorted({int(d) for d in allowed if int(d) > 0})
    if not durs:
        return None, "impossible"
    if want in durs:
        return [want], "exact"

    # 逐段数递增地做可达和（DP）。第一个能覆盖 want 的段数就是最少段数；
    # 同一段数里取总时长最短的那个组合。数值都很小（段数 ≤ MAX_SEGMENTS，
    # 时长 ≤ 几十秒），状态空间微不足道。
    reach: Dict[int, List[int]] = {0: []}
    best: Optional[Tuple[int, List[int]]] = None
    for _n in range(1, MAX_SEGMENTS + 1):
        nxt: Dict[int, List[int]] = {}
        for s, seq in reach.items():
            for d in durs:
                t = s + d
                if t not in nxt:
                    nxt[t] = seq + [d]
        reach = nxt
        hits = [(t, seq) for t, seq in reach.items() if t >= want]
        if hits:
            t, seq = min(hits, key=lambda kv: (kv[0], len(kv[1])))
            best = (t, seq)
            break
    if best is None:
        return None, "impossible"
    total, seq = best
    # 长的段排前面（"先满后尾"）：多出来的余量落在最后一段的尾部，
    # 观感上是"这一镜收尾多留了一点"，而不是中间突然变慢。
    seq = sorted(seq, reverse=True)
    return seq, ("exact" if total == want else "overshoot")


def _split_counts(total: int, allowed: List[int]) -> Optional[List[int]]:
    """把 total **精确**拆成若干个 allowed 里的时长（凑不出返回 None）。

    保留它是给需要"精确等长"的场景（如按节拍切），
    常规规划走 `_plan_counts_for` —— 后者允许轻微超出以换取更少调用。
    """
    if not allowed or total <= 0:
        return None
    if total in allowed:
        return [total]
    allowed = sorted({d for d in allowed if d > 0})
    if not allowed:
        return None
    n_max = min(MAX_SEGMENTS, max(1, -(-total // min(allowed))))   # ceil
    best: Optional[List[int]] = None
    # 深度优先找段数最少的组合
    def dfs(remain: int, acc: List[int]) -> None:
        nonlocal best
        if best is not None and len(acc) >= len(best):
            return
        if remain == 0:
            if best is None or len(acc) < len(best):
                best = list(acc)
            return
        if len(acc) >= n_max:
            return
        for d in sorted(allowed, reverse=True):
            if d <= remain:
                acc.append(d)
                dfs(remain - d, acc)
                acc.pop()
    dfs(int(total), [])
    return best



def _slice_timeline(timeline: List[Dict[str, Any]], start: float, end: float,
                    duration: float, *, is_last: bool = False) -> List[Dict[str, Any]]:
    """把整段的时间线按 [start, end) 切出来，并把时间戳**归零**到本段内。

    ★ 归属判定用**中点**，不用"是否与窗口重叠"。
      这不是洁癖，是治一个真实的病：分镜标称 26s 切成 4×6s 时，
      第 1 个动作的区间是 [0, 6.5)，它同时"重叠"第 1 段和第 2 段。
      按重叠判定，两段都会带上"他推开门" —— 于是第 2 段开头
      把第 1 段已经演过的动作**又演一遍**，用户看到的就是
      "生成的视频跟实际描述的完全不相干或者只有一小部分"。
      **一个动作只能属于一段**：落在哪一段的中点范围内，就归哪一段。
    """
    out: List[Dict[str, Any]] = []
    span = max(0.001, float(end) - float(start))
    for it in timeline:
        try:
            a, b = float(it["start"]), float(it["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if b < a:
            a, b = b, a
        mid = (a + b) / 2.0
        # 最后一段的右边界取闭区间，否则正好卡在片尾的动作会整条丢掉
        inside = (start <= mid <= end) if is_last else (start <= mid < end)
        if not inside:
            continue
        na = round(max(0.0, a - start), 3)
        nb = round(min(span, b - start), 3)
        if nb <= na:
            # 中点落在本段、但整条区间都在段外（理论到不了）→ 兜一个最小片，
            # 宁可给它 0.5s 也别把它整条丢掉（丢掉=这一段没有内容可演）
            na, nb = 0.0, round(min(span, 0.5), 3)
        seg = dict(it)
        seg["start"], seg["end"] = na, nb
        # 台词跟着它自己的动作走 —— 动作只属于一段，台词自然也只会念一遍，
        # 不会出现"同一句台词在相邻两段各配一次音"。
        out.append(seg)
    return out


def _rescale_timeline(timeline: List[Dict[str, Any]], k: float) -> List[Dict[str, Any]]:
    """把整条时间轴按比例 k 缩放（音频是主轴时用）。

    分镜的标称时间轴只是"草稿"：台词按标称时长铺开，所以"最后一句在第 12 秒结束"
    并不代表它真的要念 12 秒。真实语音长度由配音实测决定，
    这时要把时间轴同比缩放，段切片才落得对。
    """
    out: List[Dict[str, Any]] = []
    for it in timeline:
        x = dict(it)
        try:
            x["start"] = round(float(it["start"]) * k, 3)
            x["end"] = round(float(it["end"]) * k, 3)
        except (KeyError, TypeError, ValueError):
            pass
        out.append(x)
    return out


def plan_shot(
    shot: Dict[str, Any],
    *,
    model_entry: Optional[Dict[str, Any]] = None,
    resolution: str = "",
    want_duration: Optional[float] = None,
    audio_seconds: float = 0.0,
    allow_resolution_downgrade: bool = True,
    provider: str = "",
    first_frame_governs: bool = False,
) -> Dict[str, Any]:
    """把分镜和模型能力对齐，产出分段拍摄计划（纯计算）。

    ★ `audio_seconds`：**配音的真实时长**（由 `voicecast` 实测得到，不是估算）。
      这是从 MoneyPrinterTurbo 学来的最关键一条 —— 它是"音频主轴"：
      全片长度由**实测的配音音频**决定，画面去铺满它，
      **从不要求模型单次输出覆盖剧本时长**。

      我们这边的对应做法：先把台词合成出来量出真实长度，再按这个长度规划分镜。
      这样"分镜标称 10s"就不再是硬指标 —— 真正要覆盖的是"这段话念完需要几秒"。
      实测（`tests/test_audio_driven_plan.py`）：标称 12s 但台词只念 6.8s 时，
      按音频规划只需 1 段；标称 10s 而台词要 13s 时，规划出 2 段而不是被压到 6s。

    返回：
      {
        ok, model_max, requested, effective, segments: [...],
        adjustments: [...], warnings: [...], needs_chain, source
      }
    """
    entry = model_entry or {}
    nominal = float(shot.get("duration_seconds") or 5)
    timeline = normalize_timeline(shot.get("layer2_timeline"), duration=nominal)
    adjustments: List[str] = []
    warnings: List[str] = []
    # ★ 静态能力表若被**实测时长台账**改过（"要 10s 实际只给 5.6s"），
    #   必须把这件事说出来 —— 否则用户看到的是"我明明选了 10 秒，为什么给我拆成 6+6"。
    if provider and entry.get("id"):
        try:
            from core.model_catalog import caps_for_notes
            adjustments.extend(caps_for_notes(provider, str(entry.get("id"))))
        except Exception:
            pass

    # ── 决定"到底要多长" ──
    #   优先级：**配音实测 > 用户指定 > 标称（并做"台词放不下"的兜底检查）**
    #   为什么用户指定要排在"最后一句台词结束点"前面：
    #   标称时间轴上的时间戳是**草稿**（LLM 按标称时长铺的），不是事实。
    #   早先这里拿草稿的 last_line_end 去盖用户明确指定的时长，
    #   结果用户选 5 秒、规划却给出 10.4 秒 —— 用户说的不算数，这是错的。
    #   台词真的放不下时，改为**如实警告**，而不是偷偷把时长改掉。
    from core.dialogue import timeline_lines as _tls
    _lines = _tls(timeline, nominal)
    last_line_end = max([float(x["end"]) for x in _lines], default=0.0)
    source = "标称时长"
    requested = float(want_duration or nominal)
    if want_duration:
        source = "用户指定"
    if audio_seconds and audio_seconds > 0.2:
        # ★ 音频是主轴（学 MoneyPrinterTurbo）：
        #   分镜的标称时间轴只是"草稿"，真实的语音长度由配音实测决定。
        #   所以要把**整条时间轴按 音频长度/标称长度 同比缩放**，
        #   否则 last_line_end 仍是标称值（比如 12s），会把音频的结果顶掉
        #   —— 这正是"标称 10s、台词只念 7s，却仍按 10s 规划"的原因。
        #   音频能盖过"用户指定"，因为它是**实测事实**，而用户指定的是猜的。
        speech_span = max(last_line_end, 0.1)
        tail_ratio = max(0.0, (nominal - speech_span)) / max(nominal, 0.1)
        requested = audio_seconds * (1.0 + tail_ratio) + 0.4
        source = "配音实测时长"
        k = requested / max(nominal, 0.1)
        if abs(k - 1.0) > 0.02:
            timeline = _rescale_timeline(timeline, k)
            adjustments.append(
                f"按配音实测时长重排：分镜标称 {int(round(nominal))} 秒，"
                f"台词实际念完只要 {audio_seconds:.1f} 秒，"
                f"时间轴已同比缩放到 {requested:.1f} 秒。")
    elif last_line_end > 0.5 and last_line_end > requested + 0.5:
        if want_duration:
            # 用户明确指定了时长 → **以用户为准**，但要如实提醒"台词可能放不下"。
            # 偷改用户的数字是最糟的做法：他以为是 5 秒，拿到的是 10 秒，
            # 账单也跟着变了，而他还不知道为什么。
            warnings.append(
                f"这段的台词在时间轴上铺到 {last_line_end:.1f} 秒，"
                f"超过你指定的 {int(round(requested))} 秒；"
                f"已按你说的 {int(round(requested))} 秒生成 —— "
                f"台词会更紧凑。想让它完整展开就把时长调长，"
                f"或者到「分镜」页精简这段的台词。")
        else:
            # 没有用户指定、也没有音频 → 拿标称时间轴做兜底检查
            requested = last_line_end + 0.4
            source = "最后一句台词结束点"
    requested_i = max(1, int(round(requested)))
    res = str(resolution or shot.get("resolution") or "1080P")

    if abs(requested_i - int(round(nominal))) >= 1 and not want_duration \
            and source != "配音实测时长":
        adjustments.append(
            f"按{source}规划：分镜标称 {int(round(nominal))} 秒，"
            f"实际需要覆盖 {requested_i} 秒。")

    # ── 模型能力未知：如实说明，按最保守的 5 秒走 ──
    if not entry or not _as_dict(entry.get("caps")):
        warnings.append("未知该模型的时长能力，按最保守的 5 秒规划。"
                        "到「设置 → 视频模型」选一个带能力标注的版本可以避免这个提示。")
        allowed = [FALLBACK_DURATION]
        res_used = res
        model_max = FALLBACK_DURATION
        counts, kind = _plan_counts_for(allowed, requested_i)
        if not counts:
            counts, kind = [FALLBACK_DURATION], "impossible"
    else:
        # ══════════════════════════════════════════════════════════════
        # 选分辨率 + 分段数：目标函数是**花几次钱**，不是"分辨率越高越好"
        # ══════════════════════════════════════════════════════════════
        # 用户的约束很直接："太贵了"。同一段内容：
        #   1080P 拆 2 次 = 两份钱    vs    768P 一次出完 = 一份钱
        # 后者明显更划算。旧实现只在"想要的秒数**正好等于**降档后的合法时长"
        # 时才降档，于是 1080P/9s 这种情况直接掉进"拆两段"，
        # 白多花一次调用（线上实测：标称 12s、台词实测 6.1s 的海螺镜头
        # 被规划成 6+6=12s 两次调用，而 768P 一次 10s 就够了）。
        #
        # 现在对**所有可用分辨率**各算一遍分段方案，按这个优先级挑：
        #   ① 调用次数最少  ② 次数相同时分辨率最高  ③ 总时长最短（不浪费）
        #   ① 调用次数最少  ② 次数相同时**贴近用户选的分辨率**  ③ 再比清晰度
        #   ② 很关键：次数相同时绝不能偷偷把 512P 升成 1080P ——
        #   画质是上去了，但**账单也上去了**，而用户明确说过"太贵了"。
        #   用户选了什么就给他什么，除非换分辨率能少花一次钱。
        cand_res = _resolution_candidates(entry, res) if allow_resolution_downgrade else [res]
        # ★★ 画幅硬约束：降分辨率**不能跨越画幅**。
        #    （海螺 2.3 实测 768P=1:1、1080P=16:9；为省调用降档会让 16:9 分镜
        #     拿到方形片 —— 见 `core/aspect.py` 里的真实事故记录。）
        want_aspect = str(shot.get("aspect_ratio") or "16:9")
        cand_res, _asp_adj, _asp_warn, _asp_dropped = _aspect_safe_candidates(
            cand_res, provider, str(entry.get("id") or ""), want_aspect,
            first_frame_governs=first_frame_governs)
        adjustments.extend(_asp_adj)
        warnings.extend(_asp_warn)
        prefer_rank = _res_rank(res)
        best_opt = None
        # 首选分辨率下的方案单独留一份：降档说明里要说"在 1080p 下要拆 2 次"，
        # 这个 2 必须是**首选分辨率真的算出来的**次数。
        # （早先这里误用了最终 counts 的长度还套了个 max() 兜底，
        #   于是 1080p 明明一次能出完也报"要拆 2 次" —— 假数字比不说还糟。）
        base_opt = None
        for cand in cand_res:
            durs_c = allowed_durations(entry, cand)
            if not durs_c:
                continue
            counts_c, kind_c = _plan_counts_for(durs_c, requested_i)
            key = (len(counts_c) if counts_c else 99,
                   abs(_res_rank(cand) - prefer_rank),   # ② 别乱动用户选的画质
                   _res_rank(cand),                      # ③ 同等距离时取更清晰的
                   sum(counts_c or []))
            opt = (key, cand, durs_c, counts_c, kind_c)
            if str(cand).upper() == str(res).upper():
                base_opt = opt
            if counts_c and (best_opt is None or key < best_opt[0]):
                best_opt = opt
        if best_opt is None:
            # 该模型在哪个分辨率下都出不了这个长度 → 用首选分辨率的最长时长兜底
            _d = allowed_durations(entry, res) or [FALLBACK_DURATION]
            allowed = _d
            res_used = res
            model_max = max(_d)
            counts, kind = [max(_d)], "impossible"
        else:
            _, res_used, allowed, counts, kind = best_opt
            model_max = max(allowed)
            # ★ 只有**真的换了分辨率**才说这句话，而且要大小写无关地比
            #   （分镜里存的是 "1080p"，能力表里是 "1080P"，字符串直接 != 会误报）
            if str(res_used).upper() != str(res).upper():
                bc = len(base_opt[3]) if (base_opt and base_opt[3]) else 0
                if str(res).upper() in {str(x).upper() for x in _asp_dropped}:
                    # 用户首选的分辨率**存在**，只是因为实测画幅不符被排除。
                    # 这时绝不能说"该模型没有这个分辨率"（假话）——
                    # 上面 _aspect_safe_candidates 已经给了准确的说明，这里不再重复。
                    pass
                elif bc > len(counts):
                    # 真省钱：换分辨率少花一次调用
                    how = (f"一次出完 {sum(counts)} 秒" if len(counts) == 1 else
                           f"只要 {len(counts)} 段"
                           f"（{'+'.join(str(c) for c in counts)}）")
                    adjustments.append(
                        f"「{entry.get('label') or entry.get('id')}」在 {res} 下要拆 "
                        f"{bc} 次才够 {requested_i} 秒；改用 {res_used} 可以{how}"
                        f"（少 {bc - len(counts)} 次模型调用、少一份钱）。")
                else:
                    # 换分辨率不是为了省钱（比如用户选的分辨率模型根本没有）→
                    # 如实说明换了，不要编一个"省了钱"的理由出来
                    adjustments.append(
                        f"「{entry.get('label') or entry.get('id')}」没有 {res}，"
                        f"已按它支持的 {res_used} 规划"
                        f"（{len(counts)} 段 {sum(counts)} 秒）。")

    # ── 把结果如实讲给用户听（不偷偷改） ──
    total_planned = sum(counts)
    if kind == "impossible":
        adjustments.append(
            f"分镜写了 {requested_i} 秒，但模型的合法时长只有 "
            f"{'/'.join(str(d) + 's' for d in allowed)}，无法凑出 {requested_i} 秒；"
            f"已按最长 {total_planned} 秒生成。"
            f"要完整还原请换一个支持更长时长的模型。")
        warnings.append("时长被压缩，剧本内容可能装不满 —— "
                        "建议缩短这段分镜的字数或换模型。")
    elif len(counts) == 1:
        if total_planned == requested_i:
            pass                       # 一段正好，没什么好解释的
        else:
            adjustments.append(
                f"分镜需要 {requested_i} 秒，但模型的合法时长只有 "
                f"{'/'.join(str(d) + 's' for d in allowed)}，凑不出精确值；"
                f"已按 {total_planned} 秒生成，多出的部分不会浪费 —— "
                f"成片按实际内容裁切。")
    else:
        adjustments.append(
            f"分镜写了 {requested_i} 秒，模型单次最长 {model_max} 秒；"
            f"拆成 {len(counts)} 段（{'+'.join(str(c) for c in counts)}），"
            f"段间用<b>尾帧接首帧</b>串起来，观感是连续的一个镜头。")

    # ★ 统一在这里定 effective：上面每个分支各自赋值过一次，漏掉一个就是
    #   UnboundLocalError（踩过）。改成"先定 counts，再算 effective"，只此一处。
    effective = sum(counts)

    # ── 生成分段明细 ──
    segments: List[Dict[str, Any]] = []
    cursor = 0.0
    for i, dur in enumerate(counts):
        seg_tl = _slice_timeline(timeline, cursor, cursor + dur, dur,
                                 is_last=(i == len(counts) - 1))
        lines = timeline_lines(seg_tl, dur)
        segments.append({
            "index": i,
            "start": round(cursor, 3),
            "end": round(cursor + dur, 3),
            "duration": dur,
            "resolution": res_used,
            # 本段真正要演的内容（不是整段的概述）—— 这是"画面与描述对得上"的关键
            "timeline": seg_tl,
            "lines": lines,
            "line_count": len(lines),
            "line_chars": sum(x["chars"] for x in lines),
        })
        cursor += dur

    total_chars = sum(s["line_chars"] for s in segments)
    if timeline and total_chars == 0:
        warnings.append("这段分镜没有可念的台词 —— 配音会退化成朗读剧情概述。"
                        "到「分镜」页点「🎬 补全台词」可以让 AI 按剧情写出真正的对白。")

    # ★ 「这段分镜没有任何画面内容」——这是"生成的视频跟描述完全不相干"里
    #   最容易被误判成"模型不行"的一种：分镜的时间线是空的、第 1 层概述也是空的，
    #   于是提示词里**根本没有"画面内容"这一句**，模型只能自由发挥。
    #   实测线上 58 个分镜里有 1 个是这种（空 timeline + 空 layer1）。
    #   以前它一声不响地生成，现在明确拦一道。
    n_actions = sum(1 for s in segments for it in (s.get("timeline") or [])
                    if isinstance(it, dict) and str(it.get("action") or "").strip())
    if n_actions == 0 and not str(shot.get("layer1_overview") or "").strip():
        warnings.append(
            "⚠ 这段分镜**没有任何画面内容**（时间线是空的、第 1 层概述也是空的），"
            "模型拿不到要拍什么，出来的画面自然跟你的预期不相干。"
            "请先在「分镜」页把内容补上（重新生成这一镜，或手工填写各层）。")

    # ── 画幅自检：这个"计划"最终会不会因为画幅被裁掉画面 ──
    #   规划是被用户当"事实"看的（拍摄计划面板、付费前的预览），
    #   所以这里就把实测画幅摆出来：16:9 分镜 + 实测 1:1 的分辨率 = 会裁 44%。
    _want_ar = str(shot.get("aspect_ratio") or "16:9")
    aspect_note: Dict[str, Any] = {"want": _want_ar, "actual": "", "ok": True}
    try:
        from core import aspect as _asp2
        _prov = provider or ""
        _act = _asp2.expected_aspect(_prov, str(entry.get("id") or ""), res_used)
        aspect_note["actual"] = _act or ""
        _m = _asp2.resolutions_matching_aspect(
            _prov, str(entry.get("id") or ""), _want_ar, _as_dict(entry.get("caps")))
        _conf = _asp2.resolution_conflicts_aspect(
            _prov, str(entry.get("id") or ""), res_used, _want_ar)
        if _act and _conf and not first_frame_governs:
            # ⚠ 只有"文生视频"时分辨率才决定画幅；有首帧的型号由首帧决定，
            #   管线会把首帧裁成目标画幅，所以这里不能误报。
            aspect_note["ok"] = False
            aspect_note["alternatives"] = _m
            warnings.append(
                f"⚠ 画幅：分镜要 {_want_ar}，而 {res_used} 实测出的是 {_act} ——"
                f"成片会裁掉大量画面。"
                + (f"画幅相符的分辨率：{'、'.join(_m)}。" if _m else ""))
    except Exception:
        pass

    return {
        "ok": True,
        "model": entry.get("id") or "",
        "model_label": entry.get("label") or "",
        "resolution": res_used,
        "resolution_changed": res_used != res,
        "aspect": aspect_note,
        "requested": requested_i,
        "nominal": int(round(nominal)),
        "duration_source": source,
        "audio_seconds": round(float(audio_seconds or 0), 2),
        "effective": effective,
        "model_max": model_max,
        "allowed": allowed,
        "segments": segments,
        "segment_count": len(segments),
        "needs_chain": len(segments) > 1,
        "adjustments": adjustments,
        "warnings": warnings,
        "total_chars": total_chars,
    }


def plan_summary(plan: Dict[str, Any]) -> str:
    """一行话总结（给 Toast / 日志用）"""
    if not plan or not plan.get("ok"):
        return "无法规划"
    n = plan.get("segment_count") or 0
    if n <= 1:
        return f"一次生成 {plan.get('effective')}s @ {plan.get('resolution')}"
    return (f"拆 {n} 段（{'+'.join(str(s['duration']) for s in plan['segments'])}）"
            f"@ {plan.get('resolution')}，尾帧接首帧串联")
