"""把关键帧暴露到界面：
① `main.py` 的 `/prompts` 响应里带上每镜的 keyframe_path（前端据此显示"用户确认的那张图"）
② 关键帧文件同步进冻结载荷
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
mp = Path(r'D:\VideoForge-dev\backend\main.py')
text = mp.read_text(encoding='utf-8')

if 'keyframe_path' in text:
    print('main.py 已带 keyframe_path')
else:
    old = """                "character_anchor": prompts.get("character_anchor") or "",
                "scene_anchor": prompts.get("scene_anchor") or "",
            })"""
    new = """                "character_anchor": prompts.get("character_anchor") or "",
                "scene_anchor": prompts.get("scene_anchor") or "",
                # ★ 写实关键帧（= 用户确认的那张图，出片时当首帧）——
                #   有它就说明这一镜已经能"所见即所得"地出片。
                "keyframe_path": _shot_keyframe_path(pid, s),
            })"""
    if old in text:
        shutil.copyfile(mp, mp.with_suffix(mp.suffix + '.bak-' + STAMP))
        text = text.replace(old, new, 1)
        # 加一个辅助函数（放在路由之前）
        helper = '''    def _shot_keyframe_path(pid: str, shot: Dict[str, Any]) -> str:
        """某一镜的写实关键帧路径（没有就空串）。"""
        try:
            from core import keyframe as _kf
            _root = str((db.get_all_settings() or {}).get("output_dir")
                        or os.path.join(os.getcwd(), "data", "outputs"))
            return _kf.existing_keyframe(_root, shot)
        except Exception:  # noqa: BLE001
            return ""

'''
        anchor = "    @app.get(\"/api/projects/{pid}/prompts\")"
        text = text.replace(anchor, helper + anchor, 1)
        mp.write_text(text, encoding='utf-8')
        print('① main.py 已带 keyframe_path')
    else:
        print('① 未匹配')

import py_compile
py_compile.compile(str(mp), doraise=True)
print('语法 OK')

# ② 同步载荷
import hashlib
SRC = Path(r'D:\VideoForge-dev\backend')
PAYLOADS = [
    Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend'),
]
FILES = ['main.py', 'core/keyframe.py', 'core/longvideo.py', 'core/shot_prompts.py', 'core/orchestrator.py', 'core/agent_tools.py', 'core/compose.py']
for payload in PAYLOADS:
    if not payload.exists():
        print('跳过', payload)
        continue
    for rel in FILES:
        src = SRC / rel
        if not src.exists():
            print('  源缺', rel)
            continue
        dst = payload / rel
        before = hashlib.md5(dst.read_bytes()).hexdigest()[:8] if dst.exists() else 'new'
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        after = hashlib.md5(dst.read_bytes()).hexdigest()[:8]
        print(f'  {rel:26} {before} -> {after}')
    for rel in FILES:
        t = payload / rel
        if t.exists():
            py_compile.compile(str(t), doraise=True)
    print('  载荷语法 OK')
