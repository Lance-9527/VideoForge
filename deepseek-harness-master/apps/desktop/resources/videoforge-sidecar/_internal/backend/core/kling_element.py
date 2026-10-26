"""
VideoForge · 可灵 Element（角色一致性）注册

════════════════════════════════════════════════════════════════════════
为什么需要这个模块
════════════════════════════════════════════════════════════════════════
可灵的"角色一致性"**不是发参考图**，而是一套独立流程：

    ① 上传这个角色的参考图 → `POST /v1/general/advanced-custom-elements`
    ② 这是个**异步任务**：先拿到 task_id，再轮询查询才拿到系统生成的 `element_id`
    ③ 生成视频时：
         · prompt 里写 `@角色名`（官方原文："Specify an element in the format of @xxx"）
         · `contents[]` 里带 `{"type":"element","element_id":"163","id":"element_1"}`
         · 一次调用最多引用 **3 个** Element

**图片本身不能塞进 `element_id`** —— 平台一定报参数错误。
所以在补上这个模块之前，可灵这条线的 `supports_character_ref=True` 是**空头支票**：
适配器收到的是图片 data URI，发不出去，只能明确报错。

来源：
  · Element 注册 https://kling.ai/document-api/api/video/3-0-omni/elements.md
  · 生成请求体 https://kling.ai/document-api/api/video/3-0-omni/image-to-video.md
  · 能力矩阵（≤3 个 Element）https://kling.ai/document-api/guides/capability-map/video.md

════════════════════════════════════════════════════════════════════════
诚实声明
════════════════════════════════════════════════════════════════════════
**本模块没有用真实可灵账号跑通过** —— 这台机器上没有配可灵 API key
（`settings.api_keys` 里只有 seedance 和 hailuo）。
请求结构、端点、字段名均来自上面那三篇官方文档；轮询形态照抄了
`adapters/seedance.py`（项目既定模式）。第一次真用之前请先拿一个角色试。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.kling_element")

BASE_URL = "https://api-singapore.klingai.com"

# 官方：注册 Element 时参考图张数有上限；一次生成最多引用 3 个 Element。
MAX_REFS_PER_ELEMENT = 4
MAX_ELEMENTS_PER_CALL = 3

# Element 注册是异步任务，官方没给明确的轮询间隔，这里跟项目其它适配器一致。
POLL_INTERVAL = 5.0
POLL_MAX = 60          # ≈5 分钟


def _headers(api_key: str) -> Dict[str, str]:
    # 官方认证：只用 Authorization: Bearer <API Key>。
    # （AK/SK 签 JWT 属于"旧版设计标准"，不再采用。）
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "VideoForge/1.0",
    }


class ElementError(RuntimeError):
    """注册失败。带平台原文，别把它吞成"未知错误"。"""


async def register_element(
    api_key: str,
    *,
    name: str,
    description: str = "",
    reference_images: Optional[List[str]] = None,
    timeout: float = 300.0,
    client: Any = None,
) -> Dict[str, Any]:
    """注册一个可灵 Element，返回 `{"element_id": ..., "task_id": ..., "raw": ...}`。

    Args:
        name: Element 名字。**这就是 prompt 里 `@` 后面要写的那个名字**，
              所以请用角色真名（"林薇"），不要用 id。
        description: 官方支持的一段描述（可空）。
        reference_images: 参考图，**必须是 http(s) URL 或 base64 data URI**。
              本地路径请先过 `core.adapters.to_image_ref()`。
        client: 可注入的 httpx.AsyncClient（测试用）。

    Raises:
        ElementError: 平台拒绝、超时、或返回里没有 element_id。
    """
    if not api_key:
        raise ElementError("可灵 API key 未配置（设置 → API Key 里填可灵的那把）")
    if not name or not str(name).strip():
        raise ElementError("Element 必须有名字 —— 这个名字就是 prompt 里 @ 后面要写的")

    refs = [r for r in (reference_images or []) if r]
    if not refs:
        raise ElementError("至少要给一张参考图，否则注册出来的 Element 没有形象依据")
    if len(refs) > MAX_REFS_PER_ELEMENT:
        logger.info("参考图 %d 张超过官方上限 %d，只取前 %d 张",
                    len(refs), MAX_REFS_PER_ELEMENT, MAX_REFS_PER_ELEMENT)
        refs = refs[:MAX_REFS_PER_ELEMENT]

    body: Dict[str, Any] = {
        "element_name": str(name).strip(),
        "reference_type": "image_refer",
        "reference_image": refs,
    }
    if description:
        body["element_description"] = str(description)[:200]

    own_client = client is None
    if own_client:
        import httpx
        # trust_env=False：这台机器配了系统代理，代理会把这类请求拖到超时
        client = httpx.AsyncClient(timeout=60.0, trust_env=False)

    try:
        r = await client.post(f"{BASE_URL}/v1/general/advanced-custom-elements",
                              headers=_headers(api_key), json=body)
        data = r.json()
        if r.status_code != 200 or (isinstance(data, dict) and data.get("code") not in (0, None)):
            raise ElementError(
                f"注册失败 HTTP {r.status_code}："
                f"{_err_text(data)}"
            )

        task_id = ((data.get("data") or {}).get("task_id")
                   or data.get("task_id") or data.get("id"))
        if not task_id:
            raise ElementError(f"平台没返回 task_id：{str(data)[:300]}")

        # 异步：轮询到 succeeded 才拿得到 element_id
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
            q = await client.get(
                f"{BASE_URL}/v1/general/advanced-custom-elements",
                headers=_headers(api_key), params={"task_ids": task_id})
            qd = q.json()
            items = (qd.get("data") or [])
            if isinstance(items, dict):
                items = [items]
            if not items:
                continue
            it = items[0]
            status = str(it.get("task_status") or it.get("status") or "").lower()
            if status in ("succeed", "succeeded", "success"):
                eid = str(it.get("element_id") or "").strip()
                if not eid:
                    raise ElementError(
                        f"任务成功但没有 element_id（平台返回：{str(it)[:300]}）")
                return {"element_id": eid, "task_id": task_id, "name": str(name).strip(),
                        "raw": it}
            if status in ("failed", "fail"):
                raise ElementError(f"注册任务失败：{_err_text(it)}")
        raise ElementError(f"注册超时（{timeout:g} 秒内没完成），task_id={task_id}（可稍后查询）")
    finally:
        if own_client:
            try:
                await client.aclose()
            except Exception:
                pass


def _err_text(d: Any) -> str:
    """把平台的错误结构翻成人话（顶层 {code,message} 和内层都扫）。"""
    if not isinstance(d, dict):
        return str(d)[:300]
    code = d.get("code")
    msg = d.get("message")
    if msg:
        return f"[{code}] {msg}" if code else str(msg)
    inner = d.get("data")
    if isinstance(inner, dict):
        return _err_text(inner)
    return str(d)[:300]


# ──────────────────── 与数据库/分镜对接 ────────────────────


async def ensure_character_elements(
    db, project_id: str, *, api_key: str, force: bool = False,
    progress=None,
) -> Dict[str, Any]:
    """给项目里所有**还没有 element_id** 的角色注册 Element，并写回数据库。

    Returns:
        {"ok", "registered":[{name, element_id}], "skipped":[...], "failed":[{name, error}]}
    """
    from core.adapters import to_image_ref

    chars = await _maybe_await(db.list_characters(project_id)) or []
    registered, skipped, failed = [], [], []

    for c in chars:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        existing = (c.get("element_id") or "").strip()
        if existing and not force:
            skipped.append({"name": name, "element_id": existing, "reason": "已经有 element_id"})
            continue
        img = c.get("reference_image_path") or ""
        if not img:
            skipped.append({"name": name, "reason": "没有参考图，注册不出形象"})
            continue

        if progress:
            try:
                progress(f"正在注册角色「{name}」…")
            except Exception:
                pass

        try:
            res = await register_element(
                api_key, name=name,
                description=(c.get("description") or "")[:200],
                reference_images=[to_image_ref(img)],
            )
            await _maybe_await(db.update_character(
                c["id"], element_id=res["element_id"], element_status="ready"))
            registered.append({"name": name, "element_id": res["element_id"]})
        except Exception as e:
            logger.warning("注册角色 %s 的 Element 失败：%s", name, e)
            try:
                await _maybe_await(db.update_character(c["id"], element_status="failed"))
            except Exception:
                pass
            failed.append({"name": name, "error": str(e)[:200]})

    return {
        "ok": not failed,
        "registered": registered,
        "skipped": skipped,
        "failed": failed,
        "note": (f"可灵一次生成最多引用 {MAX_ELEMENTS_PER_CALL} 个 Element；"
                 f"prompt 里用 `@角色名` 指代（名字要与这边注册的一致）"),
    }


async def _maybe_await(v):
    import inspect
    if inspect.isawaitable(v):
        return await v
    return v


async def element_ids_for_shots(db, shot: Dict[str, Any]) -> List[str]:
    """取一个分镜里所有角色的 element_id（过滤掉没注册的），最多 3 个。

    这个函数是"可灵角色一致性"能不能真的生效的**唯一开关**：
    `orchestrator.generate_shot_handler` 会把它塞进 `req.extra["element_ids"]`，
    可灵适配器再从那里取。
    """
    out: List[str] = []
    for cid in (shot.get("character_ids") or []):
        if not cid:
            continue
        try:
            c = await _maybe_await(db.get_character(cid))
        except Exception:
            c = None
        if not c:
            continue
        eid = (c.get("element_id") or "").strip()
        if eid and eid not in out:
            out.append(eid)
    return out[:MAX_ELEMENTS_PER_CALL]
