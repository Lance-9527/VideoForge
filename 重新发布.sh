#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# VideoForge · 删库重建后一键恢复
# ═══════════════════════════════════════════════════════════════════
# 为什么要这个脚本
#   2026-10-26 的一次误操作，把含真实 API Key 的 credentials.yaml 推到了
#   公开仓库。虽然已从 main 移除并 force push 重建了历史，但 GitHub 对
#   "被覆盖的旧 commit" 有孤立对象保留期，在 GC 之前**仍可通过 SHA 访问**。
#   彻底清除的唯一办法 = 删掉仓库重建。
#
#   本地已备份好一切：
#     · 干净历史        D:\VideoForge-dsh\.git  (master = 0d9de073)
#     · 完整源码        D:\VideoForge-dsh\
#     · Release 说明文   D:\VideoForge-dsh\_release_backup\v0.2.1-rc.2-notes.md
#     · 安装包(分享版)  D:\VideoForge-分享版-2026-10-25\01-安装包\
#
# 用法
#   1) 浏览器打开 https://github.com/Lance-9527/VideoForge/settings
#      → General → Danger Zone → Delete this repository → 确认
#   2) 回到本目录，跑：  bash 重新发布.sh
#   3) 完事（release 附件要手动重传，脚本会打印链接）
#
# 前提：GitHub 上仓库已删除，且本机还有能推代码的 token
# ════════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

REPO="Lance-9527/VideoForge"
TAG="v0.2.1-rc.2"
EXE="D:/VideoForge-分享版-2026-10-25/01-安装包/videoforge-0.2.1-rc.2-win-x64-unsigned.exe"

echo "==> 1/5 检查本地历史干净（不能含凭据文件）"
if git ls-tree -r master --name-only | grep -q "videoforge-seed/credentials.yaml"; then
  echo "  !! master 里还有凭据文件，中止"; exit 1
fi
echo "  ✓ 干净"

echo "==> 2/5 确认远程仓库已删除"
code=$(curl -s -o /dev/null -w "%{http_code}" "https://api.github.com/repos/$REPO")
if [ "$code" = "200" ]; then
  echo "  !! 仓库还在。先在网页删掉它，再跑这个脚本。"; exit 1
fi
echo "  ✓ 已删除"

echo "==> 3/5 重建仓库"
curl -sS -X POST -H "Authorization: Bearer $(git credential fill <<<'protocol=https
host=github.com' | grep '^password=' | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"VideoForge\",\"description\":\"私人 AI 短片 Agent 桌面软件 —— 用一句话把创意变成成片\",\"private\":false,\"has_issues\":true,\"has_wiki\":false}" \
  "https://api.github.com/user/repos" | head -c 200
echo ""

echo "==> 4/5 推送干净历史 + tag"
git push -u origin master:main
git tag -f "$TAG" master
git push origin "refs/tags/$TAG"

echo "==> 5/5 创建 Release 并上传分享版安装包"
TOKEN=$(git credential fill <<<'protocol=https
host=github.com' | grep '^password=' | cut -d= -f2)
REL=$(curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary "$(python -c "
import json
notes = open('_release_backup/v0.2.1-rc.2-notes.md', encoding='utf-8').read()
print(json.dumps({'tag_name':'$TAG','name':'VideoForge v0.2.1-rc.2（分享版）','body':notes,'draft':False,'prerelease':True}, ensure_ascii=False))
")" \
  "https://api.github.com/repos/$REPO/releases" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "  上传安装包（约 398MB，慢）..."
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/octet-stream" \
  --data-binary "@$EXE" \
  "https://uploads.github.com/repos/$REPO/releases/$REL/assets?name=videoforge-0.2.1-rc.2-win-x64-unsigned.exe" \
  | python -c "import sys,json; d=json.load(sys.stdin); print('  ✓', d.get('browser_download_url'))"

curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/octet-stream" \
  --data-binary "@D:/VideoForge-分享版-2026-10-25/01-安装包/校验-MD5.txt" \
  "https://uploads.github.com/repos/$REPO/releases/$REL/assets?name=checksum-MD5.txt" >/dev/null

echo ""
echo "完成： https://github.com/$REPO"
echo "提醒：4 个凭据（MiniMax×2 / DeepSeek / token6688 / 浏览器会话 secret）仍需轮换。"
