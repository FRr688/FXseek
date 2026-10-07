#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。
#
# 用法：
#   export GITHUB_TOKEN=ghp_xxxxxxxx      # 经典 PAT，勾 repo 权限（令牌只走环境变量，不写进任何文件）
#   bash tools/release.sh v1.0.2          # 建 Release + 上传 dmg 与三段演示视频
#
# 可选环境变量：
#   REPO=FRr688/FXseek                    目标仓库
#   FXSEEK_PROXY=http://127.0.0.1:31188   网络代理（本机 dev-sidecar 的系统代理端口），不需要就设成空串
#   DEMO_DIR="$HOME/Desktop/FXseek演示/发布用"   演示视频所在目录
#   DRY_RUN=1                             只打印将要做什么，不真的调用 API
#
# 为什么要有这个脚本：Web 界面拖 279 MB 的 dmg 容易中途失败，而且失败后要重来；
# 这里逐个文件上传、已存在就跳过，失败重跑是幂等的。
set -euo pipefail

TAG="${1:-v1.0.2}"
NAME="${2:-FXseek ${TAG#v}}"
REPO="${REPO:-FRr688/FXseek}"
PROXY="${FXSEEK_PROXY-http://127.0.0.1:31188}"
DEMO_DIR="${DEMO_DIR:-$HOME/Downloads/CC工作区/FXseek演示/发布用}"   # 演示视频新家（2026-10 从桌面搬走）
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="https://api.github.com/repos/$REPO"

DRY="${DRY_RUN:-}"
[ -n "${GITHUB_TOKEN:-}" ] || { echo "❌ 请先 export GITHUB_TOKEN=你的PAT（勾 repo 权限）"; exit 1; }

CURL=(curl -sS --fail-with-body --max-time 3600)
[ -n "$PROXY" ] && CURL+=(-x "$PROXY")
CURL+=(-H "Authorization: Bearer $GITHUB_TOKEN"
       -H "Accept: application/vnd.github+json"
       -H "X-GitHub-Api-Version: 2022-11-28")

jget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }

echo "== 仓库 $REPO · 标签 $TAG =="

# ---------- 1. 建 Release（已存在则复用）----------
BODY_FILE="$HERE/docs/release-notes.md"
if [ -f "$BODY_FILE" ]; then BODY="$(python3 -c 'import json,sys;print(json.dumps(open(sys.argv[1],encoding="utf-8").read()))' "$BODY_FILE")"
else BODY='"首个公开版本：本机多模态素材库（文字搜画面 / 语音 / 以图搜图 / 以音搜素材 · 30+ 格式）。\n\n非商业用途免费（PolyForm Noncommercial 1.0.0），**商业用途需授权**，见 COMMERCIAL.md。"'; fi

if [ -n "$DRY" ]; then
  echo "  [dry-run] POST $API/releases  tag=$TAG name=$NAME"
  REL_JSON='{"id":0,"upload_url":"https://uploads.github.com/repos/x/y/releases/0/assets{?name,label}","assets":[]}'
else
  REL_JSON="$("${CURL[@]}" -X POST "$API/releases" -d "{\"tag_name\":\"$TAG\",\"name\":\"$NAME\",\"body\":$BODY,\"draft\":false,\"prerelease\":false}" || true)"
  if ! echo "$REL_JSON" | grep -q '"upload_url"'; then
    echo "  （Release 已存在，改为读取）"
    REL_JSON="$("${CURL[@]}" "$API/releases/tags/$TAG")"
  fi
fi
UP="$(echo "$REL_JSON" | jget "d['upload_url'].split('{')[0]")"
HTML="https://github.com/$REPO/releases/tag/$TAG"
echo "  Release: $HTML"

# ---------- 2. 上传附件（已存在同名则跳过）----------
have() { echo "$REL_JSON" | python3 -c "import json,sys;print(any(a['name']==sys.argv[1] for a in json.load(sys.stdin).get('assets',[])))" "$1"; }

up() { # $1=文件路径
  local f="$1" n; n="$(basename "$f")"
  [ -f "$f" ] || { echo "  ⏭  找不到 $f，跳过"; return; }
  if [ "$(have "$n")" = "True" ]; then echo "  ⏭  $n 已存在，跳过"; return; fi
  local mb; mb="$(echo "scale=1;$(stat -f%z "$f")/1048576" | bc)"
  echo "  ⬆  上传 $n（${mb} MB）…"
  [ -n "$DRY" ] && return
  local ct=application/octet-stream
  case "$n" in *.mp4) ct=video/mp4;; *.dmg) ct=application/x-apple-diskimage;; esac
  "${CURL[@]}" -X POST "$UP?name=$n" -H "Content-Type: $ct" --data-binary "@$f" >/dev/null
  echo "     ✅ $n"
}

up "$HERE/dist/FXseek-1.0.2.dmg"
for f in "$DEMO_DIR"/FXseek-*.mp4; do up "$f"; done

echo "== 完成 → $HTML =="
