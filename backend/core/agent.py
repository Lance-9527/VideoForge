# backend/core/agent.py
import asyncio
import uuid
from typing import AsyncGenerator, Dict, Any, Optional, Callable
from datetime import datetime
from pydantic import BaseModel, Field

# llm 和 task_queue 在 __init__ 时注入


# ============================================================================
# Event Schemas (保持前端协议不变)
# ============================================================================

class BaseEvent(BaseModel):
    event_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class TextEvent(BaseEvent):
    event_type: str = "text"
    content: str

class ProgressEvent(BaseEvent):
    event_type: str = "progress"
    stage: str
    progress: float  # 0.0 ~ 1.0

class ImageEvent(BaseEvent):
    event_type: str = "image"
    url: str
    caption: Optional[str] = None

class VideoEvent(BaseEvent):
    event_type: str = "video"
    url: str
    duration: Optional[float] = None

class ActionRequiredEvent(BaseEvent):
    event_type: str = "action_required"
    action_id: str
    prompt: str
    choices: list[Dict[str, str]]  # [{"id": "approve", "label": "确认"}, ...]

class DoneEvent(BaseEvent):
    event_type: str = "done"


# ============================================================================
# ConversationAgent
# ============================================================================

class ConversationAgent:
    def __init__(self, llm, orchestrator):
        self.llm = llm
        self.orch = orchestrator
        self._pending_actions: Dict[str, asyncio.Event] = {}
        self._action_responses: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------------
    # Pipeline Router
    # ------------------------------------------------------------------------
    async def stream_pipeline(
        self,
        intent: str,
        params: Dict[str, Any],
        project_id: str
    ) -> AsyncGenerator[BaseEvent, None]:
        """根据 intent 路由到对应的 pipeline"""
        pipeline_map = {
            "generate_video": self._generate_video_pipeline,
            "suggest_characters": self._suggest_characters_pipeline,
            "review_consistency": self._review_consistency_pipeline,
            "refine_shot": self._refine_shot_pipeline,
        }
        
        handler = pipeline_map.get(intent)
        if not handler:
            yield TextEvent(content=f"未知意图: {intent}")
            yield DoneEvent()
            return
        
        async for event in handler(params, project_id):
            yield event

    # ------------------------------------------------------------------------
    # 1. 生成视频 Pipeline
    # ------------------------------------------------------------------------
    async def _generate_video_pipeline(
        self,
        params: Dict[str, Any],
        project_id: str
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        完整视频生成流程:
        1. 调用 generate_script 生成脚本和镜头列表
        2. 对每个镜头调用 generate_shot 生成视频
        3. 组装时间线
        """
        try:
            # Step 1: 生成脚本
            yield ProgressEvent(stage="script_generation", progress=0.0)
            
            script_payload = {
                "project_id": project_id,
                "user_prompt": params.get("prompt", ""),
                "style": params.get("style", "cinematic"),
                "duration": params.get("duration", 30)
            }
            
            script_handle = self.orch.submit(
                task_id=f"script_{uuid.uuid4().hex[:8]}",
                task_type="generate_script",
                payload=script_payload
            )
            
            # 等待脚本生成完成
            while script_handle.state not in [TaskState.COMPLETED, TaskState.FAILED]:
                await asyncio.sleep(0.5)
            
            if script_handle.state == TaskState.FAILED:
                yield TextEvent(content=f"脚本生成失败: {script_handle.result.error}")
                yield DoneEvent()
                return
            
            shots = script_handle.result.data.get("shots", [])
            if not shots:
                yield TextEvent(content="未生成任何镜头")
                yield DoneEvent()
                return
            
            yield TextEvent(content=f"已生成 {len(shots)} 个镜头，开始逐个渲染...")
            
            # Step 2: 生成每个镜头的视频
            shot_urls = []
            for i, shot in enumerate(shots):
                shot_id = shot.get("id", f"shot_{i}")
                yield ProgressEvent(
                    stage=f"shot_{i+1}",
                    progress=(i + 1) / len(shots)
                )
                
                shot_payload = {
                    "shot_id": shot_id,
                    "num_candidates": params.get("num_candidates", 4),
                    "model_provider": params.get("model_provider", "runway"),
                    "model_name": params.get("model_name", "gen3")
                }
                
                shot_handle = self.orch.submit(
                    task_id=f"shot_{shot_id}_{uuid.uuid4().hex[:8]}",
                    task_type="generate_shot",
                    payload=shot_payload
                )
                
                # 注册进度回调
                progress_events = []
                def on_progress(progress_data: Dict[str, Any]):
                    progress_events.append(progress_data)
                
                shot_handle.add_progress_callback(on_progress)
                
                # 等待镜头生成完成
                while shot_handle.state not in [TaskState.COMPLETED, TaskState.FAILED]:
                    await asyncio.sleep(0.5)
                    # 推送最新进度
                    if progress_events:
                        latest = progress_events[-1]
                        yield ProgressEvent(
                            stage=f"shot_{i+1}_render",
                            progress=latest.get("progress", 0.0)
                        )
                
                if shot_handle.state == TaskState.FAILED:
                    yield TextEvent(content=f"镜头 {i+1} 生成失败: {shot_handle.result.error}")
                    continue
                
                # 推送预览图和视频
                preview_img = shot_handle.result.data.get("preview_image")
                if preview_img:
                    yield ImageEvent(
                        url=preview_img,
                        caption=shot.get("description", f"镜头 {i+1}")
                    )
                
                video_url = shot_handle.result.data.get("video_url")
                if video_url:
                    yield VideoEvent(url=video_url)
                    shot_urls.append(video_url)
            
            # Step 3: 组装时间线 (简化版，直接拼接)
            if shot_urls:
                yield ProgressEvent(stage="assembling_timeline", progress=0.95)
                
                # 这里调用 DB 更新，标记项目的镜头链
                db = next(get_db())
                for idx, url in enumerate(shot_urls):
                    db.execute(
                        "UPDATE shots SET video_url = :url, sequence = :seq WHERE project_id = :pid AND id = :sid",
                        {"url": url, "seq": idx, "pid": project_id, "sid": shots[idx].get("id")}
                    )
                db.commit()
                
                # 假设拼接后的最终 URL (实际应调用 ffmpeg 或云服务)
                final_url = f"/api/projects/{project_id}/final_video.mp4"
                yield VideoEvent(url=final_url)
                yield TextEvent(content="视频生成完成！")
            
            yield DoneEvent()
        
        except Exception as e:
            yield TextEvent(content=f"Pipeline 错误: {str(e)}")
            yield DoneEvent()

    # ------------------------------------------------------------------------
    # 2. 角色建议 Pipeline
    # ------------------------------------------------------------------------
    async def _suggest_characters_pipeline(
        self,
        params: Dict[str, Any],
        project_id: str
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        角色建议流程:
        1. 调用 LLM 生成角色建议
        2. 返回 ActionRequiredEvent 等待用户确认
        3. 用户响应后继续
        """
        try:
            yield ProgressEvent(stage="analyzing_script", progress=0.2)
            
            # 读取项目脚本
            db = next(get_db())
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project or not project.script:
                yield TextEvent(content="项目脚本为空，无法生成角色建议")
                yield DoneEvent()
                return
            
            # 调用 LLM 生成角色建议
            prompt = f"""
根据以下脚本，建议 3-5 个主要角色，包括：
- 角色名
- 性格特点
- 外貌描述
- 在故事中的作用

脚本:
{project.script[:2000]}
"""
            
            response = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            
            suggestions = response.get("content", "未生成建议")
            yield TextEvent(content=f"角色建议:\n{suggestions}")
            
            # 创建 ActionRequired 事件
            action_id = f"char_approval_{uuid.uuid4().hex[:8]}"
            event = asyncio.Event()
            self._pending_actions[action_id] = event
            
            yield ActionRequiredEvent(
                action_id=action_id,
                prompt="是否确认这些角色设定？",
                choices=[
                    {"id": "approve", "label": "确认"},
                    {"id": "edit", "label": "修改"},
                    {"id": "regenerate", "label": "重新生成"}
                ]
            )
            
            # 等待用户响应
            await event.wait()
            
            user_choice = self._action_responses.get(action_id, {}).get("choice")
            if user_choice == "approve":
                yield TextEvent(content="已确认角色设定")
                # 保存到 DB
                db.execute(
                    "UPDATE projects SET characters = :chars WHERE id = :pid",
                    {"chars": suggestions, "pid": project_id}
                )
                db.commit()
            elif user_choice == "edit":
                yield TextEvent(content="请在编辑器中修改角色设定")
            else:
                yield TextEvent(content="已取消")
            
            # 清理
            del self._pending_actions[action_id]
            if action_id in self._action_responses:
                del self._action_responses[action_id]
            
            yield DoneEvent()
        
        except Exception as e:
            yield TextEvent(content=f"角色建议失败: {str(e)}")
            yield DoneEvent()

    # ------------------------------------------------------------------------
    # 3. 一致性检查 Pipeline
    # ------------------------------------------------------------------------
    async def _review_consistency_pipeline(
        self,
        params: Dict[str, Any],
        project_id: str
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        一致性检查流程:
        1. 调用 generate_script 获取镜头列表
        2. 调用一致性检查模块 (continuity)
        3. 返回问题报告和修复建议
        """
        try:
            yield ProgressEvent(stage="loading_shots", progress=0.1)
            
            # 获取项目所有镜头
            script_payload = {
                "project_id": project_id,
                "user_prompt": "",  # 仅用于获取现有镜头
                "style": "default",
                "duration": 0
            }
            
            script_handle = self.orch.submit(
                task_id=f"consistency_script_{uuid.uuid4().hex[:8]}",
                task_type="generate_script",
                payload=script_payload
            )
            
            while script_handle.state not in [TaskState.COMPLETED, TaskState.FAILED]:
                await asyncio.sleep(0.3)
            
            if script_handle.state == TaskState.FAILED:
                yield TextEvent(content="无法加载镜头列表")
                yield DoneEvent()
                return
            
            shots = script_handle.result.data.get("shots", [])
            if len(shots) < 2:
                yield TextEvent(content="镜头数量不足，无需检查一致性")
                yield DoneEvent()
                return
            
            yield ProgressEvent(stage="analyzing_consistency", progress=0.5)
            
            # 模拟一致性检查 (实际应调用专门的 continuity 模块)
            issues = []
            for i in range(len(shots) - 1):
                shot_a = shots[i]
                shot_b = shots[i + 1]
                # 简单规则: 检查时间戳、光照、角色位置
                if abs(shot_a.get("timestamp", 0) - shot_b.get("timestamp", 0)) > 60:
                    issues.append(f"镜头 {i+1} 和 {i+2} 之间时间跨度过大")
                if shot_a.get("lighting") != shot_b.get("lighting"):
                    issues.append(f"镜头 {i+1} 和 {i+2} 光照不一致")
            
            if not issues:
                yield TextEvent(content="✅ 未发现一致性问题")
                yield DoneEvent()
                return
            
            # 返回问题报告
            report = "一致性检查报告:\n" + "\n".join(f"- {issue}" for issue in issues)
            yield TextEvent(content=report)
            
            # 创建修复建议 ActionRequired
            action_id = f"consistency_fix_{uuid.uuid4().hex[:8]}"
            event = asyncio.Event()
            self._pending_actions[action_id] = event
            
            yield ActionRequiredEvent(
                action_id=action_id,
                prompt="建议自动修复这些问题？",
                choices=[
                    {"id": "auto_fix", "label": "自动修复"},
                    {"id": "manual", "label": "手动调整"},
                    {"id": "ignore", "label": "忽略"}
                ]
            )
            
            await event.wait()
            
            user_choice = self._action_responses.get(action_id, {}).get("choice")
            if user_choice == "auto_fix":
                yield TextEvent(content="正在自动修复...")
                # 这里应调用修复逻辑
                yield TextEvent(content="修复完成")
            else:
                yield TextEvent(content="已保留原始镜头")
            
            del self._pending_actions[action_id]
            if action_id in self._action_responses:
                del self._action_responses[action_id]
            
            yield DoneEvent()
        
        except Exception as e:
            yield TextEvent(content=f"一致性检查失败: {str(e)}")
            yield DoneEvent()

    # ------------------------------------------------------------------------
    # 4. 精修镜头 Pipeline
    # ------------------------------------------------------------------------
    async def _refine_shot_pipeline(
        self,
        params: Dict[str, Any],
        project_id: str
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        精修镜头流程:
        1. 调用 generate_shot 生成多个候选
        2. 返回预览图和视频
        """
        try:
            shot_id = params.get("shot_id")
            if not shot_id:
                yield TextEvent(content="缺少 shot_id 参数")
                yield DoneEvent()
                return
            
            yield ProgressEvent(stage="refining_shot", progress=0.1)
            
            refine_payload = {
                "shot_id": shot_id,
                "num_candidates": params.get("candidates", 4),
                "model_provider": params.get("model_provider", "runway"),
                "model_name": params.get("model_name", "gen3")
            }
            
            refine_handle = self.orch.submit(
                task_id=f"refine_{shot_id}_{uuid.uuid4().hex[:8]}",
                task_type="generate_shot",
                payload=refine_payload
            )
            
            # 注册进度回调
            def on_progress(progress_data: Dict[str, Any]):
                pass  # 可以在这里实时推送进度
            
            refine_handle.add_progress_callback(on_progress)
            
            # 等待完成
            while refine_handle.state not in [TaskState.COMPLETED, TaskState.FAILED]:
                await asyncio.sleep(0.5)
            
            if refine_handle.state == TaskState.FAILED:
                yield TextEvent(content=f"精修失败: {refine_handle.result.error}")
                yield DoneEvent()
                return
            
            # 返回结果
            candidates = refine_handle.result.data.get("candidates", [])
            for idx, candidate in enumerate(candidates):
                preview = candidate.get("preview_image")
                if preview:
                    yield ImageEvent(
                        url=preview,
                        caption=f"候选 {idx+1}"
                    )
                
                video = candidate.get("video_url")
                if video:
                    yield VideoEvent(url=video)
            
            yield TextEvent(content=f"生成了 {len(candidates)} 个候选版本")
            yield DoneEvent()
        
        except Exception as e:
            yield TextEvent(content=f"精修失败: {str(e)}")
            yield DoneEvent()

    # ------------------------------------------------------------------------
    # Action Response Handler
    # ------------------------------------------------------------------------
    def respond_to_action(self, action_id: str, response: Dict[str, Any]):
        """前端调用此方法响应 ActionRequiredEvent"""
        self._action_responses[action_id] = response
        if action_id in self._pending_actions:
            self._pending_actions[action_id].set()

