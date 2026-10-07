# FXseek 媒体库

**一个自包含的 macOS 本地素材库 —— 用自然语言、自己的声音、或一张参考图，找你硬盘里的图片 / 视频 / 音频 / 文档。**

检索、打标、转写**全部在本机完成，素材不出网**；装完即用，不需要你机器上有 Python 或任何第三方依赖
（应用自带独立运行时与内置多模态模型）。

```
「有气球的画面」        → 找到那张图片，并告诉你它为什么匹配
按住说「找狗的照片」      → 语音 → 意图 → 检索
哼一段副歌             → 声学指纹直接定位那段音频
拿一张图去搜           → 相似素材，视频直接跳到命中的那一帧
```

---

![FXseek 主界面 · 夜间主题（简体中文）](docs/demo/01-night-zh.jpg)

## 三段演示

**① 打字就能搜画面** —— 输入「狗」「针织衫」，结果带**匹配度角标**；搜索框里双击还能唤出搜索历史：
![文字检索画面演示](docs/demo/search.gif)

▶ [完整演示 · 45 秒（有声，1600×900）](../../releases/download/v1.0.2/FXseek-search-demo.mp4)

**② 界面随你** —— 白天 / 夜间主题、简体中文 / English，点一下就换；缩略图墙、列表视图、详情区随意切换：
![界面展示](docs/demo/ui-tour.gif)

▶ [完整演示 · 22 秒（有声）](../../releases/download/v1.0.2/FXseek-ui-tour.mp4)

**③ 漫步时光** —— 只留图片与视频，按列交错缓缓滚动；点任意一张，它会优雅地淡出：
![漫步时光 · 回忆长廊](docs/demo/memory-lane.gif)

▶ [完整演示 · 30 秒（有声）](../../releases/download/v1.0.2/FXseek-memory-lane.mp4)

> 完整演示视频（有声 · 1600×900）与安装包一起放在 [Releases](../../releases)；
> 动图为了加载速度压到 620px，看细节请点上面的完整视频或下面的原图。

### 静态截图（看得更清楚）

| 白天主题 · English | 漫步时光 · 回忆长廊 |
|---|---|
| ![白天主题（English）](docs/demo/02-daylight-en.jpg) | ![漫步时光](docs/demo/03-memory-lane.jpg) |

## 能做什么

| | 能力 |
|---|---|
| 🔍 **文字搜画面** | 不只搜文件名：用向量检索理解"夕阳下的水面""手心里的汗珠"这类**画面内容**，再做精排。 |
| 🎙 **语音检索** | 按住说话即搜；**哼唱**走声学指纹（Chromaprint）。指纹优先、意图分流、双路兜底。 |
| 🖼 **以图搜图 / 以音搜素材** | 给一张图或一段音频，找库里的相似素材；命中视频时封面即**命中帧**。 |
| 🗂 **30+ 格式** | 图片 8 种、视频 8 种、音频 8 种；PDF / Office / WPS / 纯文本 / 压缩包清单等文档。 |
| 🎬 **不只是找** | 缩略图墙、内置播放器、音频**歌词页**（可一键转写）、侧栏最近上传、回收站。 |
| ✨ **漫步时光** | 一键进入"回忆长廊"：只留图片与视频，按列交错缓缓滚动，点一下让某张优雅淡出。 |
| 🤖 **接给本机 AI Agent** | 内置 MCP server，DSH / Codex / Claude Code 等 6 个 Agent 一键检测与配置。 |
| 🌗 **日常体验** | 中英双语、深浅色主题（可跟随时间）、索引源管理、本地 HTTP API。 |

> 低调但重要的一点：**没有一条检索请求会离开这台机器。** 内置的 WeMM-Embedding-2B 在本地跑，
> 转写也只在你选择的本地/自建服务上跑。

## 下载（普通用户）

1. 到 [Releases](../../releases) 下载 `FXseek-1.0.5.dmg`
2. **安装前先删掉旧版**：`/Applications/FXseek.app`（直接覆盖会留下旧文件）
3. 打开 dmg，把 `FXseek.app` 拖进"应用程序"
4. 首次启动会**自动下载内置模型（约 1.8 GB）**，默认走国内镜像、失败自动换官方源，带进度条
5. 未做 Apple 开发者签名，第一次打开若被拦，请**右键 → 打开**（或到"系统设置 → 隐私与安全性"里允许）

**要求**：Apple Silicon（M 系列）+ macOS 13 或更高。已在 Apple M4 上验证。

## 从源码构建（开发者）

仓库只放源码，构建前需要自备三样东西（都已被 `.gitignore` 忽略）：
`venv/`（自包含 CPython 3.11 + 依赖）、`bin/`（ffmpeg / ffprobe / fpcalc）、`model/`（可让首次启动自动下载）。
**完整步骤与两个真实踩过的坑**见 → [docs/运行时与二进制.md](docs/运行时与二进制.md)。

```bash
bash tools/build_app.sh          # 只出 .app
bash tools/build_app.sh --dmg    # 出 .app + .dmg
hdiutil verify dist/FXseek-1.0.5.dmg     # 必须打印 VALID

# 开发模式：直接起服务，改 ui.html / settings.html 刷新即生效
./venv/cpython-3.11/bin/python3.11 app.py --port 8231

# 依赖体检（改打包瘦身列表前务必先跑，见文档里的"空壳元数据"坑）
./venv/cpython-3.11/bin/python3.11 tools/audit_deps.py
```

## 项目结构

| 路径 | 说明 |
|---|---|
| `app.py` | HTTP 服务 + Web UI + 检索 API（纯标准库；每请求从磁盘读前端，改完刷新即生效） |
| `indexer.py` / `fastsearch.py` | 索引构建 + 三级检索（文件名 → 向量 → 深度重排） |
| `embed.py` / `serve.py` | 模型加载与自检 / OpenAI 兼容的本地向量服务 |
| `voice_intent.py` / `fingerprint.py` / `recorder.py` | 语音意图判别（纯 numpy）、声学指纹、麦克风采集 |
| `model_dl.py` | 首次启动的模型下载（双源 + 断点 + 进度） |
| `mcp_server.py` / `mcp_agents.py` / `fxseek_skill/` | 把媒体库接给本机 AI Agent |
| `launcher.py` / `tray_helper.py` | 原生启动器（Mach-O，保证麦克风权限归属正确）、菜单栏 |
| `ui.html` / `settings.html` / `i18n.js` / `icons.js` | 前端（零构建、原生 JS） |
| `tools/` | 打包脚本、依赖体检、原生启动器源码、示例素材生成 |
| `docs/` | [工程手册](docs/手册.md)（设计取舍与踩坑）、[运行时与二进制](docs/运行时与二进制.md）、[演示素材](docs/demo/)（README 里的截图与动图） |

## 遇到问题？

最常见的几类故障与它们的根因、修复版本见下表；设计取舍与更细的排查过程在 [docs/手册.md](docs/手册.md)。

| 症状 | 根因 | 从哪版修好 |
|---|---|---|
| 后台「自动转写」跑着跑着就停住，**一个文件失败后整批再也不动** | 服务整体不可用（连接失败 / 504 / 超时 / `database is locked`）被当成"这个文件失败"逐个拉黑，200 个待办最后全进冷藏 | **1.0.2 / 1.0.3** |
| 本地模型（oMLX / Ollama / LM Studio）转写失败，日志里有 `504` 或超时 | 请求走了**系统代理**（dev-sidecar、公司代理、各类加速器），而模型服务就在 `127.0.0.1`；短请求没事，长音频上传会被代理自身的 60 秒超时掐断 | **1.0.2** |
| 某首歌转写**几分钟都出不来**，或结果是一大段重复的歌词 | 本地 ASR 退化复读后不会自己停，一路生成到 `max_tokens` 上限（实测单窗 130~220 秒） | **1.0.3 / 1.0.4** |
| 菜单栏图标**过一段时间就消失**，找不到了 | 探活走了系统代理 → 误判服务已死；自杀退出码是 0 而 keeper 只认 2 → 永不重启；磁盘上**上次会话遗留的命令文件被重放**（`quit`）→ 主程序自己退出 | **1.0.5** |
| 从源码跑正常，**打包成 .app 后**「测试连接」报 `No such file or directory: 'ffmpeg'` | 双击启动走 LaunchServices，PATH 只有 `/usr/bin:/bin:/usr/sbin:/sbin`；裸 `subprocess.run(["ffmpeg", …])` 找不到内置 `bin/ffmpeg` | 已改为一律用 `indexer._find_bin()` 解析出的绝对路径 |

> 诊断这类**间歇性**故障的通则：**先确认"日志里的因果是不是你以为的那个"**。
> 例如 `FXseek.log` 行首没有时间戳，就得靠相邻的 HTTP 访问行来定位时序 ——
> 我们就是这样才发现「托盘判服务已死」的那几行，上面其实紧挨着 `GET /health 200`。

## 许可（重要）

- 本项目采用 **PolyForm Noncommercial License 1.0.0**（[LICENSE](LICENSE)）：
  **个人学习 / 研究 / 业余爱好 / 慈善 / 教育 / 公共研究 / 政府**等非商业用途——免费，随便用、可以改。
- **任何商业用途都需要单独授权**（公司内部使用、随产品分发、SaaS、二次开发交付……）
  → 用途、报价与常见问题见 **[COMMERCIAL.md](COMMERCIAL.md)**。
- 内置/依赖的第三方组件与模型许可见 **[THIRD-PARTY.md](THIRD-PARTY.md)**
  （特别是 FFmpeg、Chromaprint 与内置模型的许可 —— 商用分发前请务必核对）。

## English

**FXseek** is a self-contained macOS media library for Apple Silicon: search your local photos, videos,
audio and documents by **natural language**, by **voice** (speech *or* humming), or by **reference image/audio**.
Everything — indexing, tagging, transcription — runs **on your machine**; nothing leaves it.
It ships with its own Python runtime and a built-in multimodal model (downloaded on first launch, ~1.8 GB).

Highlights: semantic search over image/video frames, Chromaprint audio fingerprinting, 30+ file formats,
built-in player with lyric/transcription view, a "Memory Lane" (漫步时光) ambient gallery mode, and a built-in
**MCP server** so local AI agents (DSH, Codex, Claude Code, …) can search your library directly.

Licensed under the **PolyForm Noncommercial License 1.0.0** — free for personal, educational, charitable,
research and government use. **Commercial use requires a separate license** — see [COMMERCIAL.md](COMMERCIAL.md).
