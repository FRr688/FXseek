#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""把 FXseek 的 MCP server 注册进本机各 agent 的配置里。

六个 agent 的配置格式**各不相同**（JSON/YAML/TOML，键名也不一样），
所以每家写一个适配器。这个模块只做三件事：

    detect()      这个 agent 装没装？配置文件在哪？
    is_enabled()  它现在挂没挂 fxseek？（读回来核对，不靠自己的记录）
    enable() / disable()   写进去 / 拿出来

安全约定（重要）：
  * 每次写入前把原文件备份成 `<名字>.fxseek.bak`，**只备份一次**
    （已有备份就不再覆盖，否则第二次写入会把「已被污染」的文件当成原件）。
  * 只动我们自己的那一个键，其余内容原样保留。
    JSON 用 json 模块整读整写；YAML/TOML 没装对应库时用**行级手术**，
    绝不因为「解析失败」就把用户整个配置文件清空。
  * 写文件用「临时文件 + 原子替换」，中途失败不会留下半截文件。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))

# 注册进各 agent 的那条命令。用绝对路径，因为 agent 启动 MCP 时的 cwd 不可控。
PYTHON = os.path.join(HERE, "venv", "cpython-3.11", "bin", "python3.11")
SERVER_PY = os.path.join(HERE, "mcp_server.py")
SERVER_KEY = "fxseek"

# 配套的技能包。MCP 只解决「工具能被调用」，但 agent 并不知道**什么时候该调**；
# 技能就是那份说明书，所以开启 MCP 时一并放进各家的 skills 目录。
#
# 一个刻意的规矩：**关 MCP 不删技能**。用户很可能已经把它当资料留着、甚至改过，
# 替他们删掉是丢人家的东西。只有「开了 MCP 但技能不见了」才补回去。
SKILL_NAME = "fxseek-media"
SKILL_SRC = os.path.join(HERE, "fxseek_skill", "SKILL.md")


def _cmd():
    """返回 (command, args)。"""
    py = PYTHON if os.path.exists(PYTHON) else sys.executable
    return py, [SERVER_PY]


# ---------------------------------------------------------------------------
# 通用读写工具
# ---------------------------------------------------------------------------


def _backup(path):
    """备份一次。已存在备份就不动它——那一份才是真正的原件。"""
    bak = path + ".fxseek.bak"
    if os.path.exists(path) and not os.path.exists(bak):
        try:
            shutil.copy2(path, bak)
            return bak
        except OSError:
            return None
    return None


def _atomic_write(path, text):
    """原子写：先写同目录临时文件，再 os.replace 顶上。"""
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".fxseek-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        return json.loads(txt) if txt else {}
    except (OSError, json.JSONDecodeError):
        return None          # 解析失败 → 由调用方决定怎么办（绝不覆盖）


def _write_json(path, data):
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _which(name):
    try:
        return subprocess.run(
            ["/usr/bin/which", name], capture_output=True, text=True, timeout=5
        ).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _q(s):
    """给 YAML/TOML 用的**带引号**字符串字面量。

    一律加引号，不做「安全字符就裸写」的优化：TOML 的裸字符串（bare key/
    bare value）只允许 ASCII 的 A-Za-z0-9_- ，路径里的中文会让
    整个文件解析失败（实测 tomllib 报 "Invalid value"）。
    YAML 宽松得多，但统一加引号最省心。

    不能用 json.dumps —— 它会把中文转义成 \\u5de5\\u4f5c...，在 JSON 里合法，
    写进 YAML/TOML 却会变成**字面反斜杠 u 文本**，路径直接废掉。
    """
    s = str(s)
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# 6 个适配器
#
# 每个适配器是一个 dict：
#   id / name / 主页 / 配置路径 / detect 用的目录或可执行名
#   read()  -> 当前是否已挂 fxseek（True/False/None=读不了）
#   add()   -> 写进去
#   remove()-> 拿出来
# ---------------------------------------------------------------------------


def _mk_json_mcp(mcp_key):
    """常见的一层 JSON 结构：{"<mcp_key>": {"<server>": {...}}}。

    opencode / workbuddy / claude code 都是这个形状，只是键名不同。

    注意：这里所有适配器的 read/add/remove 都**不接受参数**——配置路径
    在各自的定义处已经写死了。统一签名是为了 detect_all() 能一视同仁地调用。
    """
    py, args = _cmd()

    def read():
        d = _read_json(WORKBUDDY_CFG)
        if d is None:
            return None
        return SERVER_KEY in (d.get(mcp_key) or {})

    def remove():
        d = _read_json(WORKBUDDY_CFG)
        if d is None:
            return False, "配置文件不是合法 JSON，没有改动它"
        if isinstance(d.get(mcp_key), dict) and SERVER_KEY in d[mcp_key]:
            d[mcp_key].pop(SERVER_KEY, None)
            _write_json(WORKBUDDY_CFG, d)
        return True, None

    return read, remove


# ---- 1. dsh ---------------------------------------------------------------
# ~/.dsh/storages/mcp_connector.json —— 用的是插件（dsh-mcp-connector）自己的
# storage-domain 表结构。**必须写全**，少一个字段整个 domain 就打不开：
#
#   DomainError: stored record 'fxseek' in table 'connections'
#                does not match its schema
#   ZodError: connectorId 缺、kind 缺、createdAt 缺、updatedAt 缺
#
# 后果很隐蔽——插件只会打一句 `dsh: warning: 1 entry did not activate`，
# 界面上没有任何提示，但那条连接**根本不会建立**，Agent 也就拿不到工具。
#
# 字段清单对着插件里的 connectionRecordSchema（lib/schema.js:103）抄，
# 值对着 buildManualRecord（lib/connectors/manual-connector.js:49）抄：
# 自定义 stdio server 的 connectorId 固定是 '__custom__'（constants.js:13）、
# kind 是 'manual'。
DSH_CFG = os.path.join(HOME, ".dsh", "storages", "mcp_connector.json")
DSH_CUSTOM_CONNECTOR_ID = "__custom__"


def _dsh_read():
    d = _read_json(DSH_CFG)
    if d is None:
        return None
    conns = (d.get("tables") or {}).get("connections") or {}
    return SERVER_KEY in conns


def _dsh_add():
    d = _read_json(DSH_CFG)
    if d is None:
        return False, "DSH 的连接配置不是合法 JSON，未改动"
    now = int(time.time() * 1000)          # 插件用 Date.now()，是毫秒
    conns = d.setdefault("tables", {}).setdefault("connections", {})
    old = conns.get(SERVER_KEY) or {}
    conns[SERVER_KEY] = {
        "key": SERVER_KEY,
        # ↓ 这四个是必填。以前漏了，导致 DSH 直接拒绝整张表。
        "connectorId": DSH_CUSTOM_CONNECTOR_ID,
        "kind": "manual",
        "createdAt": old.get("createdAt") or now,
        "updatedAt": now,
        "name": "FXseek 媒体库",
        "serverKey": SERVER_KEY,
        "serverName": SERVER_KEY,
        "transport": "stdio",
        "command": _cmd()[0],
        "args": list(_cmd()[1]),
        "env": {},
        "cwd": "",
        "headers": {},
        "scope": "global",
        "enabled": True,
    }
    _write_json(DSH_CFG, d)
    return True, None


def _dsh_remove():
    d = _read_json(DSH_CFG)
    if d is None:
        return False, "DSH 的连接配置不是合法 JSON，未改动"
    conns = (d.get("tables") or {}).get("connections") or {}
    if SERVER_KEY in conns:
        conns.pop(SERVER_KEY, None)
        _write_json(DSH_CFG, d)
    return True, None


# ---- 2. opencode ----------------------------------------------------------
# ~/.config/opencode/opencode.json  {"mcp": {"<n>": {"type":"local","command":[...]}}}
# 注意：opencode 的 command 是**数组**（可执行文件 + 参数全塞进去）。
OPENCODE_CFG = os.path.join(HOME, ".config", "opencode", "opencode.json")


def _opencode_read():
    d = _read_json(OPENCODE_CFG)
    if d is None:
        return None
    return SERVER_KEY in (d.get("mcp") or {})


def _opencode_add():
    d = _read_json(OPENCODE_CFG)
    if d is None:
        return False, "opencode.json 不是合法 JSON，未改动"
    py, args = _cmd()
    d.setdefault("mcp", {})[SERVER_KEY] = {
        "type": "local",
        "command": [py] + list(args),
        "enabled": True,
    }
    _write_json(OPENCODE_CFG, d)
    return True, None


def _opencode_remove():
    d = _read_json(OPENCODE_CFG)
    if d is None:
        return False, "opencode.json 不是合法 JSON，未改动"
    if isinstance(d.get("mcp"), dict) and SERVER_KEY in d["mcp"]:
        d["mcp"].pop(SERVER_KEY, None)
        _write_json(OPENCODE_CFG, d)
    return True, None


# ---- 3. workbuddy ---------------------------------------------------------
# ~/.workbuddy/mcp.json  {"mcpServers": {...}}  标准格式
WORKBUDDY_CFG = os.path.join(HOME, ".workbuddy", "mcp.json")
_wb_read, _wb_remove = _mk_json_mcp("mcpServers")


def _workbuddy_add():
    # workbuddy 的 mcp.json 是标准 stdio 形状，不需要 "type"/"enabled" 这两个额外键
    d = _read_json(WORKBUDDY_CFG)
    if d is None:
        return False, "workbuddy 的 mcp.json 不是合法 JSON，未改动"
    py, args = _cmd()
    d.setdefault("mcpServers", {})[SERVER_KEY] = {
        "command": py,
        "args": list(args),
    }
    _write_json(WORKBUDDY_CFG, d)
    return True, None


# ---- 4. hermes ------------------------------------------------------------
# ~/.hermes/config.yaml   mcp_servers: {}    —— YAML
HERMES_CFG = os.path.join(HOME, ".hermes", "config.yaml")


def _hermes_read():
    try:
        with open(HERMES_CFG, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return False
    # mcp_servers 下面的子键缩进是 4 空格（用行级追加时是我们自己写的），
    # 但也可能是用户手写的别的缩进——所以按「缩进 + fxseek:」宽松匹配。
    for ln in txt.splitlines():
        s = ln.rstrip()
        if s.strip() == "%s:" % SERVER_KEY and s != s.lstrip():
            return True
    return False


def _hermes_block(indent="    "):
    py, args = _cmd()
    lines = ["%s%s:" % (indent, SERVER_KEY),
             "%s    command: %s" % (indent, _q(py))]
    lines.append("%s    args:" % indent)
    for a in args:
        lines.append("%s        - %s" % (indent, _q(a)))
    return "\n".join(lines) + "\n"


def _hermes_add():
    """按行做手术，不解析整个 YAML——那样一旦有语法问题就会把用户的配置整个毁掉。

    顶层的 `mcp_servers:` 有三种可能，都要处理：
        mcp_servers: {}          ← 空映射，要把它展开成块
        mcp_servers:             ← 已经是块，直接在下面插
        （不存在）                ← 在文件末尾追加一节
    """
    try:
        with open(HERMES_CFG, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return False, "读不到 hermes 的 config.yaml"
    if _hermes_read():
        return True, None          # 已经有了

    lines = txt.splitlines(keepends=True)
    out, done = [], False
    for ln in lines:
        stripped = ln.rstrip("\n")
        # 顶层键（没有前导空格）且是 mcp_servers
        if not done and stripped.startswith("mcp_servers:"):
            after = stripped[len("mcp_servers:"):].strip()
            out.append("mcp_servers:\n")
            if after in ("", "{}"):        # 空映射 → 展开成块
                out.append(_hermes_block("    "))
                done = True
                continue
            # 理论上不该有内联内容（比如 mcp_servers: {a: 1}），保守起见不动它，
            # 改用「末尾追加」策略，但也只能再开一个同名键 —— 所以这种情况直接报错。
            out.append(ln if ln.endswith("\n") else ln + "\n")
            return False, ("hermes 的 mcp_servers 里有内联内容，"
                           "为避免写坏配置没有改动，请手动添加")
        out.append(ln)
    if not done:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append("mcp_servers:\n")
        out.append(_hermes_block("    "))
    _atomic_write(HERMES_CFG, "".join(out))
    return True, None


def _hermes_remove():
    try:
        with open(HERMES_CFG, "r", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return False, "读不到 hermes 的 config.yaml"
    out, skip = [], False
    for ln in lines:
        stripped = ln.rstrip("\n")
        if not skip and stripped == "    %s:" % SERVER_KEY:
            skip = True
            continue
        if skip:
            # 比 4 空格更深缩进的行（command / args / - xxx）都属于这个块
            if stripped.startswith("        "):
                continue
            # 空行也带走
            if stripped.strip() == "":
                continue
            skip = False
        out.append(ln)
    _atomic_write(HERMES_CFG, "".join(out))
    return True, None


# ---- 5. codex -------------------------------------------------------------
# ~/.codex/config.toml   [mcp_servers.fxseek] command=..., args=[...]
CODEX_CFG = os.path.join(HOME, ".codex", "config.toml")


def _codex_read():
    try:
        with open(CODEX_CFG, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return False
    return ("[mcp_servers.%s]" % SERVER_KEY) in txt


def _codex_add():
    try:
        with open(CODEX_CFG, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        txt = ""
    if _codex_read():
        return True, None
    py, args = _cmd()
    blk_lines = ["[mcp_servers.%s]" % SERVER_KEY,
                 "command = %s" % _q(py),
                 "args = [%s]" % ", ".join(_q(a) for a in args)]
    blk = "\n".join(blk_lines) + "\n"

    # 关键：TOML 的 [xxx] 节会一直吃到下一个 [ 开头的行为止。
    # 所以新节必须紧跟在本节之后，**绝不能直接甩到文件末尾** ——
    # 那样它会被最后一个节（比如 [tui]）吞掉，变成那个节里的键。
    lines = txt.splitlines(keepends=True)
    out, inserted = [], False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not inserted and s == "[mcp_servers]":
            out.append(ln if ln.endswith("\n") else ln + "\n")
            # 跳过紧跟其后的、属于这一节的键值行，插到它们后面
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt == "" or nxt.startswith("#"):
                    out.append(lines[j]); j += 1; continue
                if nxt.startswith("["):
                    break
                out.append(lines[j]); j += 1
            out.append("\n" + blk)
            inserted = True
            # 余下的行交给外层继续处理
            for k in range(j, len(lines)):
                out.append(lines[k])
            break
        out.append(ln)
    if not inserted:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append("\n[mcp_servers]\n" + blk)
    _atomic_write(CODEX_CFG, "".join(out))
    return True, None


def _codex_remove():
    try:
        with open(CODEX_CFG, "r", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return False, "读不到 codex 的 config.toml"
    out, skip = [], False
    for ln in lines:
        s = ln.strip()
        if s == "[mcp_servers.%s]" % SERVER_KEY:
            skip = True
            continue
        if skip:
            if s.startswith("[") and s.endswith("]"):     # 到了下一节
                skip = False
            else:
                continue                                   # 丢掉本节的键值
        out.append(ln)
    _atomic_write(CODEX_CFG, "".join(out))
    return True, None


# ---- 6. claude code -------------------------------------------------------
# ~/.claude.json  顶层 {"mcpServers": {...}}（user 级作用域）
CLAUDE_CFG = os.path.join(HOME, ".claude.json")


def _claude_read():
    d = _read_json(CLAUDE_CFG)
    if d is None:
        return None
    return SERVER_KEY in (d.get("mcpServers") or {})


def _claude_add():
    d = _read_json(CLAUDE_CFG)
    if d is None:
        return False, "~/.claude.json 不是合法 JSON，未改动"
    py, args = _cmd()
    d.setdefault("mcpServers", {})[SERVER_KEY] = {
        "type": "stdio",
        "command": py,
        "args": list(args),
    }
    _write_json(CLAUDE_CFG, d)
    return True, None


def _claude_remove():
    d = _read_json(CLAUDE_CFG)
    if d is None:
        return False, "~/.claude.json 不是合法 JSON，未改动"
    if isinstance(d.get("mcpServers"), dict) and SERVER_KEY in d["mcpServers"]:
        d["mcpServers"].pop(SERVER_KEY, None)
        _write_json(CLAUDE_CFG, d)
    return True, None


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

AGENTS = [
    {
        "id": "dsh", "name": "DSH", "full": "DeepSeek Harness",
        "cfg": DSH_CFG, "probe": os.path.join(HOME, ".dsh"),
        "sk_dir": os.path.join(HOME, ".dsh", "skills"),
        "read": _dsh_read, "add": _dsh_add, "remove": _dsh_remove,
        "note": "连接记录写入 DSH 的 storage，重启 DSH 后生效。",
    },
    {
        "id": "opencode", "name": "opencode", "full": "opencode CLI",
        "cfg": OPENCODE_CFG, "probe": os.path.join(HOME, ".config", "opencode"),
        "sk_dir": os.path.join(HOME, ".config", "opencode", "skills"),
        "read": _opencode_read, "add": _opencode_add, "remove": _opencode_remove,
        "note": "写入 opencode.json 的 mcp 段（type=local）。",
    },
    {
        "id": "workbuddy", "name": "workbuddy", "full": "WorkBuddy",
        "cfg": WORKBUDDY_CFG, "probe": os.path.join(HOME, ".workbuddy"),
        "sk_dir": os.path.join(HOME, ".workbuddy", "skills"),
        "read": _wb_read, "add": _workbuddy_add, "remove": _wb_remove,
        "note": "写入 ~/.workbuddy/mcp.json 的标准 mcpServers 段。",
    },
    {
        "id": "hermes", "name": "hermes", "full": "Hermes Agent",
        "cfg": HERMES_CFG, "probe": os.path.join(HOME, ".hermes"),
        "sk_dir": os.path.join(HOME, ".hermes", "skills"),
        "read": _hermes_read, "add": _hermes_add, "remove": _hermes_remove,
        "note": "写入 ~/.hermes/config.yaml 的 mcp_servers 段。",
    },
    {
        "id": "codex", "name": "codex", "full": "Codex CLI",
        "cfg": CODEX_CFG, "probe": os.path.join(HOME, ".codex"),
        "sk_dir": os.path.join(HOME, ".codex", "skills"),
        "read": _codex_read, "add": _codex_add, "remove": _codex_remove,
        "note": "写入 ~/.codex/config.toml 的 [mcp_servers.fxseek] 节。",
    },
    {
        "id": "claude", "name": "claude code", "full": "Claude Code",
        "cfg": CLAUDE_CFG, "probe": os.path.join(HOME, ".claude"),
        "sk_dir": os.path.join(HOME, ".claude", "skills"),
        "read": _claude_read, "add": _claude_add, "remove": _claude_remove,
        "note": "写入 ~/.claude.json 顶层的 mcpServers（用户级）。",
    },
]

BY_ID = {a["id"]: a for a in AGENTS}


# ---------------------------------------------------------------------------
# 配套技能包
# ---------------------------------------------------------------------------
#
# 为什么要有这一步：光把 MCP server 注册进去，agent 只知道「有这么几个工具」，
# 并不知道**什么时候该用**——它可能拿 search_media 去查网上的东西，或者压根想不起来。
# 技能（SKILL.md）就是那份说明书：该用的时机、查询怎么写、结果怎么读、边界在哪。
#
# 三条规矩：
#   1. 只在**缺失**时写入。已经存在就一律不动——用户可能改过它。
#   2. 关闭 MCP **不删除**技能。删人家目录里的东西是不可逆的，不做。
#   3. 单个 agent 注入失败不影响其它 agent，也不影响 MCP 本身能不能用。


def skill_path(a):
    """这个 agent 的 SKILL.md 该放哪。"""
    return os.path.join(a["sk_dir"], SKILL_NAME, "SKILL.md")


def has_skill(a):
    return os.path.isfile(skill_path(a))


def _read_skill_src():
    try:
        with open(SKILL_SRC, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _same_as_src(path):
    """目标文件和源文件内容一致吗（忽略换行差异）。"""
    src = _read_skill_src()
    if src is None:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().replace("\r\n", "\n") == src.replace("\r\n", "\n")
    except OSError:
        return False


def inject_skills(ids=None, force=False):
    """把技能包写进各 agent 的 skills 目录。

    返回 [{id, ok, action, path, error}]，action 取值：
      created  原来没有，写进去了
      replaced force=True 且内容不一样，覆盖了
      kept     已经有了，没动它（默认行为）
      same     force=True 但内容本来就一样
      missing  源文件不在（打包漏了），什么都没做
      error    写失败了
    """
    src = _read_skill_src()
    out = []
    for a in AGENTS:
        if ids and a["id"] not in ids:
            continue
        p = skill_path(a)

        if src is None:
            out.append({"id": a["id"], "ok": False, "action": "missing",
                        "path": p, "error": "技能源文件不在：%s" % SKILL_SRC})
            continue

        exists = os.path.isfile(p)
        if exists and not force:
            out.append({"id": a["id"], "ok": True, "action": "kept", "path": p})
            continue
        if exists and _same_as_src(p):
            out.append({"id": a["id"], "ok": True, "action": "same", "path": p})
            continue

        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            _atomic_write(p, src)
            out.append({"id": a["id"], "ok": True, "path": p,
                        "action": "replaced" if exists else "created"})
        except Exception as e:  # noqa: BLE001
            out.append({"id": a["id"], "ok": False, "action": "error",
                        "path": p, "error": str(e)})
    return out


def skill_status():
    """每个 agent 的技能在不在（给设置页画状态用）。"""
    return [{"id": a["id"], "name": a["name"], "path": skill_path(a),
             "has": has_skill(a), "installed": os.path.isdir(a["sk_dir"])}
            for a in AGENTS]


def detect_all():
    """返回每个 agent 的状态：装没装、配置在哪、挂没挂 fxseek。"""
    out = []
    for a in AGENTS:
        installed = os.path.isdir(a["probe"]) or os.path.exists(a["cfg"])
        connected = None
        if installed:
            try:
                connected = a["read"]()
            except Exception:  # noqa: BLE001
                connected = None
        out.append({
            "id": a["id"],
            "name": a["name"],
            "full": a["full"],
            "installed": installed,
            "config": a["cfg"],
            "config_exists": os.path.exists(a["cfg"]),
            "connected": bool(connected) if connected is not None else False,
            "readable": connected is not None,
            "skill": has_skill(a),
            "skill_path": skill_path(a),
            "note": a["note"],
        })
    return out


def enable(ids=None, with_skill=True):
    """把 fxseek 注册进指定的 agent（不传 = 所有已安装的）。

    顺带把配套技能放进它的 skills 目录 —— 但**已存在就绝不动**
    （见 inject_skills 的说明）。技能注入失败不算 MCP 失败，只在结果里标出来。
    """
    results = []
    for a in AGENTS:
        if ids and a["id"] not in ids:
            continue
        if not (os.path.isdir(a["probe"]) or os.path.exists(a["cfg"])):
            results.append({"id": a["id"], "ok": False,
                            "error": "没有检测到这个 agent"})
            continue
        try:
            bak = _backup(a["cfg"])
            ok, err = a["add"]()
            row = {"id": a["id"], "ok": bool(ok), "error": err, "backup": bak}
            if ok and with_skill:
                r = inject_skills([a["id"]])[0]
                row["skill"] = r["action"]
                row["skill_path"] = r["path"]
                if not r["ok"]:
                    row["skill_error"] = r.get("error")
            results.append(row)
        except Exception as e:  # noqa: BLE001
            results.append({"id": a["id"], "ok": False, "error": str(e)})
    return results


def disable(ids=None):
    """从指定的 agent 里摘掉 fxseek（不传 = 所有已挂的）。

    **不动技能目录**：关掉检索不等于要人家把说明书也扔了，
    而且用户可能已经把它当资料留着。要删得他们自己删。
    """
    results = []
    for a in AGENTS:
        if ids and a["id"] not in ids:
            continue
        keep = has_skill(a)
        try:
            connected = a["read"]() if os.path.exists(a["cfg"]) else False
        except Exception:  # noqa: BLE001
            connected = False
        if not connected:
            results.append({"id": a["id"], "ok": True, "skipped": True,
                            "skill_kept": keep})
            continue
        try:
            _backup(a["cfg"])
            ok, err = a["remove"]()
            results.append({"id": a["id"], "ok": bool(ok), "error": err,
                            "skill_kept": keep})
        except Exception as e:  # noqa: BLE001
            results.append({"id": a["id"], "ok": False, "error": str(e),
                            "skill_kept": keep})
    return results


if __name__ == "__main__":
    if "--skills" in sys.argv:
        print("FXseek 技能包注入自检（源：%s）\n" % SKILL_SRC)
        rows = inject_skills(force="--force" in sys.argv)
        for r in rows:
            print("  %-12s %-9s %s" % (r["id"], r["action"], r.get("error") or r["path"]))
    elif "--test" in sys.argv:
        print("FXseek MCP 各 agent 适配器自检\n")
        _py, _args = _cmd()
        print("server 命令: %s %s\n" % (_py, " ".join(_args)))
        for row in detect_all():
            mark = "✓ 已连接" if row["connected"] else (
                "· 已安装未连接" if row["installed"] else "— 未安装")
            print("  %-12s %-16s %s" % (row["name"], mark, row["config"]))
            if row["installed"] and not row["readable"]:
                print("               （配置存在但读不出来）")
            if row["installed"]:
                print("               技能: %s  %s" % (
                    "有" if row["skill"] else "没有", row["skill_path"]))
