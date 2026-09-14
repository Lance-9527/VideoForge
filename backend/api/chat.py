"""
VideoForge · 对话 API（流式 + 非流式）
"""

import asyncio
import json
import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.db import Database
from core.chat import (
    LLMProvider, Message, ChatRequest, ChatResponse,
    get_provider, list_providers, PROVIDERS,
    AGENT_SYSTEM_PROMPT,
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


class ChatProvidersResponse(BaseModel):
    providers: List[Dict[str, Any]]


# ──────────── 工具 ────────────

def _get_db():
    """获取 db 单例（从 main 注入）"""
    from main import get_app_state
    return get_app_state()["db"]


def _resolve_api_key(provider: str, provided: Optional[str]) -> str:
    """解析 API key：优先用请求里的，否则从 settings 读"""
    if provided:
        return provided
    db = _get_db()
    settings = db.get_all_settings()
    llm_keys = settings.get("llm_api_keys", {})
    return llm_keys.get(provider, "")


# ──────────── 端点 ────────────

@router.get("/providers")
async def get_chat_providers():
    """列出所有支持的 LLM provider"""
    return {"code": "000000", "message": "success", "data": {"providers": list_providers()}}


@router.post("/")
async def chat(req: ChatRequestSchema):
    """同步对话（一次性返回完整响应）"""
    try:
        api_key = _resolve_api_key(req.provider, req.api_key)
        provider = get_provider(req.provider, api_key=api_key, model=req.model or "")

        # 构建消息列表
        messages = []
        if req.system or AGENT_SYSTEM_PROMPT:
            messages.append(Message(
                role="system",
                content=req.system or AGENT_SYSTEM_PROMPT,
            ))
        for m in req.messages:
            messages.append(Message(role=m.role, content=m.content))

        chat_req = ChatRequest(
            messages=messages,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )

        result = await provider.chat(chat_req)

        return {
            "code": "000000",
            "data": {
                "content": result.content,
                "model": result.model,
                "provider": req.provider,
                "usage": result.usage,
            },
        }
    except Exception as e:
        logger.exception("Chat failed")
        raise HTTPException(status_code=500, detail={"message": str(e)})


@router.post("/stream")
async def chat_stream(req: ChatRequestSchema):
    """流式对话（SSE）"""
    try:
        api_key = _resolve_api_key(req.provider, req.api_key)
        provider = get_provider(req.provider, api_key=api_key, model=req.model or "")

        messages = []
        if req.system or AGENT_SYSTEM_PROMPT:
            messages.append(Message(
                role="system",
                content=req.system or AGENT_SYSTEM_PROMPT,
            ))
        for m in req.messages:
            messages.append(Message(role=m.role, content=m.content))

        chat_req = ChatRequest(
            messages=messages,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )

        async def event_generator():
            try:
                async for chunk in provider.stream_chat(chat_req):
                    if chunk:
                        # SSE 格式：data: <json>\n\n
                        payload = json.dumps({"delta": chunk}, ensure_ascii=False)
                        yield f"data: {payload}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                err = json.dumps({"error": str(e)})
                yield f"data: {err}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as e:
        logger.exception("Stream chat failed")
        raise HTTPException(status_code=500, detail={"message": str(e)})


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

        p = get_provider(provider, api_key=api_key, model=model or "")
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


def _get_db():
    from main import get_app_state
    return get_app_state()["db"]


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
