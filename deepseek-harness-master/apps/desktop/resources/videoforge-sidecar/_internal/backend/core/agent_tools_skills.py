# -*- coding: utf-8 -*-
"""VideoForge · Skill 相关的 Agent 工具（学习 / 加载 / 更新）

═══════════════════════════════════════════════════════════════
为什么要有这些工具
═══════════════════════════════════════════════════════════════
用户教过一次的做法，下一次开新会话就忘了 —— 因为那些知识只活在一轮对话里。
Skill 把「一套可复用的做法」落成文件，于是可以跨会话保留、可以被复用、
也可以被后续经验继续打磨。这就是"skill 学习"。

模型看到的**只有**名字 + 一句话描述（渐进式加载的第一层）。
真正需要某个 skill 时才 `use_skill` 把正文拉进来 —— 否则几十个 skill 的正文
会把系统提示词吃光。本仓两个内置 skill 的实测差距：
目录约 300 字符，正文合计约 3.6 万字符，**差 100 倍以上**。

═══════════════════════════════════════════════════════════════
什么时候该学一个新 skill
═══════════════════════════════════════════════════════════════
· 用户明确说"以后都这样做" / "记住这个流程" / "下次也这么干"；
· 用户纠正了你的做法，而且这个纠正**对将来的任务也成立**（不是这一次的特例）；
· 你摸索出了一个多步流程，且这个流程**会在别的项目里重复用到**。

**不要**为一次性的、只跟当前项目有关的细节建 skill（那属于项目设置，不是 skill）。
`learn_skill` 的 description 是模型唯一能看到的东西，写不好模型永远不知道该用它。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .agent_tools import REGISTRY, ToolResult, err, ok, merged

logger = logging.getLogger("videoforge.agent.tools.skills")


def _reg():
    """取 skill 注册表。

    ★ `extra_dirs` 必须走 `skills.extra_dirs_from_settings()` —— 和
      `agent_tools.build_agent_prompt()` 用**同一个**函数。
      两处各解析一遍的话，`get_registry()` 的缓存 key 会**交替变化**，
      于是每轮对话都重建注册表 + 全量扫盘，挂在注册表上的状态
      （如使用次数统计）也会跟着重置。详见那个函数的说明。
    """
    from .skills import extra_dirs_from_settings, get_registry
    from .agent_tools import _services
    _db, settings, _q = _services()
    d = str((settings or {}).get("data_dir") or "")
    return get_registry(d or None, extra_dirs=extra_dirs_from_settings(settings))


@REGISTRY.register(
    "list_skills",
    "列出可用的 skill（只有名字和一句话描述 —— 这是渐进式加载的第一层）。"
    "要用某个 skill 的完整内容，再调 `use_skill`。"
    "`stats=true` 会带上使用次数（哪个 skill 真被用上了）。",
    {"stats": {"type": "boolean", "desc": "是否带使用统计"}},
)
async def t_list_skills(args: Dict[str, Any]) -> ToolResult:
    try:
        reg = _reg()
        if args.get("stats"):
            st = reg.stats()
            return ok(count=st["count"], skills=st["skills"],
                      never_used=st["never_used"])
        sk = reg.catalog()
        out: Dict[str, Any] = {"count": len(sk), "skills": sk}
        problems = reg.errors()
        if problems:
            # ★ 坏目录必须报出来 —— "skill 莫名消失且不报错"是最难查的问题
            out["problems"] = problems[:10]
            out["problems_hint"] = ("这些目录没有被加载（缺少 SKILL.md 或名字不合法）。"
                                    "如果用户以为某个 skill 已经装好了，把这句告诉他。")
        if not sk:
            out["hint"] = ("目前一个 skill 都没有。用户教你一套可复用的做法时，"
                           "用 `learn_skill` 记下来。")
        return ok(**out)
    except Exception as e:
        logger.exception("list_skills failed")
        return err("列出 skill 失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "use_skill",
    "加载一个 skill 的**完整正文**（渐进式加载的第二层）。"
    "描述里提到适用场景时就该调它 —— 正文里通常有具体步骤、禁用项和验收标准。"
    "带 rules 的 skill 会同时返回机器可执行规则。",
    {"name": {"type": "string", "desc": "skill 名（用 list_skills 查）", "required": True}},
)
async def t_use_skill(args: Dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return err("必须给 `name`。用 `list_skills` 看有哪些。")
    try:
        from .skills import SkillError
        try:
            data = _reg().load(name)
        except SkillError as e:
            return err(str(e))
        return ok(**data)
    except Exception as e:
        logger.exception("use_skill failed")
        return err("加载 skill 失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "learn_skill",
    "把一套可复用的做法记成一个 skill，**跨会话保留**。"
    "在用户说「以后都这样做」「记住这个流程」，或纠正了你一个将来仍然成立的做法时用它。"
    "★ `description` 极其重要：它是模型唯一能看到的内容，"
    "要写成「什么时候该用这个 skill」，而不是「这个 skill 是什么」。"
    "正文里写清步骤、禁用项、验收标准 —— 写成将来能照着执行的清单，别写成日记。"
    "如果这个做法带**可自动执行的规则**（阈值、禁用词、映射表），"
    "用 `rules` 传结构化 JSON，这样代码能直接执行它而不只是读一遍。",
    {"name": {"type": "string", "desc": "skill 名（中文/字母/数字/下划线/连字符）", "required": True},
     "description": {"type": "string",
                     "desc": "一句话说明**什么时候该用**（模型唯一能看到的内容）",
                     "required": True},
     "body": {"type": "string", "desc": "skill 正文（Markdown，写清步骤与验收标准）",
              "required": True},
     "tags": {"type": "array", "desc": "标签，便于检索"},
     "version": {"type": "string", "desc": "版本号，默认 1.0"},
     "rules": {"type": "object", "desc": "可选：机器可执行的结构化规则"},
     "overwrite": {"type": "boolean", "desc": "同名时是否整个替换"}},
    writes=True,
)
async def t_learn_skill(args: Dict[str, Any]) -> ToolResult:
    try:
        from .skills import SkillError
        try:
            r = _reg().learn(
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                body=str(args.get("body") or ""),
                version=str(args.get("version") or ""),
                tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
                rules=args.get("rules") if isinstance(args.get("rules"), dict) else None,
                overwrite=bool(args.get("overwrite")),
            )
        except SkillError as e:
            return err(str(e))
        return ok(**merged(r, hint="已写入 learned 目录，下一次会话也能通过 list_skills 看到。"))
    except Exception as e:
        logger.exception("learn_skill failed")
        return err("学习 skill 失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "update_skill",
    "给一个**已有** skill 追加「实战补充」—— 用来沉淀后续经验，而不是重写整个 skill。"
    "内置 skill 不会被改写：会生成一份 learned 派生副本（含原正文和原规则）再追加，"
    "这样程序升级不丢学习成果。",
    {"name": {"type": "string", "desc": "skill 名", "required": True},
     "lesson": {"type": "string",
                "desc": "要补充的经验（写清「什么情况下不要怎么做」这类可执行结论）",
                "required": True},
     "description": {"type": "string", "desc": "可选：顺便更新那句话描述"},
     "tags": {"type": "array", "desc": "可选：追加标签"}},
    writes=True,
)
async def t_update_skill(args: Dict[str, Any]) -> ToolResult:
    try:
        from .skills import SkillError
        try:
            r = _reg().update(
                str(args.get("name") or ""),
                str(args.get("lesson") or ""),
                description=(str(args["description"]) if args.get("description") else None),
                tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            )
        except SkillError as e:
            return err(str(e))
        return ok(**merged(r, hint=("生成了派生副本（原内置文件没动）" if r.get("created_copy")
                                    else "就地追加到了 learned skill")))
    except Exception as e:
        logger.exception("update_skill failed")
        return err("补充 skill 失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "remove_skill",
    "删除一个**学过的** skill。内置 skill 删不掉（会说明原因）—— "
    "它们随程序发布，删了升级也会回来。",
    {"name": {"type": "string", "desc": "skill 名", "required": True}},
    writes=True,
)
async def t_remove_skill(args: Dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return err("必须给 `name`")
    try:
        from .skills import SkillError
        try:
            r = _reg().remove(name)
        except SkillError as e:
            return err(str(e))
        return ok(**r)
    except Exception as e:
        logger.exception("remove_skill failed")
        return err("删除 skill 失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "audit_character_image",
    "出角色图**之前**做一次体检：对着 character-casting-studio skill 的质量门，"
    "报告合规项、与剧本设定的冲突（如「抗战剧本却穿西装」）、以及数据缺口"
    "（缺五官槽位/身份锚点/服装/年龄）。返回的是**证据**，不是感觉。"
    "★ 它不阻断出图：有缺口照样能出，只是质量没保障。",
    {"name": {"type": "string",
              "desc": "角色名（在当前项目里查）", "required": True},
     "prompt": {"type": "string", "desc": "可选：已有的提示词；不传就现拼一条来体检"}},
    needs_project=True,
)
async def t_audit_character_image(args: Dict[str, Any]) -> ToolResult:
    from .agent_tools import _services
    db, _settings, _q = _services()
    pid = str(args.get("project_id") or "")
    name = str(args.get("name") or "").strip()
    if not pid or not name:
        return err("必须给 `name`，且要在有项目的上下文里调用")
    try:
        from . import casting as C
        chars = db.list_characters(pid) or []
        norm = lambda s: "".join(str(s or "").split())
        char = next((c for c in chars if norm(c.get("name")) == norm(name)), None)
        if char is None:
            return err("项目里没有叫 %r 的角色。现有：%s"
                       % (name, "、".join(str(c.get("name")) for c in chars[:12]) or "（无）"))
        ctx = C.context_from_db(db, pid)
        prompt = str(args.get("prompt") or "")
        if not prompt:
            prompt = C.build_casting_prompt(char, ctx=ctx)
        audit = C.audit(char, prompt, ctx=ctx)
        # ══════════════════════════════════════════════════════════════
        # ★★ 这里**不能**写 `ok(..., **audit)`。
        #
        # `ok(**kw)` 返回的是 `{"ok": True, **kw}`，而 `audit()` 的返回值里
        # **本身带一个 `"ok"` 键**（`not mismatches and passed == total`）。
        # Python 的字典字面量遇到重复键是**后写的覆盖先写的**，于是：
        #
        #     ok(character=..., **audit)  →  {"ok": True, "ok": False, ...}
        #                                 →  最终 "ok": False
        #
        # 后果很讽刺：**体检只要查出任何缺口或与剧本的冲突，工具就被报成
        # "调用失败"** —— 而那恰恰是这份报告最有价值的时候。
        # 审计自己的结论放在 `audit.ok` 里，与工具调用的成败分开。
        # ══════════════════════════════════════════════════════════════
        return ok(character=name, summary=C.summarize(audit),
                  prompt_chars=len(prompt), audit=audit)
    except Exception as e:
        logger.exception("audit_character_image failed")
        return err("角色图体检失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "audit_character_batch",
    "检查一批角色的**辨识度**是否真的分开了：对着 skill 的批量差异门，"
    "找出会出成「兄弟姐妹脸」的角色对（相邻角色至少要有 5 个维度不同）。"
    "也报出槽位太少的角色（填不满就没法保证一致性）。",
    {},
    needs_project=True,
)
async def t_audit_character_batch(args: Dict[str, Any]) -> ToolResult:
    from .agent_tools import _services
    db, _settings, _q = _services()
    pid = str(args.get("project_id") or "")
    if not pid:
        return err("需要在有项目的上下文里调用")
    try:
        from . import casting as C
        chars = db.list_characters(pid) or []
        if not chars:
            return err("这个项目还没有角色。先用 characters_from_script 或手动建角色。")
        ctx = C.context_from_db(db, pid)
        rep = C.batch_report(chars, ctx=ctx)
        # ★ 同一个坑（见 `t_audit_character_image` 的说明）：`batch_report()`
        #   的返回值里也带 `"ok"` 键，直接 `ok(**rep)` 会让"发现了兄弟姐妹脸"
        #   被报成"工具调用失败"。这里改名成 `batch_ok`，并保留完整报告。
        return ok(characters=rep.get("characters", 0),
                  pairs=rep.get("pairs", 0),
                  risky_pairs=rep.get("risky_pairs") or [],
                  thin_fingerprints=rep.get("thin_fingerprints") or [],
                  advice=rep.get("advice", ""),
                  batch_ok=rep.get("ok"),
                  report=rep)
    except Exception as e:
        logger.exception("audit_character_batch failed")
        return err("批量辨识度体检失败：%s: %s" % (type(e).__name__, e))
