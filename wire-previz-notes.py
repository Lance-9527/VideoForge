"""在白膜渲染成功后导出「白膜规格说明」（白膜视频 + 每机位静帧 + 自动 prompt.txt 三件套的第三件）。

落点：`core/previz_blender.py` —— 它是白膜渲染的入口（写 previz_path 的地方）。
规格从 `core/previz_design.py` 产出的 JSON 取；导出文本由 `core/previz_notes.py` 负责。
"""
import re
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
p = Path(r'D:\VideoForge-dev\backend\core\previz_blender.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

# 找到写 previz_path 的那一段（渲染结果汇总处），在它前面插入导出
m = re.search(r'( *)previz_path=s\["frames"\]\[0\] if s\["frames"\] else None,', text)
if not m:
    print('未匹配 previz_path 落点')
    raise SystemExit(1)

indent = m.group(1)
insert = f'''{indent}# ★ 2026-10-22：导出「白膜规格说明」（Proma/blockout 的做法：
{indent}#   白膜视频 + 每机位静帧 + 自动 prompt.txt —— 三件套缺一不可，
{indent}#   否则"白膜锁定了什么"只躺在 JSON 里，用户看不到、下游也拿不到）。
{indent}try:
{indent}    from core import previz_notes as _notes
{indent}    _spec = None
{indent}    try:
{indent}        from core import previz_design as _pd
{indent}        _spec = _pd.load_spec(db, project_id) if hasattr(_pd, "load_spec") else None
{indent}    except Exception:  # noqa: BLE001
{indent}        _spec = None
{indent}    if _spec:
{indent}        _out = _outputs_root(db)
{indent}        _notes.export_notes(_spec, _out, project_id)
{indent}except Exception as _e:  # noqa: BLE001
{indent}    logger.warning("导出白膜规格说明失败（不影响白膜本体）：%s", _e)

'''
text = text[:m.start()] + insert + text[m.start():]
p.write_text(text, encoding='utf-8')
print('已在 previz_blender.py 插入规格说明导出')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')

# previz_design 需要一个 load_spec(db, project_id) 读回规格
pd = Path(r'D:\VideoForge-dev\backend\core\previz_design.py')
t = pd.read_text(encoding='utf-8')
if 'def load_spec(' not in t:
    helper = '''

def load_spec(db: Any, project_id: str) -> Optional[Dict[str, Any]]:
    """读回已生成的白膜规格（没有就 None）。

    ★ 与 `generate_spec` 的落盘位置一致：`<output_root>/<pid>/previz_spec.json`。
    """
    path = spec_path(db, project_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None
'''
    t = t.rstrip('\n') + '\n' + helper
    pd.write_text(t, encoding='utf-8')
    print('previz_design.load_spec 已加')
else:
    print('previz_design.load_spec 已存在')

py_compile.compile(str(pd), doraise=True)
print('语法 OK')
