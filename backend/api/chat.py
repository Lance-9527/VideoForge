"""
VideoForge Chat API - 改造版
新增 action 端点 + 流式事件类型
保持向后兼容原有 /api/chat/ 和 /api/chat/stream
"""

import asyncio
import logging
import json
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


logger = logging.getLogger(__name__)

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
    """获取 LLM 客户端。

    ★ v1.4.3: 原来这里 `return LLMClient()` —— 没传 settings，
      所以拿不到用户在设置里配的 minimax key，每次调用都会 401。
      现在从全局 settings 读。
    """
    from main import get_app_state
    state = get_app_state()
    try:
        s = state["db"].get_all_settings()
        client = LLMClient(settings=s)
        logger.info(
            "chat LLM 客户端: provider=%s model=%s key=%s (db=%s)",
            client.provider, client.model, "有" if client.api_key else "无",
            getattr(state.get("db"), "db_path", "?"),
        )
        if not client.api_key:
            logger.warning(
                "chat LLM 没有 API key —— provider=%s model=%s。"
                "请到「设置」里配置对应 provider 的 key。",
                client.provider, client.model,
            )
        return client
    except Exception:
        logger.exception("构造 LLMClient 失败，回退默认（多半会 401）")
        return LLMClient()


async def get_agent(
    session_id: str,
    project_id: Optional[str],
    llm: LLMClient = Depends(get_llm_client)
) -> ConversationAgent:
    """获取或创建 Agent 实例。

    ★ v1.4.3 修两处（1.4.0 的 agent/chat 签名不匹配，实际调用直接 500）：
      1. `ConversationAgent(llm_client=..., project_id=...)`
         → agent 的真实签名是 `__init__(self, llm, orchestrator)`；
         project_id 是 `stream_pipeline(intent, params, project_id)` 的参数，
         不是构造函数参数。
      2. **没传 orchestrator** —— agent 需要 TaskQueue 才能 submit 任务，
         少了它整个生成流程根本走不动。
    """
    cache_key = f"{session_id}:{project_id or 'default'}"

    # ★ v1.4.5: AGENT_INSTANCES 原来**只增不减** —— 每个新 session_id
    #   就在全局字典里留下一个 agent，永远不清理（内存泄漏）。
    #   现在超过上限就淘汰最老的一批。
    if len(AGENT_INSTANCES) > 64:
        for _old in list(AGENT_INSTANCES.keys())[:32]:
            AGENT_INSTANCES.pop(_old, None)

    if cache_key not in AGENT_INSTANCES:
        orch = None
        try:
            from main import get_app_state
            orch = get_app_state().get("queue")
        except Exception:
            orch = None
        AGENT_INSTANCES[cache_key] = ConversationAgent(
            llm=llm,
            orchestrator=orch,
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
                # ★ v1.4.3: 先提意图，再跑对应 pipeline
                #   （原来调 agent.process_message() —— agent 根本没这个方法）
                intent = await agent.extract_intent(request.message)
                action = str(intent.get("action") or "chat")
                params = intent.get("params") or {}
                if action == "chat":
                    # 闲聊：直接回一句话，不进 pipeline
                    reply = ""
                    try:
                        resp = await agent.llm.chat(
                            [{"role": "user", "content": request.message}],
                            max_tokens=800,
                        )
                        reply = resp or ""
                    except Exception as e:
                        reply = f"（对话失败：{e}）"
                    yield f"data: {json.dumps({'type': 'text', 'content': reply}, ensure_ascii=False, default=str)}" + chr(10) * 2
                    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False, default=str)}" + chr(10) * 2
                    return

                async for event in agent.stream_pipeline(
                    action, params, request.project_id or ""
                ):
                    event_dict = event.model_dump()
                    event_dict["type"] = event.type

                    # action_required 注册到全局字典（点确认按钮时用）
                    if event.type == "action_required":
                        PENDING_ACTIONS[event.action_id] = {
                            "agent": agent,
                            "session_id": session_id,
                            "timestamp": str(event.timestamp),
                        }

                    # ★ StreamingResponse 必须 yield str/bytes —— 原来 yield dict
                    #   直接 500（'dict' object has no attribute 'encode'）
                    yield ("data: " + json.dumps(event_dict, ensure_ascii=False, default=str)
                           + chr(10) + chr(10))

                    if event.type == "done":
                        break

            except Exception as e:
                import traceback
                logger.exception("chat stream 失败")
                err = {"type": "error", "message": str(e),
                       "trace": traceback.format_exc()[-800:]}
                yield ("data: " + json.dumps(err, ensure_ascii=False, default=str)
                       + chr(10) + chr(10))
        
        return StreamingResponse(event_generator())
    
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
        
        # ★ v1.4.3: 方法名错 —— agent 里叫 respond_to_action(action_id, response)，
        #   这里调的 resolve_action 不存在 -> 点确认按钮必然 AttributeError。
        agent.respond_to_action(action_id, {"choice": choice})
        
        # 清理
        del PENDING_ACTIONS[action_id]
        
        return ActionResponse(
            status="resolved",
            message=f"Action resolved with choice: {choice}"
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/canonical-characters")
async def get_canonical_characters_endpoint(project_id: str):
    """取项目里**用户主动给定为基准**的角色（一致性第一优先级）。

    ★ v1.4.2：agent 生成脚本/分镜时会把这些角色注入 LLM 上下文，
      保证"用用户给定的那个角色"，而不是"项目里最常见的那个"。
    """
    from main import get_app_state
    db = get_app_state()["db"]
    chars = db.get_canonical_characters(project_id)
    return {
        "characters": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "description": c.get("description"),
                "costume_main": c.get("costume_main"),
                "reference_image_path": c.get("reference_image_path"),
            }
            for c in chars
        ],
        "total": len(chars),
    }


@router.get("/validate-continuity")
async def validate_continuity_endpoint(project_id: str):
    """生成前的**阻断式**一致性 + 转镜体检。

    ★ v1.4.2：原来只有 diff_shots()（只报告），用户照样能点生成。
      现在 fatal（换模型/画幅不一致）直接拦下，并给出统一建议。
    """
    from main import get_app_state
    from core.consistency import validate_consistency, plan_unify, consistency_brief
    from core.continuity import chain_plan

    db = get_app_state()["db"]
    shots = db.list_shots(project_id) if hasattr(db, "list_shots") else []
    if not shots:
        return {"ok": True, "message": "项目还没有分镜", "shots": 0}

    try:
        report = validate_consistency(shots, project_id=project_id, db=db)
    except Exception as e:
        type_name = type(e).__name__
        if type_name == "ConsistencyError":
            return {
                "ok": False,
                "blocked": True,
                "message": str(e),
                "report": getattr(e, "report", {}),
            }
        raise

    plan = chain_plan(shots)
    return {
        "ok": True,
        "blocked": False,
        "shots": len(shots),
        "report": report,
        "brief": consistency_brief(shots),
        "chain_plan": plan,
        "unify_plan": plan_unify(shots),
    }


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

