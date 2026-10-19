"""
VideoForge Chat API - 改造版
新增 action 端点 + 流式事件类型
保持向后兼容原有 /api/chat/ 和 /api/chat/stream
"""

import asyncio
import uuid
from typing import Dict, Optional, Any
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

from core.agent import ConversationAgent, AgentEvent
from core.llm import LLMClient
def get_current_user():  # 占位 — 实际应从 main app 注入 user
    return None


router = APIRouter(prefix="/api/chat", tags=["chat"])

# 全局 pending actions 存储（生产环境应用 Redis）
PENDING_ACTIONS: Dict[str, Dict[str, Any]] = {}
AGENT_INSTANCES: Dict[str, ConversationAgent] = {}  # 按 session_id 缓存


# ============ Request/Response Models ============

class ChatRequest(BaseModel):
    message: str
    project_id: Optional[str] = None
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    project_id: Optional[str] = None


class ActionRequest(BaseModel):
    action_id: str
    choice: str


class ActionResponse(BaseModel):
    status: str
    message: str


# ============ 依赖注入 ============

async def get_llm_client() -> LLMClient:
    """获取 LLM 客户端（实际应从全局池获取）"""
    # 这里简化，实际应在 main.py 初始化时创建单例
    return LLMClient()


async def get_agent(
    session_id: str,
    project_id: Optional[str],
    llm: LLMClient = Depends(get_llm_client)
) -> ConversationAgent:
    """获取或创建 Agent 实例"""
    cache_key = f"{session_id}:{project_id or 'default'}"
    
    if cache_key not in AGENT_INSTANCES:
        AGENT_INSTANCES[cache_key] = ConversationAgent(
            llm_client=llm,
            project_id=project_id
        )
    
    return AGENT_INSTANCES[cache_key]


# ============ 原有端点（向后兼容）============

@router.post("/", response_model=ChatResponse)
async def chat_sync(
    request: ChatRequest,
    llm: LLMClient = Depends(get_llm_client),
    current_user: dict = Depends(get_current_user)
):
    """
    原有同步聊天端点（向后兼容）
    收集所有流式事件后一次性返回文本
    """
    try:
        session_id = request.session_id or str(uuid.uuid4())
        agent = await get_agent(session_id, request.project_id, llm)
        
        # 收集所有文本事件
        text_parts = []
        async for event in agent.process_message(request.message):
            if event.type == "text":
                text_parts.append(event.content)
            elif event.type == "done":
                break
        
        reply = "\n".join(text_parts)
        
        return ChatResponse(
            reply=reply or "I'm processing your request...",
            project_id=request.project_id
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    llm: LLMClient = Depends(get_llm_client),
    current_user: dict = Depends(get_current_user)
):
    """
    改造后的流式端点
    返回 SSE 格式事件：text / progress / action_required / image / video / done
    """
    try:
        session_id = request.session_id or str(uuid.uuid4())
        agent = await get_agent(session_id, request.project_id, llm)
        
        async def event_generator():
            try:
                async for event in agent.process_message(
                    request.message,
                    context={"user_id": current_user.get("id")}
                ):
                    # 转换为 SSE 格式
                    event_dict = event.dict()
                    
                    # 如果是 action_required，注册到全局字典
                    if event.type == "action_required":
                        PENDING_ACTIONS[event.action_id] = {
                            "agent": agent,
                            "session_id": session_id,
                            "timestamp": event.timestamp
                        }
                    
                    yield {
                        "event": event.type,
                        "data": event_dict
                    }
                    
                    if event.type == "done":
                        break
            
            except Exception as e:
                yield {
                    "event": "error",
                    "data": {"message": str(e)}
                }
        
        return EventSourceResponse(event_generator())
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ 新端点：Action 处理 ============

@router.post("/action", response_model=ActionResponse)
async def handle_action(
    request: ActionRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    处理用户的 action 选择（不调用 LLM，直接解锁 Agent 等待）
    
    Args:
        request: {action_id, choice}
    
    Returns:
        {status: "resolved", message: "..."}
    """
    action_id = request.action_id
    choice = request.choice
    
    if action_id not in PENDING_ACTIONS:
        raise HTTPException(
            status_code=404,
            detail=f"Action {action_id} not found or already resolved"
        )
    
    try:
        action_data = PENDING_ACTIONS[action_id]
        agent: ConversationAgent = action_data["agent"]
        
        # 解锁 Agent 的等待
        agent.resolve_action(action_id, choice)
        
        # 清理
        del PENDING_ACTIONS[action_id]
        
        return ActionResponse(
            status="resolved",
            message=f"Action resolved with choice: {choice}"
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/actions/pending")
async def list_pending_actions(current_user: dict = Depends(get_current_user)):
    """
    列出当前 pending 的 actions（调试用）
    """
    return {
        "pending_actions": [
            {
                "action_id": aid,
                "session_id": data["session_id"],
                "timestamp": data["timestamp"]
            }
            for aid, data in PENDING_ACTIONS.items()
        ]
    }


# ============ 原有辅助端点（假设存在）============

@router.get("/history")
async def get_chat_history(
    project_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """获取聊天历史（占位）"""
    # 实际应从数据库读取
    return {"messages": []}


@router.delete("/session/{session_id}")
async def clear_session(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """清理 session（占位）"""
    cache_key_prefix = f"{session_id}:"
    keys_to_delete = [k for k in AGENT_INSTANCES.keys() if k.startswith(cache_key_prefix)]
    
    for key in keys_to_delete:
        del AGENT_INSTANCES[key]
    
    return {"status": "cleared", "session_id": session_id}

