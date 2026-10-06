# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FXseek. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
模型下载模块：首次启动时按需把 WeMM 嵌入模型拉到用户数据目录。

为什么要有这个模块
------------------
模型有 1.9GB，塞进 dmg 会让安装包变得又大又难下载。所以打包版不带模型，
用户第一次打开 app 时现下。这里负责：

  1. 判断模型在不在（本地目录 / 已下载的缓存目录）；
  2. 不在就下载，带进度回调，UI 能显示百分比；
  3. 双源可选 —— 默认走国内镜像 hf-mirror.com（huggingface.co 在国内直连基本不通），
     用户可以切到 HuggingFace 官方源；
  4. 断点续传、失败重试、下载完做体积+采样校验，坏文件不会被当成好模型加载。

下载源
------
  mirror : https://hf-mirror.com            （默认，国内可达）
  hugging: https://huggingface.co           （官方，海外或挂代理时用）

两个源上的仓库是同一个：`ewin-reg/WeMM-Embedding-2B-Apple-Silicon-MLX`
（也就是 author 转出来的 MLX 混合精度版），文件清单与随包的 model/ 完全一致。

用法
----
    import model_dl
    ok = model_dl.ensure_model(progress=cb, source="mirror")
    # cb(dict) 会拿到 {"file":..., "done":..., "total":..., "pct":...}

CLI 自测：
    ./venv/cpython-3.11/bin/python3.11 model_dl.py --check
    ./venv/cpython-3.11/bin/python3.11 model_dl.py --source mirror --dir /tmp/mtest
"""

import os
import ssl
import json
import time
import shutil
import hashlib
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# 随包的模型目录（开发机上有；打包版通常没有）
BUILTIN_MODEL_DIR = os.path.join(HERE, "model")

REPO_ID = "ewin-reg/WeMM-Embedding-2B-Apple-Silicon-MLX"

# 下载源。顺序即 UI 里的展示顺序，"mirror" 是默认。
SOURCES = {
    "mirror": {
        "label": "国内镜像（推荐）",
        "label_en": "China mirror (recommended)",
        "endpoint": "https://hf-mirror.com",
    },
    "hugging": {
        "label": "HuggingFace 官方",
        "label_en": "HuggingFace official",
        "endpoint": "https://huggingface.co",
    },
}
DEFAULT_SOURCE = "mirror"

# 仓库里要拉的文件。model.safetensors 是主体（1.9GB），其余是小配置。
# 顺序刻意把大文件放最后，这样配置先下完，进度看着更顺。
MODEL_FILES = [
    "README.md",
    "chat_template.jinja",
    "config.json",
    "config_sentence_transformers.json",
    "dequant_report.json",
    "model.safetensors.index.json",
    "modeling_st_wemm.py",
    "modules.json",
    "processor_config.json",
    "sentence_bert_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "model.safetensors",
]

# 每个文件的期望大小。用来判断「下载完了没有」以及「文件是不是坏的」。
# 大小不匹配的一律重下，绝不带着半个模型去加载（会崩在 mlx 里，报错很难懂）。
EXPECTED_SIZES = {
    "README.md": 5026,
    "chat_template.jinja": 7755,
    "config.json": 73464,
    "config_sentence_transformers.json": 245,
    "dequant_report.json": 132,
    "model.safetensors.index.json": 96178,
    "modeling_st_wemm.py": 3180,
    "modules.json": 106,
    "processor_config.json": 991,
    "sentence_bert_config.json": 950,
    "tokenizer.json": 19990378,
    "tokenizer_config.json": 1171,
    "model.safetensors": 1948326321,
}

# 判定「模型齐了」必须有的文件。README / dequant_report 之类的缺了不影响跑，
# 但下面这些缺一个都加载不起来。
REQUIRED_FILES = [
    "config.json",
    "model.safetensors",
    "model.safetensors.index.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "processor_config.json",
    "modeling_st_wemm.py",
]

TOTAL_BYTES = sum(EXPECTED_SIZES.values())

# 下载/校验时对 1.9GB 大文件做「首尾各 8MB」采样哈希，避免全量读盘读好几分钟。
SAMPLE_CHUNK = 8 * 1024 * 1024
_SAMPLE_HASH = {
    # 开发机随包 model/ 的基准，用来确认用户下到的是同一份权重
    "model.safetensors": "e9065fabf1fad400",
}


# 镜像站会在文本文件末尾多发一个换行（实测 hf-mirror.com 的 config.json 是 73465
# 字节，官方仓库是 73464）。这种 1 字节的空白差异不影响加载，但如果按严格大小校验，
# 用户会卡在「大小不对」直接起不来——所以文本类文件允许「尾部空白差异」。
_TEXT_TAIL_FILES = {
    "config.json", "config_sentence_transformers.json", "dequant_report.json",
    "model.safetensors.index.json", "modules.json", "processor_config.json",
    "sentence_bert_config.json", "tokenizer_config.json", "chat_template.jinja",
    "modeling_st_wemm.py", "README.md",
}


def _size_ok(path, fn, want):
    """大小对不对。文本文件容忍尾部空白差异（镜像站多发一个 \\n）。"""
    if not want:
        return True
    got = os.path.getsize(path)
    if got == want:
        return True
    if fn in _TEXT_TAIL_FILES and want < got <= want + 8:
        # 只有尾部多出来的这几字节全是空白才认，别的差异一律当坏文件
        try:
            with open(path, "rb") as f:
                f.seek(want)
                extra = f.read()
            return extra.strip(b" \t\r\n") == b""
        except OSError:
            return False
    return False


def _log(msg):
    print("[模型下载] %s" % msg, flush=True)


# --------------------------------------------------------------------------
# 位置解析
# --------------------------------------------------------------------------

def model_dir_for(data_dir):
    """模型下载到用户数据目录下的 model/。放在数据目录而不是 .app 里，
    原因和 paths.py 那套一样：.app 在 /Applications 下是只读的，而且升级会整个覆盖。"""
    return os.path.join(data_dir, "model")


def _looks_complete(d):
    """目录里必需文件都在，且大小都对得上。"""
    if not d or not os.path.isdir(d):
        return False
    for fn in REQUIRED_FILES:
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            return False
        want = EXPECTED_SIZES.get(fn)
        if not _size_ok(p, fn, want):
            return False
    return True


def find_model(data_dir=None):
    """找一个能直接用的模型目录，找不到返回 None。

    查找顺序：
      1. 环境变量 FXSEEK_MODEL_DIR —— 手动指定，最高优先级
      2. 数据目录下的 model/     —— 用户下载的
      3. 随包的 model/           —— 开发机 / 完整版安装包
    """
    cands = []
    env = os.environ.get("FXSEEK_MODEL_DIR")
    if env:
        cands.append(os.path.abspath(os.path.expanduser(env)))
    if data_dir:
        cands.append(model_dir_for(data_dir))
    cands.append(BUILTIN_MODEL_DIR)
    for c in cands:
        if _looks_complete(c):
            return c
    return None


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

def _ssl_verify():
    """返回 requests 的 verify 参数：优先用 certifi 的 CA 包路径，没有就退回
    系统默认（True）。

    注意：requests 的 verify 收的是「CA 包路径」或布尔，不是 SSLContext。
    早先这里返回 ssl.create_default_context()，requests 会把它当路径去 stat，
    于是报 `TypeError: stat: path should be string, ... not SSLContext`。
    这台机器上 huggingface.co 直连是超时（不是证书问题），但镜像走 https
    仍然要证书。"""
    try:
        import certifi
        return certifi.where()
    except Exception:
        return True


def _http_get(url, timeout=30, retries=3, on_try=None):
    """带重试的 GET，返回 requests.Response。镜像偶尔会抽风（实测同一个 URL
    有时 200 有时超时），所以重试是必须的，不是保险起见。"""
    import requests
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, timeout=timeout, verify=_ssl_verify(),
                             stream=True, allow_redirects=True)
            if r.status_code == 200:
                return r
            last = "HTTP %s" % r.status_code
            r.close()
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if on_try:
            on_try(i + 1, last)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError("请求失败（试了 %d 次）：%s —— %s" % (retries, url, last))


def probe_source(source=DEFAULT_SOURCE, timeout=25):
    """探一下源通不通、仓库在不在。返回 (ok, 说明文字)。"""
    ep = SOURCES.get(source, SOURCES[DEFAULT_SOURCE])["endpoint"]
    url = "%s/api/models/%s" % (ep, REPO_ID)
    try:
        r = _http_get(url, timeout=timeout, retries=2)
        try:
            d = r.json()
            n = len(d.get("siblings") or [])
            r.close()
            return True, "OK（%d 个文件）" % n
        finally:
            try:
                r.close()
            except Exception:
                pass
    except Exception as e:
        return False, str(e)[:200]


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

def _file_url(source, fn):
    ep = SOURCES.get(source, SOURCES[DEFAULT_SOURCE])["endpoint"]
    return "%s/%s/resolve/main/%s" % (ep, REPO_ID, fn)


def _sample_hash(path):
    """大文件的首尾采样哈希。全量 sha256 要读 1.9GB，第一次打开 app 时
    没必要让用户干等这个；采样已经足够发现「下到一半断了」这类问题。"""
    h = hashlib.sha256()
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        h.update(f.read(SAMPLE_CHUNK))
        if size > SAMPLE_CHUNK * 2:
            f.seek(-SAMPLE_CHUNK, os.SEEK_END)
            h.update(f.read(SAMPLE_CHUNK))
    return h.hexdigest()[:16]


def download_file(source, fn, dest_dir, progress=None, cancel=None):
    """下单个文件，支持断点续传。

    关键点：先下到 <name>.part，下完校验大小，通过了才改名成正式文件。
    这样中途断电/断网/被杀进程，都不会留下一个「看起来存在但其实半截」的模型——
    那种情况最坑，因为 mlx 加载时才报错，而且报错完全看不出是下载的问题。
    """
    import requests

    final = os.path.join(dest_dir, fn)
    part = final + ".part"
    want = EXPECTED_SIZES.get(fn)

    # 已经下好了就跳过
    if os.path.isfile(final) and _size_ok(final, fn, want):
        if progress:
            progress({"file": fn, "state": "done", "done": 0, "total": want or 0, "pct": 100})
        return final

    os.makedirs(dest_dir, exist_ok=True)
    have = os.path.getsize(part) if os.path.isfile(part) else 0

    headers = {}
    mode = "wb"
    if have and want and have < want:
        headers["Range"] = "bytes=%d-" % have
        mode = "ab"
        _log("续传 %s（已有 %.1f MB）" % (fn, have / 1048576.0))
    elif have and want and have >= want:
        # 下多了（不该发生），重来
        have = 0
        try:
            os.remove(part)
        except OSError:
            pass

    import requests as _rq
    url = _file_url(source, fn)
    last = None
    for attempt in range(6):
        try:
            r = _rq.get(url, headers=headers, timeout=(30, 180), stream=True,
                        verify=_ssl_verify(), allow_redirects=True)
            if r.status_code not in (200, 206):
                last = "HTTP %s" % r.status_code
                r.close()
                raise RuntimeError(last)
            # 关键：我们带了 Range 但服务端回了 200，说明它不支持断点续传，
            # 应答体是「整个文件」而不是「剩下那一段」。此时若照旧以追加模式
            # 写，就会把整份文件接到半截文件后面，得到一个更大的坏文件
            # （实测：400 字节 + 1171 字节 = 1571 字节，然后大小校验才报错）。
            # 所以这里必须丢掉半截、从头以 wb 写。
            if have and r.status_code == 200:
                _log("%s 服务端不支持续传（HTTP 200），从头开始下载" % fn)
                have = 0
                mode = "wb"
                try:
                    os.remove(part)
                except OSError:
                    pass
            total = want or int(r.headers.get("Content-Length") or 0) + have
            got = have
            t0 = time.time()
            last_emit = 0.0
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if cancel and cancel():
                        r.close()
                        raise KeyboardInterrupt("用户取消")
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    # 进度回调限流：每 0.25 秒或每 8MB 报一次，避免刷爆 UI
                    if progress and (now - last_emit > 0.25 or got >= total):
                        last_emit = now
                        el = max(now - t0, 0.001)
                        speed = (got - have) / el
                        eta = int((total - got) / speed) if speed > 1 else 0
                        progress({"file": fn, "state": "downloading", "done": got,
                                  "total": total, "pct": int(got * 100 / total) if total else 0,
                                  "speed": speed, "eta": eta})
            r.close()
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            headers = {"Range": "bytes=%d-" % have} if have else {}
            mode = "ab" if have else "wb"
            _log("%s 第 %d 次失败（%s），%.1f MB 处继续" % (fn, attempt + 1, last, have / 1048576.0))
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError("下载 %s 失败：%s" % (fn, last))

    # 校验大小
    if not _size_ok(part, fn, want):
        raise RuntimeError("下载 %s 大小不对：得到 %d，应为 %d" % (fn, os.path.getsize(part), want))

    # 大文件再做个采样哈希确认（防止网络中间层返回了别的文件）
    ref = _SAMPLE_HASH.get(fn)
    if ref:
        got = _sample_hash(part)
        if got != ref:
            os.remove(part)
            raise RuntimeError("下载 %s 内容校验不通过（%s ≠ %s），已丢弃，请重试" % (fn, got, ref))

    # 原子改名
    if os.path.exists(final):
        os.remove(final)
    os.rename(part, final)
    if progress:
        progress({"file": fn, "state": "done", "done": want or 0,
                  "total": want or 0, "pct": 100})
    return final


def download_model(dest_dir, source=DEFAULT_SOURCE, progress=None, cancel=None):
    """按清单把模型下齐。返回 dest_dir。"""
    os.makedirs(dest_dir, exist_ok=True)

    # ★ 先花十几秒探一下源通不通：镜像/CDN 被墙或抽风时，直接报一句人话，
    #   而不是让用户对着 0% 干等（每个文件 6 次重试 × 30 秒超时，二十来个文件，
    #   最坏要几十分钟才吐出错误）。
    ok, msg = probe_source(source, timeout=12)
    if not ok:
        alt = "hugging" if source == "mirror" else "mirror"
        ok2, msg2 = probe_source(alt, timeout=12)
        if not ok2:
            raise RuntimeError(
                "模型下载源都连不上：%s（%s）／%s（%s）。"
                "请先确认网络能访问外网再重试；如果本机有代理，"
                "可在终端里带 HTTPS_PROXY=http://127.0.0.1:端口 启动本应用。"
                % (SOURCES[source]["label"], msg[:80],
                   SOURCES[alt]["label"], msg2[:80]))
        _log("默认源「%s」不通，自动改用「%s」" % (SOURCES[source]["label"],
                                                   SOURCES[alt]["label"]))
        source = alt

    n = len(MODEL_FILES)
    for i, fn in enumerate(MODEL_FILES):
        if cancel and cancel():
            raise KeyboardInterrupt("用户取消")
        if progress:
            progress({"file": fn, "state": "start", "index": i + 1, "count": n,
                      "done": 0, "total": EXPECTED_SIZES.get(fn, 0), "pct": 0})
        try:
            download_file(source, fn, dest_dir, progress=progress, cancel=cancel)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # 当前源下不动这个文件（镜像 CDN 抽风、被限速都很常见）：
            # 自动换另一个源再试一次，别让用户卡在一个源上干等。
            alt = "hugging" if source == "mirror" else "mirror"
            _log("%s 在「%s」上失败（%s），改用「%s」重试" % (fn, source, e, alt))
            if progress:
                progress({"file": fn, "state": "start", "index": i + 1, "count": n,
                          "done": 0, "total": EXPECTED_SIZES.get(fn, 0), "pct": 0})
            download_file(alt, fn, dest_dir, progress=progress, cancel=cancel)
    if not _looks_complete(dest_dir):
        raise RuntimeError("下载完了但文件不齐，请重试或换个下载源")
    return dest_dir


# --------------------------------------------------------------------------
# 对外主入口
# --------------------------------------------------------------------------

def ensure_model(data_dir, source=DEFAULT_SOURCE, progress=None, cancel=None):
    """确保有可用模型。返回模型目录路径。

    - 已有（本地随包 / 之前下过）→ 直接返回，不联网；
    - 没有 → 下到 <data_dir>/model/，完事返回。
    抛异常表示失败，调用方负责提示用户。
    """
    found = find_model(data_dir)
    if found:
        _log("已有模型：%s" % found)
        return found
    dest = model_dir_for(data_dir)
    _log("开始下载模型 → %s（源：%s）" % (dest, SOURCES.get(source, {}).get("label", source)))
    download_model(dest, source=source, progress=progress, cancel=cancel)
    _log("模型下载完成：%s" % dest)
    return dest


def clean_partials(dest_dir):
    """清掉 *.part 半截文件，释放空间。"""
    freed = 0
    if not os.path.isdir(dest_dir):
        return 0
    for fn in os.listdir(dest_dir):
        if fn.endswith(".part"):
            p = os.path.join(dest_dir, fn)
            try:
                freed += os.path.getsize(p)
                os.remove(p)
            except OSError:
                pass
    return freed


# --------------------------------------------------------------------------
# CLI 自测
# --------------------------------------------------------------------------

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="FXseek 模型下载器")
    ap.add_argument("--check", action="store_true", help="只检查有没有模型，不下载")
    ap.add_argument("--probe", action="store_true", help="探测两个下载源通不通")
    ap.add_argument("--source", default=DEFAULT_SOURCE, choices=list(SOURCES))
    ap.add_argument("--dir", help="下载到哪个目录（默认数据目录下的 model/）")
    ap.add_argument("--data-dir", help="用户数据目录")
    args = ap.parse_args()

    if args.probe:
        for k in SOURCES:
            ok, msg = probe_source(k)
            print("%-8s %-45s %s  %s" % (k, SOURCES[k]["endpoint"], "✅" if ok else "❌", msg))
        return

    data_dir = args.data_dir
    if not data_dir:
        import paths as P
        data_dir = P.DATA_DIR

    if args.check:
        d = find_model(data_dir)
        print("模型目录:", d or "（没有，需要下载）")
        print("期望总大小: %.2f GB" % (TOTAL_BYTES / 1073741824.0))
        return

    dest = args.dir or model_dir_for(data_dir)

    def cb(ev):
        if ev.get("state") == "downloading":
            print("\r  %-28s %3d%%  %.1f/%.1f MB  %.1f MB/s  ETA %ds   " % (
                ev["file"], ev["pct"], ev["done"] / 1048576.0, ev["total"] / 1048576.0,
                ev.get("speed", 0) / 1048576.0, ev.get("eta", 0)), end="", flush=True)
        elif ev.get("state") == "done":
            print("\r  %-28s 100%% 完成%s" % (ev["file"], " " * 40))

    try:
        p = download_model(dest, source=args.source, progress=cb)
        print("\n✅ 下好了：%s" % p)
    except Exception as e:
        print("\n❌ 失败：%s" % e)
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
