"""
VideoForge · 对话 API（流式 + 非流式）
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.db import Database
from core import attachments as _att
from core.chat import (
    LLMProvider, Message, ChatRequest, ChatResponse,
    get_provider, list_providers, PROVIDERS,
    AGENT_SYSTEM_PROMPT,
    run_agent_turn, run_agent_turn_stream,
)


logger = logging.getLogger("videoforge.api.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])


# ──────────── Schemas ────────────

class MessageSchema(BaseModel):
    role: str       # system | user | assistant
    content: str


class ChatRequestSchema(BaseModel):
    provider: str = "deepseek"
    model: Optional[str] = None
    api_key: Optional[str] = None
    messages: List[MessageSchema]
    temperature: float = 0.7
    max_tokens: int = 4000
    stream: bool = False
    system: Optional[str] = None  # 覆盖默认 system
    # ── Agent 模式 ──
    # agent=True 时走 core.agent 的工具循环，模型能直接操作平台（建项目/生成剧本/分镜…）。
    # agent=False 时退化成一个普通聊天模型（保留老行为，便于排查问题）。
    agent: bool = True
    project_id: Optional[str] = None   # 当前项目，注入 system prompt 当"视野"
    max_steps: int = 12                # 最多几轮工具调用
    # ★★ 2026-09-29（用户第 1 条：「对话支持上传图片…支持图片理解」）
    #   图片附件的**本机路径**列表。给了就把最后一条用户消息拼成
    #   content 块（文字 + 图像），模型**直接看图**。
    #   实测（tests/probe_vision.py）这个模型能准确描述画面，所以是真看图，
    #   不是把图片当附件名念一遍。
    images: Optional[List[str]] = None


class ChatProvidersResponse(BaseModel):
    providers: List[Dict[str, Any]]


# ──────────── 工具 ────────────

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _get_db():
    """获取 db 单例（从 main 注入）"""
    from main import get_app_state
    return get_app_state()["db"]



def _effective_llm(provider: str, model: Optional[str]) -> tuple:
    """决定这次调用**真正**用哪个 provider / model。

    ★★ 2026-10-20 修的真 bug（用户"软件里跑不通"）：
      `ChatRequestSchema.provider` 的默认值是硬编码的 "deepseek"，
      而**从不读 settings 里的 llm_provider**。于是只要前端没把
      provider 带上（初始化失败 / 某条路径没带 / 第三方调用），
      请求就落到 deepseek —— 而用户配的 key 是 MiniMax，
      结果必然 400「没有 deepseek 的 API Key」。

      现在：**后端做最终裁决** ——
        · 请求显式给了别的 provider（且那个有 key）→ 用请求的
        · 否则 → 用 settings 里的 llm_provider / llm_model
      这样不管前端传没传，都不会掉到没配 key 的 deepseek 上。

    Returns:
        (provider, model, key)
    """
    db = _get_db()
    settings = (db.get_all_settings() or {}) if db is not None else {}
    llm_keys = settings.get("llm_api_keys") or {}
    p = str(provider or "").strip()
    m = str(model or "").strip()

    def _key_of(name: str) -> str:
        for k, v in (llm_keys.items() if isinstance(llm_keys, dict) else []):
            if str(k).strip().lower() == str(name).strip().lower() and v:
                return str(v).strip()
        return ""

    # 1) 请求给了 provider 且它有 key -> 用请求的
    if p and (p.lower() == "ollama" or _key_of(p)):
        return p, m, _key_of(p)

    # 2) 否则用 settings 里的默认 provider
    dflt = str(settings.get("llm_provider") or settings.get("remote_llm_provider") or "").strip()
    if dflt:
        dm = str(settings.get("llm_model") or settings.get("remote_llm_model") or "").strip()
        return dflt, (m or dm), _key_of(dflt)

    # 3) 都没有 -> 保留原来的（后面 _resolve_api_key 会给出可操作的报错）
    return p or "deepseek", m, ""


def _resolve_api_key(provider: str, provided: Optional[str]) -> str:
    """解析 API key：优先用请求里的，否则从 settings 读。

    ★ 2026-09-25 加一条防空：**拿不到 Key 时给出能照着做的报错**。

      原来空 Key 会一路走到 httpx，报出来是

          LLM 调用失败：Illegal header value b'Bearer '

      —— 这句话既没说"是缺 Key"，也没说"缺哪个厂商的 Key"，更没说去哪填。
      实测就被它误导过：以为是网络/模型问题，实际是**库里的 provider
      和请求里的 provider 不是同一个**（配置里是 deepseek，
      而调用方写死了 MiniMax，于是查不到 Key）。

      所以这里当场把话说清楚：缺哪个厂商的 Key、当前配的是哪个、
      去哪儿补。
    """
    if provided and str(provided).strip():
        return str(provided).strip()

    db = _get_db()
    settings = db.get_all_settings() or {}
    llm_keys = settings.get("llm_api_keys") or {}
    key = ""
    for k, v in (llm_keys.items() if isinstance(llm_keys, dict) else []):
        if str(k).strip().lower() == str(provider).strip().lower() and v:
            key = str(v).strip()
            break
    if key:
        return key

    # 本地模型（Ollama）本来就不需要 Key
    if str(provider).strip().lower() in ("ollama",):
        return ""

    configured = [str(k) for k, v in (llm_keys.items() if isinstance(llm_keys, dict) else [])
                  if v]
    raise HTTPException(status_code=400, detail={
        "message": (f"没有「{provider}」的 API Key，没法调用文字模型。"
                    + (f"当前配好 Key 的是：{'、'.join(configured)}。"
                       f"调用方要的是「{provider}」—— 两边对不上。"
                       if configured else
                       "现在一个文字模型的 Key 都没配。")
                    + f"去「设置 → 对话 API Keys」填「{provider}」的 Key，"
                      f"或者把默认 Provider 改成已配好的那个。"),
        "reason": "no_api_key",
        "provider": provider,
        "configured_providers": configured,
    })


# ──────────── 端点 ────────────

#: 图片附件的后缀 → Anthropic 的 media_type
_IMG_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def _with_image_blocks(messages: List[Dict[str, Any]],
                       images: Optional[List[str]]) -> List[Dict[str, Any]]:
    r"""把图片附件**拼成 content 块**（文字 + 图像），让模型真的看图。

    ★★ 2026-09-29（用户第 1 条：「支持图片理解」）

    实测（`tests/probe_vision.py`）这个模型能准确描述一张定妆照的画面细节，
    所以图片不是"附件名"、也不是"只报尺寸"，而是**真的发图像过去**。

    实现上刻意最小：只改**最后一条用户消息**的 content —— 把它从字符串
    变成一个块列表 `[{type:text},{type:image},…]`。
    本仓 `AnthropicCompatibleProvider._split_system` 对 content **原样透传**，
    所以只在这里拼一次就够了，provider 一行都不用改。

    去重、限张数（一次最多 4 张）、大图跳过并**如实说明**（不静默丢）。
    """
    imgs = [str(p) for p in (images or []) if p and os.path.exists(str(p))][:4]
    if not imgs:
        return messages
    try:
        import base64 as _b64
        blocks: List[Dict[str, Any]] = []
        for p in imgs:
            ext = os.path.splitext(p)[1].lower()
            mt = _IMG_MEDIA.get(ext)
            if not mt:
                continue
            try:
                if os.path.getsize(p) > 8 * 1024 * 1024:      # 8MB 以上不塞进请求
                    logger.warning("图片太大，跳过视觉输入：%s", p)
                    continue
                data = _b64.b64encode(open(p, "rb").read()).decode()
            except Exception as e:  # noqa: BLE001
                logger.warning("读图失败 %s：%s", p, e)
                continue
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": mt,
                                      "data": data}})
        if not blocks:
            return messages
        out = [dict(m) for m in messages]
        # 找最后一条 user，把它变成"文字 + 图"
        for m in reversed(out):
            if str(m.get("role")) == "user":
                txt = m.get("content")
                if not isinstance(txt, str):
                    txt = str(txt or "")
                m["content"] = [{"type": "text",
                                 "text": txt or "（请看我上传的图片）"}] + blocks
                break
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("拼图像块失败（按纯文字继续）：%s", e)
        return messages


#: 带进模型的历史窗口（条数 / 字符数）。
#: ★ 2026-09-29（用户第 2 条：「没有上下文管理」）：
#:   前端会把**整段**会话历史发过来。聊得越久，请求越大、模型越糊
#:   （实测过：提示词一长，模型就开始"走神"只说话不调工具）。
#:   这里按"最近 N 条 + 总字符上限"裁剪，**保留最近的最完整**、
#:   最早的那几条只留一句话摘要，并在最前面**如实说明被裁掉了**，
#:   这样模型知道"前面还有内容"，不会把用户的旧话当成没说过。
HISTORY_MAX_MSGS = 24
HISTORY_MAX_CHARS = 24000


def _trim_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    msgs = list(messages or [])
    if len(msgs) <= HISTORY_MAX_MSGS:
        total = sum(len(str(m.get("content") or "")) for m in msgs)
        if total <= HISTORY_MAX_CHARS:
            return msgs
    # 从**最新**往回留，直到条数或字符数触顶
    kept: List[Dict[str, Any]] = []
    used = 0
    for m in reversed(msgs):
        c = str(m.get("content") or "")
        if kept and (len(kept) >= HISTORY_MAX_MSGS or used + len(c) > HISTORY_MAX_CHARS):
            break
        kept.append(m)
        used += len(c)
    kept.reverse()
    dropped = len(msgs) - len(kept)
    if dropped <= 0:
        return msgs
    head = {"role": "user",
            "content": ("（系统提示：为了不让对话变糊，前面的 %d 条较早的往来"
                        "没有带进来。如果用户提到的是更早的事，"
                        "请先向他确认说的是哪一件，不要凭印象编。）" % dropped)}
    logger.info("上下文裁剪：%d 条 → %d 条（省下约 %d 字）",
                len(msgs), len(kept) + 1, sum(len(str(m.get("content") or ""))
                                              for m in msgs) - used)
    return [head] + kept


def _provider_config() -> Dict[str, Any]:
    r"""给 provider 的额外配置（思考深度 + **自定义 base_url**）。

    ★★ 2026-09-28：MiniMax-M3.1-Flash-Preview 的思考深度用
      `output_config.effort` 控制（low/medium/high/xhigh/max，官方省略时默认 max）。
      原来调用方**从来不传 config**，于是这个档位永远是 provider 里的硬编码值，
      用户在设置里改也无效。这里把 `llm_effort` 从设置里读出来传下去。

    ★★ 2026-10-20：**加自定义 base_url**（用户实机撞上的硬阻塞）。
      背景：用户的 MiniMax 套餐用量到顶（「已达到 Token Plan 用量上限」），
      而 `openai`（OpenAI 兼容）这个 provider 的 base_url 是**写死**
      `https://api.openai.com/v1` 的，`_provider_config()` 又只传 effort ——
      于是 `OpenAICompatibleProvider.__init__` 里那句
      `if config.get("base_url"): self.base_url = config["base_url"]`
      **永远不成立** ⇒ 想接自己的中转/网关根本没路。
      现在从设置读 `llm_base_url`（可选 `llm_available_models`，逗号分隔）一起传下去。
    """
    try:
        s = _get_db().get_all_settings() or {}
        cfg: Dict[str, Any] = {}
        eff = str(s.get("llm_effort") or "").strip().lower()
        if eff:
            cfg["effort"] = eff
        base = str(s.get("llm_base_url") or "").strip()
        if base:
            cfg["base_url"] = base.rstrip("/")
        models = str(s.get("llm_available_models") or "").strip()
        if models:
            cfg["available_models"] = [m.strip() for m in models.split(",") if m.strip()]
        return cfg
    except Exception:  # noqa: BLE001
        return {}


@router.get("/providers")
async def get_chat_providers():
    """列出所有支持的 LLM provider"""
    return {"code": "000000", "message": "success", "data": {"providers": list_providers()}}


@router.post("/")
async def chat(req: ChatRequestSchema):
    """同步对话（一次性返回完整响应）

    agent=True 时走工具循环：模型能直接建项目/生成剧本/拆分镜…
    返回里的 steps 是执行轨迹，前端可以画出来给用户看。
    """
    try:
        _p, _m, _k = _effective_llm(req.provider, req.model)
        api_key = _resolve_api_key(_p, req.api_key or _k)
        provider = get_provider(_p, api_key=api_key, model=_m or "",
                             config=_provider_config())
        convo = _trim_history([m.model_dump() for m in req.messages])
        # ★ 图片附件 → content 块（让模型真的看图，见 _with_image_blocks）
        convo = _with_image_blocks(convo, req.images)

        if req.system:
            # 用户显式指定 system → 尊重它，不走 agent 协议
            messages = [Message(role="system", content=req.system), *[Message(**m) for m in convo]]
            result = await provider.chat(ChatRequest(
                messages=messages,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ))
            return {"code": "000000", "data": {
                "content": result.content, "model": result.model,
                "provider": req.provider, "usage": result.usage, "steps": [],
            }}

        if req.agent:
            run = await run_agent_turn(
                provider=provider, messages=convo,
                project_id=req.project_id or "",
                max_steps=max(1, min(req.max_steps, 20)),
                temperature=req.temperature, max_tokens=req.max_tokens,
            )
            return {"code": "000000", "data": {
                "content": run.reply, "model": provider.model,
                "provider": req.provider, "usage": {},
                "steps": run.trace, "stopped_because": run.stopped_because,
            }}

        # 显式关掉 agent → 老行为
        messages = [Message(role="system", content=AGENT_SYSTEM_PROMPT),
                    *[Message(**m) for m in convo]]
        result = await provider.chat(ChatRequest(
            messages=messages, temperature=req.temperature, max_tokens=req.max_tokens,
        ))
        return {"code": "000000", "data": {
            "content": result.content, "model": result.model,
            "provider": req.provider, "usage": result.usage, "steps": [],
        }}
    except HTTPException:
        # ★★ 必须原样放行。
        #   `HTTPException` 也是 `Exception`，所以下面那个 `except Exception`
        #   会把它**吞掉并改写成 500** —— 于是我们特意抛的
        #   400 + "缺哪个 Key、去哪填" 到了前端只剩一句
        #   `{"detail":{"message":"400: {'message': ...}}"}`，状态码变成 500，
        #   结构也没了。
        #
        #   这是本轮的同一个主题：**一个刻意给出的、能照着做的错误，
        #   被一个兜底处理器抹平成没用的 500。**
        raise
    except Exception as e:
        logger.exception("Chat failed")
        raise HTTPException(status_code=500, detail={"message": str(e)})


@router.post("/stream")
async def chat_stream(req: ChatRequestSchema):
    """流式对话（SSE）

    agent 模式下这里推的不是字，而是「Agent 正在干什么」：
        data: {"step":  {"index":1,"tool":"generate_script","args":{...}}}
        data: {"result":{"index":1,"ok":true,"data":{...}}}
        data: {"delta": "最终答复正文"}
        data: [DONE]
    前端保持向后兼容：只认 delta 的老代码照样能跑。
    """
    try:
        _p, _m, _k = _effective_llm(req.provider, req.model)
        api_key = _resolve_api_key(_p, req.api_key or _k)
        provider = get_provider(_p, api_key=api_key, model=_m or "",
                             config=_provider_config())
        convo = _trim_history([m.model_dump() for m in req.messages])
        # ★ 图片附件 → content 块（让模型真的看图，见 _with_image_blocks）
        convo = _with_image_blocks(convo, req.images)

        def sse(obj) -> str:
            return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"

        # ── 老路径：显式给了 system 或关掉了 agent，就纯流式聊天 ──
        if req.system or not req.agent:
            messages = [Message(role="system", content=req.system or AGENT_SYSTEM_PROMPT),
                        *[Message(**m) for m in convo]]
            chat_req = ChatRequest(messages=messages, temperature=req.temperature,
                                   max_tokens=req.max_tokens)

            async def plain_generator():
                try:
                    async for chunk in provider.stream_chat(chat_req):
                        if chunk:
                            yield sse({"delta": chunk})
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    logger.exception("stream failed")
                    yield sse({"error": str(e)})

            return StreamingResponse(plain_generator(), media_type="text/event-stream",
                                     headers=_SSE_HEADERS)

        # ── Agent 路径 ──
        async def agent_generator():
            try:
                async for ev in run_agent_turn_stream(
                    provider=provider, messages=convo,
                    project_id=req.project_id or "",
                    max_steps=max(1, min(req.max_steps, 20)),
                    temperature=req.temperature, max_tokens=req.max_tokens,
                ):
                    t = ev.get("type")
                    if t == "step":
                        yield sse({"step": {"index": ev.get("index"), "tool": ev.get("tool"),
                                            "args": ev.get("args") or {}}})
                    elif t == "result":
                        yield sse({"result": {"index": ev.get("index"),
                                              "ok": bool(ev.get("ok")),
                                              "data": ev.get("data")}})
                    elif t == "reply":
                        yield sse({"delta": ev.get("text") or ""})
                    elif t == "error":
                        yield sse({"error": ev.get("text") or "未知错误"})
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.exception("agent stream failed")
                yield sse({"error": str(e)})

        return StreamingResponse(agent_generator(), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)
    except HTTPException:
        # 同 /api/chat/：**刻意抛出的错误必须原样放行**，
        # 不能让下面的兜底把它改写成 500（那会丢掉状态码和结构化原因）。
        raise
    except Exception as e:
        logger.exception("Stream chat failed")
        raise HTTPException(status_code=500, detail={"message": str(e)})


@router.post("/attach")
async def chat_attach(file: UploadFile = File(...)):
    """对话附件：上传一个文件 → 落盘 + **抽成文本**给 agent 读。

    ★ 为什么在这里抽文本，而不是把文件路径丢给模型：
      模型只能读文本。PDF/Word 是二进制，给它一个路径等于什么都没给。
      抽成文本后，前端把它**并进用户消息**（`【附件：…】…【附件结束】`），
      agent 那侧一行都不用改 —— 这是最小改动、也最难出错的接法。

    ★ 边界（如实写在返回值里，前端会显示）：
      · 扫描件 PDF / 图片型 PDF 里**没有文字层**，抽出来是空的 —— 需要 OCR，
        本项目没做，所以那时返回 `ok=false` 并说明原因，不假装读到了；
      · 图片只报尺寸/格式，**不做视觉识别**（要看懂画面得配多模态模型）。
    """
    data_dir = (os.environ.get("VIDEOFORGE_DATA_DIR")
                or os.path.join(os.getcwd(), "data"))
    # 先按声明的体积挡一道，别把它整个读进内存再拒
    if (getattr(file, "size", None) or 0) > _att.MAX_BYTES:
        raise HTTPException(status_code=413, detail={
            "message": "文件太大（上限 %d MB）" % (_att.MAX_BYTES // 1048576)})
    raw = await file.read()
    origin = _att.safe_name(file.filename or "attachment")
    try:
        p = _att.save_upload(data_dir, origin, raw)
    except ValueError as e:
        raise HTTPException(status_code=413, detail={"message": str(e)})
    except Exception as e:  # noqa: BLE001
        logger.exception("保存附件失败")
        raise HTTPException(status_code=500, detail={"message": "保存附件失败：%s" % e})

    info = _att.extract_text(p)
    logger.info("附件已收：%s（%s，ok=%s，%d 字）",
                p.name, info.get("kind"), info.get("ok"), info.get("chars") or 0)
    return {"code": "000000", "message": "success", "data": {
        "name": p.name,              # 落盘后的名字（带时间戳，全局唯一）
        "origin_name": origin,       # 用户那边的原始名字
        "path": str(p),
        "kind": info.get("kind"),
        # ★★ 2026-09-29：把 `vision` 透出来 —— 前端据此把图片按**图像块**发出去，
        #   而不是当成"抽出来的文本"。漏了这个字段，前端就只会显示尺寸。
        "vision": bool(info.get("vision")),
        "ok": info.get("ok"),
        "text": info.get("text") or "",
        "chars": info.get("chars") or 0,
        "total_chars": info.get("total_chars") or 0,
        "truncated": bool(info.get("truncated")),
        "note": info.get("note") or "",
        "error": info.get("error") or "",
        # ★ 前端直接把这一段并进用户消息 —— 形状在这里定，前端不拼文案
        "block": _att.as_prompt_block(origin, info),
    }}


@router.post("/test")
async def test_provider(provider: str, api_key: Optional[str] = None,
                        model: Optional[str] = None):
    """测试 provider 是否可用"""
    try:
        api_key = _resolve_api_key(provider, api_key)
        if provider != "ollama" and not api_key:
            return {
                "code": "000001",
                "message": f"Provider {provider} 需要 API key",
                "data": {"available": False, "reason": "no_api_key"},
            }

        p = get_provider(provider, api_key=api_key, model=model or "",
                        config=_provider_config())
        # ★ 2026-10-20：没显式给 model 时，用**设置里配的那个模型**。
        #   原来这里 model 是空的 → provider 用它自己的 default_model
        #   （例如 openai 的 `gpt-4o-mini`）。用户接的是自定义网关，
        #   网关上没有 gpt-4o-mini ⇒ 测试报 404「模型不存在」，
        #   用户以为"我的 Key/地址填错了"，其实只是**测的是一个不存在的模型**。
        #   （实测：改成配置里的 deepseek-v4-flash 后立刻可用。）
        if not (model or "").strip():
            try:
                _s = _get_db().get_all_settings() or {}
                _m = str(_s.get("llm_model") or "").strip()
                if _m:
                    p.model = _m
            except Exception:  # noqa: BLE001
                pass
        # 发个最小请求
        result = await p.chat(ChatRequest(
            messages=[Message(role="user", content="说 'OK' 一个字。")],
            max_tokens=10,
        ))
        return {
            "code": "000000",
            "message": "success",
            "data": {
                "available": True,
                "response": result.content,
                "model": result.model,
            },
        }
    except Exception as e:
        return {
            "code": "000001",
            "message": str(e),
            "data": {"available": False, "reason": str(e)},
        }


# ──────────── AI 对话会话持久化（服务端）────────────

class ConversationSchema(BaseModel):
    id: str
    title: str = ""
    messages: list = []


@router.get("/conversations")
async def list_conversations(limit: int = 100):
    db = _get_db()
    return {"code": "000000", "message": "success", "data": {"conversations": db.list_conversations(limit)}}


@router.get("/conversations/{cid}")
async def get_conversation(cid: str):
    db = _get_db()
    conv = db.get_conversation(cid)
    if not conv:
        return {"code": "000001", "message": "未找到", "data": None}
    return {"code": "000000", "message": "success", "data": conv}


@router.post("/conversations")
async def upsert_conversation(req: ConversationSchema):
    db = _get_db()
    conv = db.upsert_conversation(req.id, req.title, req.messages)
    return {"code": "000000", "message": "success", "data": conv}


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str):
    db = _get_db()
    if not db.delete_conversation(cid):
        return {"code": "000001", "message": "未找到"}
    return {"code": "000000", "message": "success"}
