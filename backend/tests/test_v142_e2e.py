# -*- coding: utf-8 -*-
"""
VideoForge v1.4.2 端到端测试

覆盖三块新能力：
  1. User-defined canonical 角色（is_canonical）—— 一致性第一优先级
  2. Continuity chain_plan 的强约束（must_match_first_frame_desc）
  3. Consistency 的阻断式校验（fatal 直接拦，不让出片）

★ 全部离线：不连网、不调 LLM、不调视频 API。
  用临时 SQLite + 内存 mock（TaskQueue / LLMClient）。
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

# 让测试能 import backend.core.*
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_BACKEND.parent) not in sys.path:
    sys.path.insert(0, str(_BACKEND.parent))

from core.db import Database  # noqa: E402
from core.consistency import (  # noqa: E402
    ConsistencyError,
    canonical_from_shots,
    diff_shots,
    validate_consistency,
    plan_unify,
)
from core.continuity import chain_plan, continuity_brief  # noqa: E402


# ──────────── fixtures ────────────

@pytest.fixture()
def db() -> Database:
    """临时 SQLite 里的 VideoForge db。"""
    tmpdir = tempfile.mkdtemp(prefix="vf_test_")
    path = os.path.join(tmpdir, "test.db")
    return Database(db_path=path)


@pytest.fixture()
def project(db: Database) -> str:
    """建一个项目，返回 project_id。"""
    proj = db.create_project(title="测试项目")
    return proj["id"]


def _mk_shot(db: Database, project_id: str, idx: int, **over) -> Dict[str, Any]:
    """建一个 shot 并返回 dict。"""
    shot = db.create_shot(
        project_id=project_id,
        order_index=idx,
        description=over.pop("description", f"第{idx+1}镜"),
        duration_seconds=over.pop("duration_seconds", 5),
        aspect_ratio=over.pop("aspect_ratio", "16:9"),
        resolution=over.pop("resolution", "1080P"),
        model_provider=over.pop("model_provider", "hailuo"),
        model_name=over.pop("model_name", "MiniMax-Hailuo-02"),
        **over,
    )
    return shot


# ════════════════════════════════════════════════════════════
# 1. User-defined canonical 角色
# ════════════════════════════════════════════════════════════

def test_is_canonical_column_exists(db: Database):
    """characters / scenes 表必须有 is_canonical 列（v1.4.2 migration）。"""
    conn = sqlite3.connect(db.db_path)
    try:
        for table in ("characters", "scenes"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            assert "is_canonical" in cols, f"{table} 缺 is_canonical 列"
    finally:
        conn.close()


def test_create_and_mark_canonical(db: Database, project: str):
    """建角色 → 标 canonical → get_canonical_characters 能取到。"""
    ch = db.create_character(
        project_id=project,
        name="赛博女战士",
        description="银发、红色义眼、黑色作战服",
        is_canonical=1,
    )
    assert ch["is_canonical"] == 1

    got = db.get_canonical_characters(project)
    assert len(got) == 1
    assert got[0]["name"] == "赛博女战士"

    # 取消标记
    db.set_character_canonical(ch["id"], False)
    assert db.get_canonical_characters(project) == []


def test_canonical_beats_majority(db: Database, project: str):
    """★ 核心需求：用户给定的角色（只出现 1 次）要压过"项目内多数派"。

    场景：项目里 3 个镜头用角色"路人A"（多数派），但用户另外指定了
    "主角"为 canonical。一致性基准必须是"主角"，不是"路人A"。
    """
    # 用户给定的基准角色（只出现一次）
    db.create_character(
        project_id=project, name="主角", description="用户上传了参考图",
        is_canonical=1,
    )
    # 多数派角色（出现 3 次）
    db.create_character(
        project_id=project, name="路人A", description="路人",
        is_canonical=0,
    )

    shots = [
        {"character_ids": ["路人A"], "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02"},
        {"character_ids": ["路人A"], "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02"},
        {"character_ids": ["路人A"], "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02"},
    ]
    canon = canonical_from_shots(shots, project_id=project, db=db)
    names = [c["name"] for c in canon.get("characters", [])]
    assert "主角" in names, f"canonical 应含用户给定的'主角'，实际 {names}"


def test_character_crud(db: Database, project: str):
    """角色 CRUD 基本路径。"""
    ch = db.create_character(project_id=project, name="A", description="d")
    assert db.get_character(ch["id"])["name"] == "A"
    db.update_character(ch["id"], description="d2")
    assert db.get_character(ch["id"])["description"] == "d2"
    assert db.delete_character(ch["id"]) is True
    assert db.get_character(ch["id"]) is None


# ════════════════════════════════════════════════════════════
# 2. Continuity chain_plan 强约束
# ════════════════════════════════════════════════════════════

def test_chain_plan_has_must_match_desc():
    """chain_plan 必须给出 must_match_first_frame_desc（生成端要用的硬约束）。"""
    shots = [
        {"id": "s1", "order_index": 0, "scene_id": "sc1",
         "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-03"},
        {"id": "s2", "order_index": 1, "scene_id": "sc1",
         "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-03"},
    ]
    plan = chain_plan(shots)
    assert len(plan) == 2
    first, second = plan
    assert "must_match_first_frame_desc" in first
    assert first["prev_shot_id"] is None
    # 第二镜同场景 → 必须给出衔接约束
    assert second["prev_shot_id"] == "s1"
    assert second["must_match_first_frame_desc"], "同场景第二镜必须有衔接约束文本"
    assert "transition_type" in second


def test_chain_plan_cross_scene():
    """跨场景也要有约束（色调延续），但可以是 break 级。"""
    shots = [
        {"id": "s1", "order_index": 0, "scene_id": "sc1",
         "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-03"},
        {"id": "s2", "order_index": 1, "scene_id": "sc2",
         "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-03"},
    ]
    plan = chain_plan(shots)
    assert plan[1]["prev_shot_id"] == "s1"
    assert "must_match_first_frame_desc" in plan[1]
    assert plan[1]["same_scene_as_prev"] is False


# ════════════════════════════════════════════════════════════
# 3. Consistency 阻断式校验
# ════════════════════════════════════════════════════════════

def test_validate_consistency_blocks_fatal(db: Database, project: str):
    """★ fatal（换模型）必须被拦下，不让出片。"""
    shots = [
        {"model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02",
         "resolution": "1080P", "aspect_ratio": "16:9"},
        {"model_provider": "hailuo", "model_name": "kling-1.6",
         "resolution": "1080P", "aspect_ratio": "16:9"},
    ]
    with pytest.raises(ConsistencyError) as exc:
        validate_consistency(shots, project_id=project, db=db)
    assert "不一致" in str(exc.value)
    assert exc.value.report, "异常应带体检报告"


def test_validate_consistency_passes_when_same(db: Database, project: str):
    """条件一致时放行。"""
    shots = [
        {"model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02",
         "resolution": "1080P", "aspect_ratio": "16:9"},
        {"model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02",
         "resolution": "1080P", "aspect_ratio": "16:9"},
    ]
    report = validate_consistency(shots, project_id=project, db=db)
    assert report["consistent"] is True
    assert report["worst_level"] == "ok"


def test_validate_consistency_allow_fatal_optout(db: Database, project: str):
    """allow_fatal=True 时只报告不拦。"""
    shots = [
        {"model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02",
         "resolution": "1080P", "aspect_ratio": "16:9"},
        {"model_provider": "hailuo", "model_name": "kling-1.6",
         "resolution": "1080P", "aspect_ratio": "16:9"},
    ]
    report = validate_consistency(shots, project_id=project, db=db, allow_fatal=True)
    assert report["worst_level"] == "fatal"
    assert report["deviations"]


def test_diff_and_unify_still_work(db: Database, project: str):
    """原有 diff_shots / plan_unify 不能被改坏。"""
    shots = [
        {"model_provider": "hailuo", "model_name": "MiniMax-Hailuo-02",
         "resolution": "1080P", "aspect_ratio": "16:9"},
        {"model_provider": "hailuo", "model_name": "kling-1.6",
         "resolution": "720P", "aspect_ratio": "9:16"},
    ]
    rep = diff_shots(shots)
    assert rep["worst_level"] == "fatal"
    plan = plan_unify(shots)
    assert plan["action_count"] >= 1
    assert plan["warnings"]


# ════════════════════════════════════════════════════════════
# 4. Agent pipeline（mock TaskQueue + LLM）
# ════════════════════════════════════════════════════════════

class _FakeHandle:
    def __init__(self, state, data=None, error=None):
        self.state = state
        self.result = MagicMock()
        self.result.data = data or {}
        self.result.error = error
        self._callbacks = []

    def add_progress_callback(self, cb):
        self._callbacks.append(cb)

    def emit(self, payload):
        for cb in self._callbacks:
            cb(payload)


class _FakeTaskQueue:
    """按 task_type 返回预设结果的假 TaskQueue。"""

    def __init__(self, results: Dict[str, Any]):
        self.results = results
        self.submitted: List[Dict[str, Any]] = []

    def submit(self, task_id: str, task_type: str, payload: Dict[str, Any]):
        self.submitted.append({"task_id": task_id, "task_type": task_type, "payload": payload})
        res = self.results.get(task_type, {})
        return _FakeHandle("success", data=res)


@pytest.mark.asyncio
async def test_agent_generate_video_pipeline(db: Database, project: str, monkeypatch):
    """★ agent 端到端：意图 → generate_script → 每镜 generate_shot → 事件流。

    验证三件事：
      1. 真的会 submit generate_script + N 次 generate_shot
      2. 每次 generate_shot 的 payload 带 chain_config（转镜强约束）
      3. 事件流里有 image / video / done
    """
    import main as vf_main
    from core.agent import ConversationAgent

    monkeypatch.setattr(vf_main, "get_app_state", lambda: {"db": db}, raising=False)

    shots = [
        {"id": "s1", "order_index": 0, "scene_id": "sc1",
         "description": "村口全景", "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-03"},
        {"id": "s2", "order_index": 1, "scene_id": "sc1",
         "description": "主角回头", "model_provider": "hailuo", "model_name": "MiniMax-Hailuo-03"},
    ]
    queue = _FakeTaskQueue({
        "generate_script": {"shots": shots, "title": "村口"},
        "generate_shot": {"shot_id": "x", "video_url": "/media/x.mp4",
                          "preview_image": "/media/x.jpg", "status": "ok"},
    })

    llm = MagicMock()
    llm.chat = MagicMock(return_value=MagicMock(
        content=json.dumps({"action": "generate_video", "params": {"prompt": "村口 30 秒", "duration": 30}})
    ))

    agent = ConversationAgent(llm=llm, orchestrator=queue)
    events = []
    async for ev in agent.stream_pipeline(
        "generate_video", {"prompt": "村口 30 秒", "duration": 30}, project
    ):
        events.append(ev)

    types = [e.type for e in events]
    assert "generate_script" in [s["task_type"] for s in queue.submitted]
    shot_subs = [s for s in queue.submitted if s["task_type"] == "generate_shot"]
    assert len(shot_subs) == 2, f"应 submit 两次 generate_shot，实际 {len(shot_subs)}"

    # ★ 第二镜必须带 chain_config
    assert shot_subs[0]["payload"].get("chain_config") in (None, {}), "第一镜不该有 chain_config"
    cc = shot_subs[1]["payload"].get("chain_config")
    assert cc, "第二镜必须带 chain_config"
    assert cc.get("prev_shot_id") == "s1"
    assert cc.get("must_match_first_frame_desc")

    # 事件流
    assert "image" in types, f"应有 image 事件，实际 {types}"
    assert "done" in types, f"应有 done 事件，实际 {types}"


@pytest.mark.asyncio
async def test_agent_injects_canonical_characters(db: Database, project: str, monkeypatch):
    """★ 用户给定的角色必须被注入脚本生成的 prompt。"""
    import main as vf_main
    from core.agent import ConversationAgent

    monkeypatch.setattr(vf_main, "get_app_state", lambda: {"db": db}, raising=False)
    db.create_character(project_id=project, name="赛博女战士",
                       description="银发红义眼", is_canonical=1)

    queue = _FakeTaskQueue({
        "generate_script": {"shots": [], "title": "t"},
        "generate_shot": {"shot_id": "x", "video_url": "/media/x.mp4"},
    })
    llm = MagicMock()
    llm.chat = MagicMock(return_value=MagicMock(
        content=json.dumps({"action": "generate_video", "params": {"prompt": "p", "duration": 30}})
    ))

    agent = ConversationAgent(llm=llm, orchestrator=queue)
    async for _ in agent.stream_pipeline("generate_video", {"prompt": "p", "duration": 30}, project):
        pass

    script_sub = [s for s in queue.submitted if s["task_type"] == "generate_script"]
    assert script_sub, "应 submit generate_script"
    prompt = script_sub[0]["payload"]["user_prompt"]
    assert "赛博女战士" in prompt, f"canonical 角色应注入 prompt，实际: {prompt[:200]}"


# ════════════════════════════════════════════════════════════
# 5. 真实白膜视频（如果工作区里有）
# ════════════════════════════════════════════════════════════

def test_white_membrane_fixture_exists():
    """白膜测试素材存在（用于人工验证，不参与断言）。"""
    candidates = [
        Path(r"C:\Users\Administrator\.proma\agent-workspaces\default\workspace-files")
        / "场景B_白膜_EEVEE真实光影版.mp4",
    ]
    found = [p for p in candidates if p.exists()]
    if not found:
        pytest.skip("白膜素材不在工作区（跳过）")
    assert found[0].stat().st_size > 1000
