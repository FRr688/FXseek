# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FXseek. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""首次启动播种：让用户装完打开就能直接体验，不用先自己准备素材。

为什么要有这个模块
------------------
新用户第一次打开 FXseek 时面对的是三个空列表（没有素材、没有索引源、没有搜索
历史），他必须先想清楚「我要索引哪个文件夹」，才知道这软件是干嘛的。绝大多数人
在这一步就关掉了。所以随包带一份示例素材（9 个进索引的文件 + 2 个体验文件），
首次启动时铺到用户数据目录并预置好向量库、指纹库、索引源记录，打开即是一个
「已经装满的库」——用户随便点点就能体会到语义搜索、以图搜图、哼唱搜歌、
按格式筛选这些功能，这比任何说明书都直观。

铺什么、铺到哪
--------------
    <程序目录>/seed/assets/*        9 个示例素材          → ~/Documents/FXseek示例素材/
    <程序目录>/seed/tryme/*         2 个体验素材          → ~/Documents/FXseek体验素材/
    <程序目录>/seed/index.db        预置向量库(57 条)      → <数据目录>/index.db
    <程序目录>/seed/fingerprints.db 预置音频指纹(3 首)     → <数据目录>/fingerprints.db
    <程序目录>/seed/sources.json    索引源记录            → <数据目录>/sources.json
    <程序目录>/seed/history.json    搜索历史 + 最近上传   → 由前端首启时读取

**assets 与 tryme 是两码事**：assets 里的 9 个文件会被索引，首页网格就摆这 9 个
（和文档截图一模一样，严丝合缝）；tryme 里的 2 个文件（一张人像图、一段音频）
**不进索引**，只出现在侧栏「最近上传」里当体验入口——用户点一下，前端就去
`/v1/seed/bytes` 取原始字节，真的跑一次「以图搜图」或「以音搜素材」。这样既保证
首页数字对得上，又让用户零成本摸到最神奇的两个功能。

向量库是**同一份模型权重**在本机算出来的，换台机器装同一个 app 得到的是同一个
2048 维向量空间，所以预置库可以直接用，不需要用户在首次启动时等几分钟跑索引。
素材改动后要重新生成种子库，见 `tools/make_seed.py`。

**只在「数据目录里什么数据都没有」时才播种。** 判断依据是 index.db 与
sources.json 是否都不存在——只要用户自己建过索引源，就绝不再往里塞示例，
免得把人家辛苦索引好的库搞乱。播种完成后打一个 `.seeded` 标记。
"""
import json
import os
import shutil
import time

import paths as P

# 程序目录下的种子资源（只读）
SEED_DIR = os.path.join(P.CODE_DIR, "seed")

# 示例素材落地的文件夹名（用户可见、可整个删掉）
DEMO_DIRNAME = "FXseek示例素材"
DEMO_DIR = os.path.join(os.path.expanduser("~"), "Documents", DEMO_DIRNAME)

# 「体验素材」落地的文件夹：同样用户可见可删。这两个文件不进索引，
# 只作为侧栏「最近上传」的入口，点一下就能试「以图搜图」「以音搜素材」。
TRYME_DIRNAME = "FXseek体验素材"
TRYME_DIR = os.path.join(os.path.expanduser("~"), "Documents", TRYME_DIRNAME)

# 播种标记：有它就再也不播种了（哪怕用户把示例删了）
_SEED_FLAG = ".seeded"

# 种子库里记录的路径前缀 —— 打包时素材在 <程序目录>/seed/assets/ 下，
# 落地后要整体换成用户机器上的真实路径。
_SEED_ASSETS_SUFFIX = os.path.join("seed", "assets")


def _log(msg: str) -> None:
    print("[首次播种] %s" % msg, flush=True)


def _seed_ready() -> bool:
    """种子资源齐不齐。缺了就当没有种子，宁可空启动也不能崩。"""
    return (os.path.isdir(os.path.join(SEED_DIR, "assets"))
            and os.path.exists(os.path.join(SEED_DIR, "index.db")))


def already_seeded() -> bool:
    return os.path.exists(os.path.join(P.DATA_DIR, _SEED_FLAG))


def should_seed() -> bool:
    """要不要播种：种子资源齐全 + 没播过 + 数据目录里确实还没有用户数据。

    「有用户数据」的判定刻意保守——只看这两样：
      · index.db 存在且里面有行   → 用户已经索引过东西了
      · sources.json 存在且非空   → 用户自己加过索引源
    任何一条成立就不播种，绝不覆盖。
    """
    if not _seed_ready() or already_seeded():
        return False
    # 已经索引过东西了？
    if os.path.exists(P.DB_PATH):
        try:
            import sqlite3
            con = sqlite3.connect(P.DB_PATH)
            n = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            con.close()
            if n > 0:
                return False
        except Exception:
            return False          # 库在但读不了，别乱动
    # 已经有自己的索引源了？
    if os.path.exists(P.SOURCES_PATH):
        try:
            with open(P.SOURCES_PATH, encoding="utf-8") as f:
                if json.load(f):
                    return False
        except Exception:
            return False
    return True


def _copy_tree(src_dir: str, dst_dir: str) -> int:
    """把 src_dir 下的文件铺到 dst_dir，返回铺了几个。

    已存在的同名文件不覆盖（用户可能改过），但照样算「在」。这样重复调用是安全的。
    """
    if not os.path.isdir(src_dir):
        return 0
    os.makedirs(dst_dir, exist_ok=True)
    n = 0
    for fn in sorted(os.listdir(src_dir)):
        if fn.startswith("."):
            continue
        src, dst = os.path.join(src_dir, fn), os.path.join(dst_dir, fn)
        if os.path.exists(dst):
            n += 1
            continue
        try:
            shutil.copy2(src, dst)
            n += 1
        except OSError as e:
            _log("素材 %s 复制失败：%s" % (fn, e))
    return n


def _copy_assets() -> int:
    """铺示例素材，返回铺了几个文件（只算进索引的那 9 个）。

    分两处铺：
      seed/assets/ → ~/Documents/FXseek示例素材/      ← 进索引，首页网格显示
      seed/tryme/  → ~/Documents/FXseek体验素材/      ← 不进索引，只当「最近上传」的体验入口

    为什么分开：首页要和说明截图一模一样只有 9 项，而「最近上传」里那两个
    体验素材（测试用图像 / 测试用音频）点一下就能跑「以图搜图」「以音搜素材」，
    是最快让新用户看懂这个 app 在干嘛的入口。它们要是也进索引，首页就变 11 项了。
    体验素材放在用户自己的 Documents 下，用完随手删掉不影响任何东西。
    """
    n = _copy_tree(os.path.join(SEED_DIR, "assets"), DEMO_DIR)
    _copy_tree(os.path.join(SEED_DIR, "tryme"), TRYME_DIR)
    return n


def _rewrite_paths(db_path: str) -> int:
    """把种子库里的路径从「打包机的 seed/assets」改成用户机器上的示例目录。

    index.db 的 items.path 和 fingerprints.db 的 fingerprints.path 都存的是绝对
    路径。打包时是 <程序目录>/seed/assets/xxx，用户机器上要变成
    ~/Documents/FXseek示例素材/xxx，不改的话所有条目都是「失效记录」。
    """
    import sqlite3
    n = 0
    con = sqlite3.connect(db_path)
    try:
        if _table_exists(con, "items"):
            rows = con.execute(
                "SELECT id, path FROM items WHERE path LIKE ?",
                ("%/" + _SEED_ASSETS_SUFFIX.replace(os.sep, "/") + "/%",)).fetchall()
            for rid, old in rows:
                new = os.path.join(DEMO_DIR, os.path.basename(old))
                con.execute("UPDATE items SET path = ? WHERE id = ?", (new, rid))
                n += 1
        if _table_exists(con, "fingerprints"):
            rows = con.execute(
                "SELECT path FROM fingerprints WHERE path LIKE ?",
                ("%/" + _SEED_ASSETS_SUFFIX.replace(os.sep, "/") + "/%",)).fetchall()
            for (old,) in rows:
                new = os.path.join(DEMO_DIR, os.path.basename(old))
                con.execute("UPDATE fingerprints SET path = ? WHERE path = ?",
                            (new, old))
                n += 1
        con.commit()
    finally:
        con.close()
    return n


def _table_exists(con, name: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone())
    except Exception:
        return False


def _fix_mtimes(db_path: str) -> None:
    """把库里记的 mtime 对齐到实际落地的文件。

    索引靠 (path, mtime) 判断「有没有变过」。种子库里的 mtime 是打包机上的，
    复制到用户机器后文件 mtime 会变（copy2 保留 mtime，但打包成 zip/安装包
    走一圈就没了），对不上就会被整库判定为「已失效」然后重索引一遍——
    白等几分钟，还违背了「打开就能用」的初衷。所以落地后按实际文件重写一遍。
    """
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        if not _table_exists(con, "items"):
            return
        rows = con.execute("SELECT DISTINCT path FROM items").fetchall()
        for (p,) in rows:
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            con.execute("UPDATE items SET mtime = ? WHERE path = ?", (mt, p))
        con.commit()
    finally:
        con.close()


def _install_db(src_name: str, dst_path: str) -> bool:
    """铺一个种子数据库（素材路径重写 + mtime 校正）。"""
    src = os.path.join(SEED_DIR, src_name)
    if not os.path.exists(src):
        return False
    try:
        shutil.copy2(src, dst_path)
        _rewrite_paths(dst_path)
        _fix_mtimes(dst_path)
        return True
    except Exception as e:
        _log("%s 铺设失败：%s" % (src_name, e))
        try:
            os.remove(dst_path)
        except OSError:
            pass
        return False


def _write_sources() -> None:
    """把示例素材目录记成一个索引源。"""
    items = []
    if os.path.exists(P.SOURCES_PATH):
        try:
            with open(P.SOURCES_PATH, encoding="utf-8") as f:
                items = json.load(f) or []
        except Exception:
            items = []
    if not any(s.get("path") == DEMO_DIR for s in items):
        items.append({"path": DEMO_DIR,
                      "name": DEMO_DIRNAME + "(右键可移除)",
                      "added_at": time.time()})
    with open(P.SOURCES_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def demo_history() -> dict:
    """给前端用的首启记录（搜索历史 / 最近上传）。

    这两个列表前端存在 localStorage 里（浏览器沙箱），后端碰不到，所以这里只把
    种子内容吐给 `/v1/seed/history`，由前端在「本地一条记录都没有」时写进去。
    这样即使用户清了浏览器数据，重新打开也能恢复演示记录。
    """
    p = os.path.join(SEED_DIR, "history.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def seed_if_needed() -> dict:
    """入口：需要播种就播，返回一份结果摘要（不管播没播都会有值）。"""
    out = {"seeded": False, "reason": "", "demo_dir": DEMO_DIR,
           "assets": 0, "vectors": 0, "fingerprints": 0}
    if already_seeded():
        out["reason"] = "已播种过"
        return out
    if not _seed_ready():
        out["reason"] = "没有种子资源"
        return out
    if not should_seed():
        out["reason"] = "已有用户数据，跳过"
        return out

    P.ensure_dirs()
    out["assets"] = _copy_assets()
    idx_ok = _install_db("index.db", P.DB_PATH)
    fp_ok = _install_db("fingerprints.db", P.FP_DB)
    _write_sources()

    # 数一下到底铺了多少条，写进日志和界面提示
    try:
        import sqlite3
        if idx_ok:
            con = sqlite3.connect(P.DB_PATH)
            out["vectors"] = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            con.close()
        if fp_ok:
            con = sqlite3.connect(P.FP_DB)
            out["fingerprints"] = con.execute(
                "SELECT COUNT(*) FROM fingerprints").fetchone()[0]
            con.close()
    except Exception:
        pass

    try:
        with open(os.path.join(P.DATA_DIR, _SEED_FLAG), "w",
                  encoding="utf-8") as f:
            f.write("seeded at %s\nassets: %d\n" %
                    (time.strftime("%Y-%m-%d %H:%M:%S"), out["assets"]))
    except OSError:
        pass

    out["seeded"] = True
    out["reason"] = "ok"
    _log("已铺好 %d 个示例素材 / %d 条向量 / %d 首指纹 → %s"
         % (out["assets"], out["vectors"], out["fingerprints"], DEMO_DIR))
    return out
