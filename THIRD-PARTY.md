# Third-Party Notices

The project's **own code** is released under the [PolyForm Noncommercial License 1.0.0](LICENSE).
It depends on, bundles, or downloads at runtime a number of third-party components, each of which keeps
its own licence. **If you distribute a packaged `.app` / `.dmg`, pay attention to the entries marked ⚠️.**

## 1. Bundled with the app (`bin/`, not in the repository — see [docs/building.md](docs/building.md))

| Component | Purpose | Licence |
|---|---|---|
| ⚠️ **FFmpeg** (`ffmpeg` / `ffprobe` and `libav*.dylib`) | Video frame extraction, duration probing, audio transcoding | **GPL v3 / LGPL v3** (depends on the specific build). If you distribute a GPL build, the whole distribution must comply with GPL's obligations (supply the corresponding source, or a way to get it). Verify the licence of the build you ship before commercial distribution, or switch to an LGPL build. |
| ⚠️ **Chromaprint** (`fpcalc` / `libchromaprint.1.dylib`) | Acoustic fingerprinting (search audio by audio) | **LGPL v2.1** |
| OpenSSL (`libcrypto.3.dylib`, `libssl.3.dylib`) | FFmpeg dependency | Apache License 2.0 |
| Other `lib*.dylib` (x264/x265/SVT-AV1/opus/vpx/…) | FFmpeg codec dependencies | Their respective open-source licences (a mix of GPL/LGPL/BSD) |

> These binaries are **not distributed with this repository**: the repository only carries the build
> scripts, and the developer prepares the binaries themselves (`brew install ffmpeg chromaprint`, or the
> official static builds).

## 2. Python runtime and dependencies (`venv/`, not in the repository)

| Component | Licence |
|---|---|
| python-build-standalone (CPython 3.11, astral-sh) | PSF License + MIT for its build scripts |
| MLX / MLX-VLM (Apple) | MIT |
| Transformers / Tokenizers / HuggingFace Hub (Hugging Face) | Apache License 2.0 |
| Pillow | MIT-CMU |
| pillow-heif (HEIC/HEIF decoding, **bundles** `libheif`) | BSD-3-Clause |
| ⚠️ **libheif** (the `.dylibs/libheif*.dylib` that ships with pillow_heif) | **LGPL-3.0** (its bundled libde265 is LGPL-3.0 and libx265 is GPL-2.0; used only to decode iPhone photos. Verify before commercial distribution and meet the LGPL/GPL notice and source-availability obligations) |
| NumPy | BSD-3-Clause |
| pdfplumber / pdfminer.six | MIT |
| openpyxl | MIT |
| xlrd | BSD-3-Clause |
| python-pptx | MIT |
| olefile | BSD-2-Clause |
| PyObjC (pywebview dependency) | MIT |
| pywebview | BSD-3-Clause |

## 3. Front-end libraries (`vendor/`, **distributed with the repository**)

| Component | Purpose | Licence |
|---|---|---|
| JSZip | Unpacking the zip container inside .docx | MIT or GPLv3 (dual-licensed; used here under MIT) |
| docx-preview | Rendering .docx in the browser | MIT |
| SheetJS `xlsx.full.min.js` (community edition) | Rendering .xlsx in the browser | Apache License 2.0 |

## 4. Models (downloaded on first launch, **not distributed with the repository**)

| Model | Purpose | Licence |
|---|---|---|
| ⚠️ **WeMM-Embedding-2B** (Apple Silicon MLX build, `model/`) | Multimodal vector retrieval, image captioning | Follows the licence stated on its Hugging Face model card. **You must verify that model's licence before commercial use** — the model licence is independent of the code licence, and this project's PolyForm grant does not cover it. |
| Qwen3-ASR (optional, via a self-hosted oMLX service) | Audio transcription | Follows its model card licence |

## 5. System capabilities

- macOS built-ins — `textutil`, `sips`, AVFoundation, TCC (microphone permission) and others — are used
  under Apple's system terms.

---

If you intend to **distribute commercially**, at a minimum: verify the licences of the FFmpeg/Chromaprint
builds you ship and retain their notices, verify the licence of the bundled model (or switch to a
more permissively licensed one), and ship this file together with `LICENSE`.

---

## 中文摘要

本项目**自身代码**采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，但它依赖、内置或运行期下载了
若干第三方组件，各自保留原有许可。**分发打包好的 `.app` / `.dmg` 时请特别注意上面带 ⚠️ 的条目**：
FFmpeg（GPL v3 / LGPL v3，取决于构建）、Chromaprint（LGPL v2.1）、libheif（LGPL-3.0，内含 libde265
LGPL-3.0 与 libx265 GPL-2.0）、以及内置模型 WeMM-Embedding-2B（遵循其 Hugging Face 模型卡许可，
**代码许可不覆盖模型许可**）。

这些二进制与模型**都不随仓库分发**，仓库只保留构建脚本。商用分发前建议至少做到：核实
FFmpeg/Chromaprint 构建的许可并保留其声明、核实内置模型的许可（或改用许可更宽的模型）、随分发物
附上本文件与 `LICENSE`。
