# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""统一路径解析：**代码只读，数据可写。**

为什么要分开：
    老版本把所有东西都写在 `<程序目录>/data/` 下。开发时没问题，但打包成
    macOS `.app` 之后会踩三个坑：
      1. `/Applications` 里的 `.app` 普通用户没有写权限，装完就索引不了；
      2. 升级替换 `.app` 会把用户索引好的向量库一起覆盖掉；
      3. 往 bundle 里写文件等于改了签名内容，Gatekeeper 会报「已损坏」。

数据目录解析顺序：
    1) 环境变量 ``FXSEEK_DATA_DIR``  —— 便携模式 / 多实例 / 测试用
    2) ``~/Library/Application Support/FXseek``  —— macOS 惯例，可写、不随升级丢失
    3) ``<程序目录>/data``  —— 老版本位置；**仅当上面两个都没数据时**作为回退使用

首次启动时如果老位置有数据、新位置还没有，会自动**复制**过去（原文件不删，
老目录留作备份），复制完打一个 ``.migrated_from_legacy`` 标记避免重复搬运。
"""
import os
import shutil

__all__ = [
    "CODE_DIR", "LEGACY_DATA_DIR", "DATA_DIR", "DB_PATH", "FP_DB",
    "SETTINGS_PATH", "SOURCES_PATH", "TRASH_PATH", "THUMB_DIR", "HISTORY_PATH",
    "ensure_dirs", "describe", "enforce_thumb_limit", "thumb_total_bytes",
    "PATH", "enrich_path",
]

# 程序自身所在目录：前端资源 / 模型 / 二进制依赖都跟着它走（只读）
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_DATA_DIR = os.path.join(CODE_DIR, "data")

# ---------------------------------------------------------------- 可执行文件搜索路径
# ★ 双击图标 / 从 Finder 或程序坞启动（LaunchServices）时，进程拿到的 PATH 只有
#   `/usr/bin:/bin:/usr/sbin:/sbin` —— 用户自己 brew 装的 ffmpeg 不在里面。
#   症状不是「少个功能」，而是 `[Errno 2] No such file or directory: 'ffmpeg'`：
#   自己 shell 出去的调用直接炸，第三方库里那些 `shutil.which("ffmpeg")`
#   （mlx_vlm / transformers 解 m4a、aac 要靠它）也一起找不到。
#   另外应用**自带**一份 relocatable 的 ffmpeg/ffprobe/fpcalc 在 `bin/`，
#   所以这里把 bin/ 和几个常见安装位置补到 PATH 最前面：哪台机器上都有得用。
#   已经在 PATH 里的不重复加；PATH 本来就比这些全的话，顺序也不动。
_BIN_HOME = os.path.join(CODE_DIR, "bin")
_EXTRA_PATH = (_BIN_HOME,
               os.path.expanduser("~/bin"),
               "/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")


def _enrich_path() -> str:
    """把自带的 bin/ 与常见安装位置补进 PATH（幂等，可在任何入口调用）。"""
    cur = [p for p in os.environ.get("PATH", "").split(":") if p]
    add = [p for p in _EXTRA_PATH if p not in cur and os.path.isdir(p)]
    if add:
        os.environ["PATH"] = ":".join(add + cur)
    return os.environ.get("PATH", "")


PATH = _enrich_path()
# 公开别名：别的入口（比如给子进程拼环境时）可以再调一次，确认 PATH 带过去了
enrich_path = _enrich_path

APP_NAME = "FXseek"
_MIGRATE_FLAG = ".migrated_from_legacy"
_DATA_FILES = ("index.db", "fingerprints.db", "settings.json",
               "sources.json", "trash.json")
_DATA_DIRS = ("thumbs",)


def _has_data(d: str) -> bool:
    """判断目录里是否已有真实数据（用来决定要不要迁移 / 回退）。"""
    if not os.path.isdir(d):
        return False
    for name in _DATA_FILES:
        if os.path.exists(os.path.join(d, name)):
            return True
    for name in _DATA_DIRS:
        sub = os.path.join(d, name)
        try:
            if os.path.isdir(sub) and os.listdir(sub):
                return True
        except OSError:
            pass
    return False


def _writable(d: str) -> bool:
    """真正试着写一下，确认这个目录可用（权限/只读卷/沙箱都会被挡）。"""
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        probe = os.path.join(d, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _resolve_data_dir() -> str:
    env = os.environ.get("FXSEEK_DATA_DIR", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    primary = os.path.join(os.path.expanduser("~"),
                           "Library", "Application Support", APP_NAME)
    # 新位置还没数据、老位置有 → 先搬过去（复制，不删原文件）
    if not _has_data(primary) and _has_data(LEGACY_DATA_DIR) \
            and not os.path.exists(os.path.join(primary, _MIGRATE_FLAG)):
        _migrate(LEGACY_DATA_DIR, primary)
    # 只有真正写得进去才用它；写不进去就退回老位置，绝不把数据弄丢
    if _writable(primary):
        return primary
    print("[数据目录] %s 不可写，回退到旧位置 %s" % (primary, LEGACY_DATA_DIR))
    return LEGACY_DATA_DIR


def _migrate(old: str, new: str) -> None:
    """把老数据目录复制到新位置（只复制，不删除）。"""
    copied = []
    try:
        os.makedirs(new, mode=0o700, exist_ok=True)
        for name in _DATA_FILES:
            src = os.path.join(old, name)
            dst = os.path.join(new, name)
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied.append(name)
        for name in _DATA_DIRS:
            src = os.path.join(old, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(new, name), dirs_exist_ok=True)
                copied.append(name + "/")
        with open(os.path.join(new, _MIGRATE_FLAG), "w", encoding="utf-8") as f:
            f.write("migrated from: %s\n" % old)
        print("[数据目录] 已从旧位置迁移到 %s" % new)
        print("[数据目录] 迁移内容: %s" % (", ".join(copied) or "（无）"))
        print("[数据目录] 旧目录保留作备份: %s" % old)
    except Exception as e:                                # 迁移失败就继续用老位置
        print("[数据目录] 迁移失败(%s)，继续使用旧位置" % e)


DATA_DIR = _resolve_data_dir()

DB_PATH = os.path.join(DATA_DIR, "index.db")            # 向量库
FP_DB = os.path.join(DATA_DIR, "fingerprints.db")       # 音频指纹
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")  # 设置（含 API Key）
SOURCES_PATH = os.path.join(DATA_DIR, "sources.json")   # 索引源
TRASH_PATH = os.path.join(DATA_DIR, "trash.json")       # 回收站
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")            # 缩略图缓存
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")    # 搜索记录 / 最近上传（app.py 读写）


def ensure_dirs() -> None:
    """建好数据目录并收紧权限（目录 0700，只有当前用户能读）。"""
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
    except OSError:
        pass


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def describe() -> dict:
    """给设置页展示用：数据放在哪、老位置在不在、各文件多大。"""
    return {"data_dir": DATA_DIR,
            "custom": bool(os.environ.get("FXSEEK_DATA_DIR", "").strip()),
            "legacy_dir": LEGACY_DATA_DIR,
            "legacy_in_use": os.path.realpath(DATA_DIR) == os.path.realpath(LEGACY_DATA_DIR),
            "legacy_exists": _has_data(LEGACY_DATA_DIR),
            "bytes": {"index.db": _size(DB_PATH),
                      "fingerprints.db": _size(FP_DB),
                      "settings.json": _size(SETTINGS_PATH),
                      "sources.json": _size(SOURCES_PATH),
                      "history.json": _size(HISTORY_PATH),
                      "thumbs": thumb_total_bytes()}}


# ---------------- 缩略图缓存容量控制 ----------------

def thumb_entries() -> list:
    """返回 [(mtime, size, path)]，用于统计与淘汰。"""
    out = []
    if os.path.isdir(THUMB_DIR):
        for fn in os.listdir(THUMB_DIR):
            fp = os.path.join(THUMB_DIR, fn)
            try:
                st = os.stat(fp)
                if os.path.isfile(fp):
                    out.append((st.st_mtime, st.st_size, fp))
            except OSError:
                pass
    return out


def thumb_total_bytes() -> int:
    return sum(size for _, size, _ in thumb_entries())


def enforce_thumb_limit(limit_mb) -> int:
    """缩略图超过上限时，按修改时间从旧到新淘汰。返回删除的文件数。

    limit_mb <= 0 表示不限制。
    """
    try:
        limit = float(limit_mb or 0)
    except (TypeError, ValueError):
        limit = 0.0
    if limit <= 0:
        return 0
    budget = int(limit * 1048576)
    entries = thumb_entries()
    total = sum(size for _, size, _ in entries)
    if total <= budget:
        return 0
    # 旧的最先删；至少留一点余量，避免每次写入都触发淘汰
    entries.sort(key=lambda x: x[0])
    target = int(budget * 0.9)
    n = 0
    for _, size, fp in entries:
        if total <= target:
            break
        try:
            os.remove(fp)
            total -= size
            n += 1
        except OSError:
            pass
    return n


ensure_dirs()
