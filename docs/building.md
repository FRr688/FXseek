# Runtime and binaries: turning the repository into something buildable

The repository ships **source only**. To build a runnable `.app` / `.dmg` you need three things in the
repository root — `venv/`, `bin/` and `model/` — all three of which are `.gitignore`d
(see [../.gitignore](../.gitignore)). Here is the complete from-scratch procedure (macOS / Apple Silicon).

---

## 1. `venv/cpython-3.11/` — a self-contained Python runtime

The app does not rely on the Python on the user's machine: the whole runtime is copied into the `.app`
at build time. So it has to be a **relocatable, standalone CPython** — not the `python.org` framework
build, and not Homebrew's.

```bash
# (1) Grab the macOS arm64 build of python-build-standalone (astral-sh)
#     https://github.com/astral-sh/python-build-standalone/releases
#     Download cpython-3.11.*-aarch64-apple-darwin-install_only.tar.gz, then:
mkdir -p venv && tar -xzf cpython-3.11.*-aarch64-apple-darwin-install_only.tar.gz -C venv
mv venv/python venv/cpython-3.11        # the directory name must be exactly cpython-3.11

# (2) Install dependencies (versions per requirements.txt)
./venv/cpython-3.11/bin/python3.11 -m pip install -r requirements.txt
```

> ⚠️ Two pitfalls we hit for real:
> 1. **Don't casually delete packages out of site-packages to "slim things down."** If you remove the
>    code but leave the `*.dist-info` metadata, `pip install` reports "Requirement already satisfied"
>    and refuses to reinstall — and the problem stays hidden for a long time (this project silently lost
>    PDF text extraction for a whole release because of it). Run
>    `./venv/cpython-3.11/bin/python3.11 tools/audit_deps.py` before changing the slim-down list.
> 2. When putting something back, use `--force-reinstall --no-deps`, or that metadata will fool you again.

## 2. `bin/` — ffmpeg / ffprobe / fpcalc and their dylibs

```bash
mkdir -p bin/lib
# ffmpeg / ffprobe: use the official static builds, or install via Homebrew and move the
# dependent dylibs in alongside them
brew install ffmpeg chromaprint
cp "$(brew --prefix)/bin/fpcalc" bin/
# The important part: collect the dylibs into bin/lib and rewrite each executable's dependency
# paths to @loader_path/lib/xxx.dylib, so the .app still starts on a machine with no /opt/homebrew.
#   otool -L bin/ffmpeg          # see what it depends on
#   cp <dylib> bin/lib/          # move them in one by one
#   install_name_tool -change <old path> @loader_path/lib/<name> bin/ffmpeg
#   codesign --force --sign - bin/ffmpeg   # re-sign after modifying a Mach-O
```
The resulting layout: `bin/{ffmpeg,ffprobe,fpcalc}` plus `bin/lib/*.dylib` (about 20 files, 37 MB in this project).

> These are third-party binaries (FFmpeg is GPL/LGPL, Chromaprint is LGPL) and are **not distributed with
> the repository**. Read [../THIRD-PARTY.md](../THIRD-PARTY.md) before distributing a build.

## 3. `model/` — the built-in multimodal model (~1.8 GB)

**You normally don't need to prepare this by hand**: on first launch `model_dl.py` downloads it
automatically (mirror first, automatic fallback to the official source, resumable, with a progress bar).

When you do need to pre-place it (offline packaging / CI):

```bash
# Model repository: ewin-reg/WeMM-Embedding-2B-Apple-Silicon-MLX
export HF_ENDPOINT=https://hf-mirror.com
./venv/cpython-3.11/bin/python3.11 - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download("ewin-reg/WeMM-Embedding-2B-Apple-Silicon-MLX", local_dir="model")
PY
```
You can also point at any location with the `FXSEEK_MODEL_DIR=/path/to/model` environment variable
(highest priority).

> Check the model's own license before commercial use — **the code licence does not cover the model licence.**

## 4. Verifying the environment is complete

```bash
./venv/cpython-3.11/bin/python3.11 tools/audit_deps.py      # dependency audit (app + third-party + hollow metadata)

# ⚠️ When auditing an already-packaged .app, run it against a **copy** (or prefix with
#    PYTHONDONTWRITEBYTECODE=1): running Python inside a signed bundle writes __pycache__/*.pyc, and
#    the bundle's resource seal (CodeResources) was fixed at signing time — extra files make
#    `codesign --verify` report "a sealed resource is missing or invalid". 16 .pyc files were enough
#    to dirty the whole bundle in our testing.
cp -R dist/FXseek.app /tmp/check.app && cp tools/audit_deps.py /tmp/check.app/Contents/Resources/app/
(cd /tmp/check.app/Contents/Resources/app && PYTHONDONTWRITEBYTECODE=1 ./venv/cpython-3.11/bin/python3.11 audit_deps.py)
./venv/cpython-3.11/bin/python3.11 app.py --port 8231       # run the server; open http://127.0.0.1:8231
bash tools/build_app.sh --dmg                               # produce .app + .dmg
hdiutil verify dist/FXseek-1.0.3.dmg                        # must print VALID
```

---

[简体中文版 →](运行时与二进制.md) · [← Back to README](../README.md)
