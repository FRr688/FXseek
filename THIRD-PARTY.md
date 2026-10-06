# 第三方组件与许可（Third-Party Notices）

本项目**自身代码**采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。
但它依赖、内置或运行期下载了若干第三方组件，它们各自保留原有许可。
**分发打包好的 `.app` / `.dmg` 时，请注意下面带 ⚠️ 的条目。**

## 一、随应用内置（在 `bin/` 目录，不入仓库，见 [docs/运行时与二进制.md](docs/运行时与二进制.md)）

| 组件 | 用途 | 许可 |
|---|---|---|
| ⚠️ **FFmpeg**（`ffmpeg` / `ffprobe` 及 `libav*.dylib`） | 视频抽帧、时长探测、音频转码 | **GPL v3 / LGPL v3**（取决于具体构建）。若分发的是 GPL 构建，则整个分发物须遵守 GPL 的分发义务（提供对应源码/获取方式）。商用分发前请核实所用构建的授权，或改用 LGPL 构建。 |
| ⚠️ **Chromaprint**（`fpcalc` / `libchromaprint.1.dylib`） | 声学指纹（以音搜素材） | **LGPL v2.1** |
| OpenSSL（`libcrypto.3.dylib`、`libssl.3.dylib`） | FFmpeg 的依赖 | Apache License 2.0 |
| 其它 `lib*.dylib`（x264/x265/SVT-AV1/opus/vpx/…） | FFmpeg 编解码依赖 | 各自开源许可（GPL/LGPL/BSD 混合） |

> 这些二进制**不随本仓库分发**：仓库只保留构建脚本，运行时由开发者自行准备
> （`brew install ffmpeg chromaprint` 或使用官方静态构建）。

## 二、Python 运行时与依赖（`venv/`，不入仓库）

| 组件 | 许可 |
|---|---|
| python-build-standalone（CPython 3.11，astral-sh） | PSF License + 其打包脚本的 MIT |
| MLX / MLX-VLM（Apple） | MIT |
| Transformers / Tokenizers / HuggingFace Hub（Hugging Face） | Apache License 2.0 |
| Pillow | MIT-CMU |
| NumPy | BSD-3-Clause |
| pdfplumber / pdfminer.six | MIT |
| openpyxl | MIT |
| xlrd | BSD-3-Clause |
| python-pptx | MIT |
| olefile | BSD-2-Clause |
| PyObjC（pywebview 依赖） | MIT |
| pywebview | BSD-3-Clause |

## 三、前端库（`vendor/`，**随仓库分发**）

| 组件 | 用途 | 许可 |
|---|---|---|
| JSZip | 解压 .docx 的 zip 容器 | MIT 或 GPLv3（双许可，本项目按 MIT 使用） |
| docx-preview | 浏览器内渲染 .docx | MIT |
| SheetJS `xlsx.full.min.js`（社区版） | 浏览器内渲染 .xlsx | Apache License 2.0 |

## 四、模型（首次启动下载，**不随仓库分发**）

| 模型 | 用途 | 许可 |
|---|---|---|
| ⚠️ **WeMM-Embedding-2B**（Apple Silicon MLX 版，`model/`） | 多模态向量检索、图片描述 | 遵循其 Hugging Face 模型卡上标注的许可。**商用前必须自行核对该模型的许可条款**（模型许可与代码许可相互独立，本项目的 PolyForm 授权不覆盖模型）。 |
| Qwen3-ASR（可选，用户自建 oMLX 服务） | 音频转写 | 遵循其模型卡许可 |

## 五、系统能力

- macOS 系统自带 `textutil`、`sips`、AVFoundation、TCC（麦克风权限）等，按系统条款使用。

---

如果你要**商用分发**，建议至少做到：核实 FFmpeg/Chromaprint 构建的许可并保留其声明、
核实内置模型的许可（或改用许可更宽的模型）、随分发物附上本文件与 `LICENSE`。
