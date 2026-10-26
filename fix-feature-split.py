"""特征提取再修一次：切出的片段会跨逗号（"中，豆大的黑痣"），要把别的字段带进提示词。

正确做法：切完之后按标点再切一次，**只保留含该关键词的那一段**（长度 ≥2）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
sp = Path(r'D:\VideoForge-dev\backend\core\shot_prompts.py')
text = sp.read_text(encoding='utf-8')
shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))

old = '''            piece = desc[max(0, idx - 4): idx + len(keyword) + 2].strip("，。、 ")
            # ★ 只要**长度 ≥3 且确实含关键词**的片段：旧写法会切出"左""丝"这种单字碎片
            #   （"豆大的黑痣，左，丝"），塞进提示词反而把模型带偏。
            if len(piece) >= 3 and keyword in piece and piece not in features:
                features.append(piece)'''
new = '''            window = desc[max(0, idx - 5): idx + len(keyword) + 3]
            # ★ 切出来的窗口会**跨逗号**（"中，豆大的黑痣"）⇒ 再按标点拆开，
            #   只留**含该关键词**的那一段（旧写法把"中""左""丝"这些别的字段也带进提示词）。
            piece = ""
            for chunk in re.split(r"[，。、；;\\s]+", window):
                if keyword in chunk and len(chunk) >= 2:
                    piece = chunk
                    break
            if piece and piece not in features:
                features.append(piece)'''
if old in text:
    text = text.replace(old, new, 1)
    print('特征提取已改为"按标点拆分后只留含关键词的段"')
else:
    print('未匹配')

if '\nimport re\n' not in text:
    text = text.replace('import json\nimport logging', 'import json\nimport logging\nimport re', 1)
    print('补 re 导入')

sp.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(sp), doraise=True)
print('语法 OK')
