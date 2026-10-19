"""
VideoForge Conversation Agent - 核心编排层
负责意图识别、任务分派、流式事件生成
"""

import asyncio
import json
import uuid
from typing import AsyncGenerator, Dict, Any, Optional, List
from datetime import datetime
from pydantic import BaseModel, Field

# 假设现有导入
from core.llm import LLMClient



# ============ Pydantic Schemas ============

class IntentSchema(BaseModel):
    """LLM 返回的结构化意图"""
    action: str = Field(..., description="generate_video | suggest_characters | review_consistency | refine_shot | chat")
    params: Dict[str, Any] = Field(default_factory=dict)
    reasoning: Optional[str] = None


class AgentEvent(BaseModel):
    """流式事件基类"""
    type: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class TextEvent(AgentEvent):
    type: str = "text"
    content: str


class ProgressEvent(AgentEvent):
    type: str = "progress"
    message: str
    percent: int = Field(ge=0, le=100)


class ActionRequiredEvent(AgentEvent):
    type: str = "action_required"
    action_id: str
    prompt: str
    choices: List[Dict[str, str]]  # [{"id": "...", "label": "..."}]


class ImageEvent(AgentEvent):
    type: str = "image"
    url: str
    caption: Optional[str] = None


class VideoEvent(AgentEvent):
    type: str = "video"
    url: str
    thumbnail: Optional[str] = None
    duration: Optional[float] = None


class DoneEvent(AgentEvent):
    type: str = "done"


# ============ Agent 核心类 ============

class ConversationAgent:
    """
    对话式 Agent，负责：
    1. 用 LLM 解析用户意图
    2. 分派到 orchestrator 任务
    3. 流式返回进度/媒体/交互事件
    """
    
    INTENT_SYSTEM_PROMPT = """You are VideoForge intent classifier. Extract structured action from user message.
Return JSON with this schema:
{
  "action": "generate_video | suggest_characters | review_consistency | refine_shot | chat",
  "params": {...},
  "reasoning": "..."
}

Action types:
- generate_video: user wants to create video (params: genre, scenes, duration)
- suggest_characters: user wants character design suggestions (params: style_preference)
- review_consistency: user wants to check project consistency (params: project_id)
- refine_shot: user wants to modify a specific shot (params: shot_id, changes)
- chat: casual conversation or unclear intent (params: {})

Examples:
"Create a sci-fi action video 30 seconds" → {"action": "generate_video", "params": {"genre": "sci-fi action", "duration": 30}}
"Show me some character ideas for fantasy" → {"action": "suggest_characters", "params": {"style_preference": "fantasy"}}
"Check my project for errors" → {"action": "review_consistency", "params": {}}
"Make shot 3 darker" → {"action": "refine_shot", "params": {"shot_id": 3, "changes": "darker lighting"}}
"""

    def __init__(self, llm_client: LLMClient, project_id: Optional[str] = None):
        self.llm = llm_client
        self.project_id = project_id
        self.pending_actions: Dict[str, asyncio.Event] = {}
        self.action_results: Dict[str, Any] = {}

    async def parse_intent(self, user_message: str) -> IntentSchema:
        """用 LLM 解析用户意图"""
        try:
            response = await self.llm.chat_completion(
                messages=[
                    {"role": "system", "content": self.INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.3,
                max_tokens=500
            )
            
            # 提取 JSON（支持 markdown 包裹）
            content = response.strip()
            if not intent_str.strip().startswith("{"):
                intent = json.loads(intent_str)
            else:
                # LLM returned natural language, retry with stricter prompt
                retry_prompt = f"{prompt}\n\nYou MUST respond with valid JSON only. Previous response was not valid JSON."
                intent_str = await self.llm.generate(retry_prompt, max_tokens=300)
                intent = json.loads(intent_str)
        except json.JSONDecodeError:
            # Fallback: assume generate_video
            intent = {"intent": "generate_video", "params": {"prompt": message}}
        
        intent_type = intent.get("intent", "generate_video")
        params = intent.get("params", {})
        
        # Route to appropriate pipeline
        if intent_type == "generate_video":
            async for event in self._generate_video_pipeline(params, conversation_id):
                yield event
        elif intent_type == "suggest_characters":
            async for event in self._suggest_characters_pipeline(params):
                yield event
        elif intent_type == "review_consistency":
            async for event in self._review_consistency_pipeline(params, conversation_id):
                yield event
        elif intent_type == "refine_shot":
            async for event in self._refine_shot_pipeline(params, conversation_id):
                yield event
        else:
            yield TextEvent(text=f"Unknown intent: {intent_type}")
            yield DoneEvent()
    
    async def _generate_video_pipeline(
        self,
        params: dict,
        conversation_id: str
    ) -> AsyncIterator[ConversationEvent]:
        """Full video generation pipeline with streaming events."""
        prompt = params.get("prompt", "")
        style = params.get("style", "cinematic")
        duration = params.get("duration", 10)
        
        yield TextEvent(text=f"🎬 Starting video generation: \"{prompt}\"")
        yield ProgressEvent(stage="planning", progress=0.0)
        
        # Step 1: Generate storyboard
        yield TextEvent(text="\n📝 Planning shots...")
        # TODO: 调用 self.orch.enqueue_task("generate_script", {...}) 等待结果
        storyboard = {"shots": []}  # placeholder
        
        yield ProgressEvent(stage="planning", progress=0.3)
        yield TextEvent(text=f"✓ Created {len(storyboard['shots'])} shots")
        
        # Step 2: Generate each shot
        shot_results = []
        for i, shot in enumerate(storyboard["shots"]):
            yield ProgressEvent(
                stage="generation",
                progress=0.3 + (0.5 * (i / len(storyboard["shots"])))
            )
            yield TextEvent(text=f"\n🎥 Shot {i+1}/{len(storyboard['shots'])}: {shot['description'][:60]}...")
            
            # TODO: 调用 self.orch.enqueue_task("generate_shot", payload) 等待
        result = {"shot_id": "placeholder", "video_url": "", "preview_image": ""}
        shot_results.append(result)
        
        # Stream preview frame
        if result.get("preview_frame"):
            yield ImageEvent(
                url=result["preview_frame"],
                caption=f"Shot {i+1} preview"
            )
        
        yield ProgressEvent(stage="assembly", progress=0.8)
        yield TextEvent(text="\n🎞️ Assembling final video...")
        
        # Step 3: Assemble
        # TODO: 调用 self.orch.enqueue_task("assemble_timeline", payload)
        final_video = {"url": "", "thumbnail": ""}
        
        yield ProgressEvent(stage="complete", progress=1.0)
        yield VideoEvent(
            url=final_video["url"],
            thumbnail=final_video.get("thumbnail"),
            duration=final_video.get("duration", duration)
        )
        
        # Register action for potential refinement
        action_id = register_pending_action(
            conversation_id=conversation_id,
            action_type="refine_video",
            context={
                "video_url": final_video["url"],
                "storyboard": storyboard,
                "shot_results": shot_results
            }
        )
        
        yield ActionRequiredEvent(
            action_id=action_id,
            prompt="Would you like to refine any shots or adjust the video?",
            options=["Refine shot", "Change style", "Regenerate", "Done"]
        )
        yield DoneEvent()
    
    async def _suggest_characters_pipeline(
        self,
        params: dict
    ) -> AsyncIterator[ConversationEvent]:
        """Generate character suggestions for consistency."""
        concept = params.get("concept", "")
        count = params.get("count", 3)
        
        yield TextEvent(text=f"👥 Generating {count} character concepts for: \"{concept}\"")
        yield ProgressEvent(stage="generation", progress=0.0)
        
        # Generate character descriptions
        char_prompt = f"""Create {count} distinct character concepts for: {concept}
Return JSON array with: name, description, visual_traits, personality."""
        
        chars_str = await self.llm.generate(char_prompt, max_tokens=800)
        try:
            characters = json.loads(chars_str)
        except json.JSONDecodeError:
            characters = [{"name": f"Character {i+1}", "description": concept} for i in range(count)]
        
        yield ProgressEvent(stage="generation", progress=0.5)
        
        # Generate reference images
        for i, char in enumerate(characters):
            yield TextEvent(text=f"\n🎨 Generating {char.get('name', f'Character {i+1}')}...")
            
            ref_image = await self.orch.generate_character_reference(
                description=char.get("description", ""),
                traits=char.get("visual_traits", [])
            )
            
            char["reference_image"] = ref_image["url"]
            
            yield ImageEvent(
                url=ref_image["url"],
                caption=f"{char.get('name')}: {char.get('description', '')[:80]}"
            )
            
            yield ProgressEvent(stage="generation", progress=0.5 + (0.5 * ((i+1) / len(characters))))
        
        yield TextEvent(text="\n✓ Character concepts ready")
        yield DoneEvent()
    
    async def _review_consistency_pipeline(
        self,
        params: dict,
        conversation_id: str
    ) -> AsyncIterator[ConversationEvent]:
        """Review video for consistency issues."""
        video_url = params.get("video_url", "")
        
        yield TextEvent(text="🔍 Analyzing video for consistency issues...")
        yield ProgressEvent(stage="analysis", progress=0.0)
        
        # Extract frames for analysis
        # TODO: 调用 self.orch.enqueue_task("extract_keyframes", payload)
        frames = []
        
        yield ProgressEvent(stage="analysis", progress=0.3)
        yield TextEvent(text=f"📊 Analyzing {len(frames)} key frames...")
        
        # Check continuity
        # TODO: 调用 self.orch.enqueue_task("check_continuity", payload)
        issues = {}
        
        yield ProgressEvent(stage="analysis", progress=0.8)
        
        if not issues:
            yield TextEvent(text="\n✓ No consistency issues detected")
        else:
            yield TextEvent(text=f"\n⚠️ Found {len(issues)} potential issues:")
            for issue in issues:
                yield TextEvent(text=f"  • {issue['description']} (severity: {issue['severity']})")
                if issue.get("frame_url"):
                    yield ImageEvent(url=issue["frame_url"], caption=f"Issue: {issue['type']}")
        
        yield ProgressEvent(stage="complete", progress=1.0)
        yield DoneEvent()
    
    async def _refine_shot_pipeline(
        self,
        params: dict,
        conversation_id: str
    ) -> AsyncIterator[ConversationEvent]:
        """Refine a specific shot in the video."""
        shot_index = params.get("shot_index", 0)
        refinement = params.get("refinement", "")
        action_id = params.get("action_id")
        
        # Resolve pending action to get context
        context = None
        if action_id:
            context = resolve_action(action_id)
        
        if not context:
            yield TextEvent(text="❌ Cannot refine: no video context found")
            yield DoneEvent()
            return
        
        storyboard = context.get("storyboard", {})
        shots = storyboard.get("shots", [])
        
        if shot_index >= len(shots):
            yield TextEvent(text=f"❌ Invalid shot index: {shot_index}")
            yield DoneEvent()
            return
        
        shot = shots[shot_index]
        
        yield TextEvent(text=f"🔧 Refining shot {shot_index + 1}: \"{refinement}\"")
        yield ProgressEvent(stage="refinement", progress=0.0)
        
        # Regenerate shot with refinement
        refined_prompt = f"{shot['description']} | Refinement: {refinement}"
        
        result = await self.orch.generate_shot(
            prompt=refined_prompt,
            style=storyboard.get("style", "cinematic"),
            duration=shot.get("duration", 3)
        )
        
        yield ProgressEvent(stage="refinement", progress=0.7)
        
        if result.get("preview_frame"):
            yield ImageEvent(
                url=result["preview_frame"],
                caption=f"Refined shot {shot_index + 1}"
            )
        
        # Re-assemble with new shot
        shot_results = context.get("shot_results", [])
        shot_results[shot_index] = result
        
        yield TextEvent(text="\n🎞️ Re-assembling video...")
        # TODO: 调用 self.orch.enqueue_task("assemble_timeline", payload)
        final_video = ""
        
        yield ProgressEvent(stage="complete", progress=1.0)
        yield VideoEvent(
            url=final_video["url"],
            thumbnail=final_video.get("thumbnail"),
            duration=final_video.get("duration")
        )
        
        yield TextEvent(text="\n✓ Shot refined successfully")
        yield DoneEvent()


# Action management (in-memory for demo; use Redis in production)
PENDING_ACTIONS: dict[str, dict] = {}

def register_pending_action(
    conversation_id: str,
    action_type: str,
    context: dict
) -> str:
    """Register a pending user action that can be resumed later."""
    action_id = f"{conversation_id}:{action_type}:{hash(str(context)) % 10000}"
    PENDING_ACTIONS[action_id] = {
        "conversation_id": conversation_id,
        "action_type": action_type,
        "context": context,
        "created_at": asyncio.get_event_loop().time()
    }
    return action_id

def resolve_action(action_id: str) -> dict | None:
    """Retrieve and remove a pending action."""
    return PENDING_ACTIONS.pop(action_id, None)

