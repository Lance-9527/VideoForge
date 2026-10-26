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

    #: ⚠ 保留这个类属性只为兼容（老代码可能引用 `Database._local`），
    #:   **真正的连接器在 `__init__` 里按实例建** —— 见下面的说明。
    _local = threading.local()

    def __init__(self, db_path: str = "./data/videoforge.db"):
        # ★★ 2026-09-29（真抓到的一个**数据串库** bug）：
        #
        #   原来 `_local = threading.local()` 是**类属性**，于是所有 `Database`
        #   实例**共用同一个连接缓存**。后果：在同进程里开第二个 `Database`
        #   （指向另一个库文件）时，`_get_conn()` 会命中第一个实例留下的连接，
        #   **悄悄读到第一个库的数据**。
        #
        #   实测（`tests/test_db_two_instances.py`）：
        #     先建一个临时库（空 settings），再打开**真实的** videoforge.db
        #     （2.5MB、里面有 key），`get_all_settings()` 返回**全空** ——
        #     它读的其实是那个临时库。
        #
        #   在正式运行时通常只 new 一次 Database，所以没炸；但任何"换库/第二实例"
        #   的场景（测试脚本、按 --data-dir 打开别的库、以后的多项目库）
        #   都会**静默操作错误的文件** —— 这类 bug 最贵：不报错、数据写错地方。
        self._local = threading.local()
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

        # ⚠ 这里**不要**放 _ensure_column 调用。见 _run_column_migrations()
        #   上面的说明：列迁移必须在所有 CREATE TABLE 之后跑，
        #   否则在全新安装的库上会被静默跳过。

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
        # ★★ 2026-09-27 第 51 轮（用户第 9 条）：**每个角色固有音色**。
        #    实测 `PRAGMA table_info(characters)` 里**没有任何音色列** ——
        #    配音选角（`voicecast.cast_plan`）算出来的音色只活在内存里，
        #    用户看不到、也改不了。这里先给它一个家。
        self._ensure_column("characters", "voice_id", "TEXT DEFAULT ''")
        # ★★ 2026-10-23：`characters.gender` —— **性别不该靠猜**。
        #   实测 `PRAGMA table_info(characters)` 里没有性别列（只有
        #   `reference_features.gender` 这个 JSON 内部字段），于是：
        #     · 选角只能靠「人名用字 → 描述关键词 → 保守取男声」去推；
        #     · 用户改不了，也看不到"为什么配成这个性别"。
        #   空串 = 未填写，仍走自动推断（文本猜 → LLM 判）；填了就**以卡片为准**。
        #   走 `_ensure_column` ⇒ 老库启动即迁移，与 voice_id 同一做法。
        self._ensure_column("characters", "gender", "TEXT DEFAULT ''")
        # ★★ 2026-10-21：角色/场景的「基准」标记（canonical）。
        #   为什么要有：一致性基准原来**只**用"项目内多数派"
        #   （canonical_from_shots）。但用户**主动给定**的角色
        #   （上传了参考图 / 明确写了外观）如果只出现 1 次，
        #   多数派会把它当少数派忽略 —— **用户的意图被稀释**。
        #   有了这个标记，基准优先级变成：
        #     用户给定的 canonical  >  项目内多数派  >  全局默认
        self._ensure_column("characters", "is_canonical", "INTEGER DEFAULT 0")
        self._ensure_column("scenes", "is_canonical", "INTEGER DEFAULT 0")
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

        # ──────────── 增量迁移（必须在**所有建表之后**）────────────
        #
        # ★★ 2026-09-23 修一个静默失效的迁移：
        #   `_ensure_column` 靠 `PRAGMA table_info(<table>)` 判断列在不在，
        #   而它在**表还不存在**时只会在 except 里静默 `pass`（那个 except 是
        #   为了"表还没建好时别阻塞启动"而写的）。于是：
        #     · 老库（表已存在）→ 迁移生效，一切正常
        #     · **全新安装**（表刚建）→ 迁移跑在 CREATE TABLE 之前 → 静默跳过
        #       → 新列永远不存在 → 功能报 "no such column"
        #   这个 bug 藏得很深：老库上怎么试都是好的，只有新库炸。
        #   （是我给 characters 加 element_id 时被单测抓到的。）
        #
        #   所以：把"给已有表补列"的迁移**统一挪到建表之后**。
        #   它是幂等的（列已存在就跳过），重复调用无害。
        self._run_column_migrations()

    def _run_column_migrations(self) -> None:
        """给已存在的表补新列。**必须在建表之后调用**（见上面那段说明）。"""
        # 任务
        self._ensure_column("tasks", "failed_stage",     "TEXT")
        self._ensure_column("tasks", "progress_message", "TEXT")
        self._ensure_column("tasks", "cancellable",      "INTEGER DEFAULT 1")

        # 长视频分段（见 core/longvideo.py）
        self._ensure_column("shots", "segments",         "JSON DEFAULT '[]'")
        # 白膜（previz）状态：none / running / ready / approved / rejected
        self._ensure_column("shots", "previz_status",    "TEXT DEFAULT 'none'")
        self._ensure_column("shots", "previz_path",      "TEXT")
        self._ensure_column("shots", "previz_notes",     "TEXT")
        # 白膜用的运镜。存下来是为了在"平台不支持参考视频"时能退化成
        # "把运镜写进提示词"—— 不存的话降级路径就没信息可写。
        self._ensure_column("shots", "previz_camera",    "TEXT DEFAULT ''")
        # 终版成片是否已确认（白膜确认后才允许烧钱出终版）
        self._ensure_column("shots", "final_approved",   "INTEGER DEFAULT 0")
        # ★ 2026-10-21：转镜衔接约束（chain）落库用。
        #   prev_shot_id / transition_type / must_match_first_frame_desc
        #   —— 算出来不存就等于白算，事后也没法查"这一镜为什么接不上"。
        self._ensure_column("shots", "reference_features", "JSON")
        # ★ 逐镜转场（2026-09-24）：'auto' / 'cut' / 'dissolve' / 'dip'。
        #   含义是"**进入这个镜头**的那一刀怎么接"（即与**前一个**镜头的接缝）。
        #   为什么放在镜头这一侧：接缝没有独立 id，用户在对话里没法指着
        #   一个接缝说"这里用溶解"；镜头有 id，能指。
        #   默认 'auto' = 交给 assemble 逐接缝判断（能硬切就硬切、
        #   跳变大的溶解、换场黑场）—— 这是最省钱也最好看的行为。
        self._ensure_column("shots", "transition",         "TEXT DEFAULT 'auto'")
        self._ensure_column("shots", "transition_seconds", "REAL DEFAULT 0")

        # 可灵 Element（角色一致性）。见 core/kling_element.py：
        # 可灵锁角色靠的是已注册的 element_id，不是参考图。
        self._ensure_column("characters", "element_id",     "TEXT")
        self._ensure_column("characters", "element_status", "TEXT DEFAULT ''")

    #: 时间列后缀（统一格式化用）
    _TS_SUFFIX = "_at"

    @staticmethod
    def _norm_ts(v: Any) -> Any:
        """把时间戳字符串**统一成 `YYYY-MM-DD HH:MM:SS`**。

        ★★ 2026-10-20 修（用户实机「新建的项目在侧边栏里找不到」）：
          库里同时存在两种格式 ——
            · `update_project` 写的：`2026-10-20T18:09:37.521146`（带 T、本地时间）
            · 建表默认值 `CURRENT_TIMESTAMP` 写的：`2026-10-20 14:59:14`（空格、UTC）
          SQLite 按**字符串**比大小：`'T'`(0x54) > `' '`(0x20)
          ⇒ **所有带 T 的老记录永远排在新记录前面**，
            新项目被排到列表很后面，而侧边栏只显示前 20 个
            ⇒ 用户新建完项目**根本看不到它**（他反复说"新建了找不到"）。
          这里在**读出来的那一刻**统一格式，显示与排序就都正常了。
        """
        if isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
            return v.replace("T", " ").split(".")[0][:19]
        return v

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for k, v in list(d.items()):
            if isinstance(v, str) and v.startswith(("{", "[")):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
            elif isinstance(k, str) and k.endswith(self._TS_SUFFIX):
                d[k] = self._norm_ts(v)
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
            # ★ 排序必须用**归一化后**的时间（见 _norm_ts 的说明）：直接按原始字符串
            #   排会把带 T 的老记录全排到前面，新项目沉底。
            "SELECT * FROM projects "
            "ORDER BY replace(replace(updated_at, 'T', ' '), '.', ' ') DESC, rowid DESC "
            "LIMIT ?",
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

    # ──────────── canonical（用户主动指定的基准）────────────
    #
    # ★ 2026-10-21 补：这套方法在开源子集（VideoForge-open-source v1.4.2）里
    #   就有，dev 版缺 —— 而它正是「角色一致性以用户给定的为准」这条需求的落点。
    #   dsh 的 v3 方案要把业务搬到 DSH 底座，这块必须是**已验证**的。

    def get_canonical_characters(self, project_id: str) -> List[Dict[str, Any]]:
        """用户主动给定为基准的角色（is_canonical=1）。"""
        try:
            rows = self._execute(
                "SELECT * FROM characters WHERE project_id = ? AND is_canonical = 1 "
                "ORDER BY created_at",
                (project_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        except Exception:
            return []

    def get_canonical_scenes(self, project_id: str) -> List[Dict[str, Any]]:
        """用户主动给定为基准的场景（is_canonical=1）。"""
        try:
            rows = self._execute(
                "SELECT * FROM scenes WHERE project_id = ? AND is_canonical = 1 "
                "ORDER BY created_at",
                (project_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        except Exception:
            return []

    def set_character_canonical(self, cid: str, is_canonical: bool = True
                                ) -> Optional[Dict[str, Any]]:
        """标记/取消标记某个角色为「基准角色」。"""
        try:
            self._execute("UPDATE characters SET is_canonical = ? WHERE id = ?",
                          (1 if is_canonical else 0, cid))
            self._get_conn().commit()
            return self.get_character(cid)
        except Exception:
            return None

    def set_scene_canonical(self, sid: str, is_canonical: bool = True
                            ) -> Optional[Dict[str, Any]]:
        """标记/取消标记某个场景为「基准场景」。"""
        try:
            self._execute("UPDATE scenes SET is_canonical = ? WHERE id = ?",
                          (1 if is_canonical else 0, sid))
            self._get_conn().commit()
            return self.get_scene(sid)
        except Exception:
            return None

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
        for k in ("layer2_timeline", "layer3_constraints", "character_ids",
                  "negative_reference_paths", "candidates", "segments"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k], ensure_ascii=False)
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
        for k in ("layer2_timeline", "layer3_constraints", "character_ids",
                  "negative_reference_paths", "candidates", "segments"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k], ensure_ascii=False)
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
                "SELECT * FROM tasks WHERE status = ? "
                "ORDER BY replace(created_at, 'T', ' ') DESC, rowid DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM tasks "
                "ORDER BY replace(created_at, 'T', ' ') DESC, rowid DESC LIMIT ?",
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
