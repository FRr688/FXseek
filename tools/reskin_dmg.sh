#!/bin/bash
# 把 dist 里已有的 dmg 换个图标再打回去 —— 只动 AppIcon.icns，app 里其他一个字节都不改。
# 这样 1.1.0（已发布）和 1.1.1（预览）能同步用上新图标，又不用把老版本代码重新构建一遍。
#
# 用法： bash tools/reskin_dmg.sh 1.1.0 1.1.1
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
NEWICON="$HERE/assets/AppIcon.icns"
[ -f "$NEWICON" ] || { echo "!! 找不到 $NEWICON"; exit 1; }

reskin() {
  local VER="$1"
  local DMG="$HERE/dist/FXseek-${VER}.dmg"
  local WORK="/tmp/dmgwork-${VER}"
  echo "===== FXseek ${VER} ====="
  [ -f "$DMG" ] || { echo "!! 找不到 $DMG"; return 1; }

  local BAK="$HERE/dist/backup/FXseek-${VER}-原图标.dmg"
  if [ ! -f "$BAK" ]; then cp "$DMG" "$BAK"; echo "    原包已备份 → $BAK"; fi

  rm -rf "$WORK"; mkdir -p "$WORK/mnt" "$WORK/stage"
  hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$WORK/mnt" >/dev/null
  cp -R "$WORK/mnt/FXseek.app" "$WORK/stage/"
  hdiutil detach "$WORK/mnt" >/dev/null

  echo "    换图标：$(shasum -a 256 "$WORK/stage/FXseek.app/Contents/Resources/AppIcon.icns" | cut -c1-16) → $(shasum -a 256 "$NEWICON" | cut -c1-16)"
  cp "$NEWICON" "$WORK/stage/FXseek.app/Contents/Resources/AppIcon.icns"
  xattr -cr "$WORK/stage/FXseek.app"
  codesign --force --deep --sign - "$WORK/stage/FXseek.app" 2>/dev/null
  codesign --verify --deep --strict "$WORK/stage/FXseek.app" && echo "    重签名 OK（ad-hoc）"
  echo "    plist 版本: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$WORK/stage/FXseek.app/Contents/Info.plist") / $(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$WORK/stage/FXseek.app/Contents/Info.plist")"

  ln -s /Applications "$WORK/stage/Applications"
  rm -f "$DMG"
  hdiutil create -volname "FXseek ${VER}" -srcfolder "$WORK/stage" -ov -format UDZO -quiet "$DMG"
  hdiutil verify "$DMG" >/dev/null 2>&1 && echo "    hdiutil verify VALID"
  du -sh "$DMG" | awk '{print "    新包体积: " $1}'
  rm -rf "$WORK"
}

for v in "$@"; do reskin "$v"; done
echo "==> 全部完成"
