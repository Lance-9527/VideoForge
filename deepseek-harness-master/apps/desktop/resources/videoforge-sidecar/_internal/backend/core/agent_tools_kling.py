"""VideoForge · Agent 工具（可灵官方 MCP 通道，**免 API key**）

════════════════════════════════════════════════════════════════════════
为什么有这一批
════════════════════════════════════════════════════════════════════════
用户的原话是：「https://klingai.com/app 你直接借鉴不行吗，为什么非要
可灵的 key？」——这个质疑是对的。可灵官方本来就提供了一台远程 MCP
服务器（`https://klingai.com/mcp`），走标准 OAuth：**授权一次，
之后自动续期，全程不需要 API key**。这一批工具就是把那条路接进对话里。

════════════════════════════════════════════════════════════════════════
和 `kling.py` 适配器的关系（两条路，各有用途）
════════════════════════════════════════════════════════════════════════
    adapters/kling.py  走 apikey（api-singapore.klingai.com）
                       —— 要 key、要充值、但能做批量/无人值守
    kling_mcp（本文件）走 OAuth 官方 MCP
                       —— 不用 key、用网页账号的灵感值、**白膜可以直接上传**
                       —— 缺点：灵感值靠网页账号，额度可能很小

两条路共用一个 `Element`（角色一致性）概念，但 MCP 这条更省事：
`file_upload` 让白膜根本不需要公网 URL。

════════════════════════════════════════════════════════════════════════
工具清单
════════════════════════════════════════════════════════════════════════
    kling_status          授权/额度/可用模型一次看清（免费，生成前先调）
    kling_upload          把本地文件传给可灵拿 URL（免费）——白膜/定妆照
    kling_element         把项目角色注册成可灵 Element（角色一致性）
    kling_elements        列已有的 Element
    kling_generate        生成视频/图片（**花钱**），支持 Element 与白膜
    kling_white_model     ★ 白膜 + 定妆照 → 正式镜头（motion_control）
    kling_task            查任务状态（免费）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .agent_tools import REGISTRY, ToolResult, err, ok, merged, _services, _need

logger = logging.getLogger("videoforge.agent_tools.kling")


def _T():
    from core import kling_mcp_tools as T
    return T


def _gen_args_schema(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = {
        "prompt": {"type": "string", "desc": "画面描述"},
        "model": {"type": "string",
                  "desc": "可灵模型名（如 kling-video-v3_0）。不填用默认"},
        "duration": {"type": "number", "desc": "秒数；会自动收进该模型的合法范围"},
        "aspect_ratio": {"type": "string", "desc": "16:9 / 9:16 / 1:1"},
        "rationale": {"type": "string", "desc": "这次生成要达成什么（官方要求填写）"},
    }
    if extra:
        base.update(extra)
    return base


# ──────────────────────── 1. 状态（免费，先调）────────────────────────


@REGISTRY.register(
    "kling_status",
    "可灵官方 MCP 通道的体检：授权状态、**灵感值余额**、可用模型清单。"
    "免费、秒回。★ 生成之前先调 —— 灵感值为 0 的话任何生成都会被平台拒。",
    {},
)
async def t_kling_status(args: Dict[str, Any]) -> ToolResult:
    try:
        T = _T()
        pf = await T.preflight()
        if not pf.get("authorized"):
            return err(pf.get("how") or "还没授权",
                       how="跑 kling_mcp_auth 拿授权链接，用户在浏览器点「同意」即可，"
                           "**不需要 API key**")
        c = pf.get("credits") or {}
        note = []
        if not c.get("enough"):
            note.append(f"灵感值为 {c.get('credits')} —— 现在提交生成一定会被拒。"
                        f"充值页：{c.get('topup_url')}")
        note.append("白膜可以直接上传（file_upload），**不需要公网隧道**。")
        note.append("角色一致性走 Element，且必须用 image_to_video / image_to_image"
                    "（文生视频不支持 Element）。")
        return ok(**merged(pf, usable=bool(pf.get("ok")), notes=note))
    except Exception as e:
        logger.exception("kling_status failed")
        return err(f"查可灵状态失败：{type(e).__name__}: {e}")


# ──────────────────────── 2. 上传本地文件（免费）────────────────────────


@REGISTRY.register(
    "kling_upload",
    "把**本机文件**传给可灵，拿回一个能当生成输入的 URL（免费）。"
    "★ 关键：官方 MCP 自带上传通道，所以白膜视频/角色定妆照"
    "**不需要公网隧道、不需要域名**。支持 png/jpg/mp4（图片≤30MB）。",
    {"path": {"type": "string", "desc": "本机文件路径", "required": True}},
)
async def t_kling_upload(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "path")
    if bad:
        return bad
    try:
        T = _T()
        url = await T.upload_file(args["path"])
        return ok(url=url, path=args["path"],
                  note="这个 URL 可以直接喂给 kling_generate / kling_white_model。")
    except Exception as e:
        logger.exception("kling_upload failed")
        return err(f"上传失败：{type(e).__name__}: {e}")


# ──────────────────────── 3. Element（角色一致性）────────────────────────


@REGISTRY.register(
    "kling_element",
    "把项目里的一个角色注册成可灵 Element（可复用的主体），用于**跨镜头角色一致性**。"
    "会优先复用角色已存的 element_id；没有就用它的定妆照/参考图建一个。"
    "★ 官方规则：Element 只能用在 image_to_video / image_to_image，"
    "**文生视频、文生图不支持 Element**。",
    {"character_id": {"type": "string", "desc": "角色 id", "required": True},
     "images": {"type": "array", "desc": "图片路径列表；不填则用角色的参考图/定妆照"}},
    writes=True,
)
async def t_kling_element(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "character_id")
    if bad:
        return bad
    try:
        T = _T()
        db, _, _ = _services()
        ch = db.get_character(args["character_id"])
        if not ch:
            return err(f"角色不存在：{args['character_id']}")
        imgs: List[str] = [p for p in (args.get("images") or []) if p]
        if not imgs:
            for k in ("reference_image_path", "portrait_path", "avatar_path",
                      "image_path"):
                v = ch.get(k)
                if v and str(v).strip():
                    imgs.append(str(v))
        if not imgs:
            return err(f"角色「{ch.get('name')}」没有可用图片，"
                       f"先给它生成定妆照，或用 images 参数直接给路径")
        eid = await T.ensure_character_element(ch, imgs)
        return ok(element_id=eid, character=ch.get("name"), images=imgs,
                  how_to_use="在 kling_generate 里用 image_to_video + "
                             "element_ids=[...]；prompt 里用 <<<id>>> 指代该角色。")
    except Exception as e:
        logger.exception("kling_element failed")
        return err(f"注册 Element 失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "kling_elements",
    "列出可灵账号里已有的 Element（可复用主体）。免费。",
    {},
)
async def t_kling_elements(args: Dict[str, Any]) -> ToolResult:
    try:
        T = _T()
        els = await T.element_list()
        return ok(count=len(els), elements=els,
                  note="只有 id/name，要看细节用 kling_task 不行 —— "
                       "Element 详情需 element_get（暂未开放为工具）")
    except Exception as e:
        logger.exception("kling_elements failed")
        return err(f"列 Element 失败：{type(e).__name__}: {e}")


# ──────────────────────── 4. 生成 ────────────────────────


@REGISTRY.register(
    "kling_generate",
    "用可灵生成视频或图片（**会花灵感值**）。支持四种模式：\n"
    "  · text_to_video   文生视频（**不能带角色 Element**）\n"
    "  · image_to_video  图生视频（**角色一致性的正路**，可带 Element）\n"
    "  · image_to_image  图生图（图片 Element 在这里也支持）\n"
    "  · motion_control  动作控制（白膜走这个，见 kling_white_model）\n"
    "模型名和时长会自动按官方 who_am_i 的约束收敛，不要手写不存在的取值。",
    _gen_args_schema({
        "mode": {"type": "string",
                 "desc": "text_to_video / image_to_video / image_to_image / motion_control",
                 "required": True},
        "images": {"type": "array", "desc": "参考图/首帧的本机路径或 http URL"},
        "tail_image": {"type": "string", "desc": "尾帧（部分模型支持）"},
        "element_ids": {"type": "array", "desc": "角色 Element id 列表"},
        "resolution": {"type": "string", "desc": "720p（视频默认）/ 1080p（仅 motion_control）"},
        "enable_audio": {"type": "boolean", "desc": "是否带音频（部分模型支持）"},
        "wait": {"type": "boolean", "desc": "true = 一直等到出结果（默认 true）"},
    }),
    writes=True,
)
async def t_kling_generate(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "mode")
    if bad:
        return bad
    mode = str(args["mode"])
    try:
        T = _T()
        db, _, _ = _services()

        # 内容预检：可灵的审核同样会先跑完再判违规，钱照扣
        risk = None
        try:
            from core import moderation as _mod
            risk = _mod.check(prompt=args.get("prompt") or "")
            if risk.get("need_confirm") and not args.get("acknowledge_risk"):
                return err(
                    f"内容预检命中风险：{risk.get('summary')}",
                    hits=[h for h in (risk.get("hits") or [])][:8],
                    advice=risk.get("advice"),
                    hint="改文案，或确认要继续就再调一次并带 acknowledge_risk=true")
        except Exception:
            risk = None

        # 本机路径先上传（http 的直接用）
        async def _ref(v: str) -> str:
            if not v:
                return ""
            return v if str(v).startswith(("http://", "https://")) \
                else await T.upload_file(v)

        imgs = [await _ref(p) for p in (args.get("images") or []) if p]
        tail = await _ref(args.get("tail_image") or "")
        model = args.get("model") or ""
        dur = float(args.get("duration") or 5)
        prompt = args.get("prompt") or ""
        ratio = args.get("aspect_ratio") or "16:9"
        res = args.get("resolution") or "720p"
        rationale = args.get("rationale") or "按用户对话要求生成镜头"
        els = [str(e) for e in (args.get("element_ids") or []) if e]

        if mode == "text_to_video":
            if els:
                return err("文生视频**不支持**角色 Element（官方原文："
                           "text_to_video supports no element at all）。"
                           "要角色一致请改用 image_to_video 并给一张首帧图。")
            gid = await T.text_to_video(prompt, model=model, duration=dur,
                                        aspect_ratio=ratio, resolution=res,
                                        enable_audio=bool(args.get("enable_audio")),
                                        rationale=rationale)
        elif mode == "image_to_video":
            if not imgs:
                return err("image_to_video 至少要一张图（首帧或参考图）。"
                           "本地路径会自动上传。")
            gid = await T.image_to_video(
                prompt, model=model, first_image=imgs[0],
                tail_image=tail, images=imgs[1:], element_ids=els,
                duration=dur, aspect_ratio=ratio, resolution=res,
                enable_audio=bool(args.get("enable_audio")),
                rationale=rationale)
        elif mode == "image_to_image":
            if not imgs:
                return err("image_to_image 至少要一张参考图。")
            gid = await T.image_to_image(prompt, imgs, model=model,
                                         element_ids=els, aspect_ratio=ratio,
                                         rationale=rationale)
        elif mode == "motion_control":
            if not imgs:
                return err("motion_control 需要 image（角色图）；"
                           "动作来源用白膜时请直接调 kling_white_model。")
            gid = await T.motion_control(
                imgs[0], prompt=prompt, model=model or "kling-video-v3_0",
                resolution=res if res in T.MOTION_RESOLUTIONS else "720p",
                element_ids=els, rationale=rationale)
        else:
            return err(f"不认识的 mode：{mode}（可选 text_to_video / "
                       f"image_to_video / image_to_image / motion_control）")

        out: Dict[str, Any] = {"generation_id": gid, "mode": mode,
                               "model": model or "(默认)", "risk_level":
                               (risk or {}).get("level")}
        if args.get("wait", True):
            r = await T.wait_task(gid, timeout=float(args.get("timeout") or 900))
            out.update({"status": r.get("status"), "urls": r.get("urls") or [],
                        "elapsed": r.get("elapsed")})
            if r.get("failed") or r.get("timeout"):
                ex = T.explain_error(str(r.get("error") or r.get("status") or ""))
                return err(f"可灵生成没成功：{r.get('status')}",
                           detail=out, kind=ex["kind"], fix=ex["fix"],
                           hint="官方要求：不要自动重发，先问用户")
            if not (r.get("urls") or []):
                return err("任务完成了但没解析出 URL", detail=r)
            out["note"] = "可灵的结果 URL **24 小时后过期**，要尽快下载保存。"
        else:
            out["note"] = "已提交，用 kling_task 查进度。"
        return ok(**out)
    except Exception as e:
        try:
            T = _T()
            ex = T.explain_error(f"{type(e).__name__}: {e}")
            return err(f"可灵生成失败：{type(e).__name__}: {e}",
                       kind=ex["kind"], fix=ex["fix"])
        except Exception:
            return err(f"可灵生成失败：{type(e).__name__}: {e}")


# ──────────────────────── 5. 白膜 → 正式镜头（本批的重点）────────────────────────


@REGISTRY.register(
    "kling_white_model",
    "★白膜的正确接法★：把**白膜预演视频**当动作/运镜来源，套到角色定妆照上，"
    "生成正式镜头（可灵 motion_control）。\n"
    "为什么用这个：白膜只提供空间关系/运镜路径/时间节奏，画面质感和角色由定妆照"
    "与 Element 决定 —— 这正是用户要的「先白膜确认，再出完整视频」。\n"
    "白膜视频会被自动上传，**不需要公网 URL**。",
    {"previz_video": {"type": "string", "desc": "白膜视频本机路径", "required": True},
     "subject_image": {"type": "string", "desc": "角色定妆照本机路径", "required": True},
     "prompt": {"type": "string", "desc": "补充描述（场景/光线等）"},
     "model": {"type": "string", "desc": "默认 kling-video-v3_0"},
     "resolution": {"type": "string", "desc": "720p（默认）/ 1080p"},
     "element_ids": {"type": "array", "desc": "角色的 Element id（v3_0 支持 1 个）"},
     "wait": {"type": "boolean", "desc": "默认 true，等到出结果"}},
    writes=True,
)
async def t_kling_white_model(args: Dict[str, Any]) -> ToolResult:
    for k in ("previz_video", "subject_image"):
        bad = _need(args, k)
        if bad:
            return bad
    try:
        T = _T()
        db, _, _ = _services()
        risk = None
        try:
            from core import moderation as _mod
            risk = _mod.check(prompt=args.get("prompt") or "")
            if risk.get("need_confirm") and not args.get("acknowledge_risk"):
                return err(f"内容预检命中风险：{risk.get('summary')}",
                           advice=risk.get("advice"),
                           hint="确认要继续就带 acknowledge_risk=true 再调一次")
        except Exception:
            risk = None

        gid = await T.white_model_to_video(
            args["previz_video"], args["subject_image"],
            prompt=args.get("prompt") or "",
            model=args.get("model") or "kling-video-v3_0",
            resolution=args.get("resolution") or "720p",
            element_ids=[str(e) for e in (args.get("element_ids") or []) if e] or None)
        out: Dict[str, Any] = {"generation_id": gid,
                               "previz": args["previz_video"],
                               "subject": args["subject_image"],
                               "risk_level": (risk or {}).get("level")}
        if args.get("wait", True):
            r = await T.wait_task(gid, timeout=float(args.get("timeout") or 1200))
            out.update({"status": r.get("status"), "urls": r.get("urls") or [],
                        "elapsed": r.get("elapsed")})
            if r.get("failed") or r.get("timeout") or not (r.get("urls") or []):
                ex = T.explain_error(str(r.get("error") or r.get("status") or ""))
                return err(f"白膜生成没成功：{r.get('status')}", detail=out,
                           kind=ex["kind"], fix=ex["fix"])
            out["note"] = ("正式镜头出来了。白膜没动过 —— 不满意可以只改定妆照/"
                           "prompt 重来，不必重渲白膜。URL 24 小时后过期。")
        return ok(**out)
    except Exception as e:
        logger.exception("kling_white_model failed")
        try:
            ex = _T().explain_error(f"{type(e).__name__}: {e}")
            return err(f"白膜生成失败：{type(e).__name__}: {e}",
                       kind=ex["kind"], fix=ex["fix"])
        except Exception:
            return err(f"白膜生成失败：{type(e).__name__}: {e}")


# ──────────────────────── 6. 查任务 ────────────────────────


@REGISTRY.register(
    "kling_task",
    "查可灵生成任务的状态（免费）。提交后轮到哪一步、成品的 URL 都在这里。"
    "★ 结果 URL 24 小时过期，查到就赶紧存下来。",
    {"generation_id": {"type": "string", "desc": "生成时返回的 id", "required": True}},
)
async def t_kling_task(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "generation_id")
    if bad:
        return bad
    try:
        T = _T()
        r = await T.query_task(str(args["generation_id"]))
        if r.get("error"):
            ex = T.explain_error(r["error"])
            return err(f"任务报错：{r['error']}", kind=ex["kind"], fix=ex["fix"])
        return ok(status=r.get("status"), done=r.get("done"),
                  failed=r.get("failed"), urls=r.get("urls") or [],
                  cover=r.get("cover") or "",
                  note="URL 24 小时后过期" if r.get("done") else "还在跑，过一会儿再查")
    except Exception as e:
        logger.exception("kling_task failed")
        return err(f"查任务失败：{type(e).__name__}: {e}")
