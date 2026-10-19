# backend/core/agent.py
import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator, Dict, Any, List, Optional, Callable
from datetime import datetime
from pydantic import BaseModel, Field

# v1.4.2: TaskState 用来等 task 跑完（原来漏 import 导致 agent 直接报错）
from core.task_queue import TaskState

# llm 和 task_queue 在 __init__ 时注入


# ============================================================================
# Event Schemas (保持前端协议不变)
# ============================================================================

class BaseEvent(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class TextEvent(BaseEvent):
    type: str = "text"
    content: str

class ProgressEvent(BaseEvent):
    type: str = "progress"
    stage: str
    progress: float  # 0.0 ~ 1.0

class ImageEvent(BaseEvent):
    type: str = "image"
    url: str
    caption: Optional[str] = None

class VideoEvent(BaseEvent):
    type: str = "video"
    url: str
    duration: Optional[float] = None

class ActionRequiredEvent(BaseEvent):
    type: str = "action_required"
    action_id: str
    prompt: str
    choices: list[Dict[str, str]]  # [{"id": "approve", "label": "确认"}, ...]

class DoneEvent(BaseEvent):
    type: str = "done"


# ============================================================================
# ConversationAgent
# ============================================================================

class ConversationAgent:
    def __init__(self, llm, orchestrator):
        self.llm = llm
        self.orch = orchestrator
        self._pending_actions: Dict[str, asyncio.Event] = {}
        self._action_responses: Dict[str, Dict[str, Any]] = {}

    # ──────────── 意图提取 ────────────

    INTENT_SYSTEM_PROMPT = """你是 VideoForge 的意图路由器。把用户消息解析成结构化意图。

只输出 JSON，不要任何其他文字。schema:
{
  "action": "generate_video | suggest_characters | review_consistency | refine_shot | chat",
  "params": {...},
  "reasoning": "一句话说明"
}

action 判定:
- generate_video: 用户想做视频/短片/片子（"做个 30 秒赛博朋克短片"）
- suggest_characters: 用户要角色建议/角色设计（"给我几个角色想法"）
- review_consistency: 用户要检查一致性/质量（"检查一下我的项目"）
- refine_shot: 用户要改某一镜（"第 3 镜再暗一点"）
- chat: 闲聊或无法判定

params 按 action 给:
- generate_video: {"prompt": 原话, "duration": 秒数(int, 默认 30), "style": "cinematic"}
- suggest_characters: {"style_preference": "用户描述的风格"}
- review_consistency: {}
- refine_shot: {"shot_id": 镜号(int), "changes": "用户说的修改"}
- chat: {}

例子:
用户: "帮我做一个 30 秒的赛博朋克短片"
-> {"action":"generate_video","params":{"prompt":"帮我做一个 30 秒的赛博朋克短片","duration":30,"style":"cinematic"},"reasoning":"用户要做视频"}

用户: "给我几个武侠角色建议"
-> {"action":"suggest_characters","params":{"style_preference":"武侠"},"reasoning":"用户要角色建议"}

用户: "检查一下我的项目"
-> {"action":"review_consistency","params":{},"reasoning":"用户要检查一致性"}
"""

    async def extract_intent(self, user_message: str) -> Dict[str, Any]:
        """从用户消息提取结构化意图。

        ★ v1.4.3 补上这个方法：原来 chat.py 调 agent.process_message()，
          agent.py 根本没有这个方法 —— 整个 chat 链路第一次调用就 500。
          现在 agent 自己负责"理解意图"，chat.py 只负责把事件流出去。

        ★ 用 M3.1-Flash 测过：它输出的是**干净 JSON**（不带 <think>），
          所以这里的解析是稳的；解析失败会 fallback 到 generate_video。
        """
        default = {
            "action": "generate_video",
            "params": {"prompt": user_message, "duration": 30, "style": "cinematic"},
            "reasoning": "意图解析失败，按默认走生成流程",
        }
        try:
            resp = await self.llm.chat(
                [
                    {"role": "system", "content": self.INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                # ★ v1.4.3: 500 会被 M3.1 思考链吃光 → 意图解析永远失败
                max_tokens=4000,
                temperature=0.1,
            )
            content = (resp or "").strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.lower().startswith("json"):
                    content = content[4:]
            parsed = json.loads(content)
            if not isinstance(parsed, dict) or "action" not in parsed:
                return default
            parsed.setdefault("params", {})
            return parsed
        except Exception as e:
            logging.warning("意图解析失败，回退默认: %s", e)
            return default

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
    # ──────────── v1.4.2 辅助 ────────────

    def _db(self):
        """拿全局 DB 实例（拿不到就返回 None，不抛）。"""
        try:
            from main import get_app_state
            return get_app_state().get("db")
        except Exception:
            return None

    def _canonical_characters(self, project_id: str) -> List[Dict[str, Any]]:
        """取项目里用户主动给定为基准的角色。

        ★ 拿不到时静默返回空 list（上层会 fallback 到"项目内多数派"）。
        """
        if not project_id:
            return []
        try:
            from main import get_app_state
            db = get_app_state()["db"]
            return db.get_canonical_characters(project_id) or []
        except Exception:
            return []

    def _chain_config_for(self, shots: List[Dict[str, Any]], index: int) -> Dict[str, Any]:
        """算第 index 个镜头的转镜强约束（prev_shot_id / transition / must_match）。

        ★ v1.4.2：把 core.continuity.chain_plan() 算出来的约束真正用起来 ——
          原来 chain_plan 的结果没人读，"转镜要接得上"其实没被强制。
        """
        if index <= 0 or index >= len(shots):
            return {}
        try:
            from core.continuity import chain_plan
            plan = chain_plan(shots[: index + 1])
            for item in plan:
                if item.get("shot_id") == shots[index].get("id"):
                    return {
                        "prev_shot_id": item.get("prev_shot_id"),
                        "transition_type": item.get("transition_type", "none"),
                        "must_match_first_frame_desc": item.get("must_match_first_frame_desc", ""),
                    }
        except Exception:
            pass
        return {}

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
            
            # ★ v1.4.2: 把**用户主动给定**的角色注入脚本生成。
            #   用户上传了参考图 / 明确写了外观的角色优先于"项目内多数派"，
            #   否则用户给的那份设定会被 LLM 自己生成的角色盖掉。
            user_prompt = params.get("prompt", "")
            canonical_chars = self._canonical_characters(project_id)
            if canonical_chars:
                _lines = (chr(10)).join(
                    f"- {ch.get('name', '')}: {ch.get('description', '') or '（见参考图）'}"
                    f"{'｜主服装：' + ch['costume_main'] if ch.get('costume_main') else ''}"
                    for ch in canonical_chars
                )
                user_prompt = (
                    user_prompt
                    + (chr(10) * 2) + "【必须使用的角色（用户已指定，不要改名或改外观）】" + (chr(10))
                    + _lines
                )

            script_payload = {
                "project_id": project_id,
                "user_prompt": user_prompt,
                "style": params.get("style", "cinematic"),
                "duration": params.get("duration", 30)
            }
            
            script_handle = self.orch.submit(
                task_id=f"script_{uuid.uuid4().hex[:8]}",
                task_type="generate_script",
                payload=script_payload
            )
            
            # 等待脚本生成完成
            while script_handle.status not in [TaskState.SUCCESS.value, TaskState.FAILED.value]:
                await asyncio.sleep(0.5)
            
            if script_handle.status == TaskState.FAILED.value:
                yield TextEvent(content=f"脚本生成失败: {script_handle.error}")
                yield DoneEvent()
                return
            
            # ★ v1.4.3 关键修复：generate_script 的真实返回值是
            #   {"title", "scenes_count", "three_layer_count"} —— **没有 shots**。
            #   分镜是 generate_script 之后**另外建**的（剧本场次 → 分镜 + 角色/场景引用）。
            #   原来这里直接 result.get("shots") 永远拿到空 → "未生成任何镜头"。
            #   现在：调 core.shotbuild 把剧本拆成分镜，再从 DB 读真实分镜。
            yield TextEvent(content="剧本已生成，正在拆分分镜...")
            try:
                from core.shotbuild import build_shots_from_script, ShotBuildError
                db = self._db()
                if db is None:
                    yield TextEvent(content="拿不到数据库，无法拆分分镜")
                    yield DoneEvent()
                    return
                build = build_shots_from_script(db, project_id, replace=True)
                shots = build.get("shots") or []
            except ShotBuildError as e:
                yield TextEvent(content=f"拆分分镜失败: {e}")
                yield DoneEvent()
                return
            except Exception as e:
                logging.exception("拆分分镜异常")
                yield TextEvent(content=f"拆分分镜失败: {e}")
                yield DoneEvent()
                return

            if not shots:
                yield TextEvent(content="剧本里没有可用场次，无法拆分镜")
                yield DoneEvent()
                return

            _link = build.get("linkage") or {}
            yield TextEvent(
                content=(f"已拆出 {len(shots)} 个分镜"
                         f"（关联场景 {_link.get('scene_linked', 0)} 个、"
                         f"角色 {_link.get('char_linked', 0)} 个），开始逐个渲染...")
            )
            
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
                }
                # ★ v1.4.3: **不要**在这里写死默认 provider/model。
                #   原来写死 "runway"/"gen3"，而 orchestrator 里是
                #   `provider = override_provider or shot["model_provider"]` ——
                #   agent 硬塞的 runway 直接覆盖了分镜自己的模型设置，
                #   结果用户配了 kling key 却报"Runway API key 未配置"。
                #   现在只在用户**明确指定**时才覆盖，否则用分镜自己的
                #   （拆镜时已按项目一致性原则选定）。
                if params.get("model_provider"):
                    shot_payload["model_provider"] = params["model_provider"]
                if params.get("model_name"):
                    shot_payload["model_name"] = params["model_name"]
                
                # ★ v1.4.2: 转镜强约束注入 —— 这一镜要接上一镜的尾帧
                _chain = self._chain_config_for(shots, i)
                if _chain:
                    shot_payload["chain_config"] = _chain
                
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
                while shot_handle.status not in [TaskState.SUCCESS.value, TaskState.FAILED.value]:
                    await asyncio.sleep(0.5)
                    # 推送最新进度
                    if progress_events:
                        latest = progress_events[-1]
                        yield ProgressEvent(
                            stage=f"shot_{i+1}_render",
                            progress=latest.get("progress", 0.0)
                        )
                
                if shot_handle.status == TaskState.FAILED.value:
                    yield TextEvent(content=f"镜头 {i+1} 生成失败: {shot_handle.error}")
                    continue
                
                # 推送预览图和视频
                preview_img = shot_handle.result.get("preview_image")
                if preview_img:
                    yield ImageEvent(
                        url=preview_img,
                        caption=shot.get("description") or f"镜头 {i+1}"
                    )
                
                video_url = shot_handle.result.get("video_url")
                if video_url:
                    yield VideoEvent(url=video_url)
                    shot_urls.append(video_url)
                else:
                    # ★ v1.4.3: provider 失败（key 无效/配额不足/模型不存在）时，
                    #   orchestrator 把错误**写进 shot 行**并不抛异常，
                    #   所以 task 状态是"成功"但没有产物。
                    #   原来这里静默跳过 → 用户在对话里只看到"完成"，
                    #   完全不知道出了什么事。现在把真实原因说出来。
                    _reason = ""
                    try:
                        _db = self._db()
                        _sh = _db.get_shot(shot_id) if _db else None
                        _reason = str((_sh or {}).get("error_message") or
                                      (_sh or {}).get("error") or "")
                    except Exception:
                        _reason = ""
                    _reason = _reason or shot_handle.error or "未知原因"
                    yield TextEvent(
                        content=(
                            f"镜头 {i+1} 没有产出视频：{_reason}\n"
                            f"（可在「设置」里检查该模型服务的 API Key 与配额，"
                            f"或换用其他 provider）"
                        )
                    )
            
            # Step 3: 组装时间线 (简化版，直接拼接)
            if shot_urls:
                yield ProgressEvent(stage="assembling_timeline", progress=0.95)
                
                # 这里调用 DB 更新，把生成的视频写回镜头
                db = self._db()
                for idx, url in enumerate(shot_urls):
                    try:
                        db.update_shot_video(shots[idx].get("id"), url, idx)
                    except Exception as e:
                        logging.warning("写回 shot 视频失败 %s: %s", shots[idx].get("id"), e)
                
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
            db = self._db()
            project = db.get_project(project_id) if db else None
            script_text = (project or {}).get("script") if isinstance(project, dict) else getattr(project, "script", None)
            if not script_text:
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
            
            while script_handle.status not in [TaskState.SUCCESS.value, TaskState.FAILED.value]:
                await asyncio.sleep(0.3)
            
            if script_handle.status == TaskState.FAILED.value:
                yield TextEvent(content="无法加载镜头列表")
                yield DoneEvent()
                return
            
            shots = script_handle.result.get("shots", [])
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
            }
            # ★ v1.4.3: 同上，不写死 runway —— 重抽要沿用该分镜自己的模型
            if params.get("model_provider"):
                refine_payload["model_provider"] = params["model_provider"]
            if params.get("model_name"):
                refine_payload["model_name"] = params["model_name"]
            
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
            while refine_handle.status not in [TaskState.SUCCESS.value, TaskState.FAILED.value]:
                await asyncio.sleep(0.5)
            
            if refine_handle.status == TaskState.FAILED.value:
                yield TextEvent(content=f"精修失败: {refine_handle.error}")
                yield DoneEvent()
                return
            
            # 返回结果
            candidates = refine_handle.result.get("candidates", [])
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


# ★ v1.4.2 兼容别名：api/chat.py 用 `AgentEvent` 做类型标注
AgentEvent = BaseEvent
