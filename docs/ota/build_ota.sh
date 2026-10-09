#!/usr/bin/env bash
# 生成 OTA 安装用的 manifest.plist，并把 docs/ota/index.html 里的版本占位符替换掉。
#
# 用法：
#   bash docs/ota/build_ota.sh                       # 从 app.py 取版本号
#   bash docs/ota/build_ota.sh 1.0.7                 # 显式指定
#   PAGES_BASE=https://frr688.github.io/FXseek bash docs/ota/build_ota.sh
#   IPA_URL=https://example.com/x.ipa bash docs/ota/build_ota.sh
#
# 生成的 manifest.plist 要和 index.html 一起放到 GitHub Pages（docs/ 目录即可），
# IPA 本体放 GitHub Releases —— Apple 要求两者都是「HTTPS + 受信任证书」，
# 自签证书不行。GitHub Pages 和 Releases 都满足。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

BUNDLE_ID="${BUNDLE_ID:-com.frr688.fxseek}"
APP_NAME="${APP_NAME:-FXseek}"

# ---- 1. 版本号：和桌面端同源（app.py 的 "version"），保证两端号一致 ----
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  VERSION="$(grep -m1 '"version"' "$ROOT/app.py" | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
fi
if [ -z "$VERSION" ]; then
  echo "✗ 取不到版本号 —— 在 app.py 里找不到 \"version\"" >&2
  exit 1
fi

# ---- 2. 两个 URL ----
# PAGES_BASE：manifest.plist 所在的 HTTPS 根（GitHub Pages）
PAGES_BASE="${PAGES_BASE:-https://frr688.github.io/FXseek}"
MANIFEST_URL="${PAGES_BASE%/}/ota/manifest.plist"
# IPA_URL：IPA 本体的 HTTPS 地址（GitHub Releases，单附件上限 2GB）
IPA_URL="${IPA_URL:-https://github.com/FRr688/FXseek/releases/download/mobile-v${VERSION}/${APP_NAME}-${VERSION}.ipa}"

# ---- 3. 体积（本地有包就自动填，没有就留占位）----
IPA_FILE="${IPA_FILE:-$ROOT/dist/${APP_NAME}-${VERSION}.ipa}"
if [ -f "$IPA_FILE" ]; then
  BYTES="$(stat -f%z "$IPA_FILE" 2>/dev/null || stat -c%s "$IPA_FILE")"
  SIZE="$(awk -v b="$BYTES" 'BEGIN{printf "%.0f MB", b/1048576}')"
else
  BYTES="0"
  SIZE="— MB"
  echo "· 本地没找到 $IPA_FILE，体积先留占位"
fi
MIN_IOS="${MIN_IOS:-16.0}"

echo "版本    : $VERSION"
echo "BundleID: $BUNDLE_ID"
echo "manifest: $MANIFEST_URL"
echo "ipa     : $IPA_URL"
echo "体积    : $SIZE"

# ---- 4. 写 manifest.plist ----
cat > "$HERE/manifest.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>items</key>
  <array>
    <dict>
      <key>assets</key>
      <array>
        <dict>
          <key>kind</key>
          <string>software-package</string>
          <key>url</key>
          <string>${IPA_URL}</string>
        </dict>
      </array>
      <key>metadata</key>
      <dict>
        <key>bundle-identifier</key>
        <string>${BUNDLE_ID}</string>
        <key>bundle-version</key>
        <string>${VERSION}</string>
        <key>kind</key>
        <string>software</string>
        <key>title</key>
        <string>${APP_NAME}</string>
      </dict>
    </dict>
  </array>
</dict>
</plist>
PLIST

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$HERE/manifest.plist" >/dev/null && echo "✓ manifest.plist 语法 OK"
fi

# ---- 5. 把占位符替换进 index.html（每次从模板现算，可重复运行）----
TPL="$HERE/index.template.html"
PAGE="$HERE/index.html"
if [ -f "$TPL" ]; then
  sed -e "s|{{VERSION}}|$VERSION|g" \
      -e "s|{{SIZE}}|$SIZE|g" \
      -e "s|{{MIN_IOS}}|$MIN_IOS|g" \
      -e "s|{{MANIFEST_URL}}|$MANIFEST_URL|g" \
      -e "s|{{IPA_URL}}|$IPA_URL|g" \
      "$TPL" > "$PAGE"
  echo "✓ 已从 index.template.html 生成 index.html"
else
  # 没有模板就就地替换（首次之后 index.html 本身就是产物）
  sed -i '' \
    -e "s|itms-services://?action=download-manifest\&url=[^\"]*|itms-services://?action=download-manifest\&url=$MANIFEST_URL|" \
    "$PAGE" 2>/dev/null || true
  echo "· 没找到 index.template.html，跳过页面生成"
fi

echo
echo "接下来："
echo "  1) 把 ${APP_NAME}-${VERSION}.ipa 传到 GitHub Releases"
echo "  2) 把 docs/ota/ 提交推送 —— GitHub Pages 若设为「docs/ 目录」即自动发布"
echo "  3) 浏览器打开 ${PAGES_BASE%/}/ota/ 确认按钮 href 正确"
