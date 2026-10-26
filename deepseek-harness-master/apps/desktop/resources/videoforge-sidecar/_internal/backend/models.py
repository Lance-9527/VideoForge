"""
VideoForge · 数据模型（Pydantic schemas）

对应 SQL 表结构。参考 Pavo AI 的设计模式（雪花 ID + 大整数转 string）。
"""

from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import model_validator, field_validator, BaseModel, Field
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
    # 节奏档位：auto（按意图推断）/ fast_drama 2.5s / promo 3.5s / talking 5s / ambient 8s / legacy 12s
    pacing: str = "auto"
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
    gender: str = ""                # ★ 2026-10-23：男/女（空串 = 自动推断）
    costume_main: str = ""
    costume_alternate: str = ""
    props: List[str] = Field(default_factory=list)
    reference_features: Optional[Dict[str, Any]] = None  # 抽象化视觉指纹


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    body_type: Optional[str] = None
    age: Optional[str] = None
    # ★ 2026-10-23：`characters.gender` 列已加（core/db.py `_ensure_column`）。
    #   空串 = 回到自动推断（文本猜 → LLM 判）；填「男」/「女」= 以角色卡为准。
    #   ⚠ 旧注释写着"表里没有这一列，加了会 SQL 报错"——那是当时的实情；
    #     列补上后注释同步改掉，别让下一个人被旧注释误导。
    gender: Optional[str] = None
    costume_main: Optional[str] = None
    costume_alternate: Optional[str] = None
    props: Optional[List[str]] = None
    reference_image_path: Optional[str] = None
    reference_features: Optional[Dict[str, Any]] = None
    # ★ 2026-10-23 增：`characters.voice_id` 这一列库里有、前端也在用，但请求
    #   模型里没有 —— 于是 `PATCH /api/characters/{cid}` 带 voice_id 会**被静默忽略**
    #   （返回 200 却什么都没写），"给角色指定音色"这条因此一直做不成。
    voice_id: Optional[str] = None


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
    # ★ 2026-10-20 修复：**缺少这个字段导致台词被静默丢弃**。
    #   `core/dialogue.timeline_lines()` 明确以 `dialogue` 为配音/字幕的唯一数据源，
    #   但 Pydantic 模型里没有它 —— POST /api/shots 建分镜时 dialogue 被直接丢掉，
    #   于是 `compose` 永远认为"这一镜没有台词"，角色不出声、
    #   配音一致性整条链路拿不到输入（只有脚本生成那条绕过 API 直写库才带得上）。
    #   支持写法：{"character": "周衡", "text": "我昨天还在这里。", "emotion": "平静"}
    #            或裸字符串 "我昨天还在这里。"
    dialogue: Optional[Any] = None


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
    # ★ 2026-09-28：MiniMax-M3.1-Flash-Preview 的**思考深度**
    #   （Anthropic 兼容协议的 `output_config.effort`：low/medium/high/xhigh/max）。
    #   必须在这里声明 —— FastAPI 的请求模型是白名单，没声明的字段会被**静默丢掉**，
    #   用户在设置里改了半天其实一点用没有。`test_settings_wiring.py` 就是抓这个的。
    llm_effort: str = ""
    ollama_base_url: str = "http://localhost:11434"
    # VideoForge settings shared by the desktop and legacy frontends.
    llm_base_url: str = ""
    default_video_resolution: str = "1080p"
    allow_video_spend: bool = False
    default_video_budget_yuan: float = 0
    image_provider: str = ""
    image_model: str = ""
    tts_api_keys: Dict[str, str] = Field(default_factory=dict)
    tts_models: Dict[str, str] = Field(default_factory=dict)
    voiceover_mode: str = "auto"
    voice_rate: float = 1
    voice_volume: float = 1
    lipsync_mode: str = "off"
    dialogue_backfill: bool = True
    public_base_url: str = ""
    public_sync_dir: str = ""
    tunnel_named: str = ""
    tunnel_hostname: str = ""


def _lenient_number(v):
    """把界面上来的"数字"变成能解析的值。

    ★ 2026-09-27 第 62 轮（用户实测：**换出图模型时保存失败**）：
      设置页把数字输入框的**空字符串** `""` 原样传了上来，而
      `Optional[int]` 解析不了 `""` ⇒ FastAPI 直接 **422 整单拒绝** ✗✗ ——
      用户看到的是「保存失败: body.max_shot_seconds: …」，
      而他只是想把出图模型换成别的。一个**无关字段**把整件事弄失败。
      ⇒ 空值当"没填"（None），数字字符串正常转；转不了也当没填，
        绝不让它把整单请求打回去。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    t = str(v).strip()
    if not t:
        return None
    try:
        f = float(t)
    except (TypeError, ValueError):
        return None
    return f


def _drop_blank_fields(data):
    """★ 第 63 轮：**所有**字段的空串都当"没填"（None），一次堵死整类。

    为什么（用户实测，两轮）：设置页会把**每一个空输入框**的 `""` 一起提交。
    只要有一个字段类型不符（`Optional[int]` / `Optional[bool]` / `Optional[list]`…），
    FastAPI 就 **422 整单拒绝** ⇒ 用户"只是换个出图模型"也保存失败 ✗✗。
    上一轮只修了 3 个数字字段，别的字段照样能把它打回去（所以他说"还是不行"）。
    ⇒ 在模型入口统一处理：空串 / 纯空白 ⇒ None（= 这一项没动）。
      以后再加字段也自动受保护。
    """
    if not isinstance(data, dict):
        return data
    out = {}
    for k, v in data.items():
        if isinstance(v, str) and not v.strip():
            out[k] = None
        else:
            out[k] = v
    return out


class SettingsUpdate(BaseModel):
    # ★ 第 63 轮：任何字段的空串都当没填 —— 见 _drop_blank_fields
    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data):
        normalized = _drop_blank_fields(data)
        # Explicit resets for model choices are meaningful. Blank paths/keys retain
        # the legacy no-change behavior instead of erasing configured resources.
        if isinstance(data, dict) and isinstance(normalized, dict):
            for key in ("llm_base_url", "llm_effort", "llm_model", "default_model_provider",
                        "default_model_name", "image_provider", "image_model"):
                if key in data and isinstance(data[key], str) and not data[key].strip():
                    normalized[key] = ""
        return normalized

    # ★ 第 62 轮：界面传空串不许把整单保存打回去（见 _lenient_number）
    @field_validator("max_shot_seconds", "voice_rate", "voice_volume",
                     mode="before", check_fields=False)
    @classmethod
    def _num_lenient(cls, v):
        return _lenient_number(v)

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
    # ★ 思考深度（MiniMax-M3.1-Flash-Preview 的 output_config.effort）
    llm_effort: Optional[str] = None
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
    image_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    default_video_resolution: Optional[str] = None
    allow_video_spend: Optional[bool] = None
    default_video_budget_yuan: Optional[float] = Field(default=None, ge=0)
    lipsync_mode: Optional[Literal["off", "cloud"]] = None
    dialogue_backfill: Optional[bool] = None
    # ★★ 白膜公网通道（2026-09-25 补）。
    #   这四个字段以前**一个都不在** —— 也就是界面「设置 → 白膜公网通道」
    #   里填的东西，点保存时被 FastAPI **静默丢掉**，
    #   用户看到"保存成功"，实际什么也没存进去。
    #   "长期方案"因此从来没真正生效过，只能一直用每次换域名的临时隧道。
    #   ★ 这跟本文件里 `tts_api_keys` / `tts_models` 记的是**同一个坑**，
    #     所以顺手加了一条自动检查：tests/test_settings_wiring.py 会枚举
    #     界面上所有 data-llm-setting，断言每个都能被这里接住。
    public_base_url: Optional[str] = None     # 自己的公网前缀（对象存储/服务器）
    public_sync_dir: Optional[str] = None     # 上面那个地址对应的本地根目录
    tunnel_named: Optional[str] = None        # 命名隧道名字（长期方案，推荐）
    tunnel_hostname: Optional[str] = None     # 命名隧道对应的固定域名
    # ★ 单镜时长上限（**花费保护**，不是硬限制）。
    #   ★★ 2026-09-29 修正：这里原本写着「用户明确要求过『最多 5 秒』」，
    #   而后端就把 5 当成了**默认值** —— 后果是实测事故：
    #     13 秒的分镜被压成 3×5 秒 → 段间串联 → 写实人脸被隐私审核拒收
    #     → 一个镜头花 3 次钱、且第 3 次必失败。用户看到的是「软件跑不通」。
    #   现在：**没设过就不限制**（None/0 = 由模型能力表决定单段上限）；
    #   设了才当上限。界面上写清楚了。
    max_shot_seconds: Optional[int] = None
    # ★ 配音总开关（2026-09-29，用户要求「配音可以先不上」）。
    #   "off"  → 所有出片路径默认**不配音**（调用方显式传 with_voice 仍可覆盖）
    #   "auto" → 有台词才配（默认，也是本来的行为）
    #   "on"   → 总是尝试配音
    voiceover_mode: Optional[str] = None
    # ★ Skill 系统开关（2026-09-26 融合 character-casting-studio / biaoqing-yanyi 时加）。
    #   `use_casting_skill`：角色出图是否走 casting skill 的提示词构建。
    #     默认开。关掉会回落到旧的纯中文短提示词（264 字符），
    #     适合"平台提示词长度受限"或"想让出图更快"的场景。
    #   `skill_dirs`：额外的 skill 目录（JSON 数组字符串），
    #     让用户能指向自己的 skill 库。
    #   ★ 这两个字段和上面那几条记的是**同一个坑**：读设置的地方有、
    #     SettingsUpdate 里没有 → 前端传了会被静默丢掉。
    #     tests/test_settings_wiring.py 的双向检查会盯住这条。
    use_casting_skill: Optional[str] = None
    skill_dirs: Optional[str] = None
    # 语音相关的偏好
    voice_rate: Optional[float] = None
    voice_volume: Optional[float] = Field(default=None,
                                          validator=_lenient_number)
