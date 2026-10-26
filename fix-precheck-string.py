"""修 shot_chain.py 里被插坏的字符串（多了一个引号+换行）。"""
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\shot_chain.py')
text = p.read_text(encoding='utf-8')
bad = """            "· 判为冲突：首帧里根本没有的东西却被要求出现（如首帧手里空着却要'
            '拿起杯子）、方向互斥（首帧背对镜头却要正对镜头说话）、\""""
good = """            "· 判为冲突：首帧里根本没有的东西却被要求出现（如首帧手里空着却要拿起杯子）、"
            "方向互斥（首帧背对镜头却要正对镜头说话）、\""""
if bad in text:
    text = text.replace(bad, good, 1)
    p.write_text(text, encoding='utf-8')
    print('已修字符串')
else:
    print('未匹配（用逐行修）')
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if '首帧里根本没有的东西却被要求出现（如首帧手里空着却要' in line:
            lines[i] = '            "· 判为冲突：首帧里根本没有的东西却被要求出现（如首帧手里空着却要拿起杯子）、"'
            if i + 1 < len(lines) and lines[i + 1].strip().startswith("'拿起杯子）"):
                lines[i + 1] = '            "方向互斥（首帧背对镜头却要正对镜头说话）、"'
            p.write_text('\n'.join(lines), encoding='utf-8')
            print('已逐行修')
            break

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
