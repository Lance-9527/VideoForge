"""
VideoForge · 编排器

4 步流程的核心编排（借鉴 Pavo + Skill 手册）：
1. 剧本 → 2. 资产库 → 3. 生成视频 → 4. 后期

主要功能：
- 自定义时长（任意秒数，自动分段拼接）
- 中途换人物/场景/模型
- 多模型适配器调度
- 角色一致性（reference images + IP-Adapter 风格描述）
"""

import asyncio
import os
import shutil
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import httpx

from .db import Database
from .task_queue import TaskQueue, register_task, make_progress_updater
from .adapters import VideoGenRequest, VideoGenResult, get_adapter, list_providers
from .llm import LLMClient, ScriptService, AbstractionService


logger = logging.getLogger("videoforge.orchestrator")


# ──────────── 任务处理器 ────────────


def _media_url(local_path: Optional[str]) -> Optional[str]:
    """把本地绝对路径转成前端可访问的 /api/media URL。

    ★ v1.4.3：WebView 里 file:// 常被禁用，视频必须走 HTTP 才能在对话框里播。
    """
    if not local_path:
        return None
    try:
        p = os.path.abspath(str(local_path))
        if not os.path.exists(p):
            return None
        from urllib.parse import quote
        return "/api/media?path=" + quote(p, safe="")
    except Exception:
        return None


def _make_preview_frame(local_path: Optional[str], db=None) -> Optional[str]:
    """从视频抽第一帧当封面（用户要求图片和视频都在对话框里展示）。"""
    if not local_path or not os.path.exists(str(local_path)):
        return None
    try:
        import subprocess
        ff = None
        if db is not None:
            try:
                ff = (db.get_all_settings() or {}).get("ffmpeg_path")
            except Exception:
                ff = None
        if not ff or not os.path.exists(ff):
            ff = shutil.which("ffmpeg") or "ffmpeg"
        out = str(local_path) + ".jpg"
        subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(local_path), "-frames:v", "1", "-q:v", "3", out],
            check=True, capture_output=True, timeout=60,
        )
        if not os.path.exists(out):
            return None
        from urllib.parse import quote
        return "/api/local-image?path=" + quote(os.path.abspath(out), safe="")
    except Exception as e:
        logger.debug("抽封面失败（不影响视频展示）: %s", e)
        return None


def _extract_last_frame(video_path: str, shot_id: str = "") -> Optional[str]:
    """从视频抽**最后一帧**存成 jpg，返回本地路径。

    ★ v1.4.4："尾帧接力"的真正实现 —— 上一镜的最后一格画面，
      就是本镜首帧该接住的东西。ffmpeg 用 -sseof -1 取尾部 1 秒再取首帧。
    """
    if not video_path or not os.path.exists(str(video_path)):
        return None
    try:
        import subprocess
        out = os.path.join(os.path.dirname(str(video_path)),
                           f"lastframe_{(shot_id or 'x')[:8]}.jpg")
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            return out
        ff = shutil.which("ffmpeg") or "ffmpeg"
        subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-sseof", "-1", "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", out],
            check=True, capture_output=True, timeout=90,
        )
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            return out
        return None
    except Exception as e:
        logger.debug("抽尾帧失败: %s", e)
        return None

@register_task("generate_shot")
async def generate_shot_handler(payload: Dict[str, Any],
                                  progress_callbacks: List) -> Dict[str, Any]:
    """生成分镜视频"""
    from main import get_app_state

    state = get_app_state()
    db: Database = state["db"]
    queue: TaskQueue = state["queue"]

    shot_id = payload["shot_id"]
    num_candidates = payload.get("num_candidates", 4)
    override_provider = payload.get("model_provider")
    override_model = payload.get("model_name")
    # ★ v1.4.2: 转镜强约束（chain_config）
    #   agent 侧算好 chain_plan 后把当前镜的约束塞进 payload：
    #     {prev_shot_id, transition_type, must_match_first_frame_desc}
    #   这里把它写进 shot.reference_features['chain'] 并**注入提示词** ——
    #   原来 chain_plan 算出来的约束没人读，等于白算。
    chain_config = payload.get("chain_config") or {}

    update = make_progress_updater(payload.get("_task_id", ""), db, progress_callbacks)

    shot = db.get_shot(shot_id)
    if not shot:
        raise ValueError(f"Shot not found: {shot_id}")

    # 更新 shot 状态
    db.update_shot(shot_id, status="generating", progress=0.0)
    update(0.05, "准备生成")

    # ★ v1.4.2: 把 chain 约束持久化到 shot（前端/后续检查能看到这一镜被要求接什么）
    if chain_config:
        try:
            _shot = db.get_shot(shot_id) or {}
            _rf = _shot.get("reference_features")
            if isinstance(_rf, str):
                try:
                    _rf = json.loads(_rf or "{}")
                except Exception:
                    _rf = {}
            _rf = _rf or {}
            _rf["chain"] = {
                "prev_shot_id": chain_config.get("prev_shot_id"),
                "transition_type": chain_config.get("transition_type", "none"),
                "must_match_first_frame_desc": chain_config.get("must_match_first_frame_desc", ""),
            }
            db.update_shot(shot_id, reference_features=json.dumps(_rf, ensure_ascii=False))
        except Exception as e:
            logger.warning("写入 chain 约束失败: %s", e)

    # 获取适配器
    provider = override_provider or shot["model_provider"]
    model_name = override_model or shot["model_name"]
    settings = db.get_all_settings()
    api_keys = settings.get("api_keys", {})
    api_key = api_keys.get(provider, "")

    adapter = get_adapter(provider, api_key=api_key, config={"model_name": model_name})

    # 构造请求
    # 角色参考图
    # ⚠ 必须转成 URL / data URI：本地路径 API 不认（这是"配了 Key 也用不了模型"的直接原因）
    from .adapters import to_image_ref
    char_refs = []
    for cid in shot.get("character_ids", []):
        c = db.get_character(cid)
        if c and c.get("reference_image_path"):
            ref = to_image_ref(c["reference_image_path"])
            if ref:
                char_refs.append(ref)

    # 场景参考图
    scene_ref = None
    if shot.get("scene_id"):
        s = db.get_scene(shot["scene_id"])
        if s and s.get("reference_image_path"):
            scene_ref = to_image_ref(s["reference_image_path"])

    # ★ v1.4.3 尾帧接力（"衔接丝滑"的硬环节）
    #   chain_config.use_last_frame_of = {"from_shot_id":..., "video": <上一镜视频路径>}
    #   上一镜的**尾帧**直接当本镜的首帧 —— 提示词里写"要接得上"是软的，
    #   真正把画面接住的是这张图。
    chain_ref = None
    _clo = (chain_config or {}).get("use_last_frame_of") or {}
    _cv = _clo.get("video")
    if _cv:
        try:
            if os.path.exists(str(_cv)):
                # ★ v1.4.4: 首帧必须是**图片**，不能是视频文件！
                #   原来直接把上一镜的 mp4 当 first_frame 传过去，
                #   平台侧直接报 "build_body / 生成失败"。
                #   正确做法：用 ffmpeg 抽上一镜的**最后一帧**当本镜首帧 ——
                #   这才是真正的"尾帧接力"（画面能接住）。
                _lf = _extract_last_frame(str(_cv), shot_id)
                if _lf:
                    # 本地路径要转 data URI —— 平台只认 URL / data URI
                    chain_ref = to_image_ref(_lf)

        except Exception as _fe:
            logger.warning("抽上一镜尾帧失败（该镜改用场景图/无首帧）: %s", _fe)

    # 拼装完整提示词（三层）
    full_prompt = _compose_full_prompt(shot)

    # ★ v1.4.2: 转镜约束进提示词（这是"衔接丝滑"真正落地的一步）
    _chain_notes = []
    _must = chain_config.get("must_match_first_frame_desc")
    if _must:
        _chain_notes.append(f"[转镜衔接] {_must}")
    _t_type = chain_config.get("transition_type")
    if _t_type and _t_type != "none":
        _chain_notes.append(f"[转场方式] {_t_type}")
    if _chain_notes:
        full_prompt = full_prompt + (chr(10) * 2) + (chr(10)).join(_chain_notes)

    # ★ v1.4.3: 用模型能力表把"分辨率 / 时长"校正到**该模型真支持**的组合。
    #   以前直接用 shot 里存的值 —— 例如 doubao-seedance-2-0-fast 根本不支持
    #   1080p（只支持 480P/720P），用户拿到的是
    #   "InvalidParameter: the parameter resolution specified is not supported"。
    #   现在自动挑一个能跑的最接近组合（720P / 时长向上取，宁多不少）。
    _want_res = shot.get("resolution") or ""
    _want_dur = int(shot.get("duration_seconds") or 5)
    _res, _dur = _want_res, _want_dur
    try:
        from core.model_catalog import pick_valid_combo
        _combo = pick_valid_combo(
            shot.get("model_provider") or "",
            shot.get("model_name") or "",
            want_res=_want_res, want_dur=_want_dur,
        )
        # caps 用 "720P" 大写，adapter 用 "720p" 小写 —— 这里统一成小写
        _res = str(_combo.get("resolution") or _want_res).lower()
        _dur = int(_combo.get("duration") or _want_dur)
        if _combo.get("adjusted"):
            logger.info(
                "参数已按模型能力调整: %s/%s %s%s -> %s%s",
                shot.get("model_provider"), shot.get("model_name"),
                _want_res, _want_dur, _res, _dur,
            )
    except Exception as _e:
        logger.debug("模型能力校正跳过: %s", _e)

    # ★★ v1.4.3 预算刹车（生成**前**）★★
    #   2026-10-19 事故：没有预算闸门，一次完整流程扣了近 100 元。
    #   现在每次生成前先估成本、查项目累计，超预算直接拒绝并说明差多少。
    from core import budget as _budget
    from core import pricing as _pricing

    # 总闸：默认**关**（环境变量 + 设置开关都要开才放行）。
    # 这一条是根治 —— 2026-10-19 的事故就是因为本函数**绕过了**
    # dev 版本 core/shot_segments.py 里的三层花钱闸（主流程不走那条路）。
    _budget.assert_spend_allowed(
        db=db, provider=provider,
        detail=f"（镜头 {shot.get('order_index')}，{provider}）")

    _est = _pricing.estimate_cost_yuan(
        provider, duration_seconds=int(_dur or 5),
        resolution=_res, num_candidates=int(num_candidates or 1))
    try:
        _budget.check_budget(
            shot.get("project_id") or "",
            _est, db=db, provider=provider,
            detail=f"（镜头：{shot.get('order_index')}）",
        )
    except _budget.BudgetExceeded as _be:
        db.update_shot(shot_id, status="failed",
                       error_message=f"预算不足，已阻止生成：{_be}", progress=0)
        raise

    req = VideoGenRequest(
        prompt=full_prompt,
        duration=min(_dur, adapter.max_duration),
        aspect_ratio=shot["aspect_ratio"],
        resolution=_res,
        reference_images=char_refs,
        # ★ v1.4.3: 首帧优先级 = 上一镜尾帧 > 场景图。
        #   尾帧接住上一镜的最后一格画面，场景图保证同场景一致。
        first_frame=chain_ref or scene_ref,
        character_reference=char_refs,
        negative_prompt="\n".join(shot.get("layer3_constraints", {}).get("must_not_appear", [])),
    )

    # 校验
    err = adapter.validate_request(req)
    if err:
        db.update_shot(shot_id, status="failed", error_message=err, progress=0)
        raise ValueError(err)

    update(0.1, f"调用 {adapter.display_name}")

    # 生成 num_candidates 个候选
    candidates = []
    errors: List[str] = []
    output_dir = Path(settings.get("output_dir", "./data/outputs")) / shot["project_id"] / shot_id
    output_dir.mkdir(parents=True, exist_ok=True)

    for i in range(num_candidates):
        # ★ v1.4.5: 每个候选**之前**重新查一次预算。
        #   原来只在循环外查一次 —— 多候选时第 2 个开始就可能超预算，
        #   但闸门不会响。
        try:
            _budget.check_budget(
                shot.get("project_id") or "",
                _pricing.estimate_cost_yuan(
                    provider, duration_seconds=int(_dur or 5),
                    resolution=_res, num_candidates=1),
                db=db, provider=provider,
            )
        except _budget.BudgetExceeded as _be2:
            errors.append(f"候选{i+1}: 预算不足（{_be2}）")
            break
        try:
            result: VideoGenResult = await adapter.generate(req)
            if not result.success:
                last_err = result.error or "未知错误"
                logger.warning(f"Candidate {i+1} failed: {last_err}")
                errors.append(f"候选{i+1}: {last_err}")
                continue

            # 下载到本地
            local_path = output_dir / f"candidate_{i+1}.mp4"
            await adapter.download_to_local(result.video_url, str(local_path))

            candidates.append({
                "candidate_id": f"cand_{i+1}",
                "video_path": str(local_path),
                "video_url": result.video_url,
                "duration_seconds": result.duration_seconds,
                "is_selected": i == 0,  # 默认选第一个
                "rating": 0,
                "raw": result.raw,
            })
            update(0.1 + 0.85 * (i + 1) / num_candidates, f"候选 {i+1}/{num_candidates}")
        except Exception as e:
            logger.exception(f"Candidate {i+1} error: {e}")

    await adapter.close()

    # 更新 shot
    # 失败时把**真实原因**写进 error_message（此前只写 "All candidates failed"，
    # 用户在界面上完全看不出是缺 Key、模型名错、还是内容审核）
    fail_msg = None
    if not candidates:
        fail_msg = "；".join(errors[:3]) if errors else "全部候选生成失败（无更多信息）"
    # ★ v1.4.3: 生成成功后把**可访问的** video_url / preview_image 写回 shot 行，
    #   并放到返回值顶层。
    #   原来只写 candidates + selected_candidate_id，shot.video_url 一直是 None，
    #   agent 取 result["video_url"] 也永远取不到 → 对话框里既没有视频也没有图。
    top_video_url = None
    top_preview = None
    if candidates:
        _sel = candidates[0]
        _lp = _sel.get("video_path")
        top_video_url = _media_url(_lp) or _sel.get("video_url")
        top_preview = _make_preview_frame(_lp, db)
        # shots 表**没有** video_url / preview_image 列（视频存在 candidates JSON 里），
        #   所以这里只把封面挂到候选上，不去 UPDATE 不存在的列。
        if top_preview:
            _sel["preview_image"] = top_preview
            candidates[0] = _sel

    # ★★ v1.4.3 成本累计（生成**后**）★★
    if candidates:
        try:
            _spent = _pricing.estimate_cost_yuan(
                provider, duration_seconds=int(_dur or 5),
                resolution=_res, num_candidates=len(candidates),
            )
            _budget.add_cost(shot.get("project_id") or "", _spent, db=db)
        except Exception as _ce:
            logger.warning("成本累计失败（不影响生成）: %s", _ce)

    db.update_shot(
        shot_id,
        candidates=candidates,
        selected_candidate_id=candidates[0]["candidate_id"] if candidates else None,
        status="completed" if candidates else "failed",
        progress=1.0,
        error_message=fail_msg[:800] if fail_msg else None,
    )

    return {
        "shot_id": shot_id,
        "num_candidates": len(candidates),
        "candidates": candidates,
        "video_url": top_video_url,
        "preview_image": top_preview,
    }


@register_task("generate_script")
async def generate_script_handler(payload: Dict[str, Any],
                                   progress_callbacks: List) -> Dict[str, Any]:
    """生成剧本（LLM）"""
    from main import get_app_state

    state = get_app_state()
    db: Database = state["db"]
    settings = db.get_all_settings()

    update = make_progress_updater(payload.get("_task_id", ""), db, progress_callbacks)

    llm = _get_llm_from_settings(settings)
    svc = ScriptService(llm)

    update(0.1, "调用 LLM 生成剧本")
    # ★ 2026-09-14：场次数由 `scriptplan` 定，返回后再**强制**把总时长对齐到
    #   用户要的秒数（与 `/api/projects/{pid}/script/generate` 同一条规矩）。
    #   两条入口要是各写一套，就会出现"从这条进对、从那条进不对"的鬼故事。
    from core import scriptplan as _sp
    # ★ v1.4.3: 兼容 "total_duration" / "duration" 两种字段名
    #   （agent 之前传的是 duration，这里读 total_duration -> 静默用 30 秒）
    _want = int(payload.get("total_duration") or payload.get("duration") or 30)
    result = await svc.generate_script(
        user_prompt=payload["user_prompt"],
        total_duration=_want,
        style=payload.get("style", "cinematic"),
        auto_split=True,
        # ★ v1.4.4: 用户明确说了几个镜头就按几个生成场次。
        #   原来只用 scene_count_for(总时长) 自动算 —— 用户说"2 个镜头"
        #   被无视（10 秒自动排成 1 场）。这个 scene_count 同时会写进
        #   LLM 提示词的"必须正好 N 场"，所以这里改了对整条链都生效。
        scene_count=(int(payload.get("scene_count"))
                    if payload.get("scene_count") else _sp.scene_count_for(_want)),
    )
    # ★ v1.4.3: 用户明确说"2 个镜头"就排 2 场（原来只按总时长自动算 -> 10 秒排成 1 场）
    _fit = _sp.fit_scenes(result.get("scenes"), _want,
                         scene_count=payload.get("scene_count"))
    if _fit.get("scenes"):
        result["scenes"] = _fit["scenes"]
    result["duration_plan"] = _sp.duration_brief(_fit)
    try:
        _proj = db.get_project(payload["project_id"]) or {}
        _ps = dict(_proj.get("settings") or {})
        _ps["default_total_duration"] = _want
        db.update_project(payload["project_id"], settings=_ps)
    except Exception as e:
        logger.warning("总时长写回项目设置失败：%s", e)

    update(0.8, "生成三层提示词")

    # 为每个场次生成三层提示词
    characters = result.get("characters", [])
    scenes_meta = result.get("scenes_meta", [])

    three_layer = {}
    for scene in result.get("scenes", []):
        scene_id = f"scene_{scene['scene_number']}"
        # 找到匹配的场景 meta
        scene_meta = next((s for s in scenes_meta if s.get("name") == scene.get("title")), None)
        layer = await svc.generate_three_layer_prompt(scene, characters, scene_meta)
        three_layer[scene_id] = layer

    update(0.95, "保存剧本")

    # 保存到 DB
    db.upsert_script(
        project_id=payload["project_id"],
        user_prompt=payload["user_prompt"],
        outline=json.dumps(result, ensure_ascii=False),
        scenes=result.get("scenes", []),
        three_layer_prompts=three_layer,
    )

    return {
        "title": result.get("title"),
        "scenes_count": len(result.get("scenes", [])),
        "three_layer_count": len(three_layer),
    }


@register_task("abstract_copyright")
async def abstract_handler(payload: Dict[str, Any],
                            progress_callbacks: List) -> Dict[str, Any]:
    """版权抽象化"""
    from main import get_app_state

    state = get_app_state()
    db: Database = state["db"]
    settings = db.get_all_settings()

    update = make_progress_updater(payload.get("_task_id", ""), db, progress_callbacks)

    llm = _get_llm_from_settings(settings)
    svc = AbstractionService(llm)

    update(0.2, "分析版权特征")
    result = await svc.abstract(payload["source_description"])

    update(0.7, "生成抽象化设定")

    log_id = db.log_abstraction(
        project_id=payload["project_id"],
        source=payload["source_description"],
        abstracted=result,
        removed=result.get("removed_features", []),
        preserved=result.get("preserved_features", []),
    )

    # 自动创建角色/场景/道具
    for char in result.get("characters", []):
        db.create_character(
            project_id=payload["project_id"],
            name=char.get("abstracted_name", "未命名角色"),
            description=char.get("description", ""),
            costume_main=char.get("costume", ""),
            reference_features={
                "preserved": char.get("preserved_traits", []),
            },
        )

    for scene in result.get("scenes", []):
        db.create_scene(
            project_id=payload["project_id"],
            name=scene.get("abstracted_name", "未命名场景"),
            description=scene.get("description", ""),
        )

    for prop in result.get("props", []):
        db.create_prop(
            project_id=payload["project_id"],
            name=prop.get("abstracted_name", "未命名道具"),
            description=prop.get("description", ""),
        )

    return {
        "log_id": log_id,
        "characters": len(result.get("characters", [])),
        "scenes": len(result.get("scenes", [])),
        "props": len(result.get("props", [])),
    }


# ──────────── 工具函数 ────────────

def _compose_full_prompt(shot: Dict[str, Any]) -> str:
    """把三层提示词拼装成一段完整 prompt"""
    parts = []

    # Layer 1
    if shot.get("layer1_overview"):
        parts.append(shot["layer1_overview"])

    # Layer 2 timeline
    timeline = shot.get("layer2_timeline", [])
    if timeline:
        parts.append("\n分镜时间线：")
        for item in timeline:
            camera = item.get("camera", "static")
            parts.append(
                f"[{item.get('start', 0)}-{item.get('end', 0)}秒] "
                f"{item.get('action', '')} | 表情:{item.get('expression', '')} | "
                f"镜头:{camera} | 道具:{','.join(item.get('props_used', []))}"
            )

    # Layer 3 constraints
    constraints = shot.get("layer3_constraints", {})
    if constraints:
        parts.append("\n约束条件：")
        if constraints.get("must_not_appear"):
            parts.append("不要出现: " + "、".join(constraints["must_not_appear"]))
        if constraints.get("must_not_happen"):
            parts.append("不要发生: " + "、".join(constraints["must_not_happen"]))
        if constraints.get("must_keep"):
            parts.append("必须保持: " + "、".join(constraints["must_keep"]))
        if constraints.get("must_appear"):
            parts.append("必须出现: " + "、".join(constraints["must_appear"]))
        if constraints.get("must_happen"):
            parts.append("必须发生: " + "、".join(constraints["must_happen"]))

    return "\n".join(parts)


def _get_llm_from_settings(settings: Dict[str, Any]) -> LLMClient:
    """根据设置构造 LLM 客户端。

    ★ v1.4.3 修真 bug：原来只认**旧字段** remote_llm_key / remote_llm_provider /
      remote_llm_model，而设置页保存的是**新字段** llm_api_keys / llm_provider /
      llm_model。结果：用户明明在设置里配了 MiniMax key，
      orchestrator 里每一次 LLM 调用（脚本生成 / 角色建议 / 一致性检查）
      都退回"deepseek + 空 key" → 401，agent 链路完全跑不通。

      现在优先读新字段，回退旧字段；最后才用 LLMClient(settings=...) 的统一逻辑。
    """
    if settings.get("enable_llm_local"):
        return LLMClient(
            provider="ollama",
            base_url=settings.get("ollama_url") or settings.get("ollama_base_url", "http://localhost:11434"),
            model=settings.get("ollama_model", "qwen2.5:7b"),
        )

    provider = (settings.get("llm_provider")
                or settings.get("remote_llm_provider") or "")
    model = (settings.get("llm_model")
             or settings.get("remote_llm_model") or "")
    keys = settings.get("llm_api_keys") or {}
    api_key = (keys.get(provider) if provider else "") or settings.get("remote_llm_key", "")

    if provider and api_key:
        return LLMClient(provider=provider, api_key=api_key, model=model)

    if settings.get("remote_llm_key"):
        return LLMClient(
            provider=settings.get("remote_llm_provider", "deepseek"),
            api_key=settings["remote_llm_key"],
            model=settings.get("remote_llm_model", "deepseek-chat"),
        )

    # 兜底：交给 LLMClient(settings=...) 统一解析（它也只会拿到没配的 key，
    # 但至少 provider/model 是对的，用户在设置里补上 key 就能用）
    return LLMClient(settings=settings)


async def generate_long_video(
    shot_id: str,
    db: Database,
    queue: TaskQueue,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """长视频生成：自动分段拼接"""
    shot = db.get_shot(shot_id)
    if not shot:
        raise ValueError(f"Shot not found: {shot_id}")

    provider = shot["model_provider"]
    api_key = settings.get("api_keys", {}).get(provider, "")
    adapter = get_adapter(provider, api_key=api_key,
                            config={"model_name": shot["model_name"]})

    total_duration = shot["duration_seconds"]
    chunk_size = adapter.max_duration

    if total_duration <= chunk_size:
        # 单段即可
        return await generate_shot_handler(
            {"shot_id": shot_id, "num_candidates": 4, "_task_id": ""},
            [],
        )

    # 多段：分别生成再拼接
    segments = []
    num_segments = (total_duration + chunk_size - 1) // chunk_size
    for i in range(num_segments):
        start = i * chunk_size
        end = min(start + chunk_size, total_duration)
        sub_duration = end - start

        sub_shot = dict(shot)
        sub_shot["id"] = f"{shot_id}_seg_{i+1}"
        sub_shot["duration_seconds"] = sub_duration
        sub_shot["layer1_overview"] = (
            f"{shot.get('layer1_overview', '')}\n\n"
            f"（第 {i+1}/{num_segments} 段，{sub_duration}秒）"
        )
        # 保存临时 shot
        db.create_shot(**{k: v for k, v in sub_shot.items() if k in [
            "project_id", "scene_id", "order_index", "duration_seconds",
            "layer1_overview", "layer2_timeline", "layer3_constraints",
            "character_ids", "model_provider", "model_name", "aspect_ratio",
            "resolution", "negative_reference_paths",
        ]})
        # 触发单段生成
        ...
        # 实际拼接留到 postprocess
