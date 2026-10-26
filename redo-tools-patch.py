"""重做 agent_tools.py 的自动编译接线（先还原备份，再按原始缩进插入）。"""
import shutil
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\agent_tools.py')
bak = sorted(p.parent.glob('agent_tools.py.bak-*'))[-1]
shutil.copyfile(bak, p)
text = p.read_text(encoding='utf-8')
print('已还原自', bak.name)

old = '        return ok(\n            shots=len(shots), replaced=max(0, before) if replace else 0,'
new = '''        # ★ 2026-10-21（用户第 5 条）：分镜落库后**自动编译**三段式提示词
        #   （video_prompt / negative_prompt / audio_prompt + 角色/场景锚定），
        #   写回 layer3_constraints.prompts；编译失败只记日志，不阻塞分镜本身。
        try:
            from core.shot_prompts import compile_project_prompts as _cpp
            _n = _cpp(db, args.get("project_id") or "")
            if _n:
                logger.info("分镜提示词已编译：%d 镜", _n)
        except Exception as _e:  # noqa: BLE001
            logger.warning("分镜提示词编译失败（不影响分镜本身）：%s", _e)
        return ok(
            shots=len(shots), replaced=max(0, before) if replace else 0,'''

if old in text:
    p.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('已插入自动编译块')
else:
    print('未匹配 —— 打印 return ok( 附近内容：')
    i = text.find('return ok(', text.find('def t_plan_shots'))
    print(repr(text[i - 200:i + 200]))

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
