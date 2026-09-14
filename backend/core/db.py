"""
VideoForge · 数据库层（SQLite）

特点：
- 无外部依赖（仅 Python 标准库 sqlite3）
- WAL 模式（读写并发）
- 自动迁移（启动时建表）
- 所有 ID 用 UUID4 字符串
"""

import sqlite3
import json
import threading
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, List, Dict, Any, Iterator
from models import new_id


class Database:
    """SQLite 封装"""

    _local = threading.local()

    def __init__(self, db_path: str = "./data/videoforge.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """每个线程独立连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit mode
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """迁移辅助：检查列是否存在，不存在则 ALTER TABLE ADD COLUMN。

        SQLite 的 ALTER TABLE ADD COLUMN 不阻塞，失败时（如列已存在）记录但不抛。
        老用户升级时自动补全新字段，避免 SELECT 返回 None。
        """
        conn = self._get_conn()
        try:
            cur = conn.execute(f"PRAGMA table_info({table})")
            cols = [row[1] for row in cur.fetchall()]
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except Exception as e:
            # 不阻塞启动；migration 失败通常意味着表还没创建
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """事务上下文"""
        conn = self._get_conn()
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _init_schema(self):
        """建表（启动时执行）"""
        conn = self._get_conn()

        # ──────────── 增量迁移（新列加在已有 SQLite 表上）────────────
        # SQLite 不支持单条 ALTER TABLE 加多列，但加单列是安全的。
        # 老库启动时自动补上缺失列，避免 frontend 读到 None。
        self._ensure_column("tasks", "failed_stage",     "TEXT")
        self._ensure_column("tasks", "progress_message", "TEXT")
        self._ensure_column("tasks", "cancellable",      "INTEGER DEFAULT 1")

        # 项目
        conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          description TEXT DEFAULT '',
          status TEXT DEFAULT 'draft',
          settings JSON DEFAULT '{}',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 剧本
        conn.execute("""
        CREATE TABLE IF NOT EXISTS scripts (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_prompt TEXT,
          outline TEXT DEFAULT '',
          scenes JSON DEFAULT '[]',
          three_layer_prompts JSON DEFAULT '{}',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 角色
        conn.execute("""
        CREATE TABLE IF NOT EXISTS characters (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          description TEXT DEFAULT '',
          body_type TEXT DEFAULT '',
          age TEXT DEFAULT '',
          costume_main TEXT DEFAULT '',
          costume_alternate TEXT DEFAULT '',
          props JSON DEFAULT '[]',
          reference_image_path TEXT,
          reference_features JSON,
          status TEXT DEFAULT 'draft',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 场景
        conn.execute("""
        CREATE TABLE IF NOT EXISTS scenes (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          location_type TEXT DEFAULT 'outdoor',
          space_scale TEXT DEFAULT 'medium',
          lighting TEXT DEFAULT 'natural',
          time_of_day TEXT DEFAULT 'day',
          weather TEXT DEFAULT 'clear',
          description TEXT DEFAULT '',
          reference_image_path TEXT,
          color_palette JSON DEFAULT '[]',
          textures JSON DEFAULT '[]',
          reference_features JSON,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # ★ 老库补列：场景也要存「固定槽位」（地形/建筑/陈设/氛围/色调），
        #   否则同一地点在不同镜头里的环境描述会各写各的，场景看起来会变样。
        self._ensure_column("scenes", "reference_features", "JSON")
        # 道具
        conn.execute("""
        CREATE TABLE IF NOT EXISTS props (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          description TEXT DEFAULT '',
          reference_image_path TEXT,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 分镜
        conn.execute("""
        CREATE TABLE IF NOT EXISTS shots (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          scene_id TEXT,
          order_index INTEGER DEFAULT 0,
          duration_seconds INTEGER DEFAULT 10,
          layer1_overview TEXT DEFAULT '',
          layer2_timeline JSON DEFAULT '[]',
          layer3_constraints JSON DEFAULT '{}',
          character_ids JSON DEFAULT '[]',
          model_provider TEXT DEFAULT 'kling',
          model_name TEXT DEFAULT 'kling-1.6',
          aspect_ratio TEXT DEFAULT '16:9',
          resolution TEXT DEFAULT '1080p',
          status TEXT DEFAULT 'pending',
          progress REAL DEFAULT 0,
          candidates JSON DEFAULT '[]',
          selected_candidate_id TEXT,
          error_message TEXT,
          task_id TEXT,
          negative_reference_paths JSON DEFAULT '[]',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 抽象化日志
        conn.execute("""
        CREATE TABLE IF NOT EXISTS abstraction_logs (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          source_description TEXT,
          abstracted_description JSON,
          removed_features JSON,
          preserved_features JSON,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 任务
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY,
          type TEXT NOT NULL,
          payload JSON,
          status TEXT DEFAULT 'pending',
          progress REAL DEFAULT 0,
          progress_message TEXT,             -- 当前阶段可读消息（新增）
          failed_stage TEXT,                 -- 失败时的具体阶段 script/voice/...（新增）
          result JSON,
          error TEXT,
          cancellable INTEGER DEFAULT 1,     -- 是否可取消（新增）
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          started_at TIMESTAMP,
          finished_at TIMESTAMP
        )
        """)
        # 设置
        conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value JSON,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # AI 对话会话（服务端持久化，不依赖 webview localStorage）
        conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_conversations (
          id TEXT PRIMARY KEY,
          title TEXT DEFAULT '',
          messages JSON DEFAULT '[]',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 剧本细节（按场次保存，符合 Skill 手册场次模板）
        conn.execute("""
        CREATE TABLE IF NOT EXISTS script_scene_details (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id TEXT NOT NULL,
          scene_number INTEGER NOT NULL,
          detail JSON NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(project_id, scene_number)
        )
        """)
        # 索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scripts_project ON scripts(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_characters_project ON characters(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scenes_project ON scenes(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_props_project ON props(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shots_project ON shots(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shots_scene ON shots(scene_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")

    # ──────────── 通用 CRUD ────────────

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for k, v in list(d.items()):
            if isinstance(v, str) and v.startswith(("{", "[")):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
        return d

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._get_conn().execute(sql, params)

    # ──────────── 项目 ────────────

    def create_project(self, title: str, description: str = "",
                        settings: Optional[dict] = None) -> Dict[str, Any]:
        pid = new_id()
        self._execute(
            "INSERT INTO projects (id, title, description, settings) VALUES (?, ?, ?, ?)",
            (pid, title, description, json.dumps(settings or {})),
        )
        return self.get_project(pid)

    def get_project(self, pid: str) -> Optional[Dict[str, Any]]:
        row = self._execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_projects(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_project(self, pid: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not kwargs:
            return self.get_project(pid)
        # JSON 字段
        for k in ("settings",):
            if k in kwargs:
                kwargs[k] = json.dumps(kwargs[k])
        kwargs["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k} = ?" for k in kwargs.keys())
        vals = list(kwargs.values()) + [pid]
        self._execute(f"UPDATE projects SET {sets} WHERE id = ?", tuple(vals))
        return self.get_project(pid)

    def delete_project(self, pid: str) -> bool:
        cur = self._execute("DELETE FROM projects WHERE id = ?", (pid,))
        return cur.rowcount > 0

    # ──────────── 剧本 ────────────

    def upsert_script(self, project_id: str, user_prompt: str,
                       outline: str = "", scenes: list = None,
                       three_layer_prompts: dict = None) -> Dict[str, Any]:
        existing = self._execute(
            "SELECT id FROM scripts WHERE project_id = ?", (project_id,)
        ).fetchone()
        if existing:
            sid = existing["id"]
            self._execute(
                """UPDATE scripts
                   SET user_prompt = ?, outline = ?, scenes = ?,
                       three_layer_prompts = ?, updated_at = ?
                   WHERE id = ?""",
                (user_prompt, outline, json.dumps(scenes or []),
                 json.dumps(three_layer_prompts or {}),
                 datetime.now().isoformat(), sid),
            )
        else:
            sid = new_id()
            self._execute(
                """INSERT INTO scripts (id, project_id, user_prompt, outline, scenes, three_layer_prompts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, project_id, user_prompt, outline,
                 json.dumps(scenes or []), json.dumps(three_layer_prompts or {})),
            )
        return self.get_script(project_id)

    def get_script(self, project_id: str) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM scripts WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            return None
        return self._normalize_script(self._row_to_dict(row))

    # ──────────── 剧本规范化（JSON 反序列化 + 专业大纲渲染）────────────

    @staticmethod
    def _render_outline_text(o: Dict[str, Any]) -> str:
        """把剧本 JSON 渲染为专业可读的编剧体大纲文本（供剧本工作台直接展示）"""
        lines: List[str] = []
        if o.get("title"):
            lines.append(f"《{o['title']}》")
        if o.get("logline"):
            lines.append(f"一句话梗概：{o['logline']}")
        if o.get("style"):
            lines.append(f"风格：{o['style']}")
        scenes = o.get("scenes") or []
        if scenes:
            total = 0
            for s in scenes:
                try:
                    total += int(s.get("duration_seconds") or 0)
                except Exception:
                    pass
            lines.append(f"场次：{len(scenes)} 场 · 总时长 {total}s")
            # 目标时长（用户主页/剧本页选的那个）：写出来，才看得出"对不上"
            try:
                _dp = o.get("duration_plan") or {}
                if _dp.get("target"):
                    lines.append(f"目标总时长：{_dp['target']}s"
                                 + ("　✅ 已对齐" if _dp.get("ok") else "　⚠ 与上面的总长不一致"))
            except Exception:
                pass
            lines.append("")
            for s in scenes:
                n = s.get("scene_number")
                lines.append(f"〔场次 {n}〕{s.get('title', '')}　（{s.get('duration_seconds', 0)}s）")
                if s.get("hook"):
                    lines.append(f"　　钩子：{s['hook']}")
                if s.get("location"):
                    lines.append(f"　　地点：{s['location']}")
                chars = s.get("characters")
                if chars:
                    lines.append(f"　　人物：{'、'.join(chars) if isinstance(chars, list) else chars}")
                acts = s.get("actions")
                if acts:
                    acts = acts if isinstance(acts, list) else [acts]
                    for a in acts:
                        lines.append(f"　　动作：{a}")
                dlg = s.get("dialogues")
                if isinstance(dlg, list):
                    for d in dlg:
                        if isinstance(d, dict):
                            _dv = d.get("delivery") or ""
                            lines.append(f"　　对白：{d.get('character', '')}"
                                         + (f"（{_dv}）" if _dv else "")
                                         + f"「{d.get('text', '')}」")
                        elif isinstance(d, str):
                            lines.append(f"　　对白：{d}")
                if s.get("subtitle"):
                    lines.append(f"　　字幕：{s['subtitle']}")
                if s.get("mood"):
                    lines.append(f"　　情绪：{s['mood']}")
                lines.append("")
        else:
            # 极简结构兜底
            for key in ("summary", "outline", "content"):
                if o.get(key):
                    lines.append(str(o[key]))
        return "\n".join(lines).strip()

    @classmethod
    def _normalize_script(cls, d: Dict[str, Any]) -> Dict[str, Any]:
        """scripts 行 → 前端可用结构：JSON 字段反序列化 + outline 渲染为专业文本"""
        raw_outline = d.get("outline")
        outline_obj: Optional[Dict[str, Any]] = None
        if isinstance(raw_outline, dict):
            outline_obj = raw_outline
        elif isinstance(raw_outline, str):
            s = raw_outline.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        outline_obj = parsed
                except Exception:
                    outline_obj = None

        # scenes：字符串 → 数组
        scenes = d.get("scenes")
        if isinstance(scenes, str):
            try:
                scenes = json.loads(scenes or "[]")
            except Exception:
                scenes = []
        if not scenes and outline_obj:
            scenes = outline_obj.get("scenes") or []
        d["scenes"] = scenes if isinstance(scenes, list) else []

        # three_layer_prompts：字符串 → 对象
        tlp = d.get("three_layer_prompts")
        if isinstance(tlp, str):
            try:
                tlp = json.loads(tlp or "{}")
            except Exception:
                tlp = {}
        d["three_layer_prompts"] = tlp if isinstance(tlp, dict) else {}

        # 从剧本 JSON 中提炼标题/梗概，并把 outline 换成专业可读文本
        if outline_obj:
            d["title"] = d.get("title") or outline_obj.get("title") or ""
            d["logline"] = outline_obj.get("logline") or ""
            d["style"] = d.get("style") or outline_obj.get("style") or ""
            d["outline"] = cls._render_outline_text(outline_obj)
            d["outline_json"] = outline_obj        # 保留原始结构，供高级用途
            if not d["scenes"]:
                d["scenes"] = outline_obj.get("scenes") or []
        elif isinstance(raw_outline, str):
            d["outline"] = raw_outline
        else:
            d["outline"] = ""
        return d

    # ──────────── 角色 ────────────

    def create_character(self, **kwargs) -> Dict[str, Any]:
        cid = new_id()
        kwargs["id"] = cid
        for k in ("props", "reference_features"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k] or ([] if k == "props" else {}))
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        self._execute(
            f"INSERT INTO characters ({cols}) VALUES ({placeholders})",
            tuple(kwargs.values()),
        )
        return self.get_character(cid)

    def get_character(self, cid: str) -> Optional[Dict[str, Any]]:
        row = self._execute("SELECT * FROM characters WHERE id = ?", (cid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_characters(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM characters WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_character(self, cid: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not kwargs:
            return self.get_character(cid)
        for k in ("props", "reference_features"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k])
        sets = ", ".join(f"{k} = ?" for k in kwargs.keys())
        vals = list(kwargs.values()) + [cid]
        self._execute(f"UPDATE characters SET {sets} WHERE id = ?", tuple(vals))
        return self.get_character(cid)

    def delete_character(self, cid: str) -> bool:
        cur = self._execute("DELETE FROM characters WHERE id = ?", (cid,))
        return cur.rowcount > 0

    # ──────────── 资产库（跨项目复用：角色 / 场景）────────────

    def list_all_characters(self, exclude_project: Optional[str] = None,
                            limit: int = 500) -> List[Dict[str, Any]]:
        """资产库：跨项目列出全部角色，附带来源项目标题（供"从资产库导入"使用）"""
        sql = ("SELECT c.*, p.title AS source_project_title, p.id AS source_project_id "
               "FROM characters c LEFT JOIN projects p ON p.id = c.project_id")
        params: List[Any] = []
        if exclude_project:
            sql += " WHERE c.project_id != ?"
            params.append(exclude_project)
        sql += " ORDER BY c.created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_dict(r) for r in self._execute(sql, tuple(params)).fetchall()]

    def list_all_scenes(self, exclude_project: Optional[str] = None,
                        limit: int = 500) -> List[Dict[str, Any]]:
        """资产库：跨项目列出全部场景"""
        sql = ("SELECT s.*, p.title AS source_project_title, p.id AS source_project_id "
               "FROM scenes s LEFT JOIN projects p ON p.id = s.project_id")
        params: List[Any] = []
        if exclude_project:
            sql += " WHERE s.project_id != ?"
            params.append(exclude_project)
        sql += " ORDER BY s.created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_dict(r) for r in self._execute(sql, tuple(params)).fetchall()]

    def clone_characters(self, ids: List[str], target_project: str) -> List[Dict[str, Any]]:
        """把资产库中的角色复制到目标项目（保留内容、生成新 id，实现跨剧本复用）"""
        created: List[Dict[str, Any]] = []
        for cid in ids or []:
            src = self.get_character(cid)
            if not src:
                continue
            data = {k: v for k, v in src.items()
                    if k not in ("id", "project_id", "created_at",
                                 "source_project_title", "source_project_id")}
            data["project_id"] = target_project
            created.append(self.create_character(**data))
        return created

    def clone_scenes(self, ids: List[str], target_project: str) -> List[Dict[str, Any]]:
        """把资产库中的场景复制到目标项目"""
        created: List[Dict[str, Any]] = []
        for sid in ids or []:
            src = self.get_scene(sid)
            if not src:
                continue
            data = {k: v for k, v in src.items()
                    if k not in ("id", "project_id", "created_at",
                                 "source_project_title", "source_project_id")}
            data["project_id"] = target_project
            created.append(self.create_scene(**data))
        return created

    # ──────────── 场景 ────────────

    def create_scene(self, **kwargs) -> Dict[str, Any]:
        sid = new_id()
        kwargs["id"] = sid
        for k in ("color_palette", "textures"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k] or [])
        if "reference_features" in kwargs and not isinstance(kwargs["reference_features"], str):
            kwargs["reference_features"] = json.dumps(kwargs["reference_features"] or {})
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        self._execute(
            f"INSERT INTO scenes ({cols}) VALUES ({placeholders})",
            tuple(kwargs.values()),
        )
        return self.get_scene(sid)

    def get_scene(self, sid: str) -> Optional[Dict[str, Any]]:
        row = self._execute("SELECT * FROM scenes WHERE id = ?", (sid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_scenes(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_scene(self, sid: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not kwargs:
            return self.get_scene(sid)
        for k in ("color_palette", "textures", "reference_features"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k] if kwargs[k] is not None else {})
        sets = ", ".join(f"{k} = ?" for k in kwargs.keys())
        vals = list(kwargs.values()) + [sid]
        self._execute(f"UPDATE scenes SET {sets} WHERE id = ?", tuple(vals))
        return self.get_scene(sid)

    def delete_scene(self, sid: str) -> bool:
        cur = self._execute("DELETE FROM scenes WHERE id = ?", (sid,))
        return cur.rowcount > 0

    # ──────────── 道具 ────────────

    def create_prop(self, **kwargs) -> Dict[str, Any]:
        pid = new_id()
        kwargs["id"] = pid
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        self._execute(
            f"INSERT INTO props ({cols}) VALUES ({placeholders})",
            tuple(kwargs.values()),
        )
        return self.get_prop(pid)

    def get_prop(self, pid: str) -> Optional[Dict[str, Any]]:
        row = self._execute("SELECT * FROM props WHERE id = ?", (pid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_props(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM props WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_prop(self, pid: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not kwargs:
            return self.get_prop(pid)
        sets = ", ".join(f"{k} = ?" for k in kwargs.keys())
        vals = list(kwargs.values()) + [pid]
        self._execute(f"UPDATE props SET {sets} WHERE id = ?", tuple(vals))
        return self.get_prop(pid)

    def delete_prop(self, pid: str) -> bool:
        cur = self._execute("DELETE FROM props WHERE id = ?", (pid,))
        return cur.rowcount > 0

    # ──────────── 分镜 ────────────

    def create_shot(self, **kwargs) -> Dict[str, Any]:
        sid = new_id()
        kwargs["id"] = sid
        for k in ("layer2_timeline", "layer3_constraints", "character_ids", "negative_reference_paths"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k])
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        self._execute(
            f"INSERT INTO shots ({cols}) VALUES ({placeholders})",
            tuple(kwargs.values()),
        )
        return self.get_shot(sid)

    def get_shot(self, sid: str) -> Optional[Dict[str, Any]]:
        row = self._execute("SELECT * FROM shots WHERE id = ?", (sid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_shots(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM shots WHERE project_id = ? ORDER BY order_index, created_at",
            (project_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_shot(self, sid: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not kwargs:
            return self.get_shot(sid)
        for k in ("layer2_timeline", "layer3_constraints", "character_ids", "negative_reference_paths", "candidates"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k])
        kwargs["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k} = ?" for k in kwargs.keys())
        vals = list(kwargs.values()) + [sid]
        self._execute(f"UPDATE shots SET {sets} WHERE id = ?", tuple(vals))
        return self.get_shot(sid)

    def delete_shot(self, sid: str) -> bool:
        cur = self._execute("DELETE FROM shots WHERE id = ?", (sid,))
        return cur.rowcount > 0

    def reorder_shots(self, project_id: str, ordered_ids: List[str]):
        with self.transaction() as conn:
            for idx, sid in enumerate(ordered_ids):
                conn.execute(
                    "UPDATE shots SET order_index = ? WHERE id = ? AND project_id = ?",
                    (idx, sid, project_id),
                )

    # ──────────── 任务 ────────────

    def create_task(self, task_type: str, payload: dict) -> str:
        tid = new_id()
        self._execute(
            "INSERT INTO tasks (id, type, payload) VALUES (?, ?, ?)",
            (tid, task_type, json.dumps(payload)),
        )
        return tid

    def get_task(self, tid: str) -> Optional[Dict[str, Any]]:
        row = self._execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def update_task(self, tid: str, **kwargs):
        if not kwargs:
            return
        for k in ("payload", "result"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k])
        sets = ", ".join(f"{k} = ?" for k in kwargs.keys())
        vals = list(kwargs.values()) + [tid]
        self._execute(f"UPDATE tasks SET {sets} WHERE id = ?", tuple(vals))

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if status:
            rows = self._execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ──────────── 抽象化日志 ────────────

    def log_abstraction(self, project_id: str, source: str,
                         abstracted: dict, removed: list,
                         preserved: list) -> str:
        lid = new_id()
        self._execute(
            """INSERT INTO abstraction_logs
               (id, project_id, source_description, abstracted_description, removed_features, preserved_features)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (lid, project_id, source,
             json.dumps(abstracted),
             json.dumps(removed),
             json.dumps(preserved)),
        )
        return lid

    # ──────────── 设置 ────────────

    def get_setting(self, key: str) -> Optional[Any]:
        row = self._execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        v = row["value"]
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v

    def set_setting(self, key: str, value: Any):
        self._execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, json.dumps(value), datetime.now().isoformat()),
        )

    # ──────────── AI 对话会话 ────────────

    def list_conversations(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT id, title, messages, created_at, updated_at FROM chat_conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = self._row_to_dict(r)
            if isinstance(d.get('messages'), str):
                try: d['messages'] = json.loads(d['messages'])
                except: d['messages'] = []
            out.append(d)
        return out

    def get_conversation(self, cid: str) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT id, title, messages, created_at, updated_at FROM chat_conversations WHERE id = ?", (cid,)
        ).fetchone()
        if not row: return None
        d = self._row_to_dict(row)
        if isinstance(d.get('messages'), str):
            try: d['messages'] = json.loads(d['messages'])
            except: d['messages'] = []
        return d

    def upsert_conversation(self, cid: str, title: str, messages: list) -> Dict[str, Any]:
        now = datetime.now().isoformat()
        existing = self.get_conversation(cid)
        if existing:
            self._execute(
                "UPDATE chat_conversations SET title = ?, messages = ?, updated_at = ? WHERE id = ?",
                (title, json.dumps(messages, ensure_ascii=False), now, cid),
            )
        else:
            self._execute(
                "INSERT INTO chat_conversations (id, title, messages, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (cid, title, json.dumps(messages, ensure_ascii=False), now, now),
            )
        return self.get_conversation(cid)

    def delete_conversation(self, cid: str) -> bool:
        cur = self._execute("DELETE FROM chat_conversations WHERE id = ?", (cid,))
        return cur.rowcount > 0

    # ──────────── 剧本场次细节（Skill 手册场次模板）────────────

    def get_scene_detail(self, project_id: str, scene_number: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT id, project_id, scene_number, detail, created_at, updated_at FROM script_scene_details WHERE project_id = ? AND scene_number = ?",
            (project_id, scene_number),
        ).fetchone()
        if not row:
            return None
        d = self._row_to_dict(row)
        if isinstance(d.get('detail'), str):
            try: d['detail'] = json.loads(d['detail'])
            except: pass
        return d

    def list_scene_details(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT id, project_id, scene_number, detail, created_at, updated_at FROM script_scene_details WHERE project_id = ? ORDER BY scene_number",
            (project_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = self._row_to_dict(r)
            if isinstance(d.get('detail'), str):
                try: d['detail'] = json.loads(d['detail'])
                except: pass
            out.append(d)
        return out

    def upsert_scene_detail(self, project_id: str, scene_number: int, detail: dict) -> Dict[str, Any]:
        now = datetime.now().isoformat()
        existing = self.get_scene_detail(project_id, scene_number)
        if existing:
            self._execute(
                "UPDATE script_scene_details SET detail = ?, updated_at = ? WHERE project_id = ? AND scene_number = ?",
                (json.dumps(detail, ensure_ascii=False), now, project_id, scene_number),
            )
        else:
            self._execute(
                "INSERT INTO script_scene_details (project_id, scene_number, detail, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, scene_number, json.dumps(detail, ensure_ascii=False), now, now),
            )
        return self.get_scene_detail(project_id, scene_number)

    def delete_scene_detail(self, project_id: str, scene_number: int) -> bool:
        cur = self._execute(
            "DELETE FROM script_scene_details WHERE project_id = ? AND scene_number = ?",
            (project_id, scene_number),
        )
        return cur.rowcount > 0

    def get_all_settings(self) -> Dict[str, Any]:
        rows = self._execute("SELECT key, value FROM settings").fetchall()
        out = {}
        for r in rows:
            v = r["value"]
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            out[r["key"]] = v
        return out
