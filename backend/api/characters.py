# -*- coding: utf-8 -*-
"""
VideoForge · 角色管理 API（v1.4.2）

**为什么要有这个模块**：v1.4.2 之前，角色只有"项目内多数派"这一种隐式基准。
用户上传了参考图、明确写了外观的角色，和 LLM 自动生成的角色混在同一个列表里，
一致性检查按"多数派"推断基准 —— 用户主动给定的那份意图被稀释。

现在角色有显式的 **canonical（基准）** 标记：
  - `is_canonical = 1` → 用户主动给定，作为一致性的**第一优先级**锚点
  - 未标记 → 仍按原来的多数派推断

一致性基准优先级（见 `core/consistency.py`）：
  1. 用户给定的 canonical 角色/场景
  2. 项目内多数派推断
  3. 全局默认设置
"""

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/projects", tags=["characters"])


# ──────────── 请求模型 ────────────

class CharacterCreate(BaseModel):
    """创建角色。"""
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    body_type: str = Field(default="", max_length=100)
    age: str = Field(default="", max_length=100)
    costume_main: str = Field(default="", max_length=500)
    costume_alternate: str = Field(default="", max_length=500)
    props: List[str] = Field(default_factory=list)
    reference_image_path: Optional[str] = None
    reference_features: Optional[Dict[str, Any]] = None
    is_canonical: bool = False


class CharacterUpdate(BaseModel):
    """更新角色（所有字段可选）。"""
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    body_type: Optional[str] = Field(default=None, max_length=100)
    age: Optional[str] = Field(default=None, max_length=100)
    costume_main: Optional[str] = Field(default=None, max_length=500)
    costume_alternate: Optional[str] = Field(default=None, max_length=500)
    props: Optional[List[str]] = None
    reference_image_path: Optional[str] = None
    reference_features: Optional[Dict[str, Any]] = None
    is_canonical: Optional[bool] = None


class CanonicalRequest(BaseModel):
    """标记/取消标记 canonical。"""
    is_canonical: bool = True


# ──────────── 依赖 ────────────

def _db():
    """取全局 db 单例（由 main.py 注入）。"""
    from main import get_app_state
    return get_app_state()["db"]


# ──────────── 端点 ────────────

@router.get("/{project_id}/characters")
def list_characters(project_id: str):
    """列出项目所有角色。"""
    db = _db()
    chars = db.list_characters(project_id)
    for ch in chars:
        if isinstance(ch.get("reference_features"), str):
            try:
                ch["reference_features"] = json.loads(ch["reference_features"] or "{}")
            except Exception:
                ch["reference_features"] = {}
    return {"characters": chars, "total": len(chars)}


@router.get("/{project_id}/characters/canonical")
def list_canonical_characters(project_id: str):
    """只返回用户指定为基准的角色（一致性第一优先级）。"""
    db = _db()
    chars = db.get_canonical_characters(project_id)
    for ch in chars:
        if isinstance(ch.get("reference_features"), str):
            try:
                ch["reference_features"] = json.loads(ch["reference_features"] or "{}")
            except Exception:
                ch["reference_features"] = {}
    return {"characters": chars, "total": len(chars)}


@router.post("/{project_id}/characters")
def create_character(project_id: str, body: CharacterCreate):
    """创建角色。`is_canonical=true` 表示这是用户主动给定的基准。"""
    db = _db()
    payload = body.model_dump()
    payload["project_id"] = project_id
    payload["is_canonical"] = 1 if payload.pop("is_canonical", False) else 0
    ch = db.create_character(**payload)
    if ch is None:
        raise HTTPException(status_code=500, detail="创建角色失败")
    return ch


@router.get("/{project_id}/characters/{cid}")
def get_character(project_id: str, cid: str):
    """取单个角色。"""
    db = _db()
    ch = db.get_character(cid)
    if not ch or ch.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="角色不存在")
    return ch


@router.patch("/{project_id}/characters/{cid}")
def update_character(project_id: str, cid: str, body: CharacterUpdate):
    """更新角色字段（含 is_canonical）。"""
    db = _db()
    ch = db.get_character(cid)
    if not ch or ch.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="角色不存在")
    payload = body.model_dump(exclude_unset=True)
    if "is_canonical" in payload:
        payload["is_canonical"] = 1 if payload.pop("is_canonical") else 0
    updated = db.update_character(cid, **payload)
    return updated or ch


@router.post("/{project_id}/characters/{cid}/canonical")
def set_canonical(project_id: str, cid: str, body: CanonicalRequest):
    """把角色标记/取消标记为"用户给定的基准"。

    ★ 这是用户需求"角色一致性用用户自己给定的"的操作入口。
    """
    db = _db()
    ch = db.get_character(cid)
    if not ch or ch.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="角色不存在")
    return db.set_character_canonical(cid, body.is_canonical)


@router.delete("/{project_id}/characters/{cid}")
def delete_character(project_id: str, cid: str):
    """删除角色。"""
    db = _db()
    ch = db.get_character(cid)
    if not ch or ch.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="角色不存在")
    ok = db.delete_character(cid)
    return {"deleted": bool(ok), "id": cid}
