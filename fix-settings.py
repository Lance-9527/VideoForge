import yaml, os, shutil, time
user = os.path.expanduser(r'~\.dsh\settings.yaml.imported')
base = open(user, encoding='utf-8').read()
# 修：原文件里 `llm-pi-ai:` 紧跟在注释行末尾（缺换行），会被当成注释的一部分。
i = base.find('llm-pi-ai:')
if i > 0 and base[i-1] != '\n':
    base = base[:i] + '\n' + base[i:]
extra = '''
# ── VideoForge 产品默认值（二次开发层写的，不属于上游 DSH） ──
# ui-theme.preference: dark —— VideoForge 是暗色工作台（对齐旧版 frontend/css/main.css
#   --bg-0:#0a0a0c）；阶段卡片用的就是那套暗色 token，浅色主题下卡片发白看不清。
ui-theme:
  preference: dark
# ui-chat.transcriptView: verbose —— foldCompletedTurns=false + stepGrouping=none，
#   即「不做整轮折叠」。阶段卡片必须在本轮直接可见，不能藏在「已完成」里。
ui-chat:
  transcriptView: verbose
  collapseTiming: next-input
'''
merged = base.rstrip() + '\n' + extra
doc = yaml.safe_load(merged)          # 解析失败就在这里炸，先自证
print('PARSE OK; keys =', list(doc.keys()))
print(' llm-pi-ai =', 'llm-pi-ai' in doc, '| ui-theme =', doc.get('ui-theme'), '| ui-chat =', doc.get('ui-chat'))
targets = [
    r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-seed\settings.yaml',
    os.path.join(os.environ['LOCALAPPDATA'], 'VideoForge', 'dsh', 'settings.yaml'),
]
for t in targets:
    os.makedirs(os.path.dirname(t), exist_ok=True)
    if os.path.exists(t):
        shutil.copyfile(t, t + '.bak-' + time.strftime('%Y%m%d-%H%M%S'))
    open(t, 'w', encoding='utf-8', newline='\n').write(merged)
    # 再读一遍，自证落盘也合法
    yaml.safe_load(open(t, encoding='utf-8').read())
    print('WROTE', t, os.path.getsize(t), 'bytes, reparse OK')
