"""
VideoForge · @引用 → 各平台「方言」转换层

同一个用户写法 `@沈落`,在各家视频模型 API 里的表达方式完全不同：

| 平台 | 官方机制 | 我们的转换结果 |
|---|---|---|
| 可灵 Kling 3.0 | `@xxx` 引用 Element | 注册 Element 后 → `@沈落`（名字直传） |
| 万相 Wan2.7 r2v | prompt 里写 `图1/图2` | `@沈落` → `图1`,并出 `media[]` 数组 |
| 火山 Seedance | `@图片1`…`@图片9` | `@沈落` → `@图片1` |
| Runway gen4_image | `@TagName` | `@沈落` → `@沈落`（tag 直传） |
| 其他/不支持 | 无原生引用 | 退化为「提示词特征注入 + 参考图数组」 |

依据（官方文档核实）：
- 可灵:https://kling.ai/document-api/api/video/3-0-omni/elements.md
  （`reference_type=image_refer`，`frontal_image` + 1~3 张 `refer_images`，单次最多引用 3 个 Element）
- 万相:https://www.alibabacloud.com/help/zh/model-studio/wan-video-to-video-api-reference
  （`input.media[]`，元素 `type=reference_image/reference_video/first_frame`，参考 ≤5）
- 即梦/Seedance:官方命名 `@图片1`…`@图片9`，**禁用** `@img1` 这类英文
- Runway:`referenceImages[].tag` + prompt 里 `@TagName`（仅图像接口）

⚠️ 上游警告:「文档记载的能力 ≠ 每次生成都会遵守」。本模块只负责**正确组装请求**，
不保证模型一定遵守引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .mentions import MentionResult, ResolvedRef

# ──────────────────────── 平台能力表 ────────────────────────

# ref_style 决定 prompt 里怎么写引用；max_elems 是该平台单次能引用的上限
PLATFORM_DIALECTS: Dict[str, Dict[str, Any]] = {
    # 官方原生 @名字（可灵 Element）
    "kling": {
        "ref_style": "at_name",
        "native": True,
        "max_elems": 3,          # 官方：一次最多引用 3 个 Element
        "needs_registration": True,   # 需先调 /advanced-custom-elements 注册
        "supports_last_frame": True,
    },
    # 官方原生 @图片N（火山 Seedance / 即梦）
    "seedance": {
        "ref_style": "at_index",
        "native": True,
        "max_elems": 9,          # 2.0 上限 9；2.5 到 30
        "needs_registration": False,
        "supports_last_frame": True,
    },
    "jimeng": {
        "ref_style": "at_index",
        "native": True,
        "max_elems": 9,
        "needs_registration": False,
        "supports_last_frame": True,
    },
    # 万相 r2v：prompt 里写「图1/图2」，英文 Image 1 / Video 1
    "wanx": {
        "ref_style": "plain_index",
        "native": True,
        "max_elems": 5,          # 官方：参考图+参考视频 ≤ 5
        "needs_registration": False,
        "supports_last_frame": True,   # 但 kf2v 接口 duration 锁死 5s
    },
    # Runway：图像接口支持 @Tag；视频接口的 keyframe 与 reference 互斥
    "runway": {
        "ref_style": "at_name",
        "native": True,
        "max_elems": 3,
        "needs_registration": False,
        "supports_last_frame": True,   # 仅部分模型（gen4.5 不支持）
        "last_frame_models": ["veo3.1", "hailuo3", "seedance2", "wan3", "h3_max"],
        "mutually_exclusive": ["keyframe", "reference"],
    },
    # 海螺：主体参考 subject_reference
    "hailuo": {
        "ref_style": "subject_ref",
        "native": True,
        "max_elems": 3,
        "needs_registration": False,
        "supports_last_frame": True,   # fl2v 接口 last_frame 必填
    },
    "minimax": {
        "ref_style": "subject_ref",
        "native": True,
        "max_elems": 3,
        "needs_registration": False,
        "supports_last_frame": True,
    },
}

# 没登记的平台 → 退化：只注入特征描述 + 参考图数组
_FALLBACK = {
    "ref_style": "feature_only",
    "native": False,
    "max_elems": 0,
    "needs_registration": False,
    "supports_last_frame": False,
}


def dialect_of(provider: str) -> Dict[str, Any]:
    """取平台的引用方言配置（未知平台返回退化配置）。"""
    key = (provider or "").strip().lower()
    # 常见别名归一
    alias = {
        "klingai": "kling", "kling_ai": "kling",
        "doubao": "seedance", "volcengine": "seedance", "ark": "seedance",
        "wan": "wanx", "tongyi": "wanx", "dashscope": "wanx", "bailian": "wanx",
        "minimax_hailuo": "hailuo", "hailuoai": "hailuo",
        "runwayml": "runway", "gen4": "runway",
    }
    key = alias.get(key, key)
    return PLATFORM_DIALECTS.get(key, _FALLBACK)


# ──────────────────────── 结果结构 ────────────────────────


@dataclass
class PlatformRefs:
    """转换后的平台请求片段。"""

    provider: str
    dialect: str                                  # at_name / at_index / plain_index / subject_ref / feature_only
    prompt: str = ""                              # 重写后的提示词（引用已换成本平台写法）
    reference_images: List[str] = field(default_factory=list)   # 参考图路径（按引用顺序）
    reference_names: List[str] = field(default_factory=list)    # 对应的资产名（顺序与上图一致）
    elements: List[Dict[str, Any]] = field(default_factory=list)  # 可灵 Element 注册用
    feature_block: str = ""                       # 特征描述块（拼在 prompt 末尾）
    warnings: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)   # 因超上限被丢弃的引用

    @property
    def has_refs(self) -> bool:
        return bool(self.reference_images)

    def full_prompt(self) -> str:
        """最终送给模型的提示词 = 重写后的正文 + 特征块。"""
        parts = [self.prompt.strip()]
        if self.feature_block.strip():
            parts.append(self.feature_block.strip())
        return "\n\n".join(p for p in parts if p)


# ──────────────────────── 转换 ────────────────────────


def to_platform_refs(
    result: MentionResult,
    provider: str,
    *,
    include_kinds: Sequence[str] = ("character", "scene", "prop"),
) -> PlatformRefs:
    """把解析好的 @引用 转成指定平台要的写法。

    Args:
        result:   mentions.MentionResult
        provider: 模型提供方（kling / seedance / wanx / runway / hailuo …）
        include_kinds: 哪些种类的引用要带参考图（默认角色+场景+道具）
    """
    d = dialect_of(provider)
    style = d["ref_style"]
    max_elems = int(d.get("max_elems") or 0)

    out = PlatformRefs(provider=provider, dialect=style)

    # 只保留要带图的引用，且按出现顺序
    usable: List[ResolvedRef] = [r for r in result.refs if r.kind in include_kinds]

    # 超上限时按顺序截断（保前面的，因为官方建议"优先影响最大的素材"）
    if max_elems and len(usable) > max_elems:
        kept = usable[:max_elems]
        dropped = usable[max_elems:]
        out.dropped = [f"{r.raw}({r.name})" for r in dropped]
        out.warnings.append(
            f"{provider} 单次最多引用 {max_elems} 个素材，已丢弃："
            + "、".join(out.dropped)
        )
        usable = kept

    # 收集参考图
    for r in usable:
        if r.reference_image:
            out.reference_images.append(r.reference_image)
            out.reference_names.append(r.name)
        else:
            out.warnings.append(f"「{r.name}」还没有参考图，只能靠文字描述保证一致性")

    # 组装提示词
    prompt = result.cleaned_text or ""

    if style == "at_name":
        # 可灵 / Runway：直接 @资产名
        for r in usable:
            if r.reference_image:
                prompt = prompt.replace(r.name, f"@{r.name}", 1)
        if d.get("needs_registration") and usable:
            # 可灵需要先注册 Element
            for r in usable:
                if not r.reference_image:
                    continue
                out.elements.append({
                    "name": r.name,
                    "reference_type": "image_refer",
                    "frontal_image": r.reference_image,
                    "refer_images": [],
                    "description": r.feature_text,
                })

    elif style == "at_index":
        # 火山 Seedance / 即梦：@图片1、@图片2 …（官方命名，禁用 @img1）
        idx = 0
        for r in usable:
            if not r.reference_image:
                continue
            idx += 1
            prompt = prompt.replace(r.name, f"@图片{idx}", 1)

    elif style == "plain_index":
        # 万相 r2v：图1、图2（英文场景用 Image 1）
        idx = 0
        for r in usable:
            if not r.reference_image:
                continue
            idx += 1
            prompt = prompt.replace(r.name, f"图{idx}", 1)

    elif style == "subject_ref":
        # 海螺：subject_reference 数组，提示词里保留资产名
        # （真正的 subject_reference 由适配器用 reference_images 组装）
        pass

    else:
        # feature_only：平台不支持引用图，只能靠文字
        if usable:
            out.warnings.append(
                f"{provider} 不支持参考图引用，一致性只能靠提示词描述"
            )

    out.prompt = prompt
    out.feature_block = result.feature_block()
    return out


def check_provider_support(
    provider: str,
    *,
    need_last_frame: bool = False,
    element_count: int = 0,
    model_name: str = "",
) -> List[str]:
    """调用前的能力校验，返回警告列表（空 = 没问题）。"""
    d = dialect_of(provider)
    warns: List[str] = []

    if need_last_frame:
        if not d.get("supports_last_frame"):
            warns.append(f"{provider} 不支持尾帧，尾帧链式续接无法使用，将退化为首帧+提示词")
        else:
            allow = d.get("last_frame_models")
            if allow and model_name and model_name not in allow:
                warns.append(
                    f"{provider} 的 {model_name} 不支持尾帧（仅 {', '.join(allow)} 支持）"
                )

    max_elems = int(d.get("max_elems") or 0)
    if max_elems and element_count > max_elems:
        warns.append(f"{provider} 单次最多引用 {max_elems} 个素材，当前 {element_count} 个")

    if d.get("mutually_exclusive") and need_last_frame and element_count:
        warns.append(
            f"{provider} 的 {' 与 '.join(d['mutually_exclusive'])} 两种模式不能混用"
        )

    return warns
