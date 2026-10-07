#!/bin/bash
# 构建 FXseek.app —— 把项目打成能双击运行的 macOS 应用。
#
# 打包原则（重要）：
#   1. 不带 model/（1.9 GB）。模型在用户第一次打开时现下，走 model_dl.py 的双源逻辑。
#      否则 dmg 会有 1.5G+，用户下不动，也白白浪费带宽。
#   2. 不带 data/。那是数据目录，程序目录里有一份就会在启动时被当成「老位置」迁移进
#      用户数据目录 —— 等于把开发机的库和 API Key 一起发给顾客。
#   3. 不带 venv/ 里已被证明不用的包（llvmlite/scipy/onnxruntime/... 已删过）；
#      剩下的是运行时真要用的。
#   4. 带 bin/（ffmpeg/ffprobe/fpcalc）、seed/（示例素材 + 预置向量库）、vendor/（前端库）。
#
# 用法：
#   bash tools/build_app.sh            # 构建 dist/FXseek.app
#   bash tools/build_app.sh --dmg      # 构建后再打成 dist/FXseek-<版本>.dmg

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

APP_NAME="FXseek"
BUNDLE_ID="com.fxseek.media"
VERSION="$(grep -m1 '"version"' app.py | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
VERSION="${VERSION:-1.0.2}"
DIST="$HERE/dist"
APP="$DIST/$APP_NAME.app"
PY="$HERE/venv/cpython-3.11/bin/python3.11"

echo "==> 构建 ${APP_NAME}.app  (版本 $VERSION)"
echo "    项目目录: $HERE"

if [ ! -x "$PY" ]; then
  echo "!! 找不到 venv 解释器：$PY" >&2
  exit 1
fi

# ---------- 0. 前置检查：那些绝不能进包的东西 ----------
if [ -d "$HERE/data" ]; then
  echo "!! 程序目录里存在 data/ —— 这是数据目录，打进去会把开发机的库发给用户。" >&2
  echo "   请先移走或删除：$HERE/data" >&2
  exit 1
fi

# ---------- 1. 搭 .app 骨架 ----------
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ---------- 2. 拷贝运行时文件 ----------
echo "==> 复制程序文件"
RES="$APP/Contents/Resources/app"
mkdir -p "$RES"

# 2a. Python 源码与前端资源（白名单，避免漏进杂物）
for f in app.py launcher.py model_dl.py paths.py embed.py indexer.py fingerprint.py \
         fastsearch.py ai_desc.py asr_desc.py voice_intent.py net_util.py heif_support.py \
         seed.py serve.py \
         run_daemon.py mcp_server.py mcp_agents.py tray_helper.py recorder.py requirements.txt; do
  # 注：README.md 不进包 —— 运行期没人读它，仓库里留着给 GitHub 首页看就行
  [ -f "$f" ] && cp -p "$f" "$RES/"
done
for f in ui.html settings.html i18n.js icons.js agent_logos.js; do
  [ -f "$f" ] && cp -p "$f" "$RES/"
done

# 2b. 目录：bin（二进制工具）、vendor（前端库）、seed（示例素材 + 预置库）、assets（图标）
for d in bin vendor seed assets fxseek_skill; do
  [ -d "$d" ] && cp -R "$d" "$RES/"
done

# 2c. venv —— 整份搬过去。它是「可重定位运行时」（自包含 libpython），
#     里面的 python3.11 二进制用 @executable_path 找 dylib，换目录也能跑。
echo "==> 复制 Python 运行时（venv，约 500 MB，请稍等）"
mkdir -p "$RES/venv"
ditto "$HERE/venv/cpython-3.11" "$RES/venv/cpython-3.11"

# ---------- 2d. 瘦身 + 去垃圾 ----------
# (1) 字节码缓存：能省近 200 MB，运行时自己会重建
find "$RES" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
# (2) macOS 的 .DS_Store：Finder 生成的垃圾，会跟着 seed/ bin/ venv/ 一起被打进去
find "$RES" -name ".DS_Store" -delete 2>/dev/null || true
find "$RES" -name "*.pyc" -delete 2>/dev/null || true
# (3) 运行时用不到的标准库与包（实测过依赖：没有任何被 import 的包依赖它们）
#     ensurepip/ pip/ setuptools/ wheel  ← 只有开发时装依赖才用
#     idlelib/ turtledemo/ tkinter(+tcl/tk)  ← GUI 走 pywebview(Cocoa)，不碰 Tk
#     lib2to3 ← 已废弃的 2to3 工具链
#   ⚠️ 别随手往这个 rm 列表里加包！曾经有一次「清理没用上的包」把 pdfminer 连代码带走了，
#      结果 pdfplumber 直接 import 失败、PDF 文本提取静默降级到 textutil（很久没人发现，
#      因为元数据目录还在，pip 也以为装着呢）。
#      安全做法：改这个列表前，先跑一遍
#        ./venv/cpython-3.11/bin/python3.11 tools/audit_deps.py
#      它会逐个 import 真实用到的模块并报告缺什么。
PYLIB="$RES/venv/cpython-3.11/lib/python3.11"
SP="$PYLIB/site-packages"
BEFORE=$(du -sm "$RES" | cut -f1)
rm -rf "$PYLIB/ensurepip" "$PYLIB/idlelib" "$PYLIB/lib2to3" "$PYLIB/turtledemo" "$PYLIB/tkinter" \
       "$RES/venv/cpython-3.11/lib/tcl8.6" "$RES/venv/cpython-3.11/lib/tk8.6" \
       "$SP/pip" "$SP/setuptools" "$SP/wheel" "$SP/pkg_resources" \
       "$SP/_distutils_hack" "$SP/distutils-precedence.pth" 2>/dev/null || true
AFTER=$(du -sm "$RES" | cut -f1)
echo "    瘦身：${BEFORE} MB → ${AFTER} MB（省 $((BEFORE-AFTER)) MB）"

# ---------- 3. 启动器可执行文件 ----------
# ★ 必须是**原生 Mach-O**，不能是 bash 脚本：
#   TCC 把权限申请算在「责任进程」头上，脚本当 CFBundleExecutable 时责任进程是
#   /bin/bash（日志原文：Policy disallows prompt for Sub:{/bin/bash} … access to
#   kTCCServiceMicrophone denied）→ 麦克风弹窗永不出现、应用也不会出现在系统设置
#   的麦克风列表里。C 启动器（tools/exec_launcher.c）内部 execv 内置 Python。
if command -v cc >/dev/null 2>&1 && [ -f "$HERE/tools/exec_launcher.c" ]; then
  if cc -O2 -Wall -o "$APP/Contents/MacOS/$APP_NAME" "$HERE/tools/exec_launcher.c" 2>/tmp/fxseek_cc.log; then
    echo "    启动器：原生可执行文件（TCC 责任进程 = FXseek.app）"
  else
    echo "    !! C 启动器编译失败，回落 bash 脚本（麦克风授权会失效）："
    sed 's/^/       /' /tmp/fxseek_cc.log | head -5
    CC_FAILED=1
  fi
fi
if [ ! -x "$APP/Contents/MacOS/$APP_NAME" ] || [ -n "${CC_FAILED:-}" ]; then
  cat > "$APP/Contents/MacOS/$APP_NAME" <<'LAUNCH'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
PY="$DIR/venv/cpython-3.11/bin/python3.11"
cd "$DIR" || exit 1
export PYTHONPATH="$DIR"
export PYTHONUNBUFFERED=1
LOG="$HOME/Library/Logs/FXseek.log"
mkdir -p "$(dirname "$LOG")"
exec "$PY" -u launcher.py "$@" >>"$LOG" 2>&1
LAUNCH
  echo "    启动器：bash 脚本（回落）"
fi
chmod +x "$APP/Contents/MacOS/$APP_NAME"

# ---------- 4. Info.plist ----------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key><string>${APP_NAME} 媒体库</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleExecutable</key><string>${APP_NAME}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- 麦克风/相机的用途说明：macOS 要求声明，否则系统直接不给这个能力
       （实测：.app 里没有 NSMicrophoneUsageDescription 时，WKWebView 连
       navigator.mediaDevices 都不暴露，前端只能报到「undefined」）。 -->
  <key>NSMicrophoneUsageDescription</key><string>用于按声音检索素材（麦克风录音搜索）。</string>
  <key>NSCameraUsageDescription</key><string>用于拍照或按画面检索素材。</string>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHumanReadableCopyright</key><string>FR</string>
</dict>
</plist>
PLIST

# ---------- 5. 图标（有 assets/AppIcon.icns 就用，没有就算了） ----------
if [ -f "$HERE/assets/AppIcon.icns" ]; then
  cp "$HERE/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
else
  echo "    （没找到 assets/AppIcon.icns，先用系统默认图标）"
  # 退而求其次：从 png 现做一份
  if [ -f "$HERE/assets/icon.png" ]; then
    ICONSET="$DIST/AppIcon.iconset"
    rm -rf "$ICONSET"; mkdir -p "$ICONSET"
    for s in 16 32 64 128 256 512; do
      sips -z $s $s "$HERE/assets/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
      d=$((s*2))
      sips -z $d $d "$HERE/assets/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" >/dev/null 2>&1 || true
    rm -rf "$ICONSET"
  fi
fi

# ---------- 6. 清掉扩展属性 + 就地签名（未签名的话 Gatekeeper 会直接拦下） ----------
echo "==> 清理扩展属性"
xattr -cr "$APP" 2>/dev/null || true

echo "==> 临时签名"
codesign --force --deep --sign - "$APP" 2>/dev/null && echo "    已签名（ad-hoc）" || echo "    !! 签名失败，用户首次打开可能要在「隐私与安全性」里放行"

# ---------- 7. 体积概览 ----------
echo "==> 完成：$APP"
du -sh "$APP" | awk '{print "    .app 体积: " $1}'

# ---------- 8. 可选：打 dmg ----------
if [ "${1:-}" = "--dmg" ]; then
  echo "==> 打 dmg"
  DMG="$DIST/${APP_NAME}-${VERSION}.dmg"
  rm -f "$DMG"
  STAGE="$DIST/dmgstage"
  rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "${APP_NAME} ${VERSION}" -srcfolder "$STAGE" \
    -ov -format UDZO -quiet "$DMG"
  rm -rf "$STAGE"
  du -sh "$DMG" | awk '{print "    dmg 体积: " $1}'
  echo "==> dmg: $DMG"
fi

echo "==> 全部完成"
