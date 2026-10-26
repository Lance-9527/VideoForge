"""在白膜生成后导出「白膜规格说明」——插在白膜 approve 写库之后（安全位置）。"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
p = Path(r'D:\VideoForge-dev\backend\core\previz_blender.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

anchor = """            _LOG.info("白膜自动 approve：%d 镜 previz_status=draft_approved"
                      "（库里共 %d 镜）", _written, len(_ordered))"""
insert = anchor + """
            # ★ 2026-10-22：导出「白膜规格说明」（Proma/blockout 的三件套之一：
            #   白膜视频 + 每机位静帧 + 自动 prompt.txt）。
            #   没有它，"白膜到底锁定了什么"只躺在 spec.json 里 —— 用户看不到，
            #   下游出片也拿不到，等于白膜白渲。
            try:
                from core import previz_notes as _notes
                from core import previz_design as _pd
                _spec = _pd.load_spec(db, project_id)
                if _spec:
                    _notes.export_notes(_spec, _outputs_root(db), project_id)
                    _LOG.info("白膜规格说明已导出")
            except Exception as _e2:  # noqa: BLE001
                _LOG.warning("导出白膜规格说明失败（不影响白膜本体）：%s", _e2)"""

if anchor in text:
    text = text.replace(anchor, insert, 1)
    p.write_text(text, encoding='utf-8')
    print('已插入白膜规格说明导出')
else:
    print('未匹配锚点')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
