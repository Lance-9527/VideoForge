"""
VideoForge · @ 引用解析器

用户在对话/提示词里用 @ 引用项目资产,例如：

    @沈落 走进 @灰巷，手里提着 @木牌

解析结果用于三处：
  1. 生成时把资产的「参考图」注入适配器（first_frame / reference_images）
  2. 把资产的「固定特征描述」拼进提示词 —— 防止模型忽略参考图
  3. 未命中时给用户明确报错（而不是静默忽略）

匹配优先级：显式前缀 > 精确名 > 别名 > 包含匹配
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ──────────────────────── 常量 ────────────────────────

# 支持的显式前缀：@角色:沈落 / @场景:灰巷 / @道具:木牌 / @配音:老周
KIND_PREFIX = {
    "角色": "character",
    "人物": "character",
    "场景": "scene",
    "地点": "scene",
    "道具": "prop",
    "物品": "prop",
    "配音": "voice",
    "音色": "voice",
}

# 不参与匹配的常见误触（如邮箱、代码）
_EMAIL_LIKE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")

# 引用本身：@ 后跟 1~32 个非空白字符（中文/字母/数字/下划线，允许「:」做种类前缀）
_MENTION_RE = re.compile(r"@([\u4e00-\u9fffA-Za-z0-9_:：\-]{1,40})")

# 名称里不该出现的字符（截断用）
_STOP_CHARS = set("，。！？、；：（）()【】[]{}<>《》\"'“”‘’,.;:!?/\\|+=\n\r\t ")


# ──────────────────────── 数据结构 ────────────────────────


@dataclass
class ResolvedRef:
    """一条成功解析的引用。"""

    raw: str                      # 原始写法，如 "@角色:沈落"
    kind: str                     # character | scene | prop | voice
    asset_id: str                 # 资产 id
    name: str                     # 资产名
    matched_by: str               # exact | alias | contains | prefix
    reference_image: Optional[str] = None   # 参考图/定妆照路径
    feature_text: str = ""        # 固定特征描述，用于拼提示词


@dataclass
class MentionResult:
    """一次解析的完整结果。"""

    refs: List[ResolvedRef] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)   # 没匹配上的原始写法
    cleaned_text: str = ""        # 去掉 @ 记号后的干净文本（给模型看）

    @property
    def ok(self) -> bool:
        return not self.unresolved

    def by_kind(self, kind: str) -> List[ResolvedRef]:
        return [r for r in self.refs if r.kind == kind]

    def reference_images(self, kind: str = "character") -> List[str]:
        """取指定种类的参考图路径（按出现顺序去重）。"""
        out: List[str] = []
        for r in self.by_kind(kind):
            if r.reference_image and r.reference_image not in out:
                out.append(r.reference_image)
        return out

    def feature_block(self) -> str:
        """把所有命中资产的特征拼成一段固定描述，注入提示词用。"""
        lines = [r.feature_text for r in self.refs if r.feature_text]
        if not lines:
            return ""
        return "【必须严格保持以下设定】\n" + "\n".join(lines)


# ──────────────────────── 特征提取 ────────────────────────


def _clean(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "、".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip()


def character_feature_text(ch: Dict[str, Any]) -> str:
    """把角色资产拼成一句固定的外观描述（喂给视频模型，强化一致性）。"""
    parts: List[str] = []
    name = _clean(ch.get("name"))
    for key in ("age", "body_type", "description"):
        v = _clean(ch.get(key))
        if v:
            parts.append(v)
    costume = _clean(ch.get("costume_main"))
    if costume:
        parts.append(f"服装：{costume}")
    alt = _clean(ch.get("costume_alternate"))
    if alt:
        parts.append(f"备选服装：{alt}")
    props = ch.get("props")
    if props:
        p = _clean(props)
        if p:
            parts.append(f"随身道具：{p}")
    feat = ch.get("reference_features")
    if isinstance(feat, dict):
        extra = [_clean(v) for v in feat.values() if _clean(v)]
        if extra:
            parts.append("；".join(extra))
    if not parts:
        return f"角色「{name}」：保持外观一致"
    return f"角色「{name}」：" + "，".join(parts)


def scene_feature_text(sc: Dict[str, Any]) -> str:
    """场景资产 → 固定描述。"""
    parts: List[str] = []
    for key in ("description", "time_of_day", "weather", "atmosphere", "style"):
        v = _clean(sc.get(key))
        if v:
            parts.append(v)
    name = _clean(sc.get("name"))
    if not parts:
        return f"场景「{name}」：保持环境一致"
    return f"场景「{name}」：" + "，".join(parts)


def prop_feature_text(pp: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("description", "material", "color", "size"):
        v = _clean(pp.get(key))
        if v:
            parts.append(v)
    name = _clean(pp.get("name"))
    if not parts:
        return f"道具「{name}」：保持外观一致"
    return f"道具「{name}」：" + "，".join(parts)


# ──────────────────────── 解析器 ────────────────────────


class MentionResolver:
    """按项目资产库解析 @ 引用。

    用法：
        r = MentionResolver.from_db(db, project_id)
        res = r.resolve("@沈落 走进 @场景:灰巷")
        if not res.ok:
            print("找不到:", res.unresolved)
    """

    def __init__(
        self,
        characters: Sequence[Dict[str, Any]] = (),
        scenes: Sequence[Dict[str, Any]] = (),
        props: Sequence[Dict[str, Any]] = (),
    ) -> None:
        self._index: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {
            "character": [(c.get("name", ""), c) for c in characters],
            "scene": [(s.get("name", ""), s) for s in scenes],
            "prop": [(p.get("name", ""), p) for p in props],
        }

    # ---------- 构造 ----------

    @classmethod
    def from_db(cls, db: Any, project_id: str) -> "MentionResolver":
        def safe(fn, *a, **kw):
            try:
                return fn(*a, **kw) or []
            except Exception:
                return []

        return cls(
            characters=safe(db.list_characters, project_id),
            scenes=safe(db.list_scenes, project_id),
            props=safe(db.list_props, project_id),
        )

    # ---------- 文本预处理 ----------

    @staticmethod
    def _strip_noise(text: str) -> str:
        """去掉邮箱这类会误触 @ 的东西。"""
        return _EMAIL_LIKE.sub(lambda m: m.group(0).replace("@", "＠"), text or "")

    @staticmethod
    def _scan(text: str) -> List[Tuple[str, str, Optional[str]]]:
        """扫出所有引用。

        返回 [(raw, token, kind_or_None)]：
            raw   = "@角色:沈落"
            token = "沈落"（去掉种类前缀后的名字）
            kind  = "character" | None
        """
        found: List[Tuple[str, str, Optional[str]]] = []
        for m in _MENTION_RE.finditer(text):
            raw = m.group(0)
            body = m.group(1)
            kind: Optional[str] = None
            token = body
            # 种类前缀：@角色:沈落 / @场景：灰巷
            for sep in (":", "："):
                if sep in body:
                    head, _, tail = body.partition(sep)
                    if head in KIND_PREFIX:
                        kind = KIND_PREFIX[head]
                        token = tail
                    break
            # 截断到第一个停止符（@沈落，走进 → token="沈落"）
            cut = len(token)
            for i, ch in enumerate(token):
                if ch in _STOP_CHARS:
                    cut = i
                    break
            token = token[:cut].strip()
            if token:
                found.append((raw, token, kind))
        return found

    # ---------- 匹配 ----------

    def _lookup(self, token: str, kind: Optional[str]) -> Optional[Tuple[str, Dict[str, Any], str]]:
        """在资产库里找 token。返回 (kind, asset, matched_by)。"""
        kinds = [kind] if kind else ["character", "scene", "prop"]
        token_l = token.casefold()

        # 1) 精确
        for k in kinds:
            for name, asset in self._index.get(k, []):
                if name and name.casefold() == token_l:
                    return k, asset, "exact"

        # 2) 别名（资产里可能有 aliases 字段）
        for k in kinds:
            for name, asset in self._index.get(k, []):
                aliases = asset.get("aliases") or []
                if isinstance(aliases, str):
                    aliases = [a.strip() for a in aliases.split(",") if a.strip()]
                for a in aliases:
                    if _clean(a).casefold() == token_l:
                        return k, asset, "alias"

        # 3) 包含匹配（token 是名字的一部分，或名字包含 token）—— 仅在唯一时命中
        hits: List[Tuple[str, Dict[str, Any], str]] = []
        for k in kinds:
            for name, asset in self._index.get(k, []):
                nl = (name or "").casefold()
                if not nl:
                    continue
                if token_l in nl or nl in token_l:
                    hits.append((k, asset, "contains"))
        if len(hits) == 1:
            return hits[0]
        return None

    # ---------- 主入口 ----------

    def resolve(self, text: str) -> MentionResult:
        raw_text = text or ""
        result = MentionResult()

        for raw, token, kind in self._scan(self._strip_noise(raw_text)):
            hit = self._lookup(token, kind)
            if not hit:
                if raw not in result.unresolved:
                    result.unresolved.append(raw)
                continue
            k, asset, how = hit
            if k == "character":
                feature = character_feature_text(asset)
                refimg = asset.get("reference_image_path")
            elif k == "scene":
                feature = scene_feature_text(asset)
                refimg = asset.get("reference_image_path")
            else:
                feature = prop_feature_text(asset)
                refimg = asset.get("reference_image_path")

            result.refs.append(ResolvedRef(
                raw=raw,
                kind=k,
                asset_id=asset.get("id", ""),
                name=asset.get("name", ""),
                matched_by=how,
                reference_image=refimg or None,
                feature_text=feature,
            ))

        # 干净文本：把 @记号 换成资产名（或去掉 @）
        cleaned = raw_text
        for r in result.refs:
            cleaned = cleaned.replace(r.raw, r.name, 1)
        for u in result.unresolved:
            cleaned = cleaned.replace(u, u.lstrip("@"), 1)
        result.cleaned_text = cleaned
        return result

    def known_names(self, kind: Optional[str] = None) -> List[str]:
        """列出已知资产名，用于错误提示。"""
        kinds = [kind] if kind else ["character", "scene", "prop"]
        out: List[str] = []
        for k in kinds:
            for name, _ in self._index.get(k, []):
                if name and name not in out:
                    out.append(name)
        return out


# ──────────────────────── 便捷函数 ────────────────────────


def resolve_mentions(text: str, db: Any, project_id: str) -> MentionResult:
    """一行调用：按项目解析 @ 引用。"""
    return MentionResolver.from_db(db, project_id).resolve(text)


def unresolved_message(res: MentionResult, db: Any, project_id: str) -> str:
    """未命中时给用户的友好报错。"""
    names = MentionResolver.from_db(db, project_id).known_names()
    have = "、".join(names) if names else "（项目里还没有资产，先用对话建角色/场景）"
    bad = "、".join(res.unresolved)
    return (
        f"找不到这些引用：{bad}\n"
        f"当前项目可用的资产有：{have}\n"
        f"用法示例：@沈落 走进 @场景:灰巷"
    )
