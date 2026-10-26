"""从正确备份还原 agent_tools.py 并重新插入自动编译块。

背景（诚实记录）：上一步用 `sorted(glob('*.bak-*'))[-1]` 取"最新备份"，
但字典序里 'bak-promptlib-...' 排在 'bak-2026...' 之后 → 还原成了 2026-09-29 的旧备份。
这里显式用 2026-10-22 那份（我打补丁前刚建的）还原，再插入。
"""
import shutil
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\agent_tools.py')
good = p.parent / 'agent_tools.py.bak-20261022-000359'
assert good.exists(), f'找不到正确备份 {good}'
print('还原自', good.name, good.stat().st_size, 'B')
shutil.copyfile(good, p)
text = p.read_text(encoding='utf-8')
print('还原后大小', len(text.encode()), 'B')

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
    print('未匹配目标 return ok(，改为在 t_plan_shots 末尾追加')
    raise SystemExit(2)

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK，最终大小', p.stat().st_size, 'B')
