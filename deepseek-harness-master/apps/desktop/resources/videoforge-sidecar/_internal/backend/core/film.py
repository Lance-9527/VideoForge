"""
VideoForge · 整片合成（把多个镜头拼成一条片子）

════════════════════════════════════════════════════════════════════════
为什么单独抽出来
════════════════════════════════════════════════════════════════════════
"把镜头拼成片"这套逻辑原来**只存在于 `main.py` 的一个路由闭包里**
（`async def stitch_project(...)`，嵌在 `create_app()` 里面）。
后果：

  · **agent 用不了** —— 它不是 `@REGISTRY.register` 的工具，模型看不到它。
    于是"生成完所有镜头之后怎么办"在对话里是个死路：镜头都出来了，
    但你没法说一句"拼起来"。
  · **没法单测** —— 闭包要起整个 app 才能跑。
  · 一键成片（`/api/compose/*`）走的是另一套 `core/compose.py`，
    两套逻辑各写各的，行为不一致。

这里把它抽成可复用的函数，路由和 agent 工具都调它。
**不改变原逻辑**（选片规则/字幕来源/输出路径都照抄），只是搬家 + 能测。

════════════════════════════════════════════════════════════════════════
两条拼接路径，默认用哪条
════════════════════════════════════════════════════════════════════════
1. `assemble.concat_with_transitions`（**默认**）
   接缝归一化 + 冻结重演去重 + 逐切点转场。慢一些，但 output 是对的。
2. `postprocess.stitch_videos`（`method="fast"`）
   裸 concat，`-c copy`，快。**但要求所有输入编码参数完全一致** ——
   不一致时 ffmpeg 会返回成功却产出坏文件（`assemble.py` 里记着实测：
   4 个片段 1280x720/1920x1080/1080x1080 混用，rc=0 但静默丢了 0.95 秒）。
   所以只在你确定所有片段同源同参时才用它。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.film")


def collect_clips(shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从分镜里挑出"要用的那段视频"。

    ★★ 两条**互不相同**的产出路径，必须都认（2026-09-23 真跑时踩到）：

      · `candidates` —— `orchestrator.generate_shot_handler`（老的逐镜抽卡）写的
      · `segments`   —— `core.longvideo.generate_long_video`（**现在的主路径**，
                        `generate_final` 走的就是它）写的

    只认 `candidates` 的后果**极其隐蔽**：用 `generate_final` 生成出来的镜头，
    画面文件明明在磁盘上，`compose_film` 却报"没有任何镜头有可用的视频" ——
    用户会觉得"我明明生成了，怎么拼不了"。

    这个 bug 之前没暴露，是因为 `e2e_stitch_route.py` 是**手工造 candidates** 的，
    绕过了真实链路；而 `live_fullfilm_5s.py` 走真链路就立刻炸了。
    （教训：端到端测试不能自己造数据，得走产品真正会走的那条路。）

    字段名也要兼容：适配器存 `video_path`，长视频分段存 `path`。
    """
    import json as _json

    def _load(v):
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except Exception:
                return []
        return v or []

    out: List[Dict[str, Any]] = []
    for shot in shots:
        dur = float(shot.get("duration_seconds") or 0)

        # ★ 逐镜转场：存在分镜表上（shots.transition / transition_seconds）。
        #   语义是"**进入这个镜头**的那一刀怎么接" —— 也就是它和**前一个**
        #   镜头之间的接缝。第一个镜头的值会被忽略（前面没有东西可接）。
        #   为什么放在镜头这一侧而不是接缝那一侧：接缝没有独立 id，
        #   没法在对话里指着它说"这里用溶解"；镜头有 id，能指。
        tr = (shot.get("transition") or "").strip().lower()
        try:
            tr_sec = float(shot.get("transition_seconds") or 0)
        except (TypeError, ValueError):
            tr_sec = 0.0

        # ① 先看长视频分段（现在的主路径）
        segs = _load(shot.get("segments"))
        picked = ""
        sel = None          # 命中的候选（没有走候选那条路时保持 None）
        if isinstance(segs, list) and segs:
            ok_segs = [s for s in segs
                       if isinstance(s, dict) and s.get("status") == "ok"
                       and s.get("path") and os.path.exists(str(s["path"]))]
            if ok_segs:
                ok_segs.sort(key=lambda s: s.get("index") or 0)
                paths = [str(s["path"]) for s in ok_segs]
                if len(paths) == 1:
                    picked = paths[0]
                elif paths:
                    # 一个镜头被切成了多段 → 拼成一个片段再用。
                    # 这里只**报告**，真正的段间拼接交给上层（现在没有这种镜头，
                    # 但 60 秒长镜头会出现，所以不能静默只取第一段）。
                    out.append({
                        "shot_id": shot.get("id"),
                        "segments": paths,
                        "path": paths[0],
                        "duration": sum(float(s.get("duration") or 0) for s in ok_segs) or dur,
                        "order": shot.get("order_index") or 0,
                        "multi_segment": True,
                        "transition": tr,
                        "transition_seconds": tr_sec,
                    })
                    continue

        # ② 退回老的 candidates
        if not picked:
            cands = _load(shot.get("candidates"))
            selected_id = shot.get("selected_candidate_id")
            sel = None
            if isinstance(cands, list) and cands:
                # ★★ 候选的 id 键名**有两套**，必须都认（2026-09-27 第 70 轮实测）：
                #   · `candidate_id` —— 应用自己写的（`_register_local_candidate`、
                #     `_persist_clip` 之外的旧路径）
                #   · `id`           —— `agent_tools_production._persist_clip`
                #     和把素材接进应用的那条脚本路径写的
                #   只认 `candidate_id` 的后果**不是报错，而是静默挑错片段**：
                #   实测《执棋者》第一集 5 个镜头 `selected_candidate_id` 全是
                #   `voiced_*`（**带配音**的那一版），但因为它写的是 `id` 键，
                #   这里匹配不到 → 退回"第一条候选" = **没有音轨的原始片段** →
                #   成片 `Audio: … 2 kb/s`（≈静音）。用户听到的就是"出的视频没声音"。
                #   （同类的键名坑在本仓出现过多次：`video_path`/`path`、
                #     `candidate_id`/`id` —— 凡是"按 id 找东西"的地方都要两套都试。）
                sel = next((c for c in cands if isinstance(c, dict)
                            and str(c.get("candidate_id") or c.get("id") or "") == str(selected_id)),
                           None)
                if not sel:
                    sel = next((c for c in cands if isinstance(c, dict)), None)
                    if selected_id and sel is not None:
                        # 找不到选中的那一条 = 用户选的可能已经被删/改名了。
                        # 不许当成"就是第一条"悄悄用掉 —— 必须留在返回里，
                        # 否则用户只会看到"声音没了"却查不出为什么。
                        logger.warning(
                            "镜头 %s 的 selected_candidate_id=%r 在候选里找不到，"
                            "退回第一条候选（%r）—— 这可能导致用了错误的素材",
                            str(shot.get("id"))[:8], selected_id,
                            sel.get("candidate_id") or sel.get("id"))
            if isinstance(sel, dict):
                vp = str(sel.get("video_path") or sel.get("path") or "")
                if vp and os.path.exists(vp):
                    picked = vp

        if not picked:
            continue
        out.append({
            "shot_id": shot.get("id"),
            "path": picked,
            "duration": dur,
            "order": shot.get("order_index") or 0,
            "multi_segment": False,
            "candidate_id": (sel.get("candidate_id") or sel.get("id") or "")
                            if isinstance(sel, dict) else "",
            "source": (sel.get("source") or "") if isinstance(sel, dict) else "",
            "transition": tr,
            "transition_seconds": tr_sec,
        })
    return out


#: 逐镜转场名 → assemble 用的 `type`
SHOT_TRANSITION_TYPES = {
    "cut": "cut",
    "dissolve": "fade",
    "dip": "fadeblack",
    "fadeblack": "fadeblack",
    "fade": "fade",
}


def _apply_per_shot_transitions(paths: List[str],
                                clips: List[Dict[str, Any]],
                                trans: List[Dict[str, Any]],
                                ) -> tuple:
    """把分镜表上的逐镜转场盖到全局转场方案上。

    `trans[i]` 描述的是"第 i 段与它前一段之间"的接缝，所以**第 0 个位置
    永远忽略**（前面没有东西可接）。镜头侧写的 `transition` 同理：
    它说的是"进入我这一刀怎么接"。

    返回 (新的 trans, 给人看的变化说明)。
    """
    if len(trans) < len(paths):
        trans = list(trans) + [{} for _ in range(len(paths) - len(trans))]
    else:
        trans = list(trans)
    if len(trans) > len(paths):
        trans = trans[:len(paths)]

    changed: List[str] = []
    for i in range(1, min(len(paths), len(clips))):
        name = str(clips[i].get("transition") or "").strip().lower()
        if not name or name == "auto":
            continue                      # 没设 → 保留全局模式的结果
        t = SHOT_TRANSITION_TYPES.get(name)
        if not t:
            logger.warning("镜头 %s 写了不认识的转场 %r，已忽略",
                           str(clips[i].get("shot_id"))[:8], name)
            continue
        entry: Dict[str, Any] = {"type": t}
        sec = float(clips[i].get("transition_seconds") or 0)
        if sec > 0 and t != "cut":
            entry["seconds"] = sec
        trans[i] = entry
        changed.append(f"第 {i+1} 个镜头用{name}")
    return trans, changed


def collect_subtitle_segments(shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按分镜顺序累加时间轴，生成字幕段。

    ★ 台词优先。没有台词才退回动作描述。
      旧版无条件用 `action +（expression）`，字幕变成"画面说明"，
      跟嘴里说的话对不上（用户明确否决过这种"旁白念剧情"的写法）。
    """
    from core.dialogue import timeline_lines as _tl

    segs: List[Dict[str, Any]] = []
    cur = 0.0
    for shot in shots:
        dur = float(shot.get("duration_seconds") or 10)
        timeline = shot.get("layer2_timeline") or []
        if isinstance(timeline, str):
            import json
            try:
                timeline = json.loads(timeline)
            except Exception:
                timeline = []
        lines = _tl(timeline, dur) if timeline else []
        if lines:
            for ln in lines:
                segs.append({
                    "start": cur + float(ln["start"]),
                    "end": cur + float(ln["end"]),
                    "text": ln["text"],
                    "character": ln.get("character") or "",
                })
        else:
            for item in (timeline if isinstance(timeline, list) else []):
                if not isinstance(item, dict):
                    continue
                segs.append({
                    "start": cur + float(item.get("start") or 0),
                    "end": cur + float(item.get("end") or dur),
                    "text": str(item.get("action") or "").strip(),
                })
        cur += dur
    return [s for s in segs if s["text"]]


async def compose_film(
    db,
    project_id: str,
    *,
    shot_ids: Optional[List[str]] = None,
    output_filename: str = "final.mp4",
    add_subtitles: bool = False,
    method: str = "safe",
    transition: str = "auto",
    transition_seconds: float = 0.0,
    progress=None,
    # 2026-10-20: 默认改为 True，与 main.py 所有 HTTP 端点
    #   (payload.get("with_voice", True)) 及工具语义保持一致。
    with_voice: bool = True,
) -> Dict[str, Any]:
    """把一个项目的镜头拼成一条片子。

    Args:
        db: Database
        project_id: 项目 id
        shot_ids: 只拼指定的镜头；不给就拼全部
        output_filename: 输出文件名
        add_subtitles: 是否烧录字幕（台词优先）
        method: `safe`（默认，接缝归一化+转场）或 `fast`（裸 concat）
        transition: 转场风格
            · `auto`（**默认，推荐**）—— 让 assemble **逐接缝判断**：
              能硬切就硬切；跳变太大的自动用溶解摊薄；换场用黑场。
            · `cut`      —— 一律硬切（最快、无损）
            · `dissolve` —— 一律交叉溶解（柔和，但会吃掉时长）
            · `dip`      —— 跨场景一律黑场 + 地点字幕卡
        transition_seconds: 溶解/黑场秒数；0 = 用 assemble 自己的默认
        progress: 进度回调 (0~1, 文案)
        with_voice: **先按角色配音再合成**（2026-09-27 第 70 轮）。
            用户要求「每个角色都有自己适配的音色」+「整条流程用软件自己跑通」。
            以前"真视频"这条路只拼画面：配音在**另一条路**
            （`core.compose` / `POST /voice/dub`），于是必须"先 dub 再 compose"，
            而这一步只有我写的脚本在做 —— 用户按正常流程走就永远出无声片。
            现在把它接进来：`with_voice=True` 时先 `core.dubbing.dub_project`
            给每一镜挂上音轨（**不重跑视频模型，零额外花费**），再拼接。

    Returns:
        {ok, output_path, clip_count, total_duration, skipped:[], notes, error?}
    """
    def tick(p: float, msg: str) -> None:
        if progress:
            try:
                progress(p, msg)
            except Exception:
                pass

    shots = db.list_shots(project_id) or []
    if shot_ids:
        want = set(shot_ids)
        shots = [s for s in shots if s.get("id") in want]
    if not shots:
        return {"ok": False, "error": "这个项目还没有分镜", "clip_count": 0}

    # ★ 2026-10-20：notes 提到配音之前声明 —— 配音失败时也要往里写警告
    #   （原来在本函数后半段才 `notes: List[str] = []`，提前 append 会 UnboundLocalError）
    notes: List[str] = []

    # ── 先配音，再拼（顺序不能反：配音是"挂进每一镜"的）────────────
    dub_info: Dict[str, Any] = {}
    if with_voice:
        tick(0.03, "按角色配音（每个角色用自己的音色念自己的台词）")
        try:
            from core import dubbing as _dub
            dub_info = await _dub.dub_project(db, project_id, select=True)
        except Exception as e:  # noqa: BLE001
            logger.exception("配音失败")
            dub_info = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not dub_info.get("ok"):
            # ★ 2026-10-20 改：**不再直接放弃**。
            #   原实现一旦配音没成功就 return 失败，用户看到的是
            #   「配音这一步没成功，先不拼了」→ 整个九步链路卡在最后一步。
            #   而 compose.py 自己的设计原则写的是
            #   「**绝不因为没配音而失败**：可用 silent provider 出静音片段」。
            #   实测触发场景：用户跳过出片阶段（draft/final）→ 没有任何片段，
            #   dub 一个都配不上 → 整条流程白走。
            #   现在改为：记一条明确警告，**继续拼**（成片可能无声，但用户拿到了东西），
            #   并把 with_voice 降级为 False，下游不要再声称"有声成片"。
            _dub_err = (str(dub_info.get("error") or "")
                        + "；".join(str(w) for w in (dub_info.get("warnings") or [])[:3])
                        + str(dub_info.get("summary") or ""))
            notes.append(
                "配音这一步没成功 —— 已按**无声成片**继续拼，不阻断交付。\n"
                + _dub_err.strip()
                + "\n（补齐台词 / 出好片段后重新合成即可得到有声版）")
            logger.warning("配音失败但继续合成（无声）：%s", _dub_err[:300])
            with_voice = False
        # 重新取分镜：配音把"选中候选"改成了带音轨的那一版，
        # 用旧快照拼会拼到没声音的原始片段（这正是那次静音成片的一半原因）。
        shots = db.list_shots(project_id) or []
        if shot_ids:
            want = set(shot_ids)
            shots = [s for s in shots if s.get("id") in want]

    clips = collect_clips(shots)
    if not clips:
        return {
            "ok": False, "clip_count": 0,
            "error": ("没有任何镜头有可用的视频。"
                      "每个镜头都要先出片（对话框里说\"生成第 N 个镜头\"）才能拼。"),
            "total_shots": len(shots),
        }
    if len(clips) < len(shots):
        logger.info("合成：%d 个镜头里只有 %d 个有可用视频", len(shots), len(clips))

    paths = [c["path"] for c in clips]
    tick(0.1, f"准备拼接 {len(paths)} 个片段")

    settings = db.get_all_settings() or {}
    out_dir = Path(settings.get("output_dir") or "./data/outputs") / str(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / output_filename

    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    ff = get_ffmpeg_for_postprocess()

    # ★ 被切成多段的镜头：先把它的各段拼成一个片段，再参与全片拼接。
    #   不做这一步的话，那种镜头只会用第一段 —— 成片**静默少了一截**，
    #   而用户完全不知道为什么片子变短了。
    multi = [c for c in clips if c.get("multi_segment") and c.get("segments")]
    if multi:
        from core import assemble as _asm
        fixed: List[str] = []
        for c in clips:
            segs = c.get("segments") or []
            if not (c.get("multi_segment") and len(segs) > 1):
                fixed.append(c["path"])
                continue
            merged = out_dir / f"_shot_{str(c.get('shot_id'))[:8]}_merged.mp4"
            tick(0.15, f"合并 {len(segs)} 段（镜头 {str(c.get('shot_id'))[:8]}）")
            try:
                await _asm.concat_with_transitions(
                    [str(s) for s in segs], str(merged),
                    [{"type": "cut"} for _ in segs],
                    w=0, h=0,  # 0 → 让 assemble 自己探测
                    work_dir=str(out_dir / "_work_merge"),
                )
                if merged.exists():
                    fixed.append(str(merged))
                    continue
            except Exception as e:
                logger.warning("合并分段镜头失败（改用第一段）：%s", e)
            fixed.append(c["path"])
        paths = fixed

    # 目标画幅：从第一个片段探出来，避免"按参数表猜"猜错
    w = h = 0
    fps = 30
    try:
        from core import seamfix as _sf
        st = await _sf.analyze_clip(paths[0])
        w, h = int(st.get("w") or 0), int(st.get("h") or 0)
        fps = int(round(float(st.get("fps") or 30))) or 30
    except Exception as e:
        logger.warning("探测首个片段失败（用默认 1920x1080@30）：%s", e)
    if not w or not h:
        w, h = 1920, 1080

    tick(0.25, "拼接中（会做接缝归一化，可能需要一会儿）")
    # notes 已在函数开头声明（配音失败要写警告）
    try:
        if method == "fast" and len(paths) > 1:
            from core.postprocess import stitch_videos
            await stitch_videos(paths, str(out_path), ff, method="concat",
                                total_duration=sum(c["duration"] for c in clips))
            notes.append("用了 fast 模式（裸 concat）—— 片段编码参数不一致时可能静默丢帧，"
                         "画质/接缝有问题请改用默认的 safe 模式")
        else:
            from core import assemble
            # ★★ 转场：**不要**给每个接缝塞 `{"type":"cut"}`。
            #   实测确认过 assemble 的语义：`transitions[i]` 里**没有**显式指定
            #   type 时，它才会去逐接缝判断（能硬切就硬切、跳变大的用溶解摊薄、
            #   换场用黑场）。你一旦显式写 `cut`，那层判断就**完全不会运行** ——
            #   等于每次都在要求"全程硬切"，把 assemble 最值钱的能力关掉了。
            #   （我没意识到这点时就是这么写的，转场一直是"统一硬切"。）
            mode = (transition or "auto").strip().lower()
            if mode == "cut":
                trans = [{"type": "cut"} for _ in paths]
            elif mode == "dissolve":
                trans = [{}] + [
                    {"type": "fade", **({"seconds": transition_seconds}
                                        if transition_seconds > 0 else {})}
                    for _ in paths[1:]]
            elif mode == "dip":
                trans = [{}] + [
                    {"type": "fadeblack", **({"seconds": transition_seconds}
                                             if transition_seconds > 0 else {})}
                    for _ in paths[1:]]
            else:
                # auto：留空，让 assemble 自己判断（推荐）
                trans = [{} for _ in paths]
                if transition_seconds > 0:
                    trans = [{}] + [{"seconds": transition_seconds}
                                    for _ in paths[1:]]

            # ★★ 逐镜覆盖（2026-09-24）：分镜表上写了转场的镜头，
            #    就用它自己的设置盖掉全局模式。
            #
            #    三种语义必须分清楚，这是这块最容易搞错的地方：
            #      · transition="cut"      → 显式 {"type":"cut"}
            #        （注意：显式写 cut 会**关掉** assemble 的逐接缝判断，
            #         这正是用户要的效果 —— "这一刀我就是要硬切"）
            #      · transition="dissolve" → {"type":"fade"}
            #      · transition="dip"      → {"type":"fadeblack"}
            #      · transition="auto"/空  → **不动**，保留全局模式算出来的值
            #        （空字符串不能当成 cut，否则用户没设过的镜头会被
            #         静默改成硬切 —— 那正是之前"全片统一硬切"的老毛病）
            trans, per_shot = _apply_per_shot_transitions(paths, clips, trans)
            if per_shot:
                notes.append("逐镜转场：" + "、".join(per_shot))

            res = await assemble.concat_with_transitions(
                paths, str(out_path),
                trans,
                w=w, h=h, fps=fps,
                work_dir=str(out_dir / "_work"),
                # auto 模式下让"换场"那层也能生效（它决定跨场景要不要黑场+字幕卡）
                scene_transition_mode="auto" if mode == "auto" else mode,
            )
            if isinstance(res, dict):
                notes.append(f"转场模式：{mode}；接缝处理：{res.get('mode')}；"
                             f"各段时长 {res.get('durations')}")
                if res.get("cuts"):
                    notes.append(f"各接缝转场时长 {res.get('cuts')}")
                if res.get("total"):
                    notes.append(f"拼接后总长 {res['total']}s")
                # ★★ 音轨策略要如实带出来（2026-09-27 第 70 轮）。
                #   实测事故：每一镜都有配音（76 kb/s），成片却只有 2 kb/s 静音轨，
                #   原因是归一化把每段音轨抹掉了。这个字段让"声音到底怎么处理的"
                #   一眼可见，而不是只能去听。
                _aud = res.get("audio") or {}
                if _aud:
                    audio_policy = str(_aud.get("policy") or "")
                    if _aud.get("inputs_have_audio"):
                        notes.append("音轨：" + audio_policy)
                    else:
                        notes.append("音轨：输入片段**都没有声音** —— "
                                     "成片只有静音轨（要先做配音）")
    except Exception as e:
        logger.exception("拼接失败")
        return {"ok": False, "error": f"拼接失败：{type(e).__name__}: {e}",
                "clip_count": len(paths)}

    if not out_path.exists():
        return {"ok": False, "error": f"拼接命令跑完了但没产出文件（{out_path}）",
                "clip_count": len(paths)}

    tick(0.85, "完成拼接")
    final_path = str(out_path)
    sub_path = ""

    if add_subtitles:
        try:
            from core.postprocess import generate_srt_from_timeline, burn_subtitles
            segs = collect_subtitle_segments(shots)
            if segs:
                srt = out_dir / (Path(output_filename).stem + ".srt")
                generate_srt_from_timeline(segs, str(srt))
                sub_out = out_dir / (Path(output_filename).stem + ".sub.mp4")
                await burn_subtitles(final_path, str(srt), str(sub_out), ff)
                if sub_out.exists():
                    final_path, sub_path = str(sub_out), str(srt)
                    notes.append(f"烧录了 {len(segs)} 条字幕")
            else:
                notes.append("没有可用的台词，跳过字幕")
        except Exception as e:
            logger.warning("字幕失败（正片已生成，不影响）：%s", e)
            notes.append(f"字幕生成失败（正片没问题）：{e}")

    total = 0.0
    try:
        from core import previz as pv
        total = pv.probe_duration(final_path)
    except Exception:
        pass

    # ── 成片到底有没有声音：**量出来**再报，不靠猜 ──────────────────
    #   用户原话：「出的视频没声音 ✗」。而"有没有声音"是可以用码率量出来的：
    #   实测静音轨是 `Audio: aac … 2 kb/s`，真配音是 76~192 kb/s。
    #   所以这里必须给一个**可核对**的数字，而不是只说"应该有声"。
    aud = await _probe_audio(final_path)
    if aud.get("has_audio") and (aud.get("kbps") or 0) < 8:
        notes.append(f"⚠ 成片音轨只有 {aud.get('kbps')} kb/s —— 这是**静音轨**，"
                     f"不是配音。先跑一次配音（让每个角色用自己的音色把台词念出来）"
                     f"再合成，成片才会有声音。")
    elif aud.get("has_audio"):
        notes.append(f"成片有声音：音轨 {aud.get('kbps')} kb/s、"
                     f"{aud.get('channels') or '?'} 声道")

    # ── 这一版能不能在**网页播放器里立刻播**：量 moov 的位置，不靠猜 ──────
    #   用户原话：「在对话框里面的视频看不了」，截图里是 0:00 + 播放键。
    #   根因是 `moov`（元数据）被写在文件末尾（漏了 `-movflags +faststart`）⇒
    #   浏览器要读完**整个文件**才显示时长。实测带字幕那一版正是这样。
    #   这里兜一道：不合格就重封装（`-c copy`，不重编码），并把结论如实带出去。
    streamable: Optional[bool] = None
    try:
        from core.postprocess import ensure_faststart, moov_at_end
        if moov_at_end(final_path):
            _r = await ensure_faststart(final_path, ff)
            if _r.get("changed"):
                notes.append("成片原本不能边下边播（moov 在尾部），已重封装成 faststart")
            elif _r.get("error"):
                notes.append(f"⚠ 成片无法边下边播，重封装也失败了：{_r['error']}")
        streamable = not moov_at_end(final_path)
        if not streamable:
            notes.append("⚠ 这条成片在网页里可能**卡在 0:00**（元数据在文件尾部）——"
                         "用「▶ 本机播放」或下载后看")
    except Exception as e:  # noqa: BLE001
        logger.warning("faststart 检查失败：%s", e)

    tick(1.0, "合成完成")
    return {
        "ok": True,
        "output_path": final_path,
        "output_url": f"/api/media?path={__import__('urllib.parse', fromlist=['quote']).quote(final_path)}",
        "srt_path": sub_path,
        "clip_count": len(paths),
        "total_shots": len(shots),
        "total_duration": round(total, 2),
        "resolution": f"{w}x{h}",
        "audio": aud,
        "has_voice": bool(aud.get("has_audio") and (aud.get("kbps") or 0) >= 8),
        "plays_in_browser": streamable,
        "dub": ({k: dub_info.get(k) for k in ("ok", "done", "skipped", "summary", "cast")}
                if with_voice else {}),
        "notes": notes,
        "skipped_shots": len(shots) - len(paths),
    }


async def _probe_audio(path: str) -> Dict[str, Any]:
    """量一条成片的音轨（有没有 / 多少 kb/s / 几声道）。

    ★ 为什么非要用码率判"是不是静音"：本仓实测过两种都"有音轨"的片子 ——
      真配音（AAC 76~192 kb/s）和纯静音轨（AAC 2 kb/s）。
      只看 `Audio:` 字样是**分不出**来的，而用户听到的差别就是"有声/没声"。
    """
    out: Dict[str, Any] = {"has_audio": False, "kbps": 0, "channels": ""}
    try:
        import asyncio as _aio
        import re as _re
        import subprocess as _sp
        from core.ffmpeg_manager import get_ffmpeg_for_postprocess
        ff = get_ffmpeg_for_postprocess()
        proc = await _aio.create_subprocess_exec(
            ff, "-hide_banner", "-i", str(path),
            stdout=_sp.DEVNULL, stderr=_sp.PIPE)
        _, err = await proc.communicate()
        txt = (err or b"").decode("utf-8", "replace")
        if "Audio:" not in txt:
            return out
        out["has_audio"] = True
        m = _re.search(r"Audio:.*?(\d+) kb/s", txt)
        out["kbps"] = int(m.group(1)) if m else 0
        c = _re.search(r"Audio:.*?(mono|stereo|[\d.]+ Hz)", txt)
        out["channels"] = c.group(1) if c else ""
    except Exception as e:  # noqa: BLE001
        logger.warning("量成片音轨失败（如实报未知）：%s", e)
    return out
