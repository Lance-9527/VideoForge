"""
VideoForge · 数据模型（Pydantic schemas）

对应 SQL 表结构。参考 Pavo AI 的设计模式（雪花 ID + 大整数转 string）。
"""

from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field
import uuid


def new_id() -> str:
    """生成 UUID4 字符串（替代雪花 ID，避免 JS 精度问题）"""
    return str(uuid.uuid4())


# ──────────── 通用 ────────────

class APIResponse(BaseModel):
    """统一响应格式（Pavo 风格）"""
    code: str = "000000"
    message: str = "success"
    data: Optional[Any] = None


# ──────────── 项目 ────────────

class ProjectCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    settings: Optional[Dict[str, Any]] = Field(default_factory=lambda: {
        "default_aspect_ratio": "16:9",
        "default_duration_per_shot": 10,
        "default_model": "kling",
        "default_resolution": "1080p",
        "style": "cinematic",  # cinematic/documentary/anime/ad/mv
    })


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class Project(BaseModel):
    id: str
    title: str
    description: str
    status: str  # draft/scripting/asset_building/generating/postprocessing/completed
    settings: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ──────────── 剧本 ────────────

class ScriptScene(BaseModel):
    """单个场次"""
    scene_number: int
    title: str
    location: str
    duration_seconds: int
    characters: List[str] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)
    dialogues: List[Dict[str, str]] = Field(default_factory=list)
    mood: str = "neutral"


class ScriptGenerateRequest(BaseModel):
    """生成剧本请求。project_id 从 URL 路径取，这里不重复"""
    user_prompt: str
    # 5 秒 ~ 30 分钟（1800 秒）。上限是硬约束：再长单次剧本生成已经不现实
    total_duration_seconds: int = Field(default=30, ge=5, le=1800)
    style: str = "cinematic"  # cinematic/documentary/anime/ad/mv
    auto_split_scenes: bool = True


class Script(BaseModel):
    id: str
    project_id: str
    user_prompt: str
    outline: str
    scenes: List[ScriptScene]
    three_layer_prompts: Dict[str, Any]  # shot_id -> {layer1, layer2, layer3}
    created_at: datetime


class ScriptUpdate(BaseModel):
    outline: Optional[str] = None
    scenes: Optional[List[ScriptScene]] = None
    three_layer_prompts: Optional[Dict[str, Any]] = None


# ──────────── 资产（角色 / 场景 / 道具）──────────

class CharacterCreate(BaseModel):
    project_id: str
    name: str
    description: str = ""           # 脸型/五官/标志特征
    body_type: str = ""             # 身高/体型/姿态
    age: str = ""
    costume_main: str = ""
    costume_alternate: str = ""
    props: List[str] = Field(default_factory=list)
    reference_features: Optional[Dict[str, Any]] = None  # 抽象化视觉指纹


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    body_type: Optional[str] = None
    age: Optional[str] = None
    costume_main: Optional[str] = None
    costume_alternate: Optional[str] = None
    props: Optional[List[str]] = None
    reference_image_path: Optional[str] = None
    reference_features: Optional[Dict[str, Any]] = None


class Character(CharacterCreate):
    id: str
    reference_image_path: Optional[str] = None
    status: str = "draft"  # draft/reference_generated/ready
    created_at: datetime


class SceneCreate(BaseModel):
    project_id: str
    name: str
    location_type: str = "outdoor"  # indoor/outdoor/mixed
    space_scale: str = "medium"     # large/medium/small
    lighting: str = "natural"
    time_of_day: str = "day"
    weather: str = "clear"
    description: str = ""
    # 固定槽位（地形/建筑/陈设/氛围/色调）。★ 必须在创建时就能传进来，
    # 否则 FastAPI 校验会把前端传的 reference_features 直接丢掉（曾真实发生过）。
    reference_features: Optional[Dict[str, Any]] = None


class SceneUpdate(BaseModel):
    name: Optional[str] = None
    location_type: Optional[str] = None
    space_scale: Optional[str] = None
    lighting: Optional[str] = None
    time_of_day: Optional[str] = None
    weather: Optional[str] = None
    description: Optional[str] = None
    reference_image_path: Optional[str] = None
    # 固定槽位（地形/建筑/陈设/氛围/色调）：逐字进每个镜头的提示词，
    # 保证同一地点在不同镜头里长得一样
    reference_features: Optional[Dict[str, Any]] = None


class Scene(SceneCreate):
    id: str
    reference_image_path: Optional[str] = None
    color_palette: List[str] = Field(default_factory=list)
    textures: List[str] = Field(default_factory=list)
    reference_features: Optional[Dict[str, Any]] = None
    created_at: datetime


class PropCreate(BaseModel):
    project_id: str
    name: str
    description: str = ""


class PropUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    reference_image_path: Optional[str] = None


class Prop(PropCreate):
    id: str
    reference_image_path: Optional[str] = None
    created_at: datetime


# ──────────── 分镜（核心）──────────

class ShotTimelineItem(BaseModel):
    """第 2 层：分秒时间线"""
    start: int               # 秒
    end: int                 # 秒
    action: str              # 动作描述
    expression: str = ""     # 表情
    camera: str = "static"    # 镜头：static/pan/tilt/zoom/dolly/tracking/crane
    props_used: List[str] = Field(default_factory=list)


class ShotCreate(BaseModel):
    project_id: str
    scene_id: Optional[str] = None      # 中途可换
    order_index: int = 0
    duration_seconds: int = 10          # 用户自定义时长（5/10/30/60/任意）

    # 三层提示词
    layer1_overview: str = ""
    layer2_timeline: List[ShotTimelineItem] = Field(default_factory=list)
    layer3_constraints: Dict[str, List[str]] = Field(default_factory=lambda: {
        "must_not_appear": [],
        "must_not_happen": [],
        "must_keep": [],
        "must_appear": [],
        "must_happen": [],
    })

    # 引用
    character_ids: List[str] = Field(default_factory=list)

    # 模型（中途可换）
    model_provider: str = "kling"
    model_name: str = "kling-1.6"
    aspect_ratio: str = "16:9"
    resolution: str = "1080p"

    # 负向参考图（不希望出现的内容）
    negative_reference_paths: List[str] = Field(default_factory=list)


class ShotUpdate(BaseModel):
    """中途修改 shot（人物/场景/模型任意）"""
    scene_id: Optional[str] = None
    order_index: Optional[int] = None
    duration_seconds: Optional[int] = None
    layer1_overview: Optional[str] = None
    layer2_timeline: Optional[List[ShotTimelineItem]] = None
    layer3_constraints: Optional[Dict[str, List[str]]] = None
    character_ids: Optional[List[str]] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    aspect_ratio: Optional[str] = None
    resolution: Optional[str] = None


class Shot(ShotCreate):
    id: str
    status: str = "pending"  # pending/generating/completed/failed
    progress: float = 0.0
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    selected_candidate_id: Optional[str] = None
    error_message: Optional[str] = None
    task_id: Optional[str] = None
    created_at: datetime


# ──────────── 任务 ────────────

class TaskInfo(BaseModel):
    id: str
    type: str
    status: str  # 借鉴 MPT 状态机：pending / script / material / shot / voice / subtitle / bgm / postprocess / publish / success / failed / cancelled
    progress: float = 0.0
    progress_message: str = ""              # 当前阶段可读消息（前端展示）
    failed_stage: Optional[str] = None      # 失败时具体卡在哪个阶段（script / voice / subtitle 等）
    payload: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cancellable: bool = True                # 是否支持取消
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ──────────── 视频生成 ────────────

class GenerateVideoRequest(BaseModel):
    # ⚠ shot_id 必须可选：路由本身已经是 POST /api/shots/{sid}/generate，
    #   前端只传 {num_candidates} 是合理的。此前定义为必填 →
    #   所有"生成"请求都在 Pydantic 校验阶段就以
    #   `body.shot_id: Field required` 失败，**根本到不了模型那一层**。
    shot_id: Optional[str] = None
    model_provider: Optional[str] = None  # 不传则用 shot 自身设置
    model_name: Optional[str] = None
    num_candidates: int = 4  # 一次生成几个候选
    # 兼容「生成」页的新参数（走本地合成时使用）
    mode: Optional[str] = None
    duration_seconds: Optional[int] = None
    voice_id: Optional[str] = None
    with_voice: Optional[bool] = None
    burn_subtitles: Optional[bool] = None
    auto_images: Optional[bool] = None
    image_provider: Optional[str] = None
    image_size: Optional[str] = None
    prefer_local: Optional[bool] = None


class AbstractionRequest(BaseModel):
    """版权抽象化请求"""
    project_id: str
    source_description: str


# ──────────── 设置 ────────────

class AppSettings(BaseModel):
    """应用全局设置（API keys 等）"""
    api_keys: Dict[str, str] = Field(default_factory=dict)  # provider -> key
    default_model_provider: str = "kling"
    default_model_name: str = "kling-1.6"
    default_aspect_ratio: str = "16:9"
    default_resolution: str = "1080p"
    output_dir: str = "./data/outputs"
    cache_dir: str = "./data/cache"
    ffmpeg_path: str = "ffmpeg"
    enable_analytics: bool = False
    enable_llm_local: bool = False  # 是否用本地 Ollama
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    remote_llm_provider: str = "deepseek"  # deepseek/openai
    remote_llm_key: Optional[str] = None

    # LLM 对话设置（新）
    llm_provider: str = "deepseek"        # 默认对话 provider
    llm_model: str = "deepseek-chat"
    llm_api_keys: Dict[str, str] = Field(default_factory=dict)  # 各 provider 的 key
    ollama_base_url: str = "http://localhost:11434"


class SettingsUpdate(BaseModel):
    api_keys: Optional[Dict[str, str]] = None
    default_model_provider: Optional[str] = None
    default_model_name: Optional[str] = None
    default_aspect_ratio: Optional[str] = None
    default_resolution: Optional[str] = None
    output_dir: Optional[str] = None
    cache_dir: Optional[str] = None
    ffmpeg_path: Optional[str] = None
    enable_analytics: Optional[bool] = None
    enable_llm_local: Optional[bool] = None
    ollama_url: Optional[str] = None
    ollama_model: Optional[str] = None
    remote_llm_provider: Optional[str] = None
    remote_llm_key: Optional[str] = None
    # LLM 对话
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_keys: Optional[Dict[str, str]] = None
    ollama_base_url: Optional[str] = None
    # ★ 语音合成（TTS）的 Key。以前这里没有这个字段 → 前端就算传了也会被
    #   FastAPI 校验**静默丢掉**，于是 SiliconFlow 那 8 个音色永远不可能可用。
    tts_api_keys: Optional[Dict[str, str]] = None
    # ★ 配音模型（2026-09-14 用户要求"真正的去选择配音模型"）。
    #   结构：{"minimax": "speech-2.8-hd", "dashscope": "cosyvoice-v2"}
    #   以前**没有**这个字段 → 型号只能写死在适配器里（minimax 一直是
    #   `speech-02-hd`，且 dispatcher 从不把 config 传下去，连那个写死的
    #   分支都走不到）。字段在这里 = 前端能存、后端能读、合成时真的用上。
    tts_models: Optional[Dict[str, str]] = None
    image_provider: Optional[str] = None
    # 语音相关的偏好
    voice_rate: Optional[float] = None
    voice_volume: Optional[float] = None
