import io,sys,re
sys.stdout.reconfigure(encoding="utf-8")
p="core/stages.py"
s=io.open(p,encoding="utf-8",errors="replace").read()
i=s.find("STAGES")
# 打印 STAGES 定义块
m=re.search(r"STAGES\s*(?::[^=]*)?=\s*\[", s)
if m:
    start=m.start()
    # 找配对的右括号
    depth=0; end=start
    for j in range(m.end()-1, len(s)):
        if s[j]=='[': depth+=1
        elif s[j]==']':
            depth-=1
            if depth==0: end=j+1; break
    print(s[start:end][:4000])
else:
    print("STAGES 定义未找到；搜 Stage( 构造")
    for i,l in enumerate(s.splitlines(),1):
        if re.search(r"Stage\(|key=|cost_label", l): print(f"{i}: {l.strip()[:140]}")
