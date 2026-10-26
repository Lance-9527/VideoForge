"""把用户给的两份手册装进 VideoForge 的 skill 库（learned 目录）。

Skill 注册表约定（见 backend/core/skills.py）：
  builtin  backend/skills_builtin/  只读
  learned  <data_dir>/skills/       可写，`learn_skill` 写这里  ← 我们放这里
  external settings.skill_dirs      用户自己指的外部目录

每个 skill 是一个目录，里面有带 YAML frontmatter 的 `SKILL.md`。
邵氏手册本身已有 name/description；短剧手册只有 type/topic，缺 name/description，
所以给它补一段标准 frontmatter，正文原样保留。
"""
import os
import re
from pathlib import Path

DATA = Path(os.path.expandvars(r'%LOCALAPPDATA%\VideoForge\data'))
SKILLS = DATA / 'skills'
SKILLS.mkdir(parents=True, exist_ok=True)

SRC_SHAW = Path(r'E:\其他\xwechat_files\wxid_59zkrzsbtr3721_310c\msg\file\2026-10\邵氏电影风格制作手册.md')
SRC_DRAMA = Path(r'E:\其他\xwechat_files\wxid_59zkrzsbtr3721_310c\msg\file\2026-10\2026-10-12-resource-短剧制作-案例+公式+模板+提示词(1).md')


def strip_frontmatter(text: str) -> str:
    """去掉开头的 --- ... --- 块，返回正文。"""
    match = re.match(r'^---\r?\n.*?\r?\n---\r?\n', text, re.S)
    return text[match.end():] if match else text


def install(name: str, header: str, body: str) -> Path:
    folder = SKILLS / name
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'SKILL.md'
    target.write_text(header + body, encoding='utf-8', newline='\n')
    print(f'写入 {target}  ({len(target.read_text(encoding="utf-8"))} 字符)')
    return target


# ① 邵氏风格手册：原本就是 skill-template，frontmatter 直接可用，正文全留
shaw = SRC_SHAW.read_text(encoding='utf-8')
install('shaw-brothers-style', '', shaw)

# ② 短剧制作手册：补 name/description，正文保留
drama_raw = SRC_DRAMA.read_text(encoding='utf-8')
drama_body = strip_frontmatter(drama_raw)
drama_header = '''---
name: short-drama-formula
aliases: ["短剧公式", "竖屏短剧", "short-drama", "爆款短剧模板"]
description: 短剧/微短剧的爆款公式与可执行模板（强钩子 + 30 秒爽点 + 集尾悬念 + 角色压抑-反击 + 反派降智），含海内外案例、题材模板、以及可直接喂给视频模型的提示词库；做 1-3 分钟竖屏剧或 1-5 分钟 AI 短片时用。
type: skill-template
status: active
source: 用户提供（2026-10-12 v2）
owner: Lance (贾维)
---

'''
install('short-drama-formula', drama_header, drama_body)

# ③ 让注册表扫到（learned 目录是默认路径，这里只做自检）
print('\n当前 learned 目录下的 skill：')
for child in sorted(SKILLS.iterdir()):
    if child.is_dir() and (child / 'SKILL.md').exists():
        head = (child / 'SKILL.md').read_text(encoding='utf-8')[:400]
        name = re.search(r'^name:\s*(.+)$', head, re.M)
        print(' -', child.name, '| name =', name.group(1).strip() if name else '(缺 name!)')
