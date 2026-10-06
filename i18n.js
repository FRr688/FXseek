/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 * Copyright (c) 2026 FR. All rights reserved.
 * 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。
 */
/* ============================================================================
 * FXseek 媒体库 —— 界面语言（简体中文 / English）
 *
 * 设计：**词典 + DOM 翻译器**，不改调用点。
 *   - 界面上的静态文案（HTML 里写死的、以及 JS 用模板字符串拼出来的）
 *     统一由 `apply(root)` 遍历文本节点与 title/placeholder/aria-label/alt
 *     属性来翻译。这样 ui.html / settings.html 里上百处 innerHTML 拼接
 *     一个字都不用动，改词条也只改这一处。
 *   - JS 里少数「先查后拼」的逻辑需要拿到**当前语言**做分支时，用 `T(zh, en)`。
 *
 * 两个必须守住的点：
 *   1. **幂等**。apply() 可以对同一棵子树反复跑（语言切换、DOM 更新都会再跑），
 *      所以翻译前必须能拿到「原文」。用 WeakMap 记住每个节点上一次被翻译前的
 *      文本，永远从原文出发；否则英译中再译英会越滚越长。
 *   2. **跳过用户数据**。日志、文件名、路径、搜索词这些是用户自己的内容，
 *      翻了就是灾难。靠 `SKIP_SEL` 与 `data-no-i18n` 挡住。
 * ==========================================================================*/
(function () {
  'use strict';

  var ZH2EN =   {
    "开启后，编码助手（DSH、Codex、Claude Code 等）就能用自然语言直接检索你的素材，拿到文件路径后自行打开、整理或加工。检索仍在本机完成，数据不出门。": "Once enabled, coding agents (DSH, Codex, Claude Code, and others) can search your assets in natural language, then open, organize, or process the files themselves once they have the paths. All searching still happens on this Mac — nothing leaves your machine.",
    "💡 MCP 走 stdio：每个 Agent 自己拉起一个轻量子进程，它再回连本服务取结果， 所以这里不需要额外的网络端口，也不用常驻。": "💡 MCP uses stdio: each agent spawns its own lightweight subprocess, which connects back to this service for results. No extra network port is needed, and nothing has to stay running.",
    "「失效记录」指索引过、但文件已经被删除或移走的条目。应用启动时会自动清理一次；不属于任何索引源的条目只报告、不自动删除。": "“Stale records” are entries that were indexed but whose files have since been deleted or moved. They are cleaned up automatically once at app launch; entries that don’t belong to any index source are only reported, never removed automatically.",
    "用官方服务商：选「服务商」→ 只粘 API Key 就行。 用本机 oMLX：服务商选「自定义 / 本地」，地址填": "With an official provider: choose a Provider → just paste your API Key. With local oMLX: choose “Custom / Local” as the provider and enter the address",
    "， 模型点右侧「获取列表」选一个即可，只要是支持转写的 ASR 模型都行（如": ", then click “Fetch List” to pick one — any transcription-capable ASR model works (e.g. ",
    "本地多模态语义素材库 —— 用自然语言按画面内容找到本机的图片、视频、音频和文档。检索全程在本机完成，不联网。": "A local multimodal semantic media library — find images, videos, audio, and documents on this Mac by describing what’s in them. Everything runs on-device, fully offline.",
    "自动模式会在本地时间 07:00–19:00 使用白天主题，其余时间使用夜晚主题；字号调整不影响资产卡片尺寸。": "Auto mode uses the light theme between 07:00–19:00 local time and the dark theme the rest of the day; changing the font size does not affect asset card size.",
    "官方服务商：选好服务商 → 只粘 API Key。本机 oMLX：服务商选「自定义 / 本地」， 地址": "Official provider: pick a provider → just paste your API Key. Local oMLX: choose “Custom / Local” as the provider, then set the address",
    "缩略图、视频首帧和音频波形保存在本机，用于加快再次浏览。清理后会按需重新生成，不影响原始资产。": "Thumbnails, video first frames, and audio waveforms are stored locally to make browsing faster. Clearing them regenerates them on demand — your original assets are untouched.",
    "向量库、指纹库、设置与预览缓存都保存在这里，和程序本身分开；升级或重装应用不会影响你的数据。": "The vector store, fingerprint store, settings, and preview cache all live here, separate from the app itself; upgrading or reinstalling the app won’t affect your data.",
    "）写进各家的 skills 目录 —— 那份说明书写清了使用时机、查询怎么写、结果怎么读。": ") into each agent’s skills directory — that document explains when to use it, how to write queries, and how to read the results.",
    "如果这个项目对你有帮助，欢迎扫码请作者喝杯咖啡。完全自愿，不影响任何功能，也不影响更新。": "If this project has been useful to you, scan the code to buy the author a coffee. It’s entirely optional — it doesn’t affect any features or updates.",
    "：文件名命中直接返回（&lt;1ms），否则向量检索，必要时才加载 WeMM 深度模型。": ": filename hits are returned immediately (&lt;1ms), otherwise it falls back to vector search and only loads the WeMM deep model when necessary.",
    "：资产、路径、文件名和标签都不会离开这台机器。只有你主动导出的配置备份会写到你选的位置。": ": assets, paths, filenames, and tags never leave this machine. The only thing written elsewhere is a config backup you export yourself.",
    "当前版本不上传任何资产、路径、文件名或标签。只有你主动导出的配置备份会写入所选位置。": "This version never uploads your assets, paths, filenames, or tags. The only file written outside the app is the config backup you export yourself.",
    "三级递进检索，够用即止：简单查询不调用向量模型，大幅降低 CPU/GPU 占用。": "Three-tier cascading retrieval that stops as soon as it has an answer: simple queries never invoke the vector model, greatly reducing CPU/GPU load.",
    "本应用内置模型、推理运行时与 ffmpeg，目标机器无需额外安装任何依赖。": "The app bundles its own models, inference runtime, and ffmpeg, so the target machine needs no extra dependencies.",
    "把音频里说的话 / 唱的歌词转成文字写进索引 —— 之后按内容就能搜到。": "Transcribe speech and lyrics in your audio into text in the index — then you can search it by content.",
    "自动扫描常见 Agent 的配置目录。已接入的会亮起来，未安装的会标灰。": "Automatically scans common agent config directories. Detected integrations light up; ones that aren’t installed stay greyed out.",
    "所有向量计算均在本机 Apple Silicon 上完成，无网络请求。": "All vector computation runs on this Mac’s Apple Silicon. No network requests.",
    "模型只在首次需要时加载，并常驻复用；文件名类查询完全不会触发模型加载。": "Models are loaded only on first use and then kept resident for reuse; filename queries never trigger model loading at all.",
    "记录索引、检索、删除等关键操作，便于排查问题；仅保留最近 200 条。": "Logs key operations such as indexing, searching, and deleting to help with troubleshooting; only the most recent 200 entries are kept.",
    "二维码是随应用一起打包的本地图片，打开这一页不会产生任何网络请求。": "The QR code is a local image bundled with the app — opening this page makes no network requests.",
    "关闭时会从各 Agent 的配置里摘掉接入条目（改动前都会备份）。": "When turned off, the integration entries are removed from each agent’s config (a backup is taken before any change).",
    "全部是只读操作。删除、导入、改设置这些都不会开放给 Agent。": "All operations are read-only. Deleting, importing, and changing settings are never exposed to agents.",
    "给图片生成文字描述与标签写进索引 —— 之后用文字就能搜到画面。": "Generate text descriptions and tags for your images and write them into the index — then you can find visuals by typing words.",
    "📘 光有 MCP，Agent 只知道「有这么几个工具」，不知道": "📘 With MCP alone, an agent only knows “these tools exist” — it doesn’t know ",
    "备份索引记录、设置、回收站与缓存配置；不会复制原始资产文件。": "Backs up index records, settings, the recycle bin, and cache configuration; original asset files are not copied.",
    "的模型，名字里常带 vl / vision / 4v": "models, whose names often contain vl / vision / 4v",
    "：查询扩展 + 多指令融合，精度最高但最耗资源。": ": query expansion + multi-instruction fusion — highest accuracy, highest resource use.",
    "（你可能自己改过），关掉 MCP 也不会删它。": "(you may have edited it yourself); turning off MCP won’t delete it.",
    "还没有描述/标签的图片视频，与还没转写的音频": "Images and videos without descriptions/tags yet, plus audio that hasn’t been transcribed",
    "：复用缓存的查询向量做矩阵检索，不重新编码。": ": reuses cached query vectors for matrix search instead of re-encoding.",
    "决定模型输出哪些字段；改坏了清空即恢复默认": "Controls which fields the model outputs; if you break it, clear the box to restore defaults",
    "：只按名称匹配，零模型调用（最省资源）。": ": matches names only, zero model calls (the lightest option).",
    "一段空闲里最多处理几个（0 = 不限）": "How many to process at most per idle window (0 = unlimited)",
    "管理本机配置、数据安全和当前版本信息。": "Manage local configuration, data safety, and current version info.",
    "，再点「获取列表」挑一个能看图的模型。": ", then click “Fetch List” and pick a model that can see images.",
    "单个视频最多抽多少帧（控制索引体积）": "Maximum frames extracted per video (controls index size)",
    "超过该秒数的音频跳过（0 = 不限）": "Skip audio longer than this many seconds (0 = unlimited)",
    "闲时逐个转写音频与视频里的说话/歌词": "Transcribe speech/lyrics in audio and video one by one while idle",
    "把媒体库接入本机 AI Agent": "Connect your library to local AI agents",
    "设置与备份 · FXseek媒体库": "Settings &amp; backup · FXseek Media Library",
    "。 所以接入时会顺手把配套技能（": ". So during setup it also installs the companion skill (",
    "你不用电脑的间隙，后台一次补一个": "one at a time in the background while you’re away from the computer",
    "立刻逐个补完，不用等上面那个门槛": "finish them all one by one right away, without waiting for the threshold above",
    "在 GitHub 上 Star": "Star on GitHub",
    "在 GitHub 上打开项目页": "Open the project page on GitHub",
    "开源不易，一颗星就是最大的鼓励": "Open source isn’t easy — a star is the biggest encouragement",
    "服务商后台生成；本地服务留空": "Generate one in your provider’s dashboard; leave blank for local services",
    "本地 / 自建服务的地址，以": "Address of your local / self-hosted service, starting with",
    "选中后自动带出地址与推荐模型": "Selecting it auto-fills the address and recommended model",
    "Agent 能用到的能力": "Capabilities available to agents",
    "本机检测到的 Agent": "Agents detected on this Mac",
    "连续多少分钟没操作才动手": "Minutes of inactivity before it starts",
    "需支持 OpenAI 的": "Must support OpenAI’s",
    "已存在的技能不会被覆盖": "Existing skills are never overwritten",
    "每个素材最多取几个标签": "Maximum tags per asset",
    "FXseek 媒体库": "FXseek Media Library",
    "从服务商拉取全部模型": "Fetch all models from provider",
    "显示 / 隐藏密钥": "Show / hide key",
    "留空 = 自动检测": "Leave empty = auto-detect",
    "音频转写（ASR）": "Audio transcription (ASR)",
    "去点个 Star": "Star the repo",
    "立即清理失效记录": "Clear stale records now",
    "AI 描述生成": "AI description generation",
    "显示或隐藏密钥": "Show or hide the API key",
    "本地服务可留空": "Optional for local services",
    "缓存：计算中…": "Cache: calculating…",
    "闲置时转写音频": "Transcribe audio when idle",
    "API 地址": "API URL",
    "MCP 服务": "MCP service",
    "↻ 重新统计": "↻ Recount",
    "什么时候该用": "when to use them",
    "保存性能设置": "Save performance settings",
    "复制诊断信息": "Copy diagnostics",
    "平衡（推荐）": "Balanced (recommended)",
    "支付宝赞赏码": "Alipay tip QR code",
    "数据存储位置": "Data storage location",
    "查看缓存状态": "View cache status",
    "检查失效记录": "Check stale records",
    "自动（推荐）": "Auto (recommended)",
    "重新注入技能": "Re-inject skill",
    "闲置时打标签": "Tag when idle",
    "待处理素材": "Pending assets",
    "微信赞赏码": "WeChat tip QR code",
    "性能与环境": "Performance & environment",
    "搜索设置…": "Search settings…",
    "支付宝赞赏": "Tip via Alipay",
    "数据与安全": "Data & security",
    "显示与外观": "Display & appearance",
    "等不及空闲": "Don't wait for idle",
    "设置与备份": "Settings & backup",
    "， 模型填": ", for model enter ",
    "保存配置": "Save",
    "全部接入": "Connect all",
    "全部断开": "Disconnect all",
    "匹配门槛": "Match threshold",
    "后台补全": "Backfill in background",
    "字体大小": "Font size",
    "导出配置": "Export",
    "帧数上限": "Max frames",
    "微信赞赏": "Tip via WeChat",
    "恢复配置": "Restore",
    "打标偏好": "Tagging preferences",
    "推理引擎": "Inference engine",
    "（未启用）": " (disabled)",
    "操作日志": "Activity log",
    "数据目录": "Data directory",
    "时长上限": "Max duration",
    "智能服务": "AI services",
    "暂无日志": "No log entries",
    "本地优先": "Local first",
    "本地规模": "Local scale",
    "构建时间": "Build time",
    "标签上限": "Max tags",
    "检查更新": "Check for updates",
    "检查失败：读不到版本信息。": "Check failed: couldn’t read version info.",
    "复制失败：读不到版本信息。": "Copy failed: couldn’t read version info.",
    "诊断信息已复制到剪贴板。": "Diagnostics copied to the clipboard.",
    "已弹出诊断信息，请手动复制。": "Diagnostics dialog shown — copy it manually.",
    "数据目录读取中…": "Loading data directory…",
    "未知": "Unknown",
    "读取失败": "Read failed",
    "检测中…": "Detecting…",
    "检索性能": "Search performance",
    "每轮上限": "Per-round limit",
    "测试连接": "Test connection",
    "深度语义": "Deep semantic",
    "清理缓存": "Clear cache",
    "清空日志": "Clear logs",
    "界面主题": "Theme",
    "界面动效": "Motion",
    "界面语言": "Language",
    "简体中文": "Simplified Chinese",
  " 简体中文：界面文案按中文显示。": " Simplified Chinese: interface copy is shown in Chinese.",
    "自动检测": "Auto-detect",
    "获取列表": "Fetch list",
    "补全标签": "Fill in tags",
    "补全转写": "Fill in transcription",
    "视频抽帧": "Video frame extraction",
    "读取中…": "Loading…",
    "赞赏支持": "Support us",
    "跟随系统": "Follow system",
    "运行平台": "Platform",
    "运行环境": "Runtime",
    "返回首页": "Back to home",
    "配置备份": "Config backup",
    "重新检测": "Rescan",
    "闲置门槛": "Idle threshold",
    "预览缓存": "Preview cache",
    "默认模式": "Default mode",
    "T 中": "T Medium",
    "T 大": "T Large",
    "T 小": "T Small",
    "即可。": "is all you need.",
    "媒体库": "Library",
    "多模态快速检索智能管家": "Multimodal Fast Retrieval Agent",
    "必须是": "must be",
    "提示词": "Prompt",
    "图片提示词": "Image prompt",
    "视频提示词": "Video prompt",
    "单张图片打标用；决定模型输出哪些字段，清空即恢复默认": "For tagging single images; controls which fields the model outputs, clear to restore the default",
    "视频抽帧拼成九宫格后打标用；清空即恢复默认": "For tagging videos after keyframes are stitched into a grid; clear to restore the default",
    "文件名": "Filename",
    "服务商": "Provider",
    "能看图": "Can see images",
    "严格": "Strict",
    "停止": "Stop",
    "关于": "About",
    "关闭": "Close",
    "刷新": "Refresh",
    "向量": "Vector",
    "外观": "Appearance",
    "夜晚": "Dark",
    "完整": "Full",
    "宽松": "Lenient",
    "密集": "Dense",
    "开启": "On",
    "最严": "Strictest",
    "模型": "Model",
    "白天": "Light",
    "稀疏": "Sparse",
    "结尾": "Ending",
    "自动": "Auto",
    "语言": "Language",
    "隐私": "Privacy",
    "高级": "Advanced",
    "选择一个文件夹，下一步会先统计里面有多少文件、大约要跑多久，再由你决定是现在索引还是稍后索引。": "Pick a folder. Next we'll count the files inside and estimate how long indexing takes, then you decide whether to index now or later.",
    "选择「稍后」不会丢失任何东西：来源会留在左侧列表（标注「未索引」）， 里面的文件": "Choosing “Later” loses nothing: the source stays in the left list (marked “Not indexed”), and its files",
    "或手动输入路径，如 ~/Pictures": "or type a path manually, e.g. ~/Pictures",
    "查看所有已删除的文件；右键可全部还原/彻底删除": "See all deleted files; right-click to restore or permanently delete them all",
    "添加索引源：选一个文件夹，把里面的素材索引进来": "Add an index source: pick a folder to index the assets inside",
    "目录已添加。建立索引后里面的文件才能被搜索到。": "Folder added. Its files become searchable after indexing.",
    "搜索全部素材… 试试「狗」「夕阳」「财报」": "Search all assets… try “dog”, “sunset”, “earnings”",
    "FXseek媒体库 · 统一资产": "FXseek Media Library · All assets",
    "以图搜图（上传图片找相似素材）": "Search by image (upload a picture to find similar assets)",    "深度语义检索（更准但更耗资源）": "Deep semantic search (more accurate, more resource-intensive)",
    "；随时点侧栏那个徽标就能开始。": "; click the badge in the sidebar anytime to start.",
    "以音搜素材（上传音频片段）": "Search by audio (upload an audio clip)",
    "刷新索引（扫描新增文件）": "Refresh index (scan for new files)",
    "是否立即建立索引？": "Build the index now?",
    "照样能浏览、能打开": "You can still browse and open them",
    "折叠/展开详情区": "Collapse/expand details",
    "开始/停止录音": "Start/stop recording",
    "按格式细分筛选": "Filter by format",
    "正在建立索引…": "Building index…",
    "麦克风录音检索": "Voice search",
    "添加素材目录": "Add media folder",
    "清除全部记录": "Clear all records",
    "用文字搜不到": "Can't find by text",
    "立即建立索引": "Index now",
    "选择一个素材": "Select an asset",
    "＋ 添加素材": "＋ Add asset",
    "临时垃圾桶": "Recycle bin",
    "清空搜索框": "Clear search",
    "稍后再索引": "Index later",
    "选择文件夹": "Choose folder",
    "，只是暂时": ", just temporarily",
    "全部素材": "All assets",
    "准备中…": "Preparing…",
    "列表视图": "List view",
    "取消录音": "Cancel recording",
    "取消搜索": "Cancel search",
    "排序字段": "Sort by",
    "排序方向": "Sort direction",
    "最近上传": "Recent uploads",
    "最近删除": "Recently deleted",
    "查看详情": "View details",
    "网格视图": "Grid view",
    "下一步": "Next",
    "搜画面": "Search by image",
    "索引源": "Index sources",
    "试播放": "Preview",
    "全部": "All",
    "图片": "Images",
    "文档": "Documents",
    "格式": "Format",
    "添加": "Add",
    "视频": "Videos",
    "音频": "Audio",
    "添加素材": "Add asset",
  "完整：无论系统设置如何，始终播放完整动画（旋转、脉冲、光晕扩散）。": "Full: always play the complete animation (spin, pulse, glow spread), regardless of system settings.",
  /* ANIM_HINT 的另外两档。之前只收了「完整」这一条，于是设置页在
     「跟随系统 / 关闭」两档下，提示行一直是中文（用户截图报的就是这两条）。
     这三条都是 #animHint 里「图标 + 一句话」的整行文本，图标那段
     被 renderStaticIcons 换成 <svg>，所以真正翻到的是 svg 后面那个
     文本节点 —— 开头带一个空格，故下面还各留一条带前导空格的版本。 */
  "跟随系统：系统开启「减弱动态效果」时，按钮动效降级为轻微的淡入缩放；否则播放完整动画。": "Follow system: when “Reduce motion” is on in macOS, button animations are reduced to a subtle fade-and-scale; otherwise the full animation plays.",
  "关闭：不播放任何按钮动效。": "Off: no button animations at all.",
  "关闭窗口行为": "Close window behavior",
  "关闭时": "On close",
  "每次询问": "Ask each time",
  "最小化到菜单栏": "Minimize to menu bar",
  "直接退出": "Quit",
  "点窗口左上角的关闭按钮（或 Cmd+W）时做什么。之前在关闭弹窗里勾过「记住我的选择」的，可在这里随时改回。": "What happens when you click the close button in the window’s top-left corner (or press Cmd+W). If you previously ticked “Remember my choice” in the close dialog, you can change it back here anytime.",
  /* CLOSE_HINT 三档：都是 #closeHint 里「图标 + 一句话」的整行文本，
     图标段被 renderStaticIcons 换成 <svg>，真正翻到的是 svg 后面带前导
     空格的文本节点，故各留一条带前导空格的版本。 */
  " 每次询问：点关闭时弹窗，让你选「退出」还是「最小化到菜单栏」。": " Ask each time: a dialog appears when you close, letting you choose Quit or Minimize to menu bar.",
  " 最小化到菜单栏：点关闭直接收起，后台继续待命，菜单栏图标随时唤回。": " Minimize to menu bar: closing hides the window while the app keeps working in the background — the menu bar icon brings it back anytime.",
  " 直接退出：点关闭立刻结束程序与后台服务。": " Quit: closing immediately ends the app and its background service.",
  "本地资产：": "Local assets: ",
  /* 这一条只翻到「个文件 · 向量」为止，不带 vectors —— 紧跟其后的
     「条」碎片（数字被 <b> 隔开时它单独成一个文本节点）负责补上
     「vectors」。两处都写就会拼出「vectors 57 vectors」。 */
  "个文件 · 向量": "files ·",
  "条": " vectors",
  "索引来源：": "Index sources: ",
  "种类型": " types",
  "位置：": "Location: ",
  "用户数据目录": "User data directory",
  "向量库": "Vector store",
  "· 指纹库": "· Fingerprint store",
  "· 预览缓存": "· Preview cache",
  "· 搜索记录": "· Search history",
  "搜索记录": "Search history",
  "自己填接口地址，本地服务一般不用 Key（地址要以 /v1 结尾）": "Enter the endpoint URL yourself; local services usually need no key (URL must end with /v1)",
  /* AI / ASR 渠道的说明小字（settings.html 的 AI_PROVIDERS / ASR_PROVIDERS note 字段） */
  "platform.openai.com 申请 Key · gpt-4o-mini 能看图又便宜": "Get a key at platform.openai.com · gpt-4o-mini sees images and is cheap",
  "aistudio.google.com 申请 Key · gemini-2.0-flash 有免费额度": "Get a key at aistudio.google.com · gemini-2.0-flash has a free tier",
  "bailian.console.aliyun.com 申请 Key · 通义千问 VL 系列能看图": "Get a key at bailian.console.aliyun.com · the Qwen-VL series sees images",
  "bigmodel.cn 申请 Key · glm-4v-flash 免费档就够用": "Get a key at bigmodel.cn · the free glm-4v-flash tier is enough",
  "platform.moonshot.cn 申请 Key · 选带 vision 的型号": "Get a key at platform.moonshot.cn · pick a vision-capable model",
  "siliconflow.cn 申请 Key · 聚合了不少开源视觉模型": "Get a key at siliconflow.cn · aggregates plenty of open vision models",
  "console.x.ai 申请 Key · 选带 vision 的型号": "Get a key at console.x.ai · pick a vision-capable model",
  "platform.openai.com 申请 Key · whisper-1": "Get a key at platform.openai.com · whisper-1",
  "console.groq.com 申请 Key · whisper-large-v3，速度很快": "Get a key at console.groq.com · whisper-large-v3, very fast",
  "siliconflow.cn 申请 Key · SenseVoiceSmall 便宜": "Get a key at siliconflow.cn · SenseVoiceSmall is cheap",
  "自定义 / 本地（oMLX、Ollama、LM Studio…）": "Custom / local (oMLX, Ollama, LM Studio…)",
  "阿里云百炼 · 通义千问": "Alibaba Bailian · Qwen",
  "智谱 GLM": "Zhipu GLM",
  "月之暗面 Kimi": "Moonshot Kimi",
  "硅基流动 SiliconFlow": "SiliconFlow",
  "自定义 / 本地（oMLX、Ollama、LM Studio…）": "Custom / local (oMLX, Ollama, LM Studio…)",
  "7 个待补": "7 to backfill",
  "开着的时候：后台每 20 秒看一眼，空闲够久就补一个（一次只跑一个，随时会被你的搜索/索引打断，中断后下次接着来）。打标签与转写轮流取，不会互相饿着。": "While on: the background checks every 20 seconds and backfills one item once idle long enough (one at a time; your searches and indexing can interrupt it, and it resumes next time). Tagging and transcription take turns, so neither starves.",
  "打标": "Tagging",
  "/3  ·  待打标": "/3  ·  pending tags",
  "·  转写": "·  Transcribing",
  "/4  ·  待转写": "/4  ·  pending transcriptions",
  "·  本轮已补 0/10 · 空闲 9s / 900s": "·  0/10 backfilled this round · idle 9s / 900s",
  "官方服务商：选好服务商 → 只粘 API Key。本机 oMLX：服务商选「自定义 / 本地」，\n          地址": "Official providers: pick a provider → just paste the API key. Local oMLX: choose “Custom / local”, and set the\n          URL",
  "用官方服务商：选「服务商」→ 只粘 API Key 就行。\n          用本机 oMLX：服务商选「自定义 / 本地」，地址填": "For official providers: pick the provider → just paste the API key.\n          For local oMLX: choose “Custom / local”, and set the URL to",
  "，\n          模型填": ", and set the model to",
  "已接入": "Connected",
  "个 Agent（本机共检测到 6 个）。去对话里说「帮我找有气球的图片」就能用上。": " agents (6 detected on this machine). Just ask your assistant, “find me pictures with balloons.”",
  "连接记录写入 DSH 的 storage，重启 DSH 后生效。": "The connection is written to DSH storage; restart DSH to apply.",
  "📘 技能": "📘 Skill",
  "断开": "Disconnect",
  "写入 opencode.json 的 mcp 段（type=local）。": "Writes the mcp section of opencode.json (type=local).",
  "写入 ~/.workbuddy/mcp.json 的标准 mcpServers 段。": "Writes the standard mcpServers section of ~/.workbuddy/mcp.json.",
  "未接入": "Not connected",
  "接入": "Connect",
  "写入 ~/.hermes/config.yaml 的 mcp_servers 段。": "Writes the mcp_servers section of ~/.hermes/config.yaml.",
  "写入 ~/.codex/config.toml 的 [mcp_servers.fxseek] 节。": "Writes the [mcp_servers.fxseek] section of ~/.codex/config.toml.",
  "写入 ~/.claude.json 顶层的 mcpServers（用户级）。": "Writes the top-level mcpServers in ~/.claude.json (user level).",
  "💡 MCP 走 stdio：每个 Agent 自己拉起一个轻量子进程，它再回连本服务取结果，\n            所以这里不需要额外的网络端口，也不用常驻。": "💡 MCP runs over stdio: each agent spawns a lightweight child process that connects back to this service for results,\n            so no extra network port is needed and nothing stays resident.",
  "。\n            所以接入时会顺手把配套技能（": ".\n            So connecting also drops in the companion skill (",
  "）写进各家的 skills 目录 ——\n            那份说明书写清了使用时机、查询怎么写、结果怎么读。": ") into each tool's skills directory —\n            that doc explains when to use it, how to phrase queries, and how to read results. ",
  "用一句话描述找素材。中文自然语言即可，例如「有气球的画面」。": "Describe what to find in one sentence — plain natural language is fine, e.g. “photos with balloons.”",
  "给一张图，找库里长得像的。": "Give it an image and it finds visually similar items in the library.",
  "给一段音频，找同一首歌或相似的声音。": "Give it an audio clip and it finds the same song or similar sounds.",
  "按路径查一个文件的详细信息（标签、时长、尺寸等）。": "Look up one file's details by path (tags, duration, dimensions, etc.).",
  "列出当前所有索引源目录。": "List all current index source folders.",
  "最近上传的素材。": "Recently uploaded assets.",
  "媒体库总览：多少文件、多少向量、按类型分布。": "Library overview: file count, vector count, and breakdown by type.",
  "：文件名命中直接返回（<1ms），否则向量检索，必要时才加载 WeMM 深度模型。": ": filename hits return immediately (<1ms); otherwise vector search, and the WeMM deep model loads only when needed.",
  "平衡：门槛 = max(46%, 最高分×60%) —— 过滤明显不相关条目，兼顾查全与查准。": "Balanced: threshold = max(46%, top score × 60%) — filters clearly irrelevant hits while balancing recall and precision.",
  "全部被过滤时：最高分 ≥ 46% 才保留兜底结果（宽松/平衡 3 条，严格/最严 1 条）；连最高分都低于 46%，说明库里没有相关素材，直接显示「没有找到相关素材」。": "When everything is filtered out: the fallback keeps results only if the top score is ≥ 46% (3 for Loose/Balanced, 1 for Strict/Strictest). If even the top score is below 46%, the library has nothing relevant and “no matching assets found” is shown.",
  "自动：按时长分段（≤10s 每 0.5s、≤30s 每 1s、≤2min 每 2s、更长更稀疏）——精度与资源平衡。": "Auto: interval by duration (≤10s every 0.5s, ≤30s every 1s, ≤2min every 2s, sparser beyond that) — balances precision and cost.",
  "缓存：": "Cache: ",
  "个文件 ·": "files ·",
  "/ 上限 1024 MB": "/ 1024 MB limit",
  "占用 0.0%，超过上限后会自动按需重建。": "0.0% used; past the limit, entries are rebuilt on demand.",
  "模型：": "Model: ",
  "WeMM-Embedding-2B (MLX, 混合精度)": "WeMM-Embedding-2B (MLX, mixed precision)",
  "内置": "Bundled",
  "· 可用": "· Available",
  "索引：": "Index: ",
  "个文件 /": "files /",
  "个向量 (audio 3 · document 3 · image 2 · video 1)": " vectors (audio 3 · document 3 · image 2 · video 1)",
  "全部计算在本机完成，无网络请求": "All computation happens on this machine, with no network requests",
  "Qwen3.5 · 4bit 量化": "Qwen3.5 · 4-bit quantized",
  "9 个资产 · 24 条向量": "9 assets · 24 vectors",
  "没有找到匹配的设置项": "No matching settings found",
  "设置已保存": "Settings saved",
  "显示密钥": "Show key",
  "断开这个 Agent": "Disconnect this agent",
  "接入这个 Agent": "Connect this agent",
  "显示所有索引源的内容；单击可折叠 / 展开下面的目录": "Show content from all index sources; click to collapse / expand the folders below",
  "取消搜索，回到首页": "Cancel search and go back home",
  "当前静音，点击开启声音": "Currently muted; click to unmute",
  "共 9 项": "9 items",
  "24 字": "24 chars",
  "选择「稍后」不会丢失任何东西：来源会留在左侧列表（标注「未索引」），\n      里面的文件": "Choosing “Later” loses nothing: the source stays in the left list (marked “Not indexed”), and its files",
  " 自动": " Auto",
  " 白天": " Light",
  " 夜晚": " Dark",
  /* 尾部的「条」碎片会补上 vectors，这里不能再写一遍（会变成
     「vectors 57 vectors」）。只到 files 为止。 */
  " 个文件 · 向量 ": " files · ",
  " 条": " vectors",
  " 种类型": " types",
  " 位置：": " Location: ",
  "向量库 ": "Vector DB ",
  " · 指纹库 ": " · fingerprint DB ",
  " · 预览缓存 ": " · preview cache ",
  "本地 / 自建服务的地址，以 ": "Local / self-hosted address, ending with ",
  " 结尾": "",
  " 模型：": " Model: ",
  " · 可用": " · available",
  " 索引：": " Index: ",
  " 个文件 / ": " files / ",
  " 个向量 (audio 3 · document 3 · image 2 · video 1)": " vectors (audio 3 · document 3 · image 2 · video 1)",
  " 完整：无论系统设置如何，始终播放完整动画（旋转、脉冲、光晕扩散）。": " Full: always play the complete animation (spin, pulse, glow spread), regardless of system settings.",
  " 跟随系统：系统开启「减弱动态效果」时，按钮动效降级为轻微的淡入缩放；否则播放完整动画。": " Follow system: when “Reduce motion” is on in macOS, button animations are reduced to a subtle fade-and-scale; otherwise the full animation plays.",
  " 关闭：不播放任何按钮动效。": " Off: no button animations at all.",
  " 开着的时候：后台每 20 秒看一眼，空闲够久就补一个（一次只跑一个，随时会被你的搜索/索引打断，中断后下次接着来）。打标签与转写轮流取，不会互相饿着。": " When on: the background checks every 20s and backfills one item once idle long enough (one at a time, interruptible by your search/indexing, resuming next time). Tagging and transcription take turns so neither starves.",
  " 要一直跑的话，可以打开「闲置时打标签」或「闲置时转写音频」。": " To keep it running, turn on \"Tag while idle\" or \"Transcribe audio while idle\".",
  " 官方服务商：选好服务商 → 只粘 API Key。本机 oMLX：服务商选「自定义 / 本地」，\n          地址 ": " Official providers: pick a provider → just paste the API Key. Local oMLX: choose \"Custom / local\" as the provider, and set the\n          address to ",
  "，再点「获取列表」挑一个能看图的模型。\n        ": ", then click \"Fetch list\" and pick a model that can see images.\n        ",
  "需支持 OpenAI 的 ": "Must support OpenAI's ",
  " 用官方服务商：选「服务商」→ 只粘 API Key 就行。\n          用本机 oMLX：服务商选「自定义 / 本地」，地址填 ": " With an official provider: pick a provider → just paste the API Key.\n          With local oMLX: choose \"Custom / local\" as the provider and enter the address ",
  "，\n          模型点右侧「获取列表」选一个即可，只要是支持转写的 ASR 模型都行（如 ": ",\n          then click \"Fetch list\" to pick one — any transcription-capable ASR model works (e.g. ",
  " 系列）。\n        ": " series).\n        ",
  " 个 Agent（本机共检测到 6 个）。去对话里说「帮我找有气球的图片」就能用上。": " agents (6 detected on this machine). Just say \"find me pictures with balloons\" in your chat to use it.",
  "\n            💡 MCP 走 stdio：每个 Agent 自己拉起一个轻量子进程，它再回连本服务取结果，\n            所以这里不需要额外的网络端口，也不用常驻。\n          ": "\n            💡 MCP uses stdio: each agent spawns a lightweight child process that calls back to this service for results,\n            so no extra network port is needed and nothing has to stay resident.\n          ",
  "\n            📘 光有 MCP，Agent 只知道「有这么几个工具」，不知道": "\n            📘 With MCP alone, an agent only knows \"there are these tools\" — it doesn’t know ",
  /* 模型下拉框里「还没取列表」时的占位项（settings.html:1117 由 JS 拼出来），
     之前漏了，英文界面下它一直是中文。 */
  "— 点右侧「获取列表」 —": "— click \"Fetch list\" on the right —",
  "\n            （你可能自己改过），关掉 MCP 也不会删它。\n          ": "\n            (which you may have edited yourself); turning MCP off won't delete it.\n          ",
  "：查询扩展 + 多指令融合，精度最高但最耗资源。\n          ": ": query expansion + multi-instruction fusion — highest precision, heaviest cost.\n          ",
  " 平衡：门槛 = max(46%, 最高分×60%) —— 过滤明显不相关条目，兼顾查全与查准。": " Balanced: threshold = max(46%, top score × 60%) — filters clearly irrelevant hits, balancing recall and precision.",
  " 自动：按时长分段（≤10s 每 0.5s、≤30s 每 1s、≤2min 每 2s、更长更稀疏）——精度与资源平衡。": " Auto: segment by duration (≤10s every 0.5s, ≤30s every 1s, ≤2min every 2s, sparser beyond) — balancing precision and resources.",
  " 模型只在首次需要时加载，并常驻复用；文件名类查询完全不会触发模型加载。\n        ": " The model loads only on first use and stays resident for reuse; filename-only queries never trigger a model load.\n        ",
  " 全部计算在本机完成，无网络请求": " All computation happens on this machine; no network requests",
  " 二维码是随应用一起打包的本地图片，打开这一页不会产生任何网络请求。\n          ": " The QR codes are local images bundled with the app; opening this page makes no network requests.\n          ",
  " 个文件 · ": " files · ",
  " / 上限 ": " / limit ",
  " MB": " MB",
  " 占用 ": " ",
  "，超过上限后会自动按需重建。": " used; items are rebuilt on demand once the limit is exceeded.",
  "ffmpeg：": "ffmpeg: ",
  " 内置": " Built-in",
  " 系统": " System",
  " · 不可用": " · unavailable",
  " 个向量 (": " vectors (",
  " 个向量": " vectors",
  "空": "empty",
  "程序目录（旧位置）": "App directory (legacy location)",
  "自定义位置（环境变量 FXSEEK_DATA_DIR）": "Custom location (FXSEEK_DATA_DIR)",
  "指纹库": "Fingerprint store",
  " assets · ": " assets · ",
  "条向量": " vectors",
  "未安装": "Not installed",
  " · 技能": " · Skill",
  "技能": "Skill",
  "技能已就位：": "Skill in place: ",
  "技能缺失": "Skill missing",
  "个 Agent": " agents",
  /* ⚠️ 这两条**不能**再按碎片收了：未接入态的整行规则写在 RULES 末尾
     （搜索「MCP 状态行的整行版」），它吃的是 applyHtml 送进来的**原始整行**。
     而文本节点那一趟先跑 —— 只要「本机检测到 」还有词条，整行就先被啃成
     「Detected <b>6</b> …」的半成品，整行规则再也匹配不上，后半截原样留中文。
     同理见下面的「个 Agent（本机共检测到 」一簇。宁可整行翻，也不要碎片翻。
     这两条留着会互相打架，故删除：
       "本机检测到 ": "Detected "
       " 个 Agent，目前都未接入。": " agents on this Mac, none connected yet."
       "点亮右边开关即可，随时可以再关掉。": "..."
     真要补，就补**不带** <b> 的纯文本整行版（见 RULES 里 668/671 那两条）。 */
  /* 入网失败/兜底态 */
  "MCP 模块不可用：": "MCP module unavailable: ",
  "无法检测": "Unable to detect",
  "本机没有检测到支持的 Agent。装好之后再点「重新检测」。": "No supported agents detected on this Mac. Install one, then hit “Detect again”.",
  "本机没有检测到支持的 Agent。": "No supported agents detected on this Mac.",
  "装好之后再点「重新检测」。": "Install one, then hit “Detect again”.",
  " agents detected, none connected yet.": " agents on this Mac, none connected yet.",
  "Flip the switch on the right to connect \u2014 you can turn it off anytime.": "Turn on the switch on the right to connect — you can turn it off anytime.",
  "去对话里说「帮我找有气球的图片」就能用上。": "Just ask an agent in chat, \"find me pictures with balloons\", and it works.",
  " to backfill": " to backfill",
  "打标 ": "Tagged ",
  "待打标": "pending tags",
  "转写 ": "Transcribed ",
  "待转写": "pending transcriptions",
  "标签齐了": "tags done",
  "转写齐了": "transcriptions done",
  "本轮已补": "this round",
  "空闲": "idle",
  "正在补全…": "Backfilling…",
  "全部就绪": "All ready",
  "%，超过上限后会自动按需重建。": "% used; entries rebuild on demand once the limit is exceeded.",
  " 个 Agent（本机共检测到 6 个）。": " agents (6 detected on this Mac). ",
  " (detected ": " (",
  " 个）。": " detected on this Mac). ",
  " 个待补": " to backfill",
  " 待打标 ": " pending tags ",
  " 待转写 ": " pending transcriptions ",
  " 本轮已补 ": " this round ",
  " 空闲 ": " idle ",
  "共 ": "",
  " 项": " items",
  " 字": " chars",
  "以图搜图": "Search by image",
  "以音搜素材": "Search by audio",
  "AI 打标": "AI tagging",
  "音频转写": "Audio transcription",
  "同一首歌": "Same song",
  "哼唱检索": "Humming search",
  "语音检索": "Voice search",
  "MCP 技能注入": "MCP skill injection",
  "MCP 接入": "MCP connect",
  "按名字查找": "Search by name",
  "立即补全": "Backfill now",
  "打开": "Open",
  "索引": "Index",
  "检索": "Search",
  "缓存": "Cache",
  "日志": "Log",
  "清理": "Cleanup",
  "恢复": "Restore",
  "删除": "Delete",
  "暂存": "Trash",
  "个相似结果": " similar results",
  "个匹配": " match",
  " 个匹配": " match",
  "字": " chars",
  " 字 · ": " chars · ",
  "个标签": " tags",
  " 个标签 · ": " tags · ",
  "本地资产： ": "Local assets: ",
  "索引来源： ": "Index sources: ",
  "索引： ": "Index: ",
  "Connected ": "Connected ",
  "剩余 ": "Remaining ",
  "，剩余 ": ", remaining ",
  "files · ": "files · ",
  " files": " files",
  "全部 ": "All ",
  " 视频 ": " Videos ",
  " 图片 ": " Images ",
  " 音频 ": " Audio ",
  " 文档 ": " Documents ",
  "切换为大图标（2 列网格）": "Switch to large icons (2-column grid)",
  "切换为小图标列表": "Switch to small icon list",
  "试听前 20 秒": "Preview first 20s",
  "素材详情": "Asset details",
  "预览": "Preview",
  "下载": "Download",
  "文件信息": "File info",
  "文件大小": "File size",
  "文件格式": "File format",
  "色彩模式": "Color mode",
  "宽高比": "Aspect ratio",
  "索引状态": "Index status",
  "已索引": "Indexed",
  "未索引": "Not indexed",
  "（由关键帧拼图生成）": " (built from a keyframe collage)",

  "时间": "Time",
  "修改时间": "Modified",
  "创建时间": "Created",
  "AI 描述": "AI description",
  "转写内容": "Transcription",
  "完整路径": "Full path",
  "标签": "Tags",
  "尺寸": "Dimensions",
  "时长": "Duration",
  "分辨率": "Resolution",
  "采样率": "Sample rate",
  "行数": "Lines",
  "字数": "Characters",
  "容器": "Container",
  "视频编码": "Video codec",
  "编码档次": "Profile",
  "像素格式": "Pixel format",
  "帧率": "Frame rate",
  "总码率": "Total bitrate",
  "音频编码": "Audio codec",
  "声道数": "Channels",
  "DPI": "DPI",
  "帧数": "Frames",
  "词数": "Words",
  "文本编码": "Text encoding",
  "临时垃圾桶为空": "Recycle bin is empty",
  "加载素材…": "Loading assets…",
  "该类别暂无素材": "No assets in this category",
  "没有找到相关素材": "No matching assets found",
  "深度检索无结果": "Deep search returned nothing",
  "找到一个素材": "1 asset found",
  "个匹配素材": " matching assets",
  "个相似素材": " similar assets",
  "个同一首歌": " same song",
  " 个同一首歌": " same song",
  "按相似度排序": "sorted by similarity",
  "按匹配度排序": "sorted by relevance",
  "全部来源": "All sources",
  "还没有添加目录": "No folders added yet",
  "全部文档": "All documents",
  "纯文本": "Plain text",
  "网页·数据": "Web / data",
  "代码": "Code",
  "表格": "Spreadsheet",
  "幻灯片": "Slides",
  "其他": "Other",
  "清除格式筛选": "Clear format filter",
  "取消": "Cancel",
  "无": "None",
  "名称": "Name",
  "种类": "Kind",
  "添加日期": "Date added",
  "大小": "Size",
  "升序": "Ascending",
  "降序": "Descending",
  "最近搜索": "Recent searches",
  "还没有搜索记录": "No search history yet",
  "在访达中显示": "Reveal in Finder",
  "路径源": "Reveal in Finder",
  "立即打标签": "Tag now",
  "立即转写": "Transcribe now",
  "在访达中打开": "Open in Finder",
  "移除索引源": "Remove index source",
  "移除": "Remove",
  "已移除索引源": "Index source removed",
  "暂存到最近删除": "Move to Recycle Bin",
  "彻底删除": "Delete permanently",
  "全部还原": "Restore all",
  "全部彻底删除": "Delete all permanently",
  "改用指纹搜": "Search by fingerprint",
  "改用文字搜": "Search by text",
  " 量化": " quantized",
  "个资产": " assets",
  " 个资产 · ": " assets · ",
  "还没有上传过图片或音频": "No images or audio uploaded yet",
  "「以图搜图」「以音搜素材」": "“Search by image” / “Search by audio”",
  "用过的都会留在这里": "Everything you use will be kept here",
  /* ⚠️ 同上一处：已接入态的整行规则在 RULES 末尾吃**原始整行**，
     下面这两条碎片词条会先把整行啃坏（「已接入 <b>2</b> 个 Agent（本机共检测到 」
     一截先被翻掉），整行规则就永远匹配不上了。故一并删除：
       "个 Agent（本机共检测到 ": " agents (out of "
       " 个）。去对话里说「帮我找有气球的图片」就能用上。": "..."
     纯文本版（无 <b>）的规则仍保留在 RULES 里，管没有标签的场景。 */

  /* ── 2026-10 全项目查缺补漏（进度卡/搜索状态/索引对话框/录音/设置页状态）── */
  '索引完成': 'Indexing complete',
  '已完成': 'Done',
  '索引出错': 'Indexing failed',
  '准备索引…': 'Preparing…',
  '加载模型…': 'Loading model…',
  '扫描文件…': 'Scanning files…',
  '正在刷新索引…': 'Refreshing index…',
  '扫描新增与变更的文件': 'Scanning for new and changed files',
  '刷新失败：': 'Refresh failed: ',
  '顺序没能保存，已恢复': 'Couldn\'t save the order — restored',
  '还没有建立索引：可以浏览、可以打开，但用文字搜不到。点这里立即建立索引': 'Not indexed yet: you can browse and open files, but text search won\'t find them. Click to index now',
  '（还没有建立索引）': ' (not indexed)',
  '有': 'There are',
  '描述': 'Description',
  '主要内容': 'Main content',
  '当前有声，点击静音': 'Sound on — click to mute',
  '生成中…（约 10~60 秒，请勿关闭页面）': 'Generating… (about 10–60s, keep this page open)',
  '打标失败': 'Tagging failed',
  '转写中…（几秒到几十秒，请勿关闭页面）': 'Transcribing… (a few seconds to minutes, keep this page open)',
  '转写失败': 'Transcription failed',
  '（会把关键帧拼成九宫格，喂给视觉模型看一次）': '(frames are tiled into a 3×3 grid and shown to the vision model once)',
  '后退10秒': 'Back 10s',
  '前进10秒': 'Forward 10s',
  '播放 / 暂停': 'Play / pause',
  '上一个': 'Previous',
  '下一个': 'Next',
  '上一张': 'Previous',
  '下一张': 'Next',
  '播放速度': 'Playback speed',
  '静音 / 取消静音': 'Mute / unmute',
  '音量': 'Volume',
  '全屏': 'Fullscreen',
  '加载失败': 'Failed to load',
  '缩小': 'Zoom out',
  '放大': 'Zoom in',
  '适应窗口': 'Fit to window',
  '实际大小（100%）': 'Actual size (100%)',
  '旋转 90°': 'Rotate 90°',
  '开始播放（匹配帧定位）': 'start (match-frame position)',
  'FXseek Audio': 'FXseek Audio',
  'FXseek Video': 'FXseek Video',
  'docx-preview 未就绪': 'docx-preview not ready',
  'SheetJS 未就绪': 'SheetJS not ready',
  '工作簿为空': 'Workbook is empty',
  '· 已截断显示': '· truncated for display',
  '已截断显示': 'truncated for display',
  '已用默认应用打开': 'Opened with the default app',
  '移到废纸篓': 'Move to Trash',
  '30 天': '30 days',
  '已移到废纸篓': 'Moved to Trash',
  '已恢复': 'Restored',
  '该条目已不存在': 'This entry no longer exists',
  '请选择图片文件': 'Please choose an image file',
  '正在分析图片…': 'Analyzing image…',
  '请选择音频或视频文件': 'Please choose an audio or video file',
  '正在提取指纹并检索…': 'Extracting fingerprint and searching…',
  '未找到匹配素材': 'No matching assets found',
  '这一条没有可试听的片段': 'Nothing to preview for this item',
  '浏览器拦住了自动播放，再点一次': 'The browser blocked autoplay — click again',
  '上传图片': 'Upload image',
  '上传音频': 'Upload audio',
  '这张图的缩略图已丢失，请重新上传': 'This thumbnail is missing — please upload the image again',
  '音频要重新比对指纹，请再选一次这个文件：': 'The audio needs its fingerprint recomputed — please pick this file again:',
  '停止录音': 'Stop recording',
  '开始录音': 'Start recording',
  '录音失败': 'Recording failed',
  '录音太短，没有内容': 'The recording was too short',
  '还没有录音': 'No recordings yet',
  '无法识别这段录音': 'Couldn\'t recognize this recording',
  '指纹没找到，改用歌词文字搜': 'No fingerprint match — falling back to lyrics search',
  '语音转文字还没启用': 'Speech-to-text isn\'t enabled',
  '没有可用的录音': 'No usable recording',
  '改用声学指纹重试…': 'Retrying with acoustic fingerprint…',
  '指纹也没匹配到同一段音频': 'The fingerprint didn\'t match any audio either',
  '改用语音转文字重试…': 'Retrying with speech-to-text…',
  '没有听清内容': 'Couldn\'t hear it clearly',
  '没有匹配到素材': 'No assets matched',
  '录音没能匹配到素材': 'The recording didn\'t match any asset',
  '这段录音没能匹配到素材。': 'This recording didn\'t match any asset.',
  '指纹没有匹配到音频或视频': 'The fingerprint matched no audio or video',
  '哼唱没有匹配到音频或视频': 'The humming matched no audio or video',
  '听出原曲：指纹精确匹配': 'Original track found: fingerprint matched',
  '哼唱找素材：指纹匹配 · 仅音频与视频': 'Humming search · fingerprint match · audio & video only',
  '直接改上方的检索词，回车就能重搜': 'Edit the query above and press Enter to search again',
  '请先输入搜索词': 'Type a search query first',
  '正在打开文件夹选择窗口…': 'Opening the folder picker…',
  '已取消': 'Cancelled',
  '目录已添加。建立索引后，里面的文件才能被搜索到。': 'Folder added. Its files become searchable after indexing.',
  '仍然可以现在建立索引。': 'You can still index right now.',
  '（不索引）': ' (not indexed)',
  '这个目录已经索引过，再跑一次只会检查改动过的文件。': 'This folder is already indexed — a re-run only checks changed files.',
  '这个目录之前索引过一部分，现在只会补新增和改动的文件。': 'This folder was partially indexed — only new and changed files will be added now.',
  '建立索引后，里面的文件才能被搜索到。': 'Once indexed, the files inside become searchable.',
  '索引已启动，完成后自动刷新': 'Indexing started — the list refreshes when it finishes',
  '首次索引耗时与文件数量相关': 'First-indexing time depends on the file count',
  '正在重扫全部索引源…': 'Re-scanning all sources…',
  '这里面没有可索引的文件': 'Nothing indexable here',
  '约': ' about ',
  '已全部索引过，只做增量检查 ——': ' already indexed — only an incremental check',
  '几秒': 'a few seconds',
  '（改动过的文件会被重跑）': '(changed files are re-run)',
  '（首次索引，之后只补新文件）': '(first index — only new files afterwards)',
  '个文件': ' files',
  '，其中': ' — of which',
  '个已经索引过': ' files already indexed',
  '构成': 'Composition',
  '预计': 'Estimate',
  '共': 'Total',
  '隐藏密钥': 'Hide key',
  'sk-…（粘贴服务商给的密钥）': 'sk-… (paste your provider\'s key)',
  '转写': 'Transcribed',
  '正在补标签': 'Backfilling tags',
  '正在补转写': 'Backfilling transcripts',
  '不占着的话可以关掉这个页面，它在后台继续；也可以随时点「停止」。': 'You can close this page — it keeps running in the background; or click “Stop” anytime.',
  '上次补全：完成': 'Last backfill: completed',
  '个，全部成功。': ' files, all succeeded.',
  '个，失败': ' files, failed',
  '个。': ' files.',
  '启动失败': 'Start failed',
  '没有待处理的素材': 'Nothing pending to backfill',
  '已停止': 'Stopped',
  '请先选择或填写模型名': 'Pick or type a model name first',
  'AI 配置已保存': 'AI settings saved',
  '请先填写模型名': 'Enter a model name first',
  '正在获取模型列表…': 'Fetching model list…',
  '没有获取到任何模型。': 'No models were returned.',
  '该地址没有发现语音转写类模型（whisper / sensevoice / asr 等）。若用本地服务，请确认已加载 ASR 模型。': 'No transcription models found at this URL (whisper / sensevoice / asr, etc.). For a local service, make sure the ASR model is loaded.',
  '该地址没有发现能看图的模型（vl / vision / 4v 等）。若用本地服务，请确认已加载视觉模型。': 'No vision models found at this URL (vl / vision / 4v, etc.). For a local service, make sure a vision model is loaded.',
  '正在测试连接…': 'Testing connection…',
  '请先填写 API 地址与模型': 'Enter the API URL and model first',
  '请求失败：': 'Request failed: ',
  '获取失败': 'Fetch failed',
  '正在检查…': 'Checking…',
  '正在清理…': 'Cleaning…',
  '没有需要清理的记录': 'Nothing to clean',
  '日志已清空': 'Logs cleared',
  '未知错误': 'Unknown error',
  '配置已恢复': 'Settings restored',
  '本机没有检测到可接入的 Agent': 'No connectable agents detected on this Mac',
  '技能都已就位': 'Skills are all in place',
  '（技能保留，没删）': ' (skills kept, not removed)',
  '没有符合条件的素材': 'No assets match the current filters',
  '正在判断录音内容…': 'Analyzing the recording…',
  '语音识别：': 'Speech recognition: ',
  '改文字': 'Edit text',
  '音频转写配置已保存': 'Audio transcription settings saved',
  '去处理': 'Handle it',
  '正在渲染 Word 文档…': 'Rendering the Word document…',
  '正在解析表格…': 'Parsing the spreadsheet…',
  '加载中…': 'Loading…',
  '无候选': 'no candidate',
  '没有听清清晰的语音或哼唱，请靠近麦克风再说一次': 'Couldn\'t hear clear speech or humming — move closer to the mic and try again',
  '音频文件不存在': 'Audio file does not exist',
  '录音太短或无法解码（至少需要 0.8 秒）': 'Recording too short or undecodable (needs at least 0.8 seconds)',
  '音频太短': 'Audio too short',
  '文件不存在': 'File does not exist',
  '无法读取这段录音：': 'Cannot read this recording: ',
  '这段录音太短或没有声音，提取不到声学指纹': 'The recording is too short or silent — no acoustic fingerprint could be extracted',
  '录音太短了，至少要哼 3 秒左右': 'The recording is too short — hum for at least ~3 seconds',
  '这段录音太平稳（几乎没有音高变化），提取到的指纹没有区分度': 'The recording is too steady (almost no pitch change) — its fingerprint has no discriminative detail',
  '这段录音没能转成文字，也没匹配到库里已有的音频。如果是在唱歌，试着把原曲外放给麦克风听（指纹认同一段录音最准）；如果是在说话，靠近麦克风再说一次。': 'This recording couldn\'t be turned into text, and it didn\'t match anything in your library. If you were singing, play the original out loud to the mic (fingerprints match recordings best); if you were speaking, move closer to the mic and try again.',
  '听起来你在哼旋律、没有唱词，转写只得到语气词。声学指纹认的是「同一段录音」（外放原曲一定能搜到），哼唱的旋律它认不出——想按旋律找歌的话，得给歌词或者唱出词来。': 'Sounds like you were humming a melody with no words — transcription only got filler. Acoustic fingerprints recognize “the same recording” (playing the original out loud always works), not humming — for melody search, give lyrics or sing actual words.',
  '请先填写 ASR 地址与模型': 'Enter the ASR URL and model first',
  '缺少录音数据': 'Missing recording data',
  '请先填写 API 地址': 'Enter the API URL first',
  '文件不存在或不可访问': 'File does not exist or is not accessible',
  '请先在设置中配置 API 地址与模型': 'Configure the API URL and model in Settings first',
  '正在建索引，等它跑完再打标': 'Indexing in progress — wait for it to finish before tagging',
  '正在建索引，等它跑完再转写': 'Indexing in progress — wait for it to finish before transcribing',
  'AI 描述生成还没开启（设置 → 智能服务）': 'AI description is not enabled (Settings → AI services)',
  '模型还没加载完，稍后再试': 'The model is still loading — try again in a moment',
  '音频转写还没开启（设置 → 智能服务）': 'Audio transcription is not enabled (Settings → AI services)',
  '请先在设置里填语音识别服务的地址与模型': 'Enter the speech-recognition URL and model in Settings first',
  'AI 打标没开：先去「智能服务」打开「AI 描述与标签」。': 'AI tagging is off — turn on “AI description & tags” under AI services.',
  '音频转写没开：先去「智能服务」打开转写。': 'Audio transcription is off — turn it on under AI services.',
  '启动索引…': 'Starting indexing…',
  '两条路都没结果': 'Neither path produced results',
  '正在比对音频指纹…': 'Comparing audio fingerprints…',
  '正在检索相似素材…': 'Searching for similar assets…',
  '待传输录音 · 点击「搜画面」开始检索': 'Recording pending · click “Search by image” to start',
  '去打开设置': 'Open Settings',
  '语音转文字还没启用：请到「设置 → 音频转写」打开开关并填好地址与模型，之后对着麦克风说话就能直接搜素材。': "Speech-to-text isn't enabled yet: turn it on under Settings → Audio transcription and fill in the URL and model — then you can just talk into the mic to search your library.",
  '检索中…': 'Searching…',
  '深度语义检索中…': 'Deep semantic search in progress…',
  '没有匹配到素材': 'No assets matched',
  '这个文件夹不小': 'This folder is big',
  '哼唱没能匹配到库里已有的音频。指纹只能认出「同一段音频」——滤波、压缩、外放录音都没问题，但哼成旋律时音高变了就认不出。可以把原曲外放给麦克风听，或者改用文字描述来搜。': 'The humming matched none of the existing audio. Fingerprints only recognize “the same recording” — filtering, compression and speaker playback are fine, but humming the melody changes the pitch, so it won\'t match. Try playing the original out loud to the mic, or search with text instead.',
  '系统': 'System',
  '系列）。': ' series).',
  '匹配完成': 'Match complete',
  'WPS 文字': 'WPS Writer',
  'WPS 表格': 'WPS Spreadsheet',
  'WPS 演示': 'WPS Presentation',
  '电子表格': 'Spreadsheet',
  '演示文稿': 'Presentation',
  '文本文件': 'Text file',
  '文本': 'Text',
  '网页': 'Web page',
  '网页·数据': 'Web & data',
  '表格': 'Spreadsheet',
  '幻灯片': 'Slides',
  '代码': 'Code',
  '其他': 'Other',
  '直读 utf-8 / gbk': 'reads UTF-8 / GBK directly',
  '直读文本': 'reads text directly',
  'pdfplumber 提取': 'extracted with pdfplumber',
  '系统 textutil': 'system textutil',
  'OLE2 + BIFF（xlrd）': 'OLE2 + BIFF (xlrd)',
  'OLE2 容器，正文可能读不出': 'OLE2 container — body text may be unreadable',
  /* 漫步时光（回忆长廊） */
  '漫步时光': 'Memory stroll',
  '回到现在': 'Back to now',
  '只留图片和视频，让回忆循环滚动': 'Keep only photos & videos and let memories scroll',
  '点击回到素材库': 'Click to return to your library',
  '这个视图里没有图片或视频': 'No photos or videos in this view',
  '漫 步 时 光 · 回 忆 长 廊': 'S T R O L L I N G   D O W N   M E M O R Y   L A N E',
  '漫步时光：单击任意缩略图可以让它淡出，再点按钮就回到素材库': 'Memory stroll on — click any thumbnail to fade it away; click the button again to go back',
  '这一刻的回忆都看过了 — 再点一次「漫步时光」回到素材库': 'You have seen every memory here — click the button again to go back',
};

  /* 词条里带参数的（如「已接入 {n} 个 Agent」）走这里，按顺序套用。
      [正则, 英文替换模板] —— 只在当前语言是 en 时生效。 */
  var RULES = [
    [/^已接入 (\d+) 个 Agent$/, 'Connected to $1 agents'],
    [/^已断开 (\d+) 个 Agent$/, 'Disconnected $1 agents'],
    [/^已补上 (\d+) 个 Agent 的技能$/, 'Injected skills for $1 agents'],
    [/^打标 (\d+)\/(\d+)$/, 'Tagging $1/$2'],
    [/^转写 (\d+)\/(\d+)$/, 'Transcribing $1/$2'],
    [/^待打标 (\d+)$/, '$1 pending tags'],
    [/^待转写 (\d+)$/, '$1 pending transcriptions'],
    [/^(\d+) 个待补$/, '$1 to fill'],
    [/^共 (\d+) 条索引记录$/, '$1 index records'],
    [/^已清理 (\d+) 条失效记录$/, 'Cleaned $1 stale records'],
    [/^已删除 (\d+) 条失效记录$/, 'Removed $1 stale records'],
    [/^已清理 (\d+) 个缓存文件$/, 'Cleared $1 cache files'],
    [/^本轮已补 (\d+)\/(\S+)$/, 'This round: $1/$2'],
    [/^空闲 (\d+)s \/ (\d+)s$/, 'Idle $1s / $2s'],
    [/^正在补(标签|转写)$/, function (m, w) {
      return 'Filling in ' + (w === '标签' ? 'tags' : 'transcriptions');
    }],

    /* ---- 后台补全状态行：这些是 innerHTML 拼出来的，一个 ${} 断一次文本节点，
       所以「整行」和「行内碎片」两种形态都得兜住。---- */
    [/^打标\s*$/, 'Tagging '],
    [/^转写\s*$/, 'Transcribing '],
    [/^待打标\s*$/, 'pending tags '],
    [/^待转写\s*$/, 'pending transcriptions '],
    [/^\/\s*$/, '/'],
    [/^(\d+) 个待补$/, '$1 to backfill'],
    [/^\s*·\s*本轮已补 (\d+)\/(\S+) · 空闲 (\d+)s \/ (\d+)s\s*$/,
      ' · $1/$2 this round · idle $3s / $4s'],
    [/^\s*·\s*本轮已补 (\d+)\/(\S+)\s*$/, ' · $1/$2 this round'],
    [/^\s*·\s*空闲 (\d+)s \/ (\d+)s\s*$/, ' · idle $1s / $2s'],
    [/^\s*·\s*待打标\s*$/, ' · pending tags'],
    [/^\s*·\s*待转写\s*$/, ' · pending transcriptions'],
    [/^\s*·\s*标签齐了\s*$/, ' · tags done'],
    [/^\s*·\s*转写齐了\s*$/, ' · transcriptions done'],
    [/^\s*·\s*已隐藏\s*(\d+)\s*$/, ' · $1 hidden'],
      [/^\s*·\s*浏览模式\s*$/, ' · Browse mode'],
  // 结果栏整句：由 setCountHint 一次性拼出来，正则通吃「找到 N 项 + 可选尾巴 + 模式 + 耗时」。
  [/^找到 (\d+) 项$/, function (m) { return m[1] + ' items found'; }],
  [/^找到 (\d+) 项[\s\u00A0]*·[\s\u00A0]*已隐藏 (\d+) 项低匹配$/, function (m) { return m[1] + ' items found · ' + m[2] + ' low-relevance hidden'; }],
  [/^找到 (\d+) 项[\s\u00A0]*·[\s\u00A0]*浏览模式$/, function (m) { return m[1] + ' items found · Browsing'; }],
  [/^(.*?)找到 (\d+) 项(.*?)·[\s\u00A0]*深度语义[\s\u00A0]*·[\s\u00A0]*按匹配度排序$/, function (m) {
    return m[1] + m[2] + ' items found' + zhTail(m[3]) + ' · Deep semantic · sorted by relevance';
  }],
  [/^(.*?)找到 (\d+) 项(.*?)·[\s\u00A0]*(文件名|向量|自动|深度语义)([\s\u00A0]*·[\s\u00A0]*)([\d.]+)ms$/, function (m) {
    return m[1] + m[2] + ' items found' + zhTail(m[3]) + ' · ' + MODE_EN(m[4]) + ' · ' + m[6] + 'ms';
  }],
  [/^(.*?)找到 (\d+) 项(.*)$/, function (m) { return m[1] + m[2] + ' items found' + zhTail(m[3]); }],
  [/^找到 (\d+) 个匹配素材$/, function (m) { return m[1] + ' matching assets found'; }],
  [/^找到 (\d+) 个相似素材$/, function (m) { return m[1] + ' similar assets found'; }],
  [/^(.*?)找到 (\d+) 个匹配素材(.*)$/, function (m) { return leadTail(m[1]) + m[2] + ' matching assets found' + zhTail(m[3]); }],
  [/^(.*?)找到 (\d+) 个相似素材(.*)$/, function (m) { return leadTail(m[1]) + m[2] + ' similar assets found' + zhTail(m[3]); }],
  [/^(.*?)找到一个素材[\s\u00A0]*·[\s\u00A0]*(文件名|向量|自动|深度语义)[\s\u00A0]*·[\s\u00A0]*([\d.]+)ms$/, function (m) {
    return leadTail(m[1]) + '1 asset found · ' + MODE_EN(m[2]) + ' · ' + m[3] + 'ms';
  }],
  [/^(.*?)找到一个素材(.*)$/, function (m) { return leadTail(m[1]) + '1 asset found' + zhTail(m[2]).toLowerCase(); }],
  // 语音检索结果栏的前缀：「以音搜素材「文件名」」，文件名原样保留。
  /* 尾巴过 zhTail：m[2] 形如「 · 没有找到匹配的素材」，原样拼会留中文
     （全行规则排在这两条之后、抢不过它们）。zhTail 逐段 translate 并保住 · 分隔。 */
  [/^以音搜素材「(.+?)」(.*)$/, function (m) { return 'Search by audio “' + m[1] + '”' + zhTail(m[2]); }],
  [/^以图搜图「(.+?)」(.*)$/, function (m) { return 'Search by image “' + m[1] + '”' + zhTail(m[2]); }],
  [/^[\s\u00A0]*·[\s\u00A0]*已隐藏 (\d+) 项低匹配[\s\u00A0]*$/, function (m) { return ' · ' + m[1] + ' low-relevance hidden'; }],
  // 「找到 N 项 · 已隐藏 M 项低匹配 …」里的中段，整句规则兜不住时逐段补。
  [/^已隐藏 (\d+) 项低匹配$/, function (m) { return m[1] + ' low-relevance hidden'; }],
  [/^(\d+) 项低匹配$/, '$1 low-relevance'],
  [/^含 (\d+) 个同一首歌$/, function (m) { return m[1] + ' same song'; }],
  [/^个同一首歌$/, ' same song'],
  [/^[\s\u00A0]*·[\s\u00A0]*按相似度排序[\s\u00A0]*$/, ' · sorted by similarity'],
  [/^[\s\u00A0]*·[\s\u00A0]*按匹配度排序[\s\u00A0]*$/, ' · sorted by relevance'],
  [/^[\s\u00A0]*·[\s\u00A0]*含 (\d+) 个同一首歌[\s\u00A0]*$/, function (m) { return ' · ' + m[1] + ' same song'; }],
  [/^[\s\u00A0]*·[\s\u00A0]*浏览模式[\s\u00A0]*$/, ' · Browsing'],
  [/^[\s\u00A0]*·[\s\u00A0]*深度语义[\s\u00A0]*$/, ' · Deep semantic'],

    /* ---- 数据概览碎片（本地资产 / 索引来源 / 缓存 / 索引 那些行）---- */
    [/^(\d+) 个文件 · (\d+) 条向量$/, '$1 files · $2 vectors'],
    [/^(\d+) 个资产 · (\d+) 条向量$/, '$1 assets · $2 vectors'],
    [/^(\d+) 个文件$/, '$1 files'],
    [/^(\d+) 条向量$/, '$1 vectors'],
    // 同上，但数字在隔壁 <b> 里，文本节点只剩带前导空格的量词。
    [/^\s*条向量$/, ' vectors'],
    [/^(\d+) 种类型$/, '$1 types'],
    [/^(\d+) 个文件 · (\d+) MB$/, '$1 files · $2 MB'],
    [/^MB \/ 上限 (\d+) MB$/, 'MB / $1 MB limit'],
    [/^\/ 上限 (\d+) MB$/, '/ $1 MB limit'],
    [/^(\d+) 个文件 \/ (\d+) 个向量 \((.+)\)$/, '$1 files / $2 vectors ($3)'],
    [/^(\d+) 个文件 \/$/, '$1 files /'],
    [/^(\d+) 个向量 \((.+)\)$/, '$1 vectors ($2)'],
    // 同上，数字在隔壁 <b> 里，节点只剩「 个向量 (…类型统计…)」。
    [/^\s*个向量 \((.+)\)$/, ' vectors ($1)'],
    [/^索引向量：\s*$/, 'Index vectors: '],
    [/^查询向量缓存：\s*$/, 'Query vector cache: '],
    [/^缩略图缓存：\s*$/, 'Thumbnail cache: '],

    /* ---- MCP / Agent 状态行 ---- */
    [/^已接入\s*$/, 'Connected '],
    [/^(\d+) 个 Agent（本机共检测到 (\d+) 个）。(.+)$/, function (m, a, b, rest) {
      return a + ' agent(s) connected (' + b + ' detected on this machine). ' + rest;
    }],
    [/^个 Agent（本机共检测到 (\d+) 个）。(.+)$/, function (m, n, rest) {
      return ' agent(s) connected (' + n + ' detected on this machine). ' + rest;
    }],
    [/^技能已就位：(.+)$/, 'Skill in place: $1'],
    [/^(\d+) 个技能$/, '$1 skills'],
    [/^(\d+) 个失败 · (.+)$/, '$1 failed · $2'],
    [/^(\d+) 个已经索引过$/, '$1 already indexed'],
    [/^(\d+) 个来源还没有建立索引$/, '$1 sources not indexed yet'],
    [/^(\d+) 项低匹配，?\s*$/, '$1 low-relevance items '],
    [/^(\d+)% 匹配$/, '$1% match'],
    [/^加载失败：(.+)$/, 'Load failed: $1'],
    [/^检索失败：(.+)$/, 'Search failed: $1'],
    [/^获取失败：(.+)$/, 'Fetch failed: $1'],
    [/^读取失败：(.+)$/, 'Read failed: $1'],
    [/^请求失败：(.+)$/, 'Request failed: $1'],
    [/^生成中…（约 (\d+)~(\d+) 秒\)$/, 'Generating… (about $1–$2s)'],
    [/^已暂存到最近删除（(\d+) 天(.+)）$/, 'Moved to Recently deleted ($1 days$2)'],
    [/^(\d+) 天后自动清除$/, 'auto-cleared in $1 days'],

    /* ---- 运行时拼出来的 title 属性 ----
       里面带用户自己的路径/目录名，只能靠"尾巴"匹配：前缀原样留下，
       只把中文那截翻掉。同理 \n 必须保留（tooltip 里就是换行）。 */
    [/^(.+)（还没有建立索引） · 拖动可调整顺序$/, '$1 (not indexed yet) · drag to reorder'],
    [/^(.+) · 拖动可调整顺序$/, '$1 · drag to reorder'],
    [/^MCP 已接入 (\d+) 个 Agent：([\s\S]+)$/, function (m) {
      return 'MCP: ' + m[1] + ' agent' + (m[1] === '1' ? '' : 's') + ' connected: ' + m[2];
    }],
    [/^点击前往设置$/, 'Click to open settings'],

    /* ---- 补全状态行的「<b>数字</b> 后面那段」：innerHTML 里数字被 <b> 断开，
       留下的文本节点形如 "/3 &nbsp;·&nbsp; 待打标 "。&nbsp; 是 U+00A0，
       所以这里用 \u00A0 显式写，不靠 \s 碰运气。---- */
    [/^\/(\d+)[\s\u00A0]*·[\s\u00A0]*待打标[\s\u00A0]*$/,
      function (m) { return '/' + m[1] + '  ·  pending tags '; }],
    [/^\/(\d+)[\s\u00A0]*·[\s\u00A0]*待转写[\s\u00A0]*$/,
      function (m) { return '/' + m[1] + '  ·  pending transcriptions '; }],
    [/^[\s\u00A0]*·[\s\u00A0]*待打标[\s\u00A0]*$/,
      function () { return '  ·  pending tags '; }],
    [/^[\s\u00A0]*·[\s\u00A0]*待转写[\s\u00A0]*$/,
      function () { return '  ·  pending transcriptions '; }],
    [/^[\s\u00A0]*·[\s\u00A0]*标签齐了[\s\u00A0]*$/,
      function () { return '  ·  tags done '; }],
    [/^[\s\u00A0]*·[\s\u00A0]*转写齐了[\s\u00A0]*$/,
      function () { return '  ·  transcriptions done '; }],
    // 数字被 <b> 切走后，文本节点只剩纯片段「 标签齐了 」「 转写齐了 」。
    [/^[\s\u00A0]*标签齐了[\s\u00A0]*$/, ' tags done '],
    [/^[\s\u00A0]*转写齐了[\s\u00A0]*$/, ' transcriptions done '],
    [/^[\s\u00A0]*待打标[\s\u00A0]*$/, ' pending tags '],
    [/^[\s\u00A0]*待转写[\s\u00A0]*$/, ' pending transcriptions '],
    [/^[\s\u00A0]*·[\s\u00A0]*转写[\s\u00A0]*$/,
      function () { return '  ·  Transcribing '; }],
    [/^\/(\d+)[\s\u00A0]*·[\s\u00A0]*转写[\s\u00A0]*$/,
      function (m) { return '/' + m[1] + '  ·  Transcribing '; }],
    // 状态行是 parts.join(' &nbsp;·&nbsp; ') 拼出来的，一个文本节点里可能同时
    // 躺着「/3 · 标签齐了 · 转写」这种多个片段，所以不能只按「单片段」写规则：
    // 这里一次性吃掉「/N · X · Y · Z」整串，末尾没闭合的片段也一起翻掉。
    [/^(\/\d+)?[\s\u00A0]*·[\s\u00A0]*(待打标|待转写|标签齐了|转写齐了|打标|转写)((?:[\s\u00A0]*·[\s\u00A0]*(?:待打标|待转写|标签齐了|转写齐了|打标|转写))*)[\s\u00A0]*$/,
      function (m) {
        var seg = { '待打标': 'pending tags', '待转写': 'pending transcriptions',
                    '标签齐了': 'tags done', '转写齐了': 'transcriptions done',
                    '打标': 'Tagging', '转写': 'Transcribing' };
        var out = (m[1] || '') + '  ·  ' + seg[m[2]];
        if (m[3]) {
          var rest = m[3].split(/[\s\u00A0]*·[\s\u00A0]*/).filter(Boolean);
          for (var k = 0; k < rest.length; k++) out += '  ·  ' + (seg[rest[k]] || rest[k]);
        }
        return out + (/[\s\u00A0]$/.test(m[0]) ? ' ' : '');
      }],
    // 「打标 3」+<b>3</b>+「/3」：数字被 <b> 切走，文本节点只剩「打标 」或「打标 3」。
    [/^(打标|转写)[\s\u00A0]*(\d+)?[\s\u00A0]*(\/)?[\s\u00A0]*$/,
      function (m) {
        var w = m[1] === '打标' ? 'Tagged' : 'Transcribed';
        if (m[2] === undefined) return w + ' ';
        return w + ' ' + m[2] + (m[3] ? '/' : ' ');
      }],
    [/^\/(\d+)[\s\u00A0]*$/, function (m) { return '/' + m[1] + ' '; }],
    // 操作日志里拼出来的统计尾巴：「9 个相似结果」「1 个匹配」。
    [/^(\d+) 个相似结果$/, '$1 similar results'],
  [/^(\d+) 项相似结果$/, '$1 similar results'],
  [/^[\s\u00A0]*项相似结果[\s\u00A0]*$/, ' similar results'],
  [/^(\d+) 项低匹配$/, '$1 low-relevance'],
  [/^(\d+) 个待处理$/, '$1 pending'],
  [/^(\d+) 个旧文件$/, '$1 old files'],
  [/^(\d+) 条文件已不存在的指纹记录$/, '$1 fingerprint records whose files no longer exist'],
  [/^(\d+) 条关联$/, '$1 links'],
  [/^(\d+) 条日志$/, '$1 log entries'],
  [/^缩略图超上限，淘汰 (\d+) 个旧文件$/, function (m) { return 'Thumbnail cache over limit; evicted ' + m[1] + ' old files'; }],
  [/^清空了 (\d+) 条日志$/, function (m) { return 'Cleared ' + m[1] + ' log entries'; }],
  // 日志/提示里剩下的带数字尾巴（「移除 5 个失效记录」「开始补全，待处理 7 个」「「测试」12 项 · 向量」「已生成 12 个标签」）。
  [/^移除 (\d+) 个失效记录$/, function (m) { return 'Removed ' + m[1] + ' stale records'; }],
  [/^，清理 (\d+) 个失效记录$/, function (m) { return ', cleaned ' + m[1] + ' stale records'; }],
  [/^清理 (\d+) 个失效记录$/, function (m) { return 'Cleaned ' + m[1] + ' stale records'; }],
  [/^开始补(标签|转写)，待处理 (\d+) 个$/, function (m) {
    return 'Starting ' + (m[1] === '标签' ? 'tagging' : 'transcription') + ', ' + m[2] + ' pending';
  }],
  [/^开始补全，待处理 (\d+) 个$/, function (m) { return 'Backfill started, ' + m[1] + ' pending'; }],
  [/^，待处理 (\d+) 个$/, function (m) { return ', ' + m[1] + ' pending'; }],
  [/^待处理 (\d+) 个$/, function (m) { return m[1] + ' pending'; }],
  [/^(.*?)「(.+?)」(\d+) 项[\s\u00A0]*·[\s\u00A0]*(\S+?)[\s\u00A0]*$/, function (m) { return m[1] + '\u201c' + m[2] + '\u201d ' + m[3] + ' items · ' + TIER_EN(m[4]); }],
  [/^(\d+) 项 · (\S+)$/, function (m) { return m[1] + ' items · ' + TIER_EN(m[2]); }],
  [/^计划 (\d+) 个$/, function (m) { return 'Planned ' + m[1]; }],
  [/^本轮已补 (\d+)\/(\d+)$/, function (m) { return m[1] + '/' + m[2] + ' this round'; }],
  // 首页侧栏横幅：「有 12 个来源还没有建立索引」（前面挂着「有」，后面是动态数字）。
  [/^有 (\d+) 个来源还没有建立索引$/, function (m) { return m[1] + ' sources not indexed yet'; }],
  // 打标/转写进度 toast：「已生成 12 个标签，现在可以用文字搜到它了」（全角逗号也要转成半角）。
  [/^已生成 (\d+) 个标签，(.+)$/, function (m) { return m[1] + ' tags generated, now you can find it by text'; }],
  // 空闲补全的整行状态（#idleStat 里由 &nbsp;·&nbsp; 拼成一整条文本节点，无 <b> 时走这里）。
  [/^打标 (\d+)\/(\d+)[\s\u00A0]*·[\s\u00A0]*待打标 (\d+)[\s\u00A0]*·[\s\u00A0]*转写 (\d+)\/(\d+)[\s\u00A0]*·[\s\u00A0]*待转写 (\d+)[\s\u00A0]*·[\s\u00A0]*本轮已补 (\d+)\/(\S+)[\s\u00A0]*·[\s\u00A0]*空闲 (\d+)s \/ (\d+)s[\s\u00A0]*$/,
    function (m) {
      return 'Tagged ' + m[1] + '/' + m[2] + '  ·  ' + m[3] + ' pending tags  ·  Transcribed ' + m[4] + '/' + m[5] + '  ·  ' + m[6] + ' pending transcriptions  ·  ' + m[7] + '/' + m[8] + ' this round  ·  idle ' + m[9] + 's / ' + m[10] + 's';
    }],

    [/^(\d+) 个匹配$/, '$1 match'],
  /* 关于卡片 / 本地规模：「9 个资产 · 57 条向量」。数字和量词中间可能夹 <b>，
     文本节点会剩「 个资产 · 」「条向量」这类片段，上面词典里也各留了词条。 */
  [/^(\d+)[\s\u00A0]*个资产[\s\u00A0]*·[\s\u00A0]*(\d+)[\s\u00A0]*条向量$/, '$1 assets · $2 vectors'],
  [/^(\d+)[\s\u00A0]*个资产[\s\u00A0]*\/[\s\u00A0]*(\d+)[\s\u00A0]*条向量$/, '$1 assets / $2 vectors'],
  // 素材详情的「已索引（N 个向量）」：数字在文本节点里，整条匹配。
  [/^[\s\u00A0]*已索引（(\d+) 个向量）[\s\u00A0]*$/, function (m) { return 'Indexed (' + m[1] + ' vectors)'; }],
    // 日志详情：「文件名 · 151 字 · 10.7s」「文件名 · 12 个标签 · 4.3s」。
    // 前半段是用户文件名，必须原样保留，只翻后面的中文量词。
    [/^(.*?)[\s\u00A0]*·[\s\u00A0]*(\d+)[\s\u00A0]*字[\s\u00A0]*·/, '$1 · $2 chars ·'],
    [/^(.*?)[\s\u00A0]*·[\s\u00A0]*(\d+)[\s\u00A0]*个标签[\s\u00A0]*·/, '$1 · $2 tags ·'],
    [/^[\s\u00A0]*·[\s\u00A0]*本轮已补 (\d+)\/(\S+)[\s\u00A0]*·[\s\u00A0]*空闲 (\d+)s \/ (\d+)s[\s\u00A0]*$/,
      function (m) {
        return '  ·  ' + m[1] + '/' + m[2] + ' this round  ·  idle ' + m[3] + 's / ' + m[4] + 's';
      }],
  [/^(\d+) 个向量$/, '$1 vectors'],
  [/^([\d.]+) MB \/ 上限 (\d+) MB$/, '$1 MB / limit $2 MB'],
  [/^\/ 上限 ([\d.]+) MB$/, '/ $1 MB limit'],
  [/^\s*\/ 上限 (\d+) MB\s*$/, '/ $1 MB limit'],

  [/^打标 (\d+)\/(\d+)[\s\u00A0]*·[\s\u00A0]*待打标 (\d+)$/, 'Tagged $1/$2  ·  $3 pending tags'],
  [/^转写 (\d+)\/(\d+)[\s\u00A0]*·[\s\u00A0]*待转写 (\d+)$/, 'Transcribed $1/$2  ·  $3 pending transcriptions'],
  /* ── 整节点拼接串（<b> 把数字切走，只剩前后缀的文本节点）── */
  [/^\s*本地资产：<b>(\d+)<\/b>[\s\u00A0]*个文件[\s\u00A0]*·[\s\u00A0]*向量[\s\u00A0]*<b>(\d+)<\/b>[\s\u00A0]*条$/,
    function (m) { return 'Local assets: <b>' + m[1] + '</b> files · <b>' + m[2] + '</b> vectors'; }],
  [/^\s*索引来源：<b>(\d+)<\/b>[\s\u00A0]*种类型$/,
    function (m) { return 'Index sources: <b>' + m[1] + '</b> types'; }],
  [/^\s*位置：<b>([\s\S]+?)<\/b>([\s\S]*)$/,
    function (m) { return 'Location: <b>' + m[1] + '</b>' + m[2]; }],
  [/^\s*用户数据目录\s*$/, function () { return 'User data directory'; }],
  [/^\s*程序目录（旧位置）\s*$/, function () { return 'App directory (legacy)'; }],
  [/^\s*自定义位置（环境变量 FXSEEK_DATA_DIR）\s*$/, function () { return 'Custom location (FXSEEK_DATA_DIR)'; }],
  [/^\s*索引：<b>(\d+)<\/b>[\s\u00A0]*个文件[\s\u00A0]*\/[\s\u00A0]*<b>(\d+)<\/b>[\s\u00A0]*个向量[\s\u00A0]*\((.+)\)$/,
    function (m) { return 'Index: <b>' + m[1] + '</b> files / <b>' + m[2] + '</b> vectors (' + m[3] + ')'; }],
  [/^\s*模型：<b>([\s\S]+?)<\/b>\s*$/, function (m) { return ' Model: <b>' + m[1] + '</b>'; }],
  [/^\s*ffmpeg：<b>([\s\S]+?)<\/b>[\s\u00A0]*·[\s\u00A0]*([\s\S]+)$/,
    function (m) { return 'ffmpeg: <b>' + m[1] + '</b> · ' + m[2]; }],
  [/^\s*全部计算在本机完成，无网络请求\s*$/, function () { return 'All computation happens on this Mac. No network requests.'; }],
  [/^\s*(\d+) 个资产 · (\d+) 条向量\s*$/, function (m) { return m[1] + ' assets · ' + m[2] + ' vectors'; }],
  [/^\s*本地规模：\s*(\d+) 个资产 \/ (\d+) 条向量\s*$/, function (m) { return 'Local scale: ' + m[1] + ' assets / ' + m[2] + ' vectors'; }],
  [/^\s*预览缓存：\s*([\d.]+) MB \/ (\d+) 个文件\s*$/, function (m) { return 'Preview cache: ' + m[1] + ' MB / ' + m[2] + ' files'; }],
  [/^\s*预览缓存：\s*未知\s*$/, function () { return 'Preview cache: unknown'; }],
  /* 关于卡片的两条动态状态行（checkUpdate / openDataDirHint 拼的） */
  [/^检查失败：(.+)$/, function (m) { return 'Check failed: ' + m[1]; }],
  [/^数据目录（请手动打开）：(.+)$/, function (m) { return 'Data directory (open manually): ' + m[1]; }],
  [/^当前 <b>v([^<]+)<\/b>[\s 　]*·[\s 　]*本机构建于 ([0-9\- :]+)[\s 　]*—[\s 　]*这是本地构建版本，未接入在线更新源，没有更新的版本可下载。$/,
    function (m) { return 'Currently <b>v' + m[1] + '</b> · built locally on ' + m[2] +
      ' — this is a local build with no online update source connected, so there is nothing new to download.'; }],
  [/^\s*缓存：<b>(\d+)<\/b>[\s\u00A0]*个文件[\s\u00A0]*·[\s\u00A0]*<b>([\d.]+) MB<\/b>[\s\u00A0]*\/[\s\u00A0]*上限 (\d+) MB$/,
    function (m) { return 'Cache: <b>' + m[1] + '</b> files · <b>' + m[2] + ' MB</b> / limit ' + m[3] + ' MB'; }],
  [/^\s*占用 ([\d.]+)%，超过上限后会自动按需重建。\s*$/, function (m) { return m[1] + '% used; entries rebuild on demand once the limit is exceeded.'; }],
  [/^\s*向量库\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*指纹库\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*预览缓存\s*<b>([\d.]+ [KMG]?B)<\/b>\s*$/,
    function (m) { return 'Vector store <b>' + m[1] + '</b> · Fingerprints <b>' + m[2] + '</b> · Preview cache <b>' + m[3] + '</b>'; }],
  /* 设置页那行统计多了「搜索记录」一段（history.json）：条数写在后面的括号里。
     两段都先写「带括号」的版本，再留一个不带括号的兜底（条数拿不到时）。 */
  [/^\s*向量库\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*指纹库\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*预览缓存\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*搜索记录\s*<b>([\d.]+ [KMG]?B)<\/b>\s*（(\d+) 条搜索[\s\u00A0]*·[\s\u00A0]*(\d+) 条上传）\s*$/,
    function (m) { return 'Vector store <b>' + m[1] + '</b> · Fingerprints <b>' + m[2] + '</b> · Preview cache <b>' + m[3] + '</b> · Search history <b>' + m[4] + '</b> (' + m[5] + (m[5] === '1' ? ' search' : ' searches') + ' · ' + m[6] + (m[6] === '1' ? ' upload' : ' uploads') + ')'; }],
  [/^\s*向量库\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*指纹库\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*预览缓存\s*<b>([\d.]+ [KMG]?B)<\/b>[\s\u00A0]*·[\s\u00A0]*搜索记录\s*<b>([\d.]+ [KMG]?B)<\/b>\s*$/,
    function (m) { return 'Vector store <b>' + m[1] + '</b> · Fingerprints <b>' + m[2] + '</b> · Preview cache <b>' + m[3] + '</b> · Search history <b>' + m[4] + '</b>'; }],
  [/^\s*（(\d+) 条搜索[\s\u00A0]*·[\s\u00A0]*(\d+) 条上传）\s*$/,
    function (m) { return ' (' + m[1] + (m[1] === '1' ? ' search' : ' searches') + ' · ' + m[2] + (m[2] === '1' ? ' upload' : ' uploads') + ')'; }],
  [/^\s*向量库、指纹库、设置与预览缓存都保存在这里，和程序本身分开；升级或重装应用不会影响你的数据。\s*$/,
    function () { return 'The vector store, fingerprint store, settings, and preview cache all live here, separate from the app itself; upgrading or reinstalling won\u2019t affect your data.'; }],
  /* ── 整节点拼接串：innerHTML 里 <b> 把数字切成独立节点，
        整条文本节点是「前缀＋<b>数字</b>＋后缀」的组合，只能整体匹配。
        放在最后：前面的细粒度规则先跑，这些是兜底。 ── */
  [/^([\s\u00A0]*)本地资产：<b>(\d+)<\/b>([\s\u00A0]*)个文件([\s\u00A0]*)·([\s\u00A0]*)向量([\s\u00A0]*)<b>(\d+)<\/b>([\s\u00A0]*)条$/,
    function (m) { return m[1] + 'Local assets: <b>' + m[2] + '</b>' + m[3] + 'files' + m[4] + '·' + m[5] + '<b>' + m[7] + '</b>' + m[8] + 'vectors'; }],
  [/^([\s\u00A0]*)索引来源：<b>(\d+)<\/b>([\s\u00A0]*)种类型$/,
    function (m) { return m[1] + 'Index sources: <b>' + m[2] + '</b>' + m[3] + 'types'; }],
  [/^([\s\u00A0]*)个文件 · 向量 ([\s\u00A0]*)$/,
    function (m) { return m[1] + 'files · vectors '; }],
  [/^([\s\u00A0]*)个文件([\s\u00A0]*)\/([\s\u00A0]*)$/,
    function (m) { return m[1] + 'files' + m[2] + '/' + m[3]; }],
  [/^([\s\u00A0]*)个向量 \(([^)]*)\)$/,
    function (m) { return m[1] + 'vectors (' + m[2] + ')'; }],
  [/^([\s\u00A0]*)个文件([\s\u00A0]*)$/,
    function (m) { return m[1] + 'files' + m[2]; }],
  [/^([\s\u00A0]*)条向量$/,
    function (m) { return m[1] + 'vectors'; }],
  [/^([\s\u00A0]*)个资产([\s\u00A0]*)·([\s\u00A0]*)$/,
    function (m) { return m[1] + 'assets' + m[2] + '·' + m[3]; }],
  [/^([\s\u00A0]*)条向量([\s\u00A0]*)·([\s\u00A0]*)$/,
    function (m) { return m[1] + 'vectors' + m[2] + '·' + m[3]; }],

  /* ── MCP 状态行的整行版（带 <b>）──
     上面第 668/671 行那两条是按**纯文本节点**写的（数字直接跟在动词后面），
     可 settings.html 里这行是 innerHTML 拼的：
       `已接入 <b>2</b> 个 Agent（本机共检测到 <b>6</b> 个）。去对话里说「…」就能用上。`
     数字被 <b> 包住，文本节点只剩「已接入 」「 个 Agent（本机共检测到 」
     「 个）。去对话里说「…」就能用上。」这几截，两条都命中不了 ——
     前半截被词典逐字翻掉，后半截原样留着中文，就出现
     「Connected <b>2</b> 个 Agent（本机共检测到 <b>6</b> detected on this Mac)」
     这种中英夹生的结果。所以这里补一整行带标签的版本，交给 applyHtml 走。
     放在最后：前面那些通用碎片规则先跑，命中整行就轮不到它们了。 */
  [/^([\s\u00A0]*)已接入([\s\u00A0]*)<b>(\d+)<\/b>([\s\u00A0]*)个 Agent（本机共检测到([\s\u00A0]*)<b>(\d+)<\/b>([\s\u00A0]*)个）。去对话里说「帮我找有气球的图片」就能用上。$/,
    function (m) {
      var n = m[3], tot = m[6];
      return m[1] + 'Connected <b>' + n + '</b>' + m[4] + 'agent' + (n === '1' ? '' : 's') +
             ' (of <b>' + tot + '</b> detected on this Mac). Just ask an agent in chat, ' +
             '“find me pictures with balloons”, and it works.';
    }],
  [/^([\s\u00A0]*)本机检测到([\s\u00A0]*)<b>(\d+)<\/b>([\s\u00A0]*)个 Agent，目前都未接入。点亮右边开关即可，随时可以再关掉。$/,
    function (m) {
      var tot = m[3];
      return m[1] + 'Detected <b>' + tot + '</b>' + m[4] + 'agent' + (tot === '1' ? '' : 's') +
             ' on this Mac, none connected yet. Turn on the switch on the right to connect — ' +
             'you can turn it off anytime.';
    }],
  [/^([\s\u00A0]*)本机没有检测到支持的 Agent。装好之后再点「重新检测」。$/,
    function (m) { return m[1] + 'No supported agents detected on this Mac. Install one, then hit “Detect again”.'; }],
  /* ── 2026-10 全项目查缺补漏：运行时拼串 / <b> 切碎节点的锚定规则 ── */
  [/^新增 (\d+) 个文件$/, '$1 files added'],
  [/^耗时 ([\d.]+)s$/, 'took $1s'],
  [/^新增 (\d+)$/, 'added $1'],
  [/^跳过 (\d+)$/, 'skipped $1'],
  [/^向量 (\d+)$/, '$1 vectors'],
  [/^共 (\d+) 个文件$/, '$1 files'],
  [/^AI 描述 ([\s\S]+)$/, 'AI tagging: $1'],
  [/^转写 ([\s\S]+)$/, 'Transcribing: $1'],
  [/^新索引 (\d+) 个文件 \/ (\d+) 个向量(?:，AI 描述 (\d+) 张)?(?:，清理 (\d+) 项)?$/,
    function (m) { return 'New: ' + m[1] + ' files / ' + m[2] + ' vectors' + (m[3] ? ' · ' + m[3] + ' AI tagged' : '') + (m[4] ? ' · ' + m[4] + ' cleaned' : ''); }],
  [/^共 (\d+) 项 · 已按格式筛选 · 含 (\d+) 项未索引$/, '$1 items · filtered by format · $2 unindexed'],
  [/^共 (\d+) 项 · 已按格式筛选$/, '$1 items · filtered by format'],
  [/^共 (\d+) 项 · 含 (\d+) 项未索引$/, '$1 items · $2 unindexed'],
  [/^共 (\d+) 项$/, '$1 items'],
  [/^（当前显示 (\d+) 项）$/, ' (showing $1 items)'],
  [/^ · 仅(.+)$/,
    function (m) { var k = m[1].trim(); return ' · ' + (ZH2EN[k] || k) + ' only'; }],
  [/^· 已按格式筛选$/, '· filtered by format'],
  [/^ · 含 (\d+) 项未索引$/, ' · $1 unindexed'],
  [/^· 含 (\d+) 项未索引$/, '· $1 unindexed'],
  [/^还没有建立索引 —— 里面的文件可以看、可以打开，但$/, ' not indexed yet — files here can be viewed and opened, but '],
  [/^个来源还没有建立索引 —— 里面的文件可以看、可以打开，但$/, ' sources not indexed yet — files here can be viewed and opened, but '],
  [/^（当前显示 (\d+) 项）。建立索引只影响搜索，不影响原文件。$/, ' (showing $1 items). Indexing only affects search, not the original files.'],
  [/^。建立索引只影响搜索，不影响原文件。$/, '. Indexing only affects search, not the original files.'],
  [/^(\d+) 秒$/, '$1s'],
  [/^(\d+) 分 (\d+) 秒$/, '$1m $2s'],
  [/^(\d+) 分$/, '$1m'],
  [/^(\d+) 小时 (\d+) 分$/, '$1h $2m'],
  [/^(\d+) 小时$/, '$1h'],
  [/^与「([\s\S]*?)」是同一首歌（指纹关联 (\d+) 个窗口(?:，这首歌在这一份里出现在 ([\d.]+)s)?）$/,
    function (m) { return 'Same song as “' + m[1] + '” (fingerprint-linked ' + m[2] + ' windows' + (m[3] ? ', plays at ' + m[3] + 's in this copy' : '') + ')'; }],
  [/^匹配度 (\d+)%（原始相似度 ([\d.]+)）$/, 'match $1% (raw similarity $2)'],
  [/^((?:以图搜图|以音搜素材)(?:「[\s\S]*?」)? · )?没有找到匹配的素材$/,
    function (m) { return ((m[1] || '').replace(/以图搜图/, 'search by image').replace(/以音搜素材/, 'search by audio')) + 'No matching assets found'; }],
  [/^((?:以图搜图|以音搜素材)(?:「[\s\S]*?」)? · )?匹配完成 · 命中 (\d+) 个$/,
    function (m) { return ((m[1] || '').replace(/以图搜图/, 'search by image').replace(/以音搜素材/, 'search by audio')) + 'Match complete · ' + m[2] + ' hits'; }],
  [/^((?:以图搜图|以音搜素材)(?:「[\s\S]*?」)? · )?没有找到相似素材$/,
    function (m) { return ((m[1] || '').replace(/以图搜图/, 'search by image').replace(/以音搜素材/, 'search by audio')) + 'No similar assets found'; }],
  [/^相似检索完成 · ([\d.]+)ms$/, 'Similar search done · $1ms'],
  [/^深度检索完成 · ([\d.]+)ms$/, 'Deep search done · $1ms'],
  [/^深度检索失败：([\s\S]+)$/, 'Deep search failed: $1'],
  [/^打标失败：([\s\S]+)$/, 'Tagging failed: $1'],
  [/^转写失败：([\s\S]+)$/, 'Transcription failed: $1'],
  [/^无法访问麦克风：([\s\S]+)$/, 'Cannot access the microphone: $1'],
  [/^录音接口没反应：([\s\S]+)$/, 'Recording API did not respond: $1'],
  [/^打不开访达：([\s\S]+)$/, 'Cannot reveal in Finder: $1'],
  [/^打开失败：([\s\S]+)$/, 'Open failed: $1'],
  [/^无法预览：([\s\S]+)$/, 'Cannot preview: $1'],
  [/^读取文件失败 HTTP (\d+)$/, 'file read failed (HTTP $1)'],
  [/^这段音频里没有识别到语音内容 · ([\d.]+)s$/, 'No speech detected in this audio · $1s'],
  [/^([\s·]*)文字结果偏弱，已补上 (\d+) 段指纹命中([\s]*)$/, function (m) {
    return m[1] + 'Weak text results — added ' + m[2] + ' fingerprint hits' + m[3];
  }],
  [/^匹配完成 · 命中 (\d+) 个$/, 'Match complete · $1 hits'],
  [/^命中 (\d+) 个$/, '$1 hits'],
  /* 索引完成的后端 message / 操作日志尾巴（app.py refresh 完成、indexer done、日志耗时） */
  [/^完成：没有发现新文件(?:，清理 (\d+) 个失效记录)?(?:，耗时 ([\d.]+)s)?$/,
    function (m) { return 'Done: no new files found' + (m[1] ? ' · cleaned ' + m[1] + ' stale records' : '') + (m[2] ? ' · took ' + m[2] + 's' : ''); }],
  [/^完成：新增 (\d+) 个文件 \/ (\d+) 个向量(?:，清理 (\d+) 个失效记录)?(?:，耗时 ([\d.]+)s)?$/,
    function (m) { return 'Done: added ' + m[1] + ' files / ' + m[2] + ' vectors' + (m[3] ? ' · cleaned ' + m[3] + ' stale records' : '') + (m[4] ? ' · took ' + m[4] + 's' : ''); }],
  [/^索引异常：([\s\S]+)$/, 'Indexing error: $1'],
  [/^建立 (\d+) 条关联$/, 'created $1 links'],
  [/^关联扫描失败：([\s\S]+)$/, 'Link scan failed: $1'],
  [/^移除 (\d+) 个失效记录，耗时 ([\d.]+)s$/, 'removed $1 stale records · took $2s'],

  [/^恢复 (\d+) 个文件$/, 'Restore $1 files'],
  [/^彻底删除 (\d+) 个文件$/, 'Delete $1 files permanently'],
  [/^已选择：([\s\S]+)$/, 'Selected: $1'],
  [/^已取消：([\s\S]+)$/, 'Cancelled: $1'],
  [/^已添加来源：([\s\S]+)$/, 'Source added: $1'],
  [/^开始索引：([\s\S]+)$/, 'Start indexing: $1'],
  [/^统计失败：([\s\S]+)$/, 'Stats failed: $1'],
  [/^\s*个文件（只统计了前 10 万个）$/, ' files (first 100k counted)'],
  [/^图片 (\d+) · 视频 (\d+) · 音频 (\d+) · 文档 (\d+)(?: · 其他 (\d+)（不索引）)?$/,
    function (m) { return 'Images ' + m[1] + ' · Videos ' + m[2] + ' · Audio ' + m[3] + ' · Documents ' + m[4] + (m[5] ? ' · Other ' + m[5] + ' (not indexed)' : ''); }],
  [/^（新增 ([\d,]+) 个文件，之后只补新文件）$/, '(add $1 new files, then only new ones)'],
  [/^（预计 ([\s\S]+?)）。如果现在不方便，可以先选「稍后再索引」，等电脑空闲时再点左侧的来源开始；也可以在设置里打开「闲置时自动打标」，让 AI 标签分摊到空闲时段。$/,
    function (m) { return ' (estimated ' + m[1] + '). If now is not convenient, pick “Index later” and start from the source on the left when the Mac is idle; or turn on “Tag while idle” in Settings to spread AI tagging across idle time.'; }],
  [/^预计需要 ([\s\S]+?)，期间会占用 CPU\/GPU。可以等空闲再索引。$/,
    function (m) { return 'Estimated ' + m[1] + ', using CPU/GPU in the process. You can index when idle.'; }],
  [/^已全部索引过，只做增量检查 —— $/, ' already indexed — only an incremental check '],
  [/^正在(转写|打标) 最后处理「([\s\S]+?)」$/,
    function (m) { return 'Now ' + (m[1] === '转写' ? 'transcribing' : 'tagging') + ' · last: ' + m[2]; }],
  [/^正在转写$/, 'Transcribing…'],
  [/^正在打标$/, 'Tagging…'],
  [/^上次失败：([\s\S]+)$/, 'Last failure: $1'],
  [/^(\d+) 个待补$/, '$1 pending'],
  [/^ · 已完成 (\d+)( · 失败 (\d+))?( · 还剩 (\d+))?( 正在处理「([\s\S]+?)」)?$/,
    function (m) { return ' · done ' + m[1] + (m[3] ? ' · failed ' + m[3] : '') + (m[5] ? ' · left ' + m[5] : '') + (m[7] ? ' · processing “' + m[7] + '”' : ''); }],
  [/^开始补全，待处理 (\d+) 个$/, 'Backfill started, $1 pending'],
  [/^技能注入：(\d+) 个失败 · ([\s\S]*)$/, 'Skill injection: $1 failed · $2'],
  [/^技能都已就位（(\d+) 个），没有需要补的$/, 'Skills are all in place ($1) — nothing to add'],
  [/^技能注入失败：([\s\S]+)$/, 'Skill injection failed: $1'],
  [/^技能已就位：([\s\S]+)$/, 'Skill in place: $1'],
  [/^已接入 (\d+) 个 Agent，并补上了 (\d+) 个技能$/, 'Connected $1 agents, and added $2 skills'],
  [/^已接入 (\d+) 个 Agent（技能保留，没删）$/, 'Connected $1 agents (skills kept)'],
  [/^已断开 (\d+) 个 Agent（技能保留，没删）$/, 'Disconnected $1 agents (skills kept)'],
  [/^(接入|断开)失败：([\s\S]+)$/,
    function (m) { return (m[1] === '接入' ? 'Connect' : 'Disconnect') + ' failed: ' + m[2]; }],
  [/^\s*发现 <b>(\d+)<\/b> 个语音转写模型，已列出：选一个再点「保存配置」$/, ' Found <b>$1<\/b> transcription models — pick one, then click “Save”.'],
  [/^\s*发现 <b>(\d+)<\/b> 个能看图的模型，已列出：选一个再点「保存配置」$/, ' Found <b>$1<\/b> vision models — pick one, then click “Save”.'],
  [/^\s*发现 <b>(\d+)<\/b> 个模型，已列出：选一个再点「保存配置」$/, ' Found <b>$1<\/b> models — pick one, then click “Save”.'],
  [/^配置已导出：([\s\S]+)$/, 'Settings exported: $1'],
  [/^导出失败：([\s\S]+)$/, 'Export failed: $1'],
  [/^恢复失败：([\s\S]+)$/, 'Restore failed: $1'],
  [/^请求失败：([\s\S]+)$/, 'Request failed: $1'],
  /* ── 搜索状态补充（本轮用户圈出的漏翻）── */
  [/^语音检索失败：([\s\S]+)$/, 'Voice search failed: $1'],
  [/^临时垃圾桶 · (\d+) 个文件（(\d+) 天后自动清除）$/, 'Recycle bin · $1 files (auto-cleared in $2 days)'],
  [/^最近删除 · (\d+) 天后自动清除$/, 'Recently deleted · auto-cleared in $1 days'],
  /* ── 第11轮：麦克风面板/垃圾桶批量/文本预览/设置 MCP ── */
  [/^把握 (\d+)%$/, 'Confidence $1%'],
  [/^已全部还原 (\d+) 个文件$/, 'Restored all $1 files'],
  [/^已全部彻底删除 (\d+) 个文件$/, 'Permanently deleted all $1 files'],
  [/^已转写 (\d+) 字 · ([\d.]+)s，现在可以按歌词\/台词搜到它了$/, 'Transcribed $1 chars · $2s — you can now search by lyrics/dialogue'],
  [/^从 ([\d:]+) 开始播放（匹配帧定位）$/, 'Playing from $1 (match-frame position)'],
  [/^(.+?) · (\d+) 行 · ([\s\S]+?) · 已截断显示$/, '$1 · $2 lines · $3 · truncated for display'],
  [/^(.+?) · (\d+) 行 · ([\s\S]+)$/, '$1 · $2 lines · $3'],
  [/^老位置还有一份备份：([\s\S]+)$/, 'A backup still exists at the old location: $1'],
  [/^MCP 模块不可用：([\s\S]+)$/, 'MCP module unavailable: $1'],
  [/^(接入|断开)：(\d+) 个未成功 · ([\s\S]*)$/,
    function (m) { return (m[1] === '接入' ? 'Connect' : 'Disconnect') + ': ' + m[2] + ' failed · ' + m[3]; }],
  /* ── 麦克风/录音链路：backend message + 语音操作日志 ── */
  [/^录音数据解析失败：([\s\S]+)$/, 'Recording data parse failed: $1'],
  [/^无法读取这段录音：([\s\S]+)$/, 'Cannot read this recording: $1'],
  [/^留档失败：([\s\S]+)$/, 'Archive failed: $1'],
  [/^录音已留档 → ([\s\S]+)$/, 'Recording archived → $1'],
  [/^指纹未达线（(.*?) 票([\d\/]+) 均分([\d.]+) 对齐(\d+) 抖动(\d+)）$/,
    function (m) { return 'Fingerprint below threshold (' + (m[1] === '无候选' ? 'no candidate' : m[1]) + ', votes ' + m[2] + ', avg ' + m[3] + ', aligned ' + m[4] + ', jitter ' + m[5] + ')'; }],
  [/^指纹未达线（([\s\S]+?)）$/,
    function (m) { return 'Fingerprint below threshold (' + (m[1] === '无候选' ? 'no candidate' : m[1]) + ')'; }],
  [/^指纹精确命中（(.*?) 票([\d\/]+) 均分([\d.]+) 对齐(\d+) 抖动(\d+)）(?: \+ (\d+) 个同一首歌)?$/,
    function (m) { return 'Fingerprint exact hit (' + (m[1] === '无候选' ? 'no candidate' : m[1]) + ', votes ' + m[2] + ', avg ' + m[3] + ', aligned ' + m[4] + ', jitter ' + m[5] + ')' + (m[6] ? ' (+' + m[6] + ' same song)' : ''); }],
  [/^指纹精确命中（([\s\S]+?)）(?: \+ (\d+) 个同一首歌)?$/,
    function (m) { return 'Fingerprint exact hit (' + m[1] + ')' + (m[2] ? ' (+' + m[2] + ' same song)' : ''); }],
  [/^忽略 (\d+) 条文件已不存在的指纹记录$/, 'Ignored $1 fingerprint records whose files no longer exist'],
  [/^(\d+) 个匹配(?: \+ (\d+) 个同一首歌)?$/,
    function (m) { return m[1] + ' matches' + (m[2] ? ' (+' + m[2] + ' same song)' : ''); }],
  [/^(\d+) 个匹配 · 哼唱得分 ([\d.]+)$/, '$1 matches · humming score $2'],
  [/^转写只有语气词（「([\s\S]{1,8})」），不拿去搜$/,
    function (m) { return 'Transcript was only filler (' + m[1] + ') — not used for search'; }],
  [/^转写只有语气词（「([\s\S]{1,8})」），按没听清处理$/,
    function (m) { return 'Transcript was only filler (' + m[1] + ') — treated as not heard'; }],
  [/^指纹 0 命中 → 转文字兜底「([\s\S]{1,24})」$/,
    function (m) { return 'Fingerprint 0 hits → fell back to text (' + m[1] + ')'; }],
  [/^转写为空，指纹兜底命中 (\d+) 个$/, 'Transcript empty — fingerprint fallback matched $1'],
  /* ── 哼唱/同一首歌 结果条（单文本节点整行拼串）── */
  /* 语音条 note 形态：听出原曲：指纹精确匹配 · 文件名（文件名=数据，$1 原样保留） */
  /* 语音条 note 带前导 ' · ' 拼接：裸词典键对不上整节点（同听出原曲坑）→ 规则兜前缀 */
  [/^([\s·]*)指纹没找到，改用歌词文字搜([\s]*)$/,
    function (m) { return m[1] + 'No fingerprint match — falling back to lyrics search' + m[2]; }],
  [/^听出原曲：指纹精确匹配(?: · ([\s\S]+))?$/,
    function (m) { return 'Original track found: fingerprint matched' + (m[1] ? ' · ' + m[1] : ''); }],
  [/^听出原曲：指纹精确匹配 (\d+) 个素材 · 仅音频与视频$/, 'Original track found: fingerprint matched $1 assets · audio & video only'],
  [/^听出原曲：指纹精确匹配 (\d+) 个素材 · 含 (\d+) 个同一首歌 · 仅音频与视频$/, 'Original track found: fingerprint matched $1 assets (incl. $2 of the same song) · audio & video only'],
  [/^哼唱匹配到 (\d+) 个素材( · 已隐藏 (\d+) 项低匹配)? · 仅音频与视频$/,
    function (m) { return 'Humming matched ' + m[1] + ' assets' + (m[3] ? ' · ' + m[3] + ' low-relevance hidden' : '') + ' · audio & video only'; }],
  /* ── 兜底补漏：半角冒号变体 / 清理失败 ── */
  [/^清理失败：([\s\S]+)$/, 'Cleanup failed: $1'],
  [/^加载失败: ([\s\S]+)$/, 'Load failed: $1'],
  [/^加载失败：([\s\S]+)$/, 'Load failed: $1'],
  /* 预览信息 title="标签：提取方式"（两段各自在词典里） */
  [/^([^：]{1,16})：(直读 utf-8 \/ gbk|直读文本|pdfplumber 提取|系统 textutil|OLE2 \+ BIFF（xlrd）|OLE2 容器，正文可能读不出)$/,
    function (m) { return (ZH2EN[m[1]] || m[1]) + ': ' + (ZH2EN[m[2]] || m[2]); }],
  /* ── 第13轮：详情页（右侧详情栏 + 双击预览顶栏）── */
  [/^(\d+) 字$/, '$1 chars'],
  [/^详情读取失败：([\s\S]*)$/, 'Failed to load details: $1'],
  [/^这张素材还没有生成描述与标签\s*——\s*用文字是搜不到它的（只能靠以图搜图\/文件名命中）。$/,
    "No description or tags yet — you cannot search this one by text (image search or filename only)."],
  [/^还没有转写 —— 里面的歌词或说话内容\s*现在还搜不到（只能靠文件名与指纹匹配）。$/,
    "Not transcribed yet — the lyrics or speech inside cannot be searched yet (filename and fingerprint only)."],
  [/^用设置里的视觉模型给这一张生成，约 10~60 秒(（会把关键帧拼成九宫格，喂给视觉模型看一次）)?；\s*生成后立刻可以用文字搜到它。不想手动点，也可以在「设置 → 智能服务」里开「闲置时自动处理」，让它在你不用的间隙慢慢补。$/,
    function (m) {
      return 'Generate with the vision model, about 10–60s' +
        (m[1] ? ' (key frames are stitched into a 3×3 grid and shown to the model once)' : '') +
        '; it becomes searchable by text right away. Prefer not to click manually? Turn on “Idle-time auto processing” in Settings → AI services and let it fill in while you are away.';
    }],
  [/^用设置里的语音识别服务转写这一段，几秒到几十秒；\s*转写后立刻可以按歌词\/台词搜到它。不想手动点，也可以在「设置 → 智能服务」里开\s*「闲置时转写音频」，让它在你不用的间隙慢慢补。$/,
    'Transcribe this with the speech recognition service from settings — a few seconds to tens of seconds; once done you can search it by lyrics/dialogue right away. Prefer not to click manually? Turn on “Idle-time transcription” in Settings → AI services and let it fill in while you are away.'],
  [/^这段音频超过了设置里的「最长转写时长」，已跳过。\s*想转写它，可以在「设置 → 智能服务 → 音频转写」把上限调大（或填 0 表示不限），再回来点一次。$/,
    'This audio is longer than the “max transcription length” setting and was skipped. To transcribe it, raise the limit in Settings → AI services → Audio transcription (or set 0 for unlimited) and try again.'],
  [/^这段音频里没有识别到可用的语音内容（纯音乐或环境音）。$/,
    'No usable speech found in this audio (music or ambient sound only).'],

  ];

  /* 绝不翻译的容器：日志、文件名、路径、搜索历史 —— 全是用户自己的内容。
     注意这里**不含 `#q`**：搜索框里用户敲的字在 `input.value` 上，而翻译器只碰
     `ATTRS` 那四个属性、从不读 value，所以把 `#q` 拉黑等于顺手把它的
     `placeholder`（那是界面自带文案，不是用户内容）也一起放过了。
     真该挡的是搜索*历史*，那是 `.qhist` / `.histbox` 的事。 */
  var SKIP_SEL = '.loglist,table.logs,.qhist,.srclist .nm,.card .nm,.row .nm,' +
                 '.path,.p,.msgbody,.histbox,.logrow,' +
                 /* 设置页的「运行日志」卡片用的是 .logview / .lrow 这套类名，
                    和上面的 .loglist / .logrow 不是一套。漏了它，日志正文
                    （里面全是用户自己的文件名和路径）就会被翻译器碰到。 */
                 '.logview,.lrow';

  var LANG = 'zh';
  var origText = new WeakMap();   // 文本节点 → 原始字符串
  var origAttr = new WeakMap();   // 元素 → {attr: 原始值}

  function lang() { return LANG; }
  function isEn() { return LANG === 'en'; }

  /* JS 里需要按语言分支时用它：T('中文', 'English') */
  function T(zh, en) { return isEn() ? en : zh; }
  /* 检索档位（向量 / 深度语义 / 文件名）在日志尾巴里单独出现，转成英文。 */
  function TIER_EN(w) {
    if (w === '向量') return 'vector';
    if (w === '深度语义') return 'deep semantic';
    if (w === '文件名') return 'filename';
    return w;
  }

  /* 结果栏模式名（找到 N 项 · XXX · Nms）转英文。 */
  function MODE_EN(w) {
    if (w === '文件名') return 'filename';
    if (w === '向量') return 'vector';
    if (w === '深度语义') return 'deep semantic';
    if (w === '自动') return 'auto';
    return w;
  }

  /* 结果栏的中文尾巴（「 · 已隐藏 8 项低匹配」等）逐段过一遍词典/规则。
     整句规则里用它兜底，免得为了几个固定片段再写一轮正则。 */
  /* 前缀（「以音搜素材「X」 · 」）翻完去掉多余的分隔符，只留一个空格。 */
  function leadTail(s) {
    var o = zhTail(s);
    if (!o) return '';
    return o.replace(/^[\s\u00A0]*·[\s\u00A0]*/, '') + ' ';
  }

  function zhTail(s) {
    if (!s) return s;
    return s.split(/[\s\u00A0]*·[\s\u00A0]*/).map(function (seg) {
      var t = seg.trim();
      if (!t) return '';
      /* 片段未必与词典键逐字一致（前后空格、写法差异），补两个变体再查一次。
         ★ 每个变体必须拿「变体自己的原文」当基准判成败：老写法是
           if (o === t)，可变体带了前缀（'· ' + t）后**即使没翻出来**返回值
           也必然 !== t，链条在 v2 就断掉、v3（真正带前导空格、能命中
           `^ · 仅(.+)$` 这类规则的形态）永远跑不到 —— 这就是
           「找到 1 项 · 仅电子表格」这类尾巴一直留中文的根子。 */
      var o = translate(t);
      if (o === t) { var v = '· ' + t; o = translate(v); if (o === v) o = t; }
      if (o === t) { var v = ' · ' + t; o = translate(v); if (o === v) o = t; }
      if (o === t) { var v = t + ' '; o = translate(v); if (o === v) o = t; }
      return o || t;
    }).filter(function (x) { return x; }).map(function (x) {
      return x.indexOf('·') === 0 ? ' ' + x : ' · ' + x;
    }).join('');
  }

  /* 查词典；查不到就套 RULES；再不行原样返回。 */
  function translate(s) {
    if (!s) return s;
    var t = ZH2EN[s];
    if (t !== undefined) return t;
    /* 词典里存的是「裸文案」，但模板字符串拼出来的文本节点常带换行和缩进
       （比如排序菜单里的 "\n       无"）。整串查不到时，退一步用去掉首尾
       空白后再查一次，命中就把原文的首尾空白原样接回去 —— 这样既不用给
       每种缩进写一条正则，也不会弄丢布局需要的空格。 */
    var lead = s.match(/^[\s\u00A0]*/)[0];
    var tail = s.match(/[\s\u00A0]*$/)[0];
    if (lead || tail) {
      var core = s.slice(lead.length, s.length - tail.length);
      if (core) {
        var t2 = ZH2EN[core];
        if (t2 !== undefined) return lead + t2 + tail;
      }
    }
    for (var i = 0; i < RULES.length; i++) {
      var m = s.match(RULES[i][0]);
      if (m) {
        var rep = RULES[i][1];
        // 传整个 m（匹配数组），不要写成 rep.apply(null, m)：那会把 m[0]/m[1]…
        // 摊成第 1/2… 个位置实参，函数收到的 m 就变成「整串匹配结果」这个字符串
        // 本身，于是 m[1] 取到的是它第 1 个字符（于是出现 "MCP: C agents" 这种鬼话）。
        if (typeof rep === 'function') return rep(m);
        return s.replace(RULES[i][0], rep);
      }
    }
    return s;
  }

  function skip(el) {
    if (!el || el.nodeType !== 1) return false;
    /* 顺序很重要：
       1) `data-i18n-ok` 永远放行 —— 页面用它标「固定文案」；
       2) `data-no-i18n` 永远挡住 —— 页面用它标「用户内容」。
       为什么要这么绕：SKIP_SEL 是按容器写的（`.srclist .nm` / `.qhist`），
       而 TreeWalker 对元素 REJECT 会**连整棵子树一起跳过**，所以在子树里
       给某个标签加豁免是没用的 —— 得让被 REJECT 的那个容器自己带
       `data-i18n-ok`，再把它里面真正属于用户内容的节点重新标回
       `data-no-i18n` 挡住。两者同时出现时以 `data-no-i18n` 为准。 */
    if (el.hasAttribute && el.hasAttribute('data-no-i18n')) return true;
    if (el.hasAttribute && el.hasAttribute('data-i18n-ok')) return false;
    /* 顺带记一笔：`.tempty`（空态占位符）**不能**在这里豁免 —— TreeWalker 是在
       它的容器（`.logview`）那一层就 REJECT 掉的，skip() 根本轮不到那一步。
       它的翻译放在 applyNode() 末尾单独捞，见那里的注释。 */
    /* 只有 `data-i18n-ok` 容器**自己**被放行还不够：SKIP_SEL 里的选择器
       有不少是「后代型」的（`.srclist .nm`、`.card .nm`），容器里的
       `<span class="nm">` 用 closest() 自己就能命中自己，于是元素在
       TreeWalker 里被 REJECT、子树又整片跳过。所以这里再看一眼：
       只要最近的 `data-i18n-ok` 祖先比被命中的 SKIP 容器更近，
       就认定这是容器标了「这段是固定文案」——
       真正的用户内容（目录名/文件名）由页面标 `data-no-i18n`，
       上面第一行已经先挡住了。 */
    var hit = el.closest && el.closest(SKIP_SEL);
    if (!hit) return false;
    var ok = el.closest && el.closest('[data-i18n-ok]');
    if (!ok) return true;
    /* hit 和 ok 谁离 el 更近？用 DOM 深度比：
          <div class="qhist" data-i18n-ok>
            <div class="qhhd"><span class="t">最近上传</span></div>
        el = SPAN.t，hit = DIV.qhist（就是带豁免的那个容器本身），
        ok = DIV.qhist —— 两者是同一个节点，此时必须判「放行」。
        若换成用户内容：
          <div class="srclist" data-i18n-ok>
            <span class="nm" data-no-i18n><i>目录名</i></span>
        el = SPAN.nm，hit = SPAN.nm，ok = DIV.srclist：ok 更浅，说明
        「豁免在更外层、命中在里层」，该挡 —— 不过这种情况上面第一行
        的 data-no-i18n 已经先挡掉了。 */
    if (hit === ok) return false;
    return hit.contains(ok);
  }

  var ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];

  // 属性翻译单独拎出来：既要给 applyNode 的根节点用，也要给遍历到的
  // 后代元素用 —— 只看根节点的话，JS 用 innerHTML 塞进来的整块卡片
  // （里面的 title/placeholder 全在后代上）永远翻不到。
  function applyAttr(el) {
    if (!el || el.nodeType !== 1 || skip(el)) return;
    for (var a = 0; a < ATTRS.length; a++) {
      var name = ATTRS[a];
      if (!el.hasAttribute(name)) continue;
      var cur = el.getAttribute(name);
      var store = origAttr.get(el);
      if (!store) { store = {}; origAttr.set(el, store); }
      if (store[name] === undefined) store[name] = cur;
      var want = isEn() ? translate(store[name]) : store[name];
      if (cur !== want) el.setAttribute(name, want);
    }
  }

  function applyNode(root) {
    if (!root) return;
    if (root.nodeType === 3) { applyText(root); return; }
    if (root.nodeType !== 1) return;
    if (skip(root)) return;

    // 1) 属性
    applyAttr(root);

    // 2) 自身文本节点 + 后代（TreeWalker 比递归 querySelectorAll 稳，
    //    也不会把 <script>/<style> 里的内容当文案翻）
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: function (n) {
        if (n.nodeType === 1) {
          var tag = n.tagName;
          if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') {
            return NodeFilter.FILTER_REJECT;
          }
          if (skip(n)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var p = n.parentNode;
        if (p && skip(p)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n;
    while ((n = w.nextNode())) {
      if (n.nodeType === 3) applyText(n);
      else if (n.nodeType === 1) applyAttr(n);
    }

    // 3) 整行兜底：文本节点被 <b> 切碎时，上面的 applyText 只会翻到碎片。
    //    这里拿容器的 innerHTML 整行试一次，专治带 <b> 的那些规则。
    //    从 root 自己开始 —— 页面常常拿 refresh(某个容器) 单点调用。
    applyHtml(root);
    var els = root.querySelectorAll('*');
    for (var i = 0; i < els.length; i++) applyHtml(els[i]);

    /* 4) 空态占位符单独放行。`.logview`（运行日志卡片）整块在 SKIP_SEL 里，
       TreeWalker 在容器那层就 REJECT 了，子树根本走不到 —— 所以上面
       在 skip() 里给 `.tempty` 开的那个口子到不了这里，得自己捞一遍：
       `.tempty` 全应用只有设置页那几处「暂无日志 / 暂无记录」这种固定文案。 */
    var empties = root.querySelectorAll ? root.querySelectorAll('.tempty') : [];
    for (var k = 0; k < empties.length; k++) {
      var pe = empties[k];
      if (pe.hasAttribute && pe.hasAttribute('data-no-i18n')) continue;
      if (pe.firstChild && pe.firstChild.nodeType === 3) applyText(pe.firstChild);
    }
  }

  /* 同一个中文词在不同地方要翻成不同英文：「关闭」在弹窗里是 Close（关掉这个框），
     在开关对里是 Off（跟「开启 / On」配成一对）。只靠词条区分不了 ——
     词条的键就是中文字符串本身，改全局词条会把弹窗那个「关闭」也变成 Off。
     所以按属性区分：设置页那 7 个开关按钮都带 data-v="off"（`ui.html` 里没有）。 */
  var BY_ATTR = [
    ['[data-v="off"]', { '关闭': 'Off' }],
    ['[data-v="on"]', { '开启': 'On' }]
  ];
  function ctxTranslate(node, s) {
    var el = node.parentElement;
    if (!el || !el.matches || !el.getAttribute) return null;
    var t = s.replace(/^\s+|\s+$/g, '');
    for (var i = 0; i < BY_ATTR.length; i++) {
      if (!el.matches(BY_ATTR[i][0])) continue;
      var m = BY_ATTR[i][1];
      if (Object.prototype.hasOwnProperty.call(m, t)) return m[t];
    }
    return null;
  }

  function applyText(node) {
    var cur = node.nodeValue;
    if (origText.has(node)) {
      // 已被翻译过：只在「当前值仍是我们写进去的译文」时才回写，
      // 否则说明 JS 自己更新了这个节点（比如计数刷新），要重新取原文。
      var rec = origText.get(node);
      if (cur !== rec.out) { rec.zh = cur; }
      rec.out = isEn() ? (ctxTranslate(node, rec.zh) || translate(rec.zh)) : rec.zh;
      if (cur !== rec.out) node.nodeValue = rec.out;
      return;
    }
    var rec2 = { zh: cur, out: null };
    rec2.out = isEn() ? (ctxTranslate(node, cur) || translate(cur)) : cur;
    origText.set(node, rec2);
    if (rec2.out !== cur) node.nodeValue = rec2.out;
  }

  /* ---------- 整行 innerHTML 翻译（治「数字夹在 <b> 里」） ----------
     为什么需要这一步：翻译器只吃**文本节点**，而 <b> 会把一行切成好几段，
     `缓存：<b>7</b> 个文件 · <b>0.17 MB</b> / 上限 1024 MB` 到 rule 手上只剩
     「缓存：」「 个文件 · 」「 MB / 上限 1024 MB」三截碎片。
     碎片里既没有数字也没有整句，所以 RULES 里那些带 <b> 的整行规则
     **一条都不可能命中**（标签不在文本节点的 nodeValue 里）。

     办法：挑出「子孙里还有元素、且自己文本够长」的容器，把它的 innerHTML
     原样交给 translate() 试一次。命中了才写回 —— RULES 里那十几条带 <b>
     的规则本来就是按这个格式写的，正好对上。
     安全边界：
      · 只认叶子容器（子孙元素不超过 3 个），免得把整张卡片的 HTML 吞掉；
      · skip() 的容器一律跳过，用户数据永不进这个通道；
      · 只在「翻译结果确实变了」时写回，写回后再走一遍文本节点，
        让 origText 记录跟上（否则来回切语言会把译文当原文）。
     幂等靠 origHtml 那份 WeakMap：永远从最初一份原文现算。 */
   var origHtml = new WeakMap();
  function applyHtml(el) {
    if (!el || el.nodeType !== 1 || skip(el)) return;
    // 没有任何元素子节点 → 纯文本，applyText 已经处理过，不必重复
    var kids = el.children;
    if (!kids || !kids.length || kids.length > 3) return;

    /* 原文怎么来：**永远在中文态下取**。
       如果此刻已经是 en，innerHTML 里存的可能就是我们自己写进去的译文，
       再把它当原文缓存下来，切回中文就换不回去了（会把译文当原文留着）。
       所以缓存只在 isEn() 为假时才建立；en 态下拿不到缓存就直接不处理 ——
       反正切到 zh 时那一趟会把缓存补上，再切回 en 自然就正常了。 */
    var rec = origHtml.get(el);
    if (rec === undefined) {
      // 第一次见：收的是「还带着中文」的原文。判断依据是内容里有没有中文，
      // **不能**用 isEn() 挡 —— 页面总是先按中文拼 innerHTML 再翻译，
      // 若语言已持久化为 en，页面加载那一刻 LANG 已经是 'en'，这里 isEn()
      // 就恒真、永远建不起缓存，带 <b> 的整行（如「本机检测到 <b>6</b> 个
      // Agent…」）就永远翻不动、原样留中文。反过来，已经翻成英文的内容里
      // 没有中文，下面那个「有中文才收」的判断会自然把它挡掉，不会把译文
      // 误当原文缓存（那才是当初加 isEn() 要防的事）。
      var first = el.innerHTML;
      if (first.indexOf('<') < 0 || !/[\u4e00-\u9fff]/.test(first)) return;
      rec = { zh: first, out: null };
      origHtml.set(el, rec);
    } else if (el.innerHTML !== rec.out && el.innerHTML !== rec.zh) {
      /* 关键：JS 自己把这个容器重写了（缓存数、模型名、索引统计这些行
         都是 fetch 回来后重新 innerHTML 的）。此时 rec 里那份原文已经作废 ——
         继续拿它去翻译，就会把**上一次**的译文盖到这一次的新内容上
         （实测：五个不同容器全被写成「Cache: 7 files…」）。
         applyText 早就用 `cur !== rec.out` 处理过同一个问题，这里照抄一份：
         内容既不是我们写的译文、也不是原文 → 说明是新的中文原文，重新记录。
         en 态下新内容本来就是中文（页面始终按中文拼 HTML），所以可以直接收。 */
      if (!/[\u4e00-\u9fff]/.test(el.innerHTML)) { origHtml.delete(el); return; }
      rec.zh = el.innerHTML;
    }
    var want = isEn() ? translate(rec.zh) : rec.zh;
    rec.out = want;
    if (el.innerHTML !== want) el.innerHTML = want;
  }

  /* 切换语言：设 <html lang>、跑一遍、并让后续 DOM 变更自动跟上。 */
  var mo = null;
  function setLang(l) {
    LANG = (l === 'en') ? 'en' : 'zh';
    document.documentElement.lang = (LANG === 'en') ? 'en' : 'zh-CN';
    document.documentElement.setAttribute('data-i18n', LANG);
    applyNode(document.body);
    setTitle();
    watch();
  }

  /* <title> 在 <head> 里，applyNode 只从 body 往下走，够不着它。
     单独翻一下 —— 永远从最初那份原文现算，来回切不会越翻越长。
     注意：document.title 不是 HTML，词条里的 &amp; 这类实体要还原回字符，
     否则标题栏会直愣愣显示 "Settings &amp; backup"。 */
  var _t0 = null;
  function unent(s) {
    return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>')
            .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
            .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&');
  }
  function setTitle() {
    if (_t0 === null) _t0 = document.title;
    if (_t0) document.title = isEn() ? unent(translate(_t0)) : unent(_t0);
  }

  /* MutationObserver：JS 拼出来的卡片、toast、状态行都会命中。
     **必须去抖** —— 索引/补全进行中时 DOM 每秒要变动几十次，
     不防抖会把主线程吃满（这正是「开着的时候后台每 20 秒看一眼」
     那段逻辑最怕的事）。 */
  var pending = false;
  var _mq = [];      // 防抖窗口内新到的 mutation 先排队，不丢（见下注）
  function watch() {
    if (mo || !window.MutationObserver || !document.body) return;
    mo = new MutationObserver(function (muts) {
      if (LANG !== 'en') return;      // 中文是原文，不用翻译，省掉全部开销
      /* ★ 旧实现是 `if (pending) return;` —— 防抖 50ms 窗口里到达的第二波
         mutation 被**整批丢弃**，那批新建的语音条/状态条节点就永远停在中文
         （后台轮询恰好先动了一下 DOM 就必现，实测「连点两次语音条」间歇复现）。
         改为先入队、drain 完若又有存货再排下一轮，谁都不丢。 */
      for (var qi = 0; qi < muts.length; qi++) _mq.push(muts[qi]);
      if (pending) return;
      pending = true;
      var drain = function () {
        pending = false;
        var batch = _mq; _mq = [];
        // 只处理新增节点；已存在节点上被改的文本由 applyText 的
        // 原文比对兜住（mutation 里丢掉了旧值，重跑整棵树代价太大）。
        for (var i = 0; i < batch.length; i++) {
          var mu = batch[i];
          // 属性被 JS 直接 setAttribute 改掉（比如技能徽标的 title）。
          // 这类 mutation 不带来 addedNodes，只看新增节点就会漏。
          if (mu.type === 'attributes') { applyAttr(mu.target); continue; }
          var added = mu.addedNodes;
          for (var j = 0; j < added.length; j++) {
            if (added[j].nodeType === 1) applyNode(added[j]);
            else if (added[j].nodeType === 3) applyText(added[j]);
          }
        }
        if (_mq.length) { pending = true; setTimeout(drain, 50); }
      };
      setTimeout(drain, 50);
    });
    mo.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ATTRS
    });
  }

  window.I18N = {
    apply: applyNode,
    setLang: setLang,
    lang: lang,
    isEn: isEn,
    T: T,
    dict: ZH2EN,
    // 供页面在「JS 整块重建了某个容器」之后手动补一次翻译。
    // 观察器只看 addedNodes，对 innerHTML 整体替换后新增的子树够得着，
    // 但如果替换发生在观察器启动之前（或 LANG 还是 zh 时），就得靠这里补。
    refresh: function (root) { applyNode(root || document.body); }
  };

  // 页面里可能在 i18n.js 之前就设过 data-lang，这里补齐一次。
  if (document.body) applyNode(document.body);
})();
