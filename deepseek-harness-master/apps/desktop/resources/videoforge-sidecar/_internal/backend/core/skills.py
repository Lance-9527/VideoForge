# -*- coding: utf-8 -*-
"""VideoForge · Skill 注册表（渐进式加载 + 可学习）

═══════════════════════════════════════════════════════════════
为什么需要这个模块
═══════════════════════════════════════════════════════════════
VideoForge 的知识原来散在三处：硬编码在 Python 里的提示词规则、
`docs/` 下的长文档、以及我自己在对话里临时记住的偏好。
结果是**用户教过一次的东西，下一次开新会话就忘了**。

Skill 把「一套可复用的做法」变成一个**文件**：
  · 一个目录、一个 `SKILL.md`（带 YAML frontmatter 的正文）
  · 可选一个 `rules.json`（**机器可执行**的结构化规则）

═══════════════════════════════════════════════════════════════
渐进式加载（progressive disclosure）—— 这是核心设计
═══════════════════════════════════════════════════════════════
给模型看的**只有** 名字 + 一句话描述（`catalog()`）。
正文可能有 2 万字符，只有在模型明确 `use_skill("名字")` 时才加载。

理由：如果把所有 skill 正文都塞进系统提示词，几个 skill 就能吃掉上万 token，
而且大部分和当前任务无关。实测本仓两个内置 skill：目录 2 条 ≈ 300 字符，
正文合计 ≈ 3.6 万字符 —— 差 100 倍以上。

═══════════════════════════════════════════════════════════════
三个来源（同名优先级：learned > external > builtin）
═══════════════════════════════════════════════════════════════
  builtin   `backend/skills_builtin/`  —— 随程序发布，**只读**
  learned   `<data_dir>/skills/`       —— `learn_skill` 写这里，可写
  external  用户配置的目录             —— 指向自己的 skill 库

★ 为什么 builtin 必须只读：exe 升级会覆盖安装目录。
  对内置 skill 做「学习补充」时，写的是 learned 目录里的**派生副本**
  （带 `derived_from` 标记），这样升级不丢，也不会污染原始文件。

═══════════════════════════════════════════════════════════════
诚实约定
═══════════════════════════════════════════════════════════════
· 解析不出 frontmatter 的目录**不会**被静默忽略，会进 `errors` 列表并记日志 ——
  "skill 莫名消失且不报错"是最难查的一类问题。
· `rules()` 读不到 rules.json 时返回 `{}` 并在结果里带 `_missing` 说明，
  **绝不**编一套默认规则冒充是这个 skill 的规则。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("videoforge.skills")


# ──────────────────────── 常量 ────────────────────────

BUILTIN_DIR_NAME = "skills_builtin"
LEARNED_DIR_NAME = "skills"
USAGE_FILE = ".usage.json"
SKILL_FILE = "SKILL.md"
RULES_FILE = "rules.json"

# 名字限制：**禁止路径穿越**。允许中文、字母数字、下划线、连字符、点（不给用）。
_NAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff-]{0,63}$")

_SOURCE_RANK = {"learned": 3, "external": 2, "builtin": 1}


class SkillError(Exception):
    """skill 操作失败（名字非法、找不到、只读等）。"""


# ──────────────────────── frontmatter 解析 ────────────────────────

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """解析 SKILL.md 的 YAML frontmatter。

    故意**不依赖 PyYAML**（打包体积 + 少一个依赖）。只支持这里用得上的子集：
      key: value
      key: "带引号的值"
      key: [a, b, c]        行内数组
      key:                  换行缩进的列表
        - a
        - b
      key: |                块标量（多行）
        第一行
        第二行

    解析不出来就返回 ({}, 原文) —— 由调用方决定怎么办，不在这里猜。
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    data: Dict[str, Any] = {}
    cur_key: Optional[str] = None
    mode = "scalar"          # scalar | list | block
    buf: List[str] = []

    def flush() -> None:
        nonlocal cur_key, mode, buf
        if cur_key is None:
            return
        if mode == "list":
            data[cur_key] = [b.strip() for b in buf if b.strip()]
        elif mode == "block":
            data[cur_key] = "\n".join(buf).rstrip()
        elif buf:
            v = "\n".join(buf).strip()
            data[cur_key] = _unquote(v)
        cur_key, mode, buf = None, "scalar", []

    for line in raw.split("\n"):
        if not line.strip():
            continue
        stripped = line.strip()
        # 列表项
        if mode == "list" and stripped.startswith("- "):
            buf.append(stripped[2:])
            continue
        if mode == "block":
            buf.append(line)
            continue
        m2 = _KV_RE.match(line)
        if m2 and not line.startswith((" ", "\t")):
            flush()
            key, val = m2.group(1), m2.group(2).strip()
            cur_key = key
            if val in ("|", ">"):
                mode = "block"
            elif val == "":
                mode = "list"        # 可能是列表，也可能空 —— 靠后续行决定
            elif val.startswith("[") and val.endswith("]"):
                data[key] = [_unquote(x.strip()) for x in val[1:-1].split(",") if x.strip()]
                cur_key = None
            else:
                buf = [val]
                mode = "scalar"
            continue
        # 续行
        if cur_key:
            buf.append(stripped)
    flush()
    return data, body


def _unquote(s: str) -> str:
    """去掉成对的引号，并**还原转义**（与 `_yaml_str()` 对称）。

    ★ 单个从左到右的 pass（用正则），而不是几次 `.replace` —— 顺序替换会把
      `\\\\n`（字面反斜杠 + n）错解成换行。未知转义原样保留
      （所以手写的 `C:\\new` 这种不会被改写）。

    兼容性：老文件里的引号值**不含反斜杠**，这一步对它们是 no-op ✓。
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        if s[0] == '"' and "\\" in inner:
            inner = re.sub(r"\\(.)",
                           lambda m: {"n": "\n", '"': '"', "\\": "\\"}.get(
                               m.group(1), "\\" + m.group(1)),
                           inner)
        return inner
    return s


# ──────────────────────── 数据结构 ────────────────────────


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    source: str                       # builtin / learned / external
    version: str = ""
    tags: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def rules_path(self) -> Path:
        return self.dir / RULES_FILE

    @property
    def has_rules(self) -> bool:
        return self.rules_path.is_file()

    @property
    def read_only(self) -> bool:
        return self.source == "builtin"

    def brief(self) -> Dict[str, Any]:
        """给模型看的**最小**信息（渐进式加载的第一层）。"""
        return {"name": self.name, "description": self.description,
                "source": self.source, "version": self.version,
                "tags": self.tags, "has_rules": self.has_rules}

    def catalog_line(self) -> str:
        d = " ".join(self.description.split())
        if len(d) > 300:
            d = d[:297] + "..."
        extra = "，含机器规则" if self.has_rules else ""
        return "- %s：%s（来源 %s%s）" % (self.name, d, self.source, extra)


# ──────────────────────── 注册表 ────────────────────────


class SkillRegistry:
    """扫描 + 加载 + 学习 skill。

    `builtin_dir` 指向 `backend/skills_builtin`；
    `learned_dir` 指向 `<data_dir>/skills`（会被创建）。
    """

    def __init__(self, learned_dir: os.PathLike | str,
                 builtin_dir: os.PathLike | str | None = None,
                 extra_dirs: Iterable[os.PathLike | str] = ()) -> None:
        self.learned_dir = Path(learned_dir)
        if builtin_dir is None:
            found = find_builtin_dir()
            builtin_dir = found if found else _builtin_candidates()[0]
        self.builtin_dir = Path(builtin_dir)
        self.extra_dirs = [Path(p) for p in extra_dirs]
        self._skills: Dict[str, Skill] = {}
        self._errors: List[Dict[str, str]] = []
        self._usage: Dict[str, Dict[str, Any]] = {}
        self._loaded_at: float = 0.0
        self._key: Optional[Tuple[str, Tuple[str, ...]]] = None

    # ── 扫描 ──

    def refresh(self) -> "SkillRegistry":
        self._skills = {}
        self._errors = []
        self._usage = self._read_usage()
        # 先扫 rank 低的，同名高 rank 覆盖
        for src, d in (("builtin", self.builtin_dir),
                       ("external", None),
                       ("learned", self.learned_dir)):
            if src == "external":
                for e in self.extra_dirs:
                    self._scan_dir(e, "external")
                continue
            if d is None:
                continue
            self._scan_dir(d, src)
        self._loaded_at = time.time()
        logger.info("skill 注册表: %d 个 (%s)，%d 个解析失败",
                    len(self._skills),
                    ", ".join("%s=%d" % (s, sum(1 for k in self._skills.values() if k.source == s))
                              for s in ("builtin", "external", "learned")),
                    len(self._errors))
        return self

    def _scan_dir(self, root: Path, source: str) -> None:
        if not root or not Path(root).is_dir():
            return
        try:
            entries = sorted(Path(root).iterdir())
        except OSError as e:
            self._errors.append({"path": str(root), "error": "目录读不了: %s" % e})
            return
        for d in entries:
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            f = d / SKILL_FILE
            if not f.is_file():
                # 有目录没 SKILL.md —— 记下来，不静默跳过
                self._errors.append({"path": str(d), "error": "缺少 %s" % SKILL_FILE})
                continue
            try:
                sk = self._parse_skill(f, source)
            except Exception as e:                       # 解析失败必须可见
                self._errors.append({"path": str(f),
                                     "error": "%s: %s" % (type(e).__name__, e)})
                logger.warning("skill 解析失败 %s: %s", f, e)
                continue
            old = self._skills.get(sk.name)
            if old is None or _SOURCE_RANK.get(source, 0) >= _SOURCE_RANK.get(old.source, 0):
                if old is not None and old.name == sk.name and old.source != sk.source:
                    logger.info("skill %s: %s 覆盖 %s", sk.name, source, old.source)
                self._skills[sk.name] = sk

    def _parse_skill(self, f: Path, source: str) -> Skill:
        text = f.read_text(encoding="utf-8-sig")
        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or f.parent.name).strip()
        if not _NAME_RE.match(name):
            raise SkillError("skill 名不合法: %r（只允许中文/字母/数字/下划线/连字符，且不能有路径分隔符）" % name)
        desc = str(meta.get("description") or "").strip()
        if not desc:
            # 退化：取正文第一个非标题非空行
            for ln in body.split("\n"):
                s = ln.strip()
                if s and not s.startswith("#"):
                    desc = s
                    break
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return Skill(name=name, description=desc, body=body.strip() + "\n",
                     path=f, source=source, version=str(meta.get("version") or ""),
                     tags=list(tags), meta=meta)

    # ── 查询（第一层：只有名字+描述）──

    def all(self) -> List[Skill]:
        if not self._loaded_at:
            self.refresh()
        return sorted(self._skills.values(), key=lambda s: s.name)

    def names(self) -> List[str]:
        return [s.name for s in self.all()]

    def catalog(self) -> List[Dict[str, Any]]:
        return [s.brief() for s in self.all()]

    def catalog_text(self) -> str:
        """拼给模型看的目录（只有名字 + 一句话描述）。"""
        ss = self.all()
        if not ss:
            return "（当前没有任何 skill）"
        return "\n".join(s.catalog_line() for s in ss)

    def get(self, name: str) -> Optional[Skill]:
        if not self._loaded_at:
            self.refresh()
        return self._skills.get(_norm_name(name))

    def errors(self) -> List[Dict[str, str]]:
        if not self._loaded_at:
            self.refresh()
        return list(self._errors)

    # ── 查询（第二层：按需加载正文）──

    def load(self, name: str, *, record: bool = True) -> Dict[str, Any]:
        sk = self.get(name)
        if sk is None:
            near = [n for n in self.names() if _norm_name(name) in _norm_name(n)]
            msg = "没有名为 %r 的 skill。现有: %s" % (name, ", ".join(self.names()) or "无")
            if near:
                msg += "。你是不是想找: %s" % ", ".join(near)
            raise SkillError(msg)
        if record:
            self.record_use(sk.name)
        out: Dict[str, Any] = {
            "name": sk.name, "description": sk.description, "body": sk.body,
            "source": sk.source, "version": sk.version, "tags": sk.tags,
            "path": str(sk.path), "has_rules": sk.has_rules,
            "body_chars": len(sk.body),
        }
        if sk.has_rules:
            out["rules"] = self.rules(sk.name)
        return out

    def rules(self, name: str) -> Dict[str, Any]:
        """读 skill 的机器可执行规则。

        ★ 读不到就**如实说明**，绝不返回一套自造的默认值冒充是这个 skill 的规则。
        """
        sk = self.get(name)
        if sk is None:
            raise SkillError("没有名为 %r 的 skill" % name)
        p = sk.rules_path
        if not p.is_file():
            return {"_missing": "该 skill 没有 %s（只有给人/模型看的 SKILL.md 正文）" % RULES_FILE}
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as e:
            return {"_error": "%s 解析失败: %s: %s" % (RULES_FILE, type(e).__name__, e)}
        if not isinstance(data, dict):
            return {"_error": "%s 顶层不是对象，而是 %s" % (RULES_FILE, type(data).__name__)}
        return data

    # ── 学习 ──

    def learn(self, name: str, description: str, body: str, *,
              version: str = "", tags: Optional[List[str]] = None,
              rules: Optional[Dict[str, Any]] = None,
              overwrite: bool = False,
              skill_dir: Optional[str] = None) -> Dict[str, Any]:
        """新建（或覆盖）一个 learned skill。

        内置同名 skill 不会被改写 —— 会写一份 learned 副本去**遮蔽**它，
        并在 frontmatter 记 `derived_from`，这样 exe 升级时学习成果不丢。
        """
        name = _norm_name(name)
        if not _NAME_RE.match(name):
            raise SkillError(
                "skill 名不合法: %r。只允许中文/字母/数字/下划线/连字符，"
                "1-64 个字符，且不能含路径分隔符或 .." % name)
        if not str(body or "").strip():
            raise SkillError("skill 正文不能为空 —— 一个没有正文的 skill 学不到任何东西")
        description = str(description or "").strip()
        if not description:
            raise SkillError(
                "必须给一句 description：它是渐进式加载里模型**唯一**能看到的内容，"
                "没有它模型永远不知道该在什么时候用这个 skill")

        existing = self.get(name)
        if existing is not None and existing.source == "learned" and not overwrite:
            raise SkillError(
                "已经有一个学过的 skill 叫 %r（%s）。"
                "要改它请用 update_skill 追加，或把 overwrite 设为 true 整个替换。"
                % (name, existing.path))
        if existing is not None and existing.source == "builtin" and not overwrite:
            raise SkillError(
                "%r 是内置 skill。直接覆盖会写一份 learned 副本遮蔽它；"
                "如果你只是想追加经验，用 update_skill 更合适。"
                "确实要整个替换就传 overwrite=true。")

        d = self.learned_dir / name
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "description": description,
            "version": version or "1.0",
            "learned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if tags:
            meta["tags"] = list(tags)
        if existing is not None:
            meta["derived_from"] = "%s:%s" % (existing.source, existing.name)
        if skill_dir:
            meta["skill_dir"] = skill_dir
        _write_skill(d / SKILL_FILE, meta, body)
        wrote_rules = False
        if rules:
            (d / RULES_FILE).write_text(
                json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            wrote_rules = True
        self.refresh()
        logger.info("学会 skill %r（%d 字符正文%s）", name, len(body),
                    "，含规则" if wrote_rules else "")
        return {"name": name, "path": str(d / SKILL_FILE), "chars": len(body),
                "wrote_rules": wrote_rules,
                "shadowed": (existing.source if existing else None)}

    def update(self, name: str, lesson: str, *,
               description: Optional[str] = None,
               tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """给已有 skill 追加一节「实战补充」。

        内置 skill 会被复制成 learned 派生副本再追加 —— 原文件不动。
        """
        sk = self.get(name)
        if sk is None:
            raise SkillError("没有名为 %r 的 skill，没法补充。要新建请用 learn_skill" % name)
        lesson = str(lesson or "").strip()
        if not lesson:
            raise SkillError("补充内容为空 —— 没有内容的补充等于没学")

        stamp = time.strftime("%Y-%m-%d %H:%M")
        block = "\n\n## 实战补充（%s）\n\n%s\n" % (stamp, lesson)

        if sk.source == "learned":
            p = sk.path
            text = p.read_text(encoding="utf-8-sig")
            meta, body = parse_frontmatter(text)
            meta["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if tags:
                old = meta.get("tags") or []
                if isinstance(old, str):
                    old = [t.strip() for t in old.split(",") if t.strip()]
                meta["tags"] = sorted(set(list(old) + list(tags)))
            if description:
                meta["description"] = description.strip()
            _write_skill(p, meta, body.rstrip() + block)
            target = p
            created = False
        else:
            # 内置/外部 → 写 learned 派生副本
            body = sk.body.rstrip() + block
            meta = {
                "name": sk.name,
                "description": (description or sk.description).strip(),
                "version": sk.version or "1.0",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "derived_from": "%s:%s" % (sk.source, sk.name),
            }
            if tags:
                meta["tags"] = list(tags)
            elif sk.tags:
                meta["tags"] = list(sk.tags)
            d = self.learned_dir / sk.name
            d.mkdir(parents=True, exist_ok=True)
            target = d / SKILL_FILE
            _write_skill(target, meta, body)
            # 规则一起带过去，否则派生副本会突然"丢掉机器规则"
            if sk.has_rules:
                shutil.copyfile(sk.rules_path, d / RULES_FILE)
            created = True
        self.refresh()
        logger.info("skill %r 追加了 %d 字符实战补充（%s）", name, len(lesson),
                    "新建派生副本" if created else "就地追加")
        return {"name": name, "path": str(target), "created_copy": created,
                "lesson_chars": len(lesson), "total_chars": len(self.get(name).body)}

    def remove(self, name: str) -> Dict[str, Any]:
        """只删 learned 的。内置 skill 删不掉（会说明原因）。"""
        sk = self.get(name)
        if sk is None:
            raise SkillError("没有名为 %r 的 skill" % name)
        if sk.source != "learned":
            raise SkillError(
                "%r 是 %s skill，不能删（内置的随程序发布，删了升级也会回来）。"
                "只能删 learned 目录里的。" % (name, sk.source))
        shutil.rmtree(sk.dir, ignore_errors=True)
        self.refresh()
        return {"name": name, "removed": not (self.learned_dir / name).exists()}

    # ── 使用统计（"学习"的信号：哪个 skill 真被用上了）──

    def _usage_path(self) -> Path:
        return self.learned_dir / USAGE_FILE

    def _read_usage(self) -> Dict[str, Dict[str, Any]]:
        p = self._usage_path()
        if not p.is_file():
            return {}
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
            return d if isinstance(d, dict) else {}
        except Exception as e:
            logger.warning("skill 使用统计读不了（忽略，不影响功能）：%s", e)
            return {}

    def record_use(self, name: str) -> None:
        try:
            self.learned_dir.mkdir(parents=True, exist_ok=True)
            rec = self._usage.setdefault(name, {"count": 0})
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._usage_path().write_text(
                json.dumps(self._usage, ensure_ascii=False, indent=2),
                encoding="utf-8", newline="\n")
        except Exception as e:
            # ★ 统计失败绝不能影响"用 skill"这件事本身
            logger.warning("记录 skill 使用失败（忽略）：%s", e)

    def stats(self) -> Dict[str, Any]:
        out = []
        for sk in self.all():
            u = self._usage.get(sk.name) or {}
            out.append({**sk.brief(), "uses": int(u.get("count", 0)),
                        "last_used": u.get("last_used", "")})
        out.sort(key=lambda x: (-x["uses"], x["name"]))
        return {"count": len(out), "skills": out,
                "never_used": [x["name"] for x in out if not x["uses"]]}


# ──────────────────────── 工具函数 ────────────────────────


def _norm_name(s: Any) -> str:
    return str(s or "").strip()


def _yaml_str(v: Any) -> str:
    """把一个字符串写成**能被 `parse_frontmatter()` 逐字读回来**的形式。

    ★ 为什么要转义（2026-09-26 往返测试抓出来的）：
      原来只在"含 `:` / 换行 / 首尾空格"时才加引号，而且把 `"` 直接换成 `'`。
      于是下面这些值**写进去和读回来不一样**：

        · `[草稿] 还没定稿`  →  读回来是个**列表**（解析器把 `[...]` 当行内数组）
        · `| 管道开头`       →  读回来触发**块标量**模式，
                               把后面**所有 frontmatter 行**吞进这个值里 ——
                               `version` / `tags` 会**整批消失**
        · `他说"好"`         →  引号被静默改成 `'`

      现在**一律加引号并转义**（`\\` / `"` / 换行），解析器那边对称还原。
    """
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"%s"' % s


def _write_skill(path: Path, meta: Dict[str, Any], body: str) -> None:
    """写 SKILL.md：frontmatter 用简单 YAML。

    ★ 格式选择只有一条原则：**能被 `parse_frontmatter()` 逐字读回来**。

      · 标量一律走 `_yaml_str()`（加引号 + 转义）；
      · 列表写成**多行** `- 项` 形式，**不用** `[a, b]` 行内形式 ——
        行内形式遇到含逗号的项会被切错（`tags: ["a,b"]` 读回来是**两项**）。
        列表项里的换行没法放进 `- ` 行，压成空格；空项直接跳过
        （一个空标签不携带任何信息）。
    """
    lines = ["---"]
    for k, v in meta.items():
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple)):
            lines.append("%s:" % k)
            for x in v:
                item = str(x).replace("\n", " ").strip()
                if item:
                    lines.append("  - %s" % item)
        elif isinstance(v, str):
            lines.append("%s: %s" % (k, _yaml_str(v)))
        else:
            lines.append("%s: %s" % (k, v))
    lines.append("---")
    text = "\n".join(lines) + "\n\n" + body.strip() + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


# ──────────────────────── 内置 skill 目录定位 ────────────────────────


def _builtin_candidates() -> List[Path]:
    """内置 skill 目录的**所有**可能位置。

    ★ 为什么不能只写 `Path(__file__).parent.parent / "skills_builtin"`：
      PyInstaller 打包后 `__file__` 落在 `_internal/core/skills.pyc` 之类的地方，
      目录层级和源码树不一定一致（onedir / onefile 还不同）。
      本仓 `main.py` 的 `_frontend_dir()` 已经因为同一个原因写成了
      "多候选逐个试" —— 这里沿用同样的做法，别再赌层级。

      顺序按"最可能"排，找第一个真的含 SKILL.md 的。
    """
    here = Path(__file__).resolve()
    cands: List[Path] = [
        here.parent.parent / BUILTIN_DIR_NAME,            # 源码树：backend/skills_builtin
    ]
    # 打包相关：sys.executable 所在目录（exe 旁边）与 _internal 下
    try:
        exe_dir = Path(sys.executable).resolve().parent
        cands += [
            exe_dir / BUILTIN_DIR_NAME,
            exe_dir / "_internal" / BUILTIN_DIR_NAME,
            exe_dir / "backend" / BUILTIN_DIR_NAME,
        ]
    except Exception:
        pass
    # 源码树里从 core 往上找几层（应对 backend 被挪位置）
    for up in (2, 3):
        try:
            cands.append(here.parents[up] / "backend" / BUILTIN_DIR_NAME)
            cands.append(here.parents[up] / BUILTIN_DIR_NAME)
        except IndexError:
            pass
    # 当前工作目录（用户从别处启动时）
    try:
        cands.append(Path.cwd() / "backend" / BUILTIN_DIR_NAME)
        cands.append(Path.cwd() / BUILTIN_DIR_NAME)
    except Exception:
        pass
    # 去重且保序
    seen, out = set(), []
    for c in cands:
        k = str(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def find_builtin_dir() -> Optional[Path]:
    """找出真的含 skill 的那个内置目录；找不到返回 None（不编造）。"""
    for c in _builtin_candidates():
        try:
            if c.is_dir() and any((d / SKILL_FILE).is_file()
                                  for d in c.iterdir() if d.is_dir()):
                return c
        except OSError:
            continue
    return None


def builtin_rules(name: str) -> Dict[str, Any]:
    """读**内置** skill 的 rules.json（按 skill 名）。

    ★ 为什么单独一个函数（2026-10-24）
    ────────────────────────────────────────────────────────────
    `cinematic-film-style/rules.json` 里写死了它自己的规矩：

        「剧本生成与 core/video_remake.py 必须引用同一份 rules.json；
          不允许在代码里再抄一份常量。」

    而 `SkillRegistry` 的 key 依赖 `data_dir` + `extra_dirs`，各调用点算出来的
    key 不一样就会**各建各的实例**（见 `get_registry()` 的说明）—— 读内置规则
    不该受这个影响。所以这里**绕开注册表**，直接按内置目录定位：

      · 单一真相源：数值只在 rules.json 里存在一份，代码不复制常量；
      · 不依赖 data_dir：打包版 / 开发版 / 测试都能读；
      · 读不到就返回 {}，**绝不编一套默认值冒充是这个 skill 的规则**
        （与 `SkillRegistry.rules()` 的诚实约定一致）。
    """
    nm = _norm_name(name)
    root = find_builtin_dir()
    if not root:
        return {}
    try:
        for d in root.iterdir():
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            p = d / RULES_FILE
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception as e:  # noqa: BLE001
                logger.warning("内置 skill %s 的 %s 解析失败：%s", d.name, RULES_FILE, e)
                continue
            if not isinstance(data, dict):
                continue
            if _norm_name(data.get("skill")) == nm or d.name == nm:
                return data
    except OSError as e:
        logger.warning("读内置 skill 目录失败：%s", e)
    return {}


# ──────────────────────── 单例接入口 ────────────────────────


def default_learned_dir(data_dir: str) -> Path:
    """learned skill 目录：`<data_dir>/skills`。"""
    return Path(data_dir) / LEARNED_DIR_NAME


_REGISTRY: Optional[SkillRegistry] = None          # 最近一次返回的那个（保留旧引用语义）
# ★★ 按 key 各存一份：谁也不会把谁挤掉。
_REGISTRY_BY_KEY: Dict[Tuple[str, Tuple[str, ...]], SkillRegistry] = {}


def get_registry(data_dir: Optional[str] = None,
                 extra_dirs: Iterable[str] = (),
                 *, force: bool = False) -> SkillRegistry:
    """拿注册表。**按 `(learned_dir, extra_dirs)` 各存一份**，互不挤掉。

    ★★ 2026-09-26 修：原来只有**一个槽位** `_REGISTRY`，key 不匹配就整个替换。
    而不同调用方传的 `extra_dirs` **本来就不一样**（这是正常的，不是错）：

        core/agent_tools.py        传 skill_dirs 解析出来的目录
        core/agent_tools_skills.py 同上
        core/casting.py            `get_registry(data_dir)` —— 不传
        core/acting.py             `get_registry(data_dir)` —— 不传

    用户一配 `skill_dirs`，两种 key 就**交替**出现 → 每次调用都
    `SkillRegistry(...)` + `refresh()`，也就是**整棵扫盘**（要读每个
    `SKILL.md` 的 frontmatter）。而这两组调用点分别是
    "**每轮对话构建提示词**"和"**每次出角色图/分镜**"——
    等于几乎每一次操作都在全量扫盘。

    ⚠ **代价只有"重复扫盘"这一项**（2026-09-26 核实后改正）：
      我上一版在这里写过"使用次数统计会跟着重置"，**那是错的**。
      注册表没有别的可变状态 —— `_skills` / `_errors` 每次 refresh 重扫，
      而 `_usage` 是**落盘**的：`refresh()` 第一件事就是
      `self._usage = self._read_usage()`，读的是
      `<learned_dir>/.usage.json（常量 `USAGE_FILE`）`，`record_use()` 也直接写那个文件。
      **统计的唯一真相源是文件，不是对象**，所以重建不会丢统计。
      （这条值得留着：把"重建"想当然地等同于"状态清零"是错的，
        要去看那个状态到底存在哪儿。）

    现在每种 key 各留一份实例：扫盘只发生在**第一次**，
    而"改了设置要生效"也仍然成立（新 key → 新实例）。

    ⚠ 调用方注意：`extra_dirs` 请用 `extra_dirs_from_settings()` 解析，
    不要自己再写一份 —— 解析逻辑分叉只会让 key 变得更多。
    """
    global _REGISTRY
    if data_dir is None:
        data_dir = os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
    ld = default_learned_dir(str(data_dir))
    key = (str(ld), tuple(str(x) for x in extra_dirs))
    r = None if force else _REGISTRY_BY_KEY.get(key)
    if r is None:
        r = SkillRegistry(ld, extra_dirs=extra_dirs)
        r._key = key                  # type: ignore[attr-defined]
        r.refresh()
        _REGISTRY_BY_KEY[key] = r
    _REGISTRY = r                     # 旧引用语义："最近一次返回的那个"
    return r


def extra_dirs_from_settings(settings: Optional[Dict[str, Any]] = None) -> List[str]:
    """从设置里解析「额外的 skill 目录」（`skill_dirs`）。**解析只允许有这一份。**

    ★★ 为什么必须抽成一个函数（2026-09-26 修）

    原来两处调用点**各自解析**这个设置：

        core/agent_tools.py        get_registry(_d or None)            ← 根本不解析
        core/agent_tools_skills.py get_registry(d, extra_dirs=extra)   ← 自己解析

    两者算出的 `extra_dirs` 不一样 → `get_registry()` 的缓存 key 不一样。
    配合"单槽位缓存"（key 不匹配就整个替换），就变成**交替重建**：
    每次调用都 `SkillRegistry(...)` + `refresh()`（整棵扫盘）。
    实际的调用点其实有**四个**（还有 `casting.py` / `acting.py`），
    详见 `get_registry()` 的说明 —— 那里也记了最终怎么根治的
    （改成"每个 key 各存一份实例"，所以两侧不一致**不再**互相挤掉）。

    这个函数负责的仍然是"解析只能有一份"这一半：谁也不许再自己写一遍
    `settings["skill_dirs"]` 的解析 —— 否则 key 的种类只会越来越多。
    """
    s = settings or {}
    raw = s.get("skill_dirs")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("设置里的 skill_dirs 不是合法 JSON（忽略）：%s", e)
            return []
    if not isinstance(raw, list):
        logger.warning("设置里的 skill_dirs 不是数组（忽略）：%s", type(raw).__name__)
        return []
    return [str(x).strip() for x in raw if str(x).strip()]
