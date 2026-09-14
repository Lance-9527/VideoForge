"""
MiniMax（Ollama 本地）适配器

本适配器用于本地 LLM 生成剧本/分镜/提示词。
视频生成仍需调用云端 API，但剧本/创意生成可完全本地。

也可以扩展支持本地视频生成模型（如 Stable Video Diffusion、AnimateDiff）。
"""

import asyncio
import httpx
from typing import Dict, Any, List

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class OllamaLocalAdapter(VideoAdapter):
    """MiniMax 本地 LLM（用于剧本/创意生成，不是视频生成）"""
    name = "minimax"
    display_name = "MiniMax（本地 LLM）"
    description = "本地 LLM，仅用于剧本/分镜/提示词生成。视频生成仍需云端 API。"
    supported_aspect_ratios = []
    supported_resolutions = []
    max_duration = 0
    min_duration = 0
    supports_first_last_frame = False
    supports_character_ref = False
    supports_negative_prompt = False
    # ⚠ 这个适配器只是"本地 LLM 占位"，generate() 永远返回失败。
    #   它绝不能出现在视频模型下拉框里 —— 否则用户选中它必然"点了生成没反应"。
    #   真正的 MiniMax 视频通道是 hailuo 适配器。
    usable_for_video = False
    is_local = True
    requires_api_key = False

    def __init__(self, api_key: str = "", config: Dict[str, Any] = None):
        super().__init__(api_key, config)
        self.base_url = self.config.get("base_url", "http://localhost:11434")
        self.model = self.config.get("model_name", "qwen2.5:7b")

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        """不支持视频生成"""
        return self.make_result(False, error="MiniMax 本地 LLM 不支持视频生成，仅用于剧本/创意")

    async def query_task(self, task_id: str) -> VideoGenResult:
        return self.make_result(False, error="Not supported")

    async def chat(self, prompt: str, system: str = "", **kwargs) -> str:
        """调用 Ollama 聊天接口"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            **kwargs,
        }

        client = await self._ensure_client()
        try:
            r = await client.post(
                f"{self.base_url}/api/chat",
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("message", {}).get("content", "")
        except Exception as e:
            return f"[Ollama error: {e}]"

    async def generate_text(self, prompt: str, system: str = "", **kwargs) -> str:
        """生成文本（用于剧本等）"""
        return await self.chat(prompt, system, **kwargs)
