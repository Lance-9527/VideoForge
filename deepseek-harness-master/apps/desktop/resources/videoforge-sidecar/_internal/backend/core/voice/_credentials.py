# -*- coding: utf-8 -*-
"""VideoForge · 语音适配器的「多件套凭据 / 端点」解析小工具

═══════════════════════════════════════════════════════════════════════
为什么需要它（2026-10-25）
═══════════════════════════════════════════════════════════════════════
新接的这批 TTS 平台里，有三家**一把 Key 不够**：

  · Azure 语音   ：订阅 Key **+ region**（`{region}.tts.speech.microsoft.com`）
  · 火山（豆包）  ：appid **+** token **+** cluster 三件套
  · 讯飞星火     ：app_id **+** api_key **+** api_secret（还要自己算签名）

而设置页的 `tts_api_keys` 只有一个字符串位。硬要求用户「再开三个输入框」
不现实，所以这里定了一条**向后兼容**的规则，几种写法都认：

  ① 组合串（直接填在 `tts_api_keys.<provider>` 那一个框里）
        volc   →  "appid=123456;token=abcdef;cluster=volcano_tts"
        xfyun  →  "app_id=abc;api_key=def;api_secret=ghi"      （; 或 , 或换行分隔）
        azure  →  "region=eastasia;key=xxxx"                     （key= 也可省）
  ② JSON 串（同一个框）
        {"appid":"123","token":"abc","cluster":"volcano_tts"}
  ③ 设置顶层平铺（`settings["volc_appid"]` 之类）
  ④ `settings["tts_extra"]["<provider>"][...]`（前端以后要加结构化输入框就走这里）
  ⑤ 环境变量（CI/脚本场景）

**缺哪一项就报哪一项的名字**（不许只回一句"参数错误"）—— 这是本模块的存在意义。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _ci_get(mapping: Any, name: str) -> str:
    """大小写不敏感取 dict 值（设置库里 `MiniMax` / `minimax` 两种写法都有）。"""
    if not isinstance(mapping, dict):
        return ""
    target = str(name or "").strip().lower()
    for k, v in mapping.items():
        if str(k).strip().lower() == target and v not in (None, ""):
            return str(v).strip()
    return ""


def parse_credential_blob(raw: Any) -> Dict[str, str]:
    """把「一个字符串里塞多件套」的写法解析成 dict。

    支持：JSON 对象 / `a=1;b=2` / `a:1,b:2` / 换行分隔。
    解析不出来就返回空 dict（调用方继续找别的来源，不报错 —— 可能就是普通 Key）。
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k).strip().lower(): str(v).strip()
                        for k, v in obj.items() if v not in (None, "")}
        except Exception:  # noqa: BLE001 - 不是 JSON 就按分隔符试
            pass
    out: Dict[str, str] = {}
    for chunk in text.replace("\r", "\n").replace(";", "\n").replace(",", "\n").split("\n"):
        part = chunk.strip()
        if not part:
            continue
        for sep in ("=", ":"):
            if sep in part:
                k, v = part.split(sep, 1)
                k, v = k.strip().lower(), v.strip()
                if k and v and " " not in k:
                    out[k] = v
                break
    return out


def settings_snapshot(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """适配器拿到的 config 里取 settings（dispatcher 会把 settings 一并传下来）。"""
    conf = config or {}
    snap = conf.get("settings")
    return dict(snap) if isinstance(snap, dict) else {}


def resolve_value(provider_id: str,
                  part: Dict[str, Any],
                  *,
                  config: Optional[Dict[str, Any]] = None,
                  primary: str = "") -> Tuple[str, str]:
    """解析单件凭据，返回 `(值, 来源)`；找不到返回 `("", "")`。

    `part` 形状：
      {"key": "region", "label": "区域 region",
       "settings": ["azure_region"],        # 设置顶层的候选键名
       "env": ["AZURE_SPEECH_REGION"],      # 环境变量候选
       "default": "volcano_tts"}            # 可选默认值
    查找顺序：组合串 → tts_extra → 设置顶层 → 环境变量 → 默认值。
    """
    conf = config or {}
    settings = settings_snapshot(conf)
    pkey = str(part.get("key") or "").strip().lower()
    if not pkey:
        return "", ""

    # ① 组合串（primary 就是 tts_api_keys.<provider> 那个字符串）
    blob = parse_credential_blob(primary)
    if blob:
        for alias in [pkey] + [str(x) for x in (part.get("aliases") or [])]:
            if blob.get(str(alias).lower()):
                return blob[str(alias).lower()], f"{provider_id} 组合串.{alias}"

    # ② tts_extra[provider][key]
    extra = settings.get("tts_extra")
    if isinstance(extra, dict):
        per = extra.get(provider_id) or extra.get(provider_id.lower())
        if isinstance(per, dict):
            for alias in [pkey] + [str(x) for x in (part.get("aliases") or [])]:
                val = _ci_get(per, alias)
                if val:
                    return val, f"tts_extra.{provider_id}.{alias}"

    # ③ 设置顶层平铺
    candidates: List[str] = [str(x) for x in (part.get("settings") or [])]
    candidates += [f"{provider_id}_{pkey}"]
    for name in candidates:
        val = str(settings.get(name) or "").strip()
        if val:
            return val, f"settings.{name}"
        # 兼容 tts_extra 之外的嵌套：settings["tts_config"]["volc"]["appid"]
        for nest in ("tts_config", "tts_provider_config"):
            nested = settings.get(nest)
            if isinstance(nested, dict):
                per = nested.get(provider_id) or nested.get(provider_id.lower())
                val2 = _ci_get(per, pkey)
                if val2:
                    return val2, f"settings.{nest}.{provider_id}.{pkey}"

    # ④ 环境变量
    for env_name in (part.get("env") or []):
        if os.environ.get(str(env_name)):
            return str(os.environ[str(env_name)]).strip(), f"env.{env_name}"

    # ⑤ 显式默认值（例如火山的 cluster 有官方默认）
    default = str(part.get("default") or "").strip()
    if default:
        return default, "default"
    return "", ""


def resolve_parts(provider_id: str,
                  parts: Sequence[Dict[str, Any]],
                  *,
                  config: Optional[Dict[str, Any]] = None,
                  primary: str = "") -> Tuple[Dict[str, str], List[str], Dict[str, str]]:
    """批量解析多件套。

    :returns: (值字典, 缺失项的中文名列表, 来源字典)
    """
    values: Dict[str, str] = {}
    missing: List[str] = []
    sources: Dict[str, str] = {}
    for part in parts:
        val, src = resolve_value(provider_id, part, config=config, primary=primary)
        key = str(part.get("key") or "")
        if val:
            values[key] = val
            sources[key] = src
        else:
            missing.append(str(part.get("label") or key))
    return values, missing, sources


def credential_gaps(provider_id: str,
                    parts: Sequence[Dict[str, Any]],
                    *,
                    config: Optional[Dict[str, Any]] = None,
                    primary: str = "") -> List[str]:
    """只问「还缺哪些」（给 dispatcher.list_providers 标 usable 用）。"""
    _, missing, _ = resolve_parts(provider_id, parts, config=config, primary=primary)
    return missing


def resolve_base_url(provider_id: str,
                     default: str,
                     *,
                     config: Optional[Dict[str, Any]] = None,
                     env: Iterable[str] = ()) -> str:
    """端点解析：config.base_url → settings.tts_base_url_<provider> →
    settings.tts_base_url → 环境变量 → 平台默认。

    ★ 为什么允许覆盖：自建网关/中转站/本地假服务都要能替换根地址，
      否则「能不能跑通」只能靠联网验，测试和离线环境都做不了。
    """
    conf = config or {}
    settings = settings_snapshot(conf)
    # ① 适配器构造参数里显式给的（测试/自建网关最常用这条）
    explicit = str(conf.get("base_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    for name in (f"tts_base_url_{provider_id}", f"{provider_id}_base_url",
                 "tts_base_url"):
        val = str(settings.get(name) or "").strip()
        if val:
            return val.rstrip("/")
    for env_name in env:
        if os.environ.get(str(env_name)):
            return str(os.environ[str(env_name)]).strip().rstrip("/")
    return default.rstrip("/")
