"""
VideoForge · LLM 客户端

支持：
- Ollama（本地，完全离线）
- DeepSeek（云端，性价比）
- OpenAI / GPT-4
- 任意 OpenAI 兼容 API（通过 base_url 配置）
"""

import asyncio
import json
import logging
import re
from typing import Dict, Any, List, Optional
import httpx

logger = logging.getLogger("videoforge.llm")


# ──────────── 健壮性工具函数（借鉴 MPT llm.py）────────────
# 独立模块：from .llm_robust import ...
# 提供：sanitize_error_message / normalize_text_response / freeze_config /
#       is_retryable_http_status / retry_backoff_seconds / _MAX_RETRIES / _RETRY_BACKOFF
from .llm_robust import (
    sanitize_error_message as _sanitize_error_message,
    normalize_text_response as _normalize_text_response,
    freeze_config as _freeze_config,
    is_retryable_http_status as _is_retryable_http_status,
    retry_backoff_seconds as _retry_backoff_seconds,
    _MAX_RETRIES,
)

# ★ 台词规范化（三层提示词的输出要用它统一成 {character,text,emotion}）
#   ⚠️ 这两个名字**必须在这里导入**：曾经漏了导入，而调用点在一个宽泛的
#   try/except 里 —— 结果是每次调用都抛 NameError，被 except 吞掉后走
#   "文本降级"兜底，把 **LLM 的原始 JSON 文本**塞进了 layer1_overview。
#   线上表现为：分镜的第 1 层是一大段 ```json {...}```，
#   第 2/3 层全空；送给视频模型的提示词因此是一段 JSON 垃圾，
#   用户看到的就是"生成的视频跟实际描述的完全不相干"。
from .dialogue import (
    normalize_timeline,
    timeline_dialogue_stats,
)


# ──────────── Provider 端点表（OpenAI 兼容，2026-09 校验）────────────
# 说明：httpx 的 base_url 与相对 path 拼接遵循绝对路径规则，
#       因此 base_url 不带末尾斜杠、path 以 / 开头即可得到正确完整 URL。
# 之前只处理 deepseek/openai/ollama，其余 provider（含 MiniMax）base_url 为空，
# 导致 httpx.UnsupportedProtocol: Request URL is missing 'http://' —— 剧本生成必失败。
PROVIDER_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "deepseek":  {"base_url": "https://api.deepseek.com",                       "path": "/v1/chat/completions", "default_model": "deepseek-chat"},
    "openai":    {"base_url": "https://api.openai.com",                         "path": "/v1/chat/completions", "default_model": "gpt-4o"},
    "minimax":   {"base_url": "https://api.minimaxi.com",                       "path": "/v1/chat/completions", "default_model": "MiniMax-Text-01"},
    "qwen":      {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "path": "/v1/chat/completions", "default_model": "qwen-plus"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "path": "/v1/chat/completions", "default_model": "qwen-plus"},
    "glm":       {"base_url": "https://open.bigmodel.cn/api/paas/v4",           "path": "/chat/completions",    "default_model": "glm-4-plus"},
    "zhipu":     {"base_url": "https://open.bigmodel.cn/api/paas/v4",           "path": "/chat/completions",    "default_model": "glm-4-plus"},
    "kimi":      {"base_url": "https://api.moonshot.cn",                        "path": "/v1/chat/completions", "default_model": "moonshot-v1-8k"},
    "moonshot":  {"base_url": "https://api.moonshot.cn",                        "path": "/v1/chat/completions", "default_model": "moonshot-v1-8k"},
    "doubao":    {"base_url": "https://ark.cn-beijing.volces.com/api/v3",       "path": "/chat/completions",    "default_model": ""},
    "hunyuan":   {"base_url": "https://api.hunyuan.cloud.tencent.com",          "path": "/v1/chat/completions", "default_model": "hunyuan-turbo"},
}

# provider 名规范化：前端传 "MiniMax" / "MiniMax-Text-01" 等大小写混写
_PROVIDER_ALIASES = {
    "minimax": "minimax", "minimaxi": "minimax", "abab": "minimax",
    "qwen": "qwen", "tongyi": "qwen", "aliyun": "qwen", "dashscope": "dashscope",
    "glm": "glm", "zhipu": "glm", "bigmodel": "glm", "chatglm": "glm",
    "kimi": "kimi", "moonshot": "moonshot",
    "doubao": "doubao", "volcengine": "doubao", "ark": "doubao",
    "hunyuan": "hunyuan", "tencent": "hunyuan",
    "deepseek": "deepseek", "openai": "openai", "ollama": "ollama",
}


def normalize_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[p]
    # 模糊匹配（如 "minimax-Text-01" 之类的误填）
    for alias, canon in _PROVIDER_ALIASES.items():
        if alias in p:
            return canon
    return p


class LLMClient:
    """统一 LLM 客户端（所有 provider 均可解析出可用端点）"""

    def __init__(self, provider: str = "deepseek",
                 api_key: str = "",
                 base_url: str = "",
                 model: str = ""):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def resolve_endpoint(self) -> tuple:
        """返回 (base_url, chat_path, model)。未知 provider 且无 base_url 时抛明确错误。"""
        canon = normalize_provider(self.provider)
        conf = PROVIDER_ENDPOINTS.get(canon)

        if canon == "ollama":
            return (self.base_url or "http://localhost:11434", "/api/chat", self.model or "qwen2.5:7b")

        if conf:
            base = (self.base_url or "").strip().rstrip("/") or conf["base_url"]
            path = conf["path"]
            # 用户把完整 chat 路径填在 base_url 里时的兼容处理
            if base.endswith("/v1/chat/completions") or base.endswith("/chat/completions"):
                if base.endswith("/v1/chat/completions"):
                    base = base[: -len("/v1/chat/completions")]
                else:
                    base = base[: -len("/chat/completions")]
                    path = "/chat/completions"
            model = self.model or conf.get("default_model", "")
            return (base, path, model)

        # 未知 provider：必须由用户提供 base_url（OpenAI 兼容）
        base = (self.base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError(
                f"未识别的 LLM provider「{self.provider}」且未填写 base_url。"
                f"请在设置中选择已支持的 provider（DeepSeek/Qwen/GLM/Kimi/MiniMax/Doubao/混元/Ollama），"
                f"或填写 OpenAI 兼容的 base_url。"
            )
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"base_url 必须以 http:// 或 https:// 开头，当前为「{base}」")
        path = "/v1/chat/completions"
        if base.endswith("/v1/chat/completions"):
            base = base[: -len("/v1/chat/completions")]
        return (base, path, self.model or "gpt-3.5-turbo")

    def _client(self) -> httpx.AsyncClient:
        base, _path, _model = self.resolve_endpoint()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return httpx.AsyncClient(
            base_url=base,
            timeout=httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=10.0),
            headers=headers,
        )

    async def chat(self, messages: List[Dict[str, str]],
                   temperature: float = 0.7,
                   max_tokens: int = 4000,
                   response_format: Optional[Dict] = None) -> str:
        """发送聊天请求，返回文本（所有 provider 走 OpenAI 兼容协议，Ollama 单独处理）"""
        canon = normalize_provider(self.provider)
        base, path, model = self.resolve_endpoint()
        client = self._client()
        try:
            if canon == "ollama":
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                }
                r = await client.post(path, json=payload)
                r.raise_for_status()
                return r.json().get("message", {}).get("content", "")

            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if response_format:
                payload["response_format"] = response_format

            r = await client.post(path, json=payload)
            if r.status_code >= 400:
                # 把上游错误正文带出来，便于前端/日志定位（而不是只吐 HTTP 码）
                detail = ""
                try:
                    body = r.json()
                    detail = (
                        (body.get("error") or {}).get("message")
                        or body.get("message")
                        or json.dumps(body, ensure_ascii=False)[:300]
                    )
                except Exception:
                    detail = (r.text or "")[:300]
                detail = _sanitize_error_message(detail)
                raise RuntimeError(
                    f"LLM 调用失败 [{canon} {model}] HTTP {r.status_code}: {detail}"
                )
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            return _normalize_text_response(content, canon)
        finally:
            await client.aclose()


# ──────────── 健壮 JSON 提取（LLM 输出容错）────────────
# LLM 常见问题：markdown 代码块、前后说明文字、尾随逗号、单/中文引号、
# Python 风格 True/False/None、截断（max_tokens 用尽）、不可见字符。
# 解析失败会直接导致"《解析失败》"剧本 —— 这里做多级容错。

_JSON_FIXES = (
    # 中文引号 → 英文
    ("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"), ("\u2019", "'"),
    # 全角符号
    ("\uff1a", ":"), ("\uff0c", ","),
)

_PY_LITERALS = (
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
    (re.compile(r"\bNone\b"), "null"),
)

_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip_fences(text: str) -> str:
    """去掉 markdown 代码块围栏（```json ... ``` / ``` ... ```）"""
    t = text.strip()
    # 去掉 BOM / 零宽字符
    t = t.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    if t.startswith("```"):
        lines = t.split("\n")
        lines = lines[1:]                                   # 去掉 ```json
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _balanced_slice(text: str, opener: str = "{", closer: str = "}") -> Optional[str]:
    """扫描出第一个括号平衡的 JSON 片段（跳过字符串内的括号）"""
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # 未闭合 → 返回剩余部分（可能被截断，交给修复器补全）
    return text[start:]


def _fix_single_quotes(s: str) -> str:
    """把 JSON 里的单引号键/值转成双引号（启发式，避免破坏已是双引号的字符串内容）"""
    out = re.sub(r"'([^'\"\\\n]*)'(\s*:)", r'"\1"\2', s)      # 'key':
    out = re.sub(r"(:\s*)'([^'\"\\\n]*)'", r'\1"\2"', out)      # :'value'
    out = re.sub(r"(,\s*)'([^'\"\\\n]*)'", r'\1"\2"', out)      # ,'value'
    out = re.sub(r"(\[\s*)'([^'\"\\\n]*)'", r'\1"\2"', out)     # ['value'
    return out


def _repair_json(s: str) -> str:
    """修复常见 JSON 语法问题"""
    out = s
    for bad, good in _JSON_FIXES:
        out = out.replace(bad, good)
    out = _BLOCK_COMMENT.sub("", out)
    out = _LINE_COMMENT.sub("", out)
    for pat, rep in _PY_LITERALS:
        out = pat.sub(rep, out)
    if "'" in out:                     # 存在单引号 → 尝试按 JSON 单引号风格修复
        out = _fix_single_quotes(out)
    out = _TRAILING_COMMA.sub(r"\1", out)
    return out


def _in_open_string(s: str) -> bool:
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
    return in_str


def _close_truncated(s: str) -> str:
    """按括号栈补全被截断的 JSON（并闭合未结束的字符串 / 缺失的值）"""
    out = s.rstrip()
    if _in_open_string(out):
        out += '"'
    out = re.sub(r",\s*\"[^\"]*\"\s*:\s*$", "", out)   # 尾部不完整的 "key":
    out = re.sub(r":\s*$", ': ""', out)                # 值缺失
    out = re.sub(r",\s*$", "", out)

    stack: List[str] = []
    in_str = False
    esc = False
    for ch in out:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def extract_json(text: str) -> Any:
    """从 LLM 输出中尽可能提取 JSON 对象；全部失败时抛出 ValueError（附原始片段）"""
    if text is None:
        raise ValueError("LLM 返回为空")
    raw = _strip_fences(str(text))

    # 1) 直接解析（原文 / 修复后）
    for candidate in (raw, _repair_json(raw)):
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 2) 平衡括号切片：对象优先；数组仅当文本本身就是数组根时才尝试
    slices = ["{", "}"] if raw.find("{") >= 0 else None
    attempts = []
    if slices:
        piece = _balanced_slice(raw, "{", "}")
        if piece:
            attempts.append(piece)
    if raw.lstrip().startswith("["):
        piece = _balanced_slice(raw, "[", "]")
        if piece:
            attempts.append(piece)

    for piece in attempts:
        for candidate in (piece, _repair_json(piece),
                          _close_truncated(_repair_json(piece)),
                          _close_truncated(piece)):
            try:
                return json.loads(candidate)
            except Exception:
                pass

    # 3) 正则兜底：抓第一个 {...} 区块（跨行、非贪婪）
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        piece = m.group(0)
        for candidate in (piece, _repair_json(piece),
                          _close_truncated(_repair_json(piece)),
                          _close_truncated(piece)):
            try:
                return json.loads(candidate)
            except Exception:
                pass

    raise ValueError(f"无法从模型输出中解析 JSON（前 200 字）：{raw[:200]!r}")


# ──────────── 提示词模板（剧本生成 + 抽象化）────────────

SCRIPT_GENERATION_SYSTEM = """你是专业 AI 短片编剧。

你将接收用户的创意想法，输出标准剧本格式（按场次组织）。

═══════════════════════════════════════════════════════════════
★ 写法要求（从一份**真实成品剧本**里量出来的，不是泛泛的"要专业"）
  参考物料：《40集农村微短剧剧本：寒潮抢收玉米（全集分集台词+镜头）》
  （用户提供，40 集全量实测：标题 4–10 字；每场 1–3 句台词；每场正文 32–83 字）
═══════════════════════════════════════════════════════════════
1. **每场标题是一个"钩子"**：4–10 字的短句，直接点出这一场的冲突或悬念。
   例：「寒潮预警，全村慌了」「有人带头拦着不卖」「大嘴彻底慌神」「全村和解」。
   不要写"第一场""开场""发展"这种没有信息量的标题。
2. **每场三件套**：`镜头`（一句画面：地点 + 主体动作 + 天光/景别）+
   `对白`（1–2 句，口语、短、带情绪）+ 需要时 `subtitle`（字幕点题或留悬念）。
3. **台词要短**：中文约 4 字/秒；一句台词不要超过这一场秒数能说完的长度。
   宁愿少说一句，也不要写长台词（长了会被迫变速，听起来很假）。
   可以在角色名后写表演提示，例如「阿诚（喇叭喊话）：…」「大嘴（高声煽动）：…」。
4. **每场只写一两句话的信息量**（正文 30–85 字）。不要写小说，不要写文学化心理描写，
   要写**能拍出来的东西**。
5. **主角必须有前史动机**：让观众明白他为什么做这个选择
   （成品：`我爹当年和你们一样，死守不卖，赌行情翻倍…最后八亩地五毛一斤甩卖`）。
   把这段前史放在一个**独立的揭示场**里，标题就是它的钩子。
6. **反派要有"行为方式"**，不是"他很坏"：写清他**怎么做**
   （成品：大嘴"爱煽动、赌行情" → 具体动作是拦住别人、当众喊话、事后翻脸不认账）。
7. **群像要摇摆**：群众不是背景板 —— 分化 → 动摇 → 后悔 → 和解，
   用「议论 / 两拨 / 犹豫 / 后悔 / 沉默」这类**具体反应**写出来。
8. **结尾金句点题**：最后一场用一句短而狠的话 + `subtitle` 收束
   （成品：阿诚终极旁白 + 字幕「不贪即是赚，止损方为赢」）。
9. 故事要有清晰的起承转合；每场结尾**留钩子**（有冲突、有留白），让人想追下一场。
10. 适合直接改写成视频提示词（避免文学化、抽象化表述）。

JSON Schema（`subtitle` / `hook` / `role` / `motivation` 是新增字段，
下游会用它们做剧本体检与字幕，请**尽量填**）：
{
  "title": "短片标题",
  "logline": "一句话概括",
  "style": "cinematic/documentary/anime/ad/mv",
  "scenes": [
    {
      "scene_number": 1,
      "title": "4-10 字的钩子标题",
      "hook": "这一场靠什么抓住人（一句话）",
      "location": "具体地点",
      "duration_seconds": 10,
      "characters": ["角色A", "角色B"],
      "actions": ["动作1", "动作2"],
      "dialogues": [{"character": "A", "text": "台词（短、口语）", "emotion": "calm",
                     "delivery": "可选：表演提示，如 喇叭喊话"}],
      "subtitle": "可选：这一场的字幕（点题或留悬念）",
      "mood": "tense/calm/excited/..."
    }
  ],
  "characters": [
    {
      "name": "角色A",
      "role": "protagonist/antagonist/supporting",
      "description": "面部/体型/年龄描述",
      "motivation": "他的前史动机（反派则写他的行为方式）",
      "costume": "服装描述"
    }
  ],
  "scenes_meta": [
    {"name": "场景A", "lighting": "自然光", "color_palette": ["#xxx", "#xxx"]}
  ]
}
"""

THREE_LAYER_PROMPT_SYSTEM = """你是 AI 视频提示词专家，同时兼任这部剧的**台词编剧与配音导演**。

把一个场次扩展成"三层提示词"，用于视频生成 API。

═══════════════════════════════════════════════════════════════
★ 台词是一等公民（这是最容易做错、也最影响成片质量的一环）
═══════════════════════════════════════════════════════════════
必须把**真正要说出口的话**逐字写进 `dialogue.text`。
绝对不要用"说出台词""喊道""低声说道"这类**描述代替台词本身** ——
下游要拿这段文本去合成语音、对齐字幕，写成描述就等于没有台词。

错误示范（严禁）：{"action": "李连长抬头看向小虎，语气坚定地说出台词"}
正确示范：{"action": "李连长抬头看向小虎", "dialogue": {"character": "李连长",
          "text": "小虎，天黑之前必须把阵地夺回来。", "emotion": "坚定"}}

规则：
1. 每个时间片最多一条 `dialogue`；没有说话的时间片 `dialogue` 为 `null`。
2. `dialogue.character` 必须用**场次里出现的确切人物名**（下游按名字匹配音色）。
3. 台词长度必须和该时间片的秒数匹配：**中文约每秒 4 个字**。
   例：3 秒的片子最多 12 个字。宁可短，不要长 —— 长了会被迫变速，听起来很假。
4. 台词要口语化、有性格、推进剧情；不要写成旁白说明。
5. 如果整场戏确实没有对白（纯动作/空镜），全部 `dialogue` 为 `null`，
   并在 `narration` 字段写**一句旁白**（同样要短，按 4 字/秒算）。
6. `speakers` 列出本场所有开口说话的角色名（没有就空数组）。

═══════════════════════════════════════════════════════════════
画面部分
═══════════════════════════════════════════════════════════════
第 1 层（整体概述）：一段话描述本段故事 + 摄影风格 + 镜头语言 + 情绪 + 时长
第 2 层（分时段时间线）：按秒拆分动作、表情、镜头、道具、**台词**
第 3 层（约束条件）：不要出现/不要发生/必须保持/必须出现/必须发生

`layer1_overview` 是给**视频模型**看的，要写成**具体的画面描述**（谁、在哪、做什么、
镜头怎么运动、什么光线），不要写成剧情梗概 —— 模型看不懂"展现了他的悔恨"，
但看得懂"他低头看着自己流血的手，镜头缓慢推近"。

输出严格 JSON（不要 markdown 代码块包裹）：
{
  "layer1_overview": "string",
  "speakers": ["角色名", ...],
  "narration": "（仅当全场无对白时写一句旁白，否则空字符串）",
  "layer2_timeline": [
    {"start": 0, "end": 5, "action": "...", "expression": "...", "camera": "...",
     "props_used": [...],
     "dialogue": {"character": "角色名", "text": "真正要说的话", "emotion": "坚定"}}
  ],
  "layer3_constraints": {
    "must_not_appear": [...],
    "must_not_happen": [...],
    "must_keep": [...],
    "must_appear": [...],
    "must_happen": [...]
  }
}
"""

SCENE_DETAIL_SYSTEM = """你是专业 AI 短片编剧，参照《AI 短片制作 Agent Skill 手册 v1.0》的【场次模板】补全单个场次的【剧本细节】。

你必须严格按以下 JSON 结构输出（不要额外解释、不要 markdown 代码块包裹）：

{
  "scene_number": <int>,
  "title": "<场次标题>",
  "location": "<具体地点>",
  "duration_seconds": <int>,
  "characters": ["<角色 A>", "<角色 B>", ...],
  "timeline": [
    {
      "start": <int, 秒>,
      "end": <int, 秒>,
      "action": "<人物在这个时段的具体动作，含位置/姿势/表情>",
      "expression": "<表情: calm / happy / angry / scared / sad / tense>",
      "camera": "<镜头: static / pan / tilt / zoom / dolly / tracking / close-up / wide>",
      "props_used": ["<道具名>"],
      "visual_focus": "<画面焦点: 人物 / 道具 / 环境 / 特写>"
    },
    ...
  ],
  "dialogues": [
    {
      "character": "<说话角色>",
      "text": "<台词原文>",
      "emotion": "<情绪>",
      "timing": "<大概发生的秒数>"
    },
    ...
  ],
  "mood": "<场次整体情绪基调: 紧张/平静/兴奋/悲伤/悬疑/温馨>",
  "lighting": "<光影: 自然光/霓虹灯/烛光/月光/路灯>",
  "key_props": ["<关键道具>"]
}

要求：
1. timeline 必须按 2-5 秒一段拆分覆盖整个 duration_seconds，每段必须有具体动作和位置
2. dialogues 如果原场次没有对白，数组可为空 []
3. 动作描写要明确【先做什么后做什么】，避免歧义（这是 Skill 手册中"能否直接改写成视频提示词"的验收标准）
4. timeline + dialogues 一起覆盖整段时间轴
5. characters 必须从原场次/项目角色库中出现，不要凭空创造
"""

ABSTRACTION_SYSTEM = """你是版权合规与创意改编专家。

把用户输入的参考（可能涉及电影/小说/IP）抽象化，生成原创设定：

1. 去掉具体角色名、品牌名、地名（用通用化名）
2. 去掉标志性颜色组合（如沙丘的橙+蓝眼）
3. 去掉标志性道具（如哈利波特的眼镜+魔杖）
4. 去掉标志性建筑/符号
5. 保留抽象元素：色调、空间尺度、光影质感、故事张力、情绪节奏
6. 生成全新的角色/场景/道具

输出严格 JSON：
{
  "title": "新标题",
  "logline": "新故事一句话",
  "removed_features": ["被去除的具体特征"],
  "preserved_features": ["保留的抽象特征"],
  "characters": [
    {
      "original": "原角色描述",
      "abstracted_name": "新名字",
      "description": "新的具体外形",
      "costume": "新的服装",
      "preserved_traits": ["保留下来的抽象特征"]
    }
  ],
  "scenes": [
    {
      "original": "原场景描述",
      "abstracted_name": "新名字",
      "description": "新场景描述",
      "preserved": ["保留的抽象元素"]
    }
  ],
  "props": [
    {"original": "...", "abstracted_name": "...", "description": "..."}
  ],
  "style_abstract": "整体抽象后的视觉风格"
}
"""


# ──────────── 高级业务方法 ────────────

class ScriptService:
    """剧本生成服务（基于 LLM）"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def generate_script(
        self,
        user_prompt: str,
        total_duration: int = 30,
        style: str = "cinematic",
        auto_split: bool = True,
        scene_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """生成完整剧本。

        ★ 2026-09-14：`scene_count` 由**上层**（`core.scriptplan`）给定，
          不再让这里"估一个"。
          为什么：用户实测「我选的 5 秒，生成剧本却给了 60 秒」——
          旧实现把"总时长 N 秒 / 各场秒数之和必须等于 N 秒"写进提示词就完了，
          **没有任何强制**，模型吐 5×12 秒我们就照收。现在场次数与每场秒数
          都由 `scriptplan` 决定后写进提示词，返回后再由 `fit_scenes` **强制**重排。
        """
        from core.scriptplan import scene_count_for, distribute
        if not scene_count or scene_count < 1:
            scene_count = scene_count_for(total_duration)
        num_scenes = int(scene_count)
        per_list = distribute(int(total_duration), num_scenes)
        per_scene = per_list[0] if per_list else int(total_duration)

        user_msg = f"""创意想法：{user_prompt}

总时长：{total_duration} 秒
风格：{style}
场次数量：**必须正好 {num_scenes} 场**
每场秒数：{ '、'.join(str(x) for x in per_list) }（各场之和 = {total_duration} 秒）
"""
        if auto_split:
            user_msg += (f"\n请自动拆分场次，每个场次标注秒数，"
                         f"各场秒数之和必须等于 {total_duration} 秒；"
                         f"场次数只能是 {num_scenes} 场，多写或少写都会被下游裁掉。")

        text = await self.llm.chat(
            messages=[
                {"role": "system", "content": SCRIPT_GENERATION_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.8,
            max_tokens=4000,
        )

        # 解析 JSON（多级容错）；失败则用强化提示重试一次
        try:
            result = extract_json(text)
            if isinstance(result, dict):
                return result
            raise ValueError(f"返回不是 JSON 对象：{type(result).__name__}")
        except Exception as first_err:
            logger.warning("剧本 JSON 解析失败，尝试重试：%s", first_err)
            retry_text = await self.llm.chat(
                messages=[
                    {"role": "system", "content": SCRIPT_GENERATION_SYSTEM},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": str(text)[:1500]},
                    {"role": "user", "content":
                        "上面的输出无法被程序解析。请【只输出一个合法的 JSON 对象】，"
                        "不要任何解释、不要 markdown 代码块标记、不要注释。"
                        "所有字符串用双引号，不要在最后一个元素后加逗号。"
                        "必须包含字段：title, logline, style, scenes（数组，每项含 "
                        "scene_number, title, location, duration_seconds, characters, actions, dialogues, mood）。"},
                ],
                temperature=0.4,
                max_tokens=4000,
            )
            try:
                result = extract_json(retry_text)
                if isinstance(result, dict):
                    logger.info("剧本 JSON 重试解析成功")
                    return result
                raise ValueError("重试仍非 JSON 对象")
            except Exception as second_err:
                logger.error("剧本 JSON 重试仍失败：%s", second_err)
                # 不再返回"解析失败"这种内部错误标题，改为可读的降级结果
                return {
                    "title": "未命名剧本（AI 输出格式异常）",
                    "logline": user_prompt[:80],
                    "style": style,
                    "scenes": [],
                    "characters": [],
                    "_parse_error": str(second_err),
                    "_raw": str(retry_text or text)[:4000],
                }

    async def generate_three_layer_prompt(
        self,
        scene_description: Dict[str, Any],
        characters: List[Dict[str, Any]] = None,
        scene_context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """为单个场次生成三层提示词"""
        dur = int(scene_description.get("duration_seconds") or 10)
        ctx_parts = [f"场次：{scene_description.get('title', '')}"]
        ctx_parts.append(f"地点：{scene_description.get('location', '')}")
        ctx_parts.append(f"时长：{dur} 秒")
        ctx_parts.append(f"动作：{scene_description.get('actions', [])}")
        ctx_parts.append(f"对白：{scene_description.get('dialogues', [])}")
        ctx_parts.append(f"情绪：{scene_description.get('mood', 'neutral')}")
        # ★ 明确告知"每个时间片能装多少字"，模型才不会写出 5 秒念不完的长台词
        ctx_parts.append(
            f"\n台词字数硬约束：本场共 {dur} 秒，按中文 4 字/秒 计算，"
            f"**全场台词总字数不要超过 {max(4, dur * 4)} 个字**。"
            f"单个时间片的台词不超过「该片秒数 × 4」个字。")

        if characters:
            ctx_parts.append(f"\n角色：")
            for c in characters:
                ctx_parts.append(f"- {c.get('name', '')}: {c.get('description', '')} ({c.get('costume', '')})")

        if scene_context:
            ctx_parts.append(f"\n场景：{scene_context.get('description', '')}")

        text = await self.llm.chat(
            messages=[
                {"role": "system", "content": THREE_LAYER_PROMPT_SYSTEM},
                {"role": "user", "content": "\n".join(ctx_parts)},
            ],
            temperature=0.6,
            max_tokens=3000,
        )

        try:
            data = extract_json(text)
            if not isinstance(data, dict):
                raise ValueError("not dict")
            data.setdefault("layer1_overview", data.get("layer1_overview") or data.get("overview", ""))
            data.setdefault("layer2_timeline", data.get("layer2_timeline") or data.get("timeline", []))
            data.setdefault("layer3_constraints", data.get("layer3_constraints") or {})
            # 台词规范化：把 LLM 各种可能的写法统一成 {character,text,emotion}，
            # 并把"用描述代替台词"的坏输出识别出来（下游要靠它配音+配字幕）。
            # ⚠️ 这几行**单独 try**：它们出错不应该把已经解析好的 JSON 一起丢掉
            #    （历史上就是这里 NameError，导致整份解析结果被"文本降级"覆盖）。
            try:
                data["layer2_timeline"] = normalize_timeline(
                    data.get("layer2_timeline"), duration=dur)
                data["speakers"] = data.get("speakers") or [
                    d["character"] for d in
                    (it.get("dialogue") for it in data["layer2_timeline"]
                     if isinstance(it, dict)) if d]
                data["dialogue_stats"] = timeline_dialogue_stats(
                    data["layer2_timeline"], duration=dur)
            except Exception as e:
                logger.warning("台词规范化失败（保留已解析内容）：%s", e)
            return data
        except Exception as e:
            # ★ 兜底**绝不能**把原始 JSON 文本塞进 layer1_overview。
            #   旧兜底干的就是这件事，结果提示词变成一段 ```json{...}```，
            #   视频模型完全看不懂 —— 这就是"画面与描述不相干"的根因之一。
            #   现在：先尝试从文本里捞出可读的叙述，实在不行宁可留空并明确报错。
            logger.warning("三层提示词解析失败：%s", e)
            prose = _salvage_prose(text)
            return {
                "layer1_overview": prose,
                "layer2_timeline": [],
                "layer3_constraints": {},
                "_parse_failed": True,
                "_parse_error": f"{type(e).__name__}: {e}",
                "_raw_len": len(text or ""),
            }


def _salvage_prose(text: str, limit: int = 400) -> str:
    """解析失败时，从原始输出里捞出**人能读的一段话**当 layer1。

    绝不要把 JSON 原文塞进 layer1 —— 那是给视频模型看的提示词。
    """
    s = str(text or "")
    # 去掉 markdown 围栏
    s = re.sub(r"^\s*```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"```\s*$", "", s).strip()
    # 优先从 JSON 里抓 layer1_overview 的值
    m = re.search(r'"layer1_overview"\s*:\s*"((?:[^"\\]|\\.)*)"', s, re.S)
    if m:
        try:
            return json.loads('"' + m.group(1) + '"')[:limit]
        except Exception:
            return m.group(1)[:limit]
    # 退而求其次：如果整段看着还是 JSON，就别硬塞
    head = s.lstrip()[:1]
    if head in ("{", "["):
        return ""
    return s[:limit]

    async def generate_scene_detail(
        self,
        scene: Dict[str, Any],
        characters: List[Dict[str, Any]] = None,
        scene_context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        按 Skill 手册 v1.0 的【场次模板】为单个场次生成【剧本细节】。
        返回字段：scene_number/title/location/duration_seconds/characters/timeline(按秒)/
                  dialogues/mood/lighting/key_props。
        """
        ctx_parts = [
            "【场次基本信息】",
            f"场次标题：{scene.get('title', '')}",
            f"场次编号：{scene.get('scene_number', 1)}",
            f"时长：{scene.get('duration_seconds', 10)} 秒",
            f"地点：{scene.get('location', '')}",
            f"现有动作描述：{scene.get('actions', [])}",
            f"现有对白：{scene.get('dialogues', [])}",
            f"情绪基调：{scene.get('mood', 'neutral')}",
        ]
        if characters:
            ctx_parts.append("\n【角色】")
            for c in characters:
                ctx_parts.append(
                    f"- {c.get('name', '')}: {c.get('description', '')} "
                    f"(服装: {c.get('costume_main', '')})"
                )
        if scene_context:
            ctx_parts.append(f"\n【场景信息】{scene_context.get('description', '')}")

        text = await self.llm.chat(
            messages=[
                {"role": "system", "content": SCENE_DETAIL_SYSTEM},
                {"role": "user", "content": "\n".join(ctx_parts)},
            ],
            temperature=0.7,
            max_tokens=4000,
        )

        try:
            data = extract_json(text)
            if not isinstance(data, dict):
                raise ValueError("not dict")
            data.setdefault("scene_number", scene.get("scene_number", 1))
            data.setdefault("title", scene.get("title", ""))
            data.setdefault("location", scene.get("location", ""))
            data.setdefault("duration_seconds", scene.get("duration_seconds", 10))
            data.setdefault("characters", [])
            data.setdefault("timeline", [])
            data.setdefault("dialogues", [])
            data.setdefault("mood", scene.get("mood", "neutral"))
            data.setdefault("lighting", "")
            data.setdefault("key_props", [])
            return data
        except Exception as e:
            logger.warning("场次细节解析失败：%s", e)
            return {
                "scene_number": scene.get("scene_number", 1),
                "title": scene.get("title", ""),
                "location": scene.get("location", ""),
                "duration_seconds": scene.get("duration_seconds", 10),
                "characters": [],
                "timeline": [],
                "dialogues": [],
                "mood": scene.get("mood", "neutral"),
                "lighting": "",
                "key_props": [],
                "raw_text": text,
                "parse_error": True,
                "parse_error_detail": str(e),
            }


class AbstractionService:
    """版权抽象化服务"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def abstract(self, source_description: str) -> Dict[str, Any]:
        """把可能涉及版权的描述抽象化"""
        text = await self.llm.chat(
            messages=[
                {"role": "system", "content": ABSTRACTION_SYSTEM},
                {"role": "user", "content": f"原始描述：\n{source_description}"},
            ],
            temperature=0.7,
            max_tokens=4000,
        )

        try:
            data = extract_json(text)
            if isinstance(data, dict):
                return data
            raise ValueError("not dict")
        except Exception as e:
            logger.warning("抽象化结果解析失败：%s", e)
            return {
                "_parse_error": True,
                "_raw": text,
                "title": "版权抽象化（AI 输出格式异常）",
                "removed_features": [],
                "preserved_features": [],
                "characters": [],
                "scenes": [],
            }
