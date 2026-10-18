"""FXseek 手机版 —— 桌面端这一侧的地基。

手机要连上来，电脑得先做到三件事：

  1. **能被发现** —— mDNS 广播 `_fxseek._tcp`，手机不用手输 IP
  2. **能安全地连** —— 6 位配对码换一张长期 token，之后走 Bearer
  3. **能问清楚对方会什么** —— `/v1/mobile/caps`（见 app.py）

★ 设计原则：**纯增量**。局域网开关默认关着，关着的时候这个模块等于不存在：
  不改任何现有行为、不加任何新依赖、`127.0.0.1` 回环请求永远放行 ——
  桌面端自己的 UI 是回环，所以它完全感知不到这里发生的事情。

★ 为什么 mDNS 用 `dns-sd` 而不是 pip 装 `zeroconf`：
   `dns-sd` 是 macOS 自带的 Bonjour 命令行（/usr/bin/dns-sd），背后就是系统
   自己的 mDNSResponder —— 和「访达里能互相看见」用的是同一套东西，最不容易出
   怪问题。引 zeroconf 要多带一个 pip 包进包体，做的事却一模一样。

★ 为什么配对码是「临时」的而不是固定的：
   固定码印在设置页上，等于把密码贴在门上；6 位数字的穷举空间只有 100 万，
   所以配了「5 分钟过期 + 同 IP 试错限速 + 用掉立刻换一个」三件事。
"""

import ipaddress
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time

# Bonjour 服务类型。手机端按这个名字去 browse。★ 前后下划线都不能省。
SERVICE_TYPE = "_fxseek._tcp"
API_VERSION = 1

# 配对码有效期（秒）
CODE_TTL = 300
# 同一个来源 IP 的试错上限与冷却（秒）
TRY_MAX = 5
TRY_COOLDOWN = 60
# 设备上次活跃时间的落盘节流（秒）—— 每个请求都写一次 settings.json 太浪费
SEEN_THROTTLE = 600
# 「这台手机此刻在线」的判定窗口（秒）。手机每张缩略图都是一次请求，正常连着
# 的时候几乎每秒都有；超过这个时间没有任何请求，就当它断开/息屏了。
# ★ 只存在内存里：这是「此刻在不在」而不是「来过没有」，重启后一律显示离线。
# ★ 90 → 60（范老板 2026-10-13）：他要的是「断开 1 分钟后指示灯熄灭」。
#   顶栏那几枚手机角标走的就是这个窗口，得和侧栏的 `PHONE_SOURCE_TTL` 对齐，
#   否则同一个「断没断」在两处给出不一样的答案。手机心跳是 20 秒一次，
#   60 秒 = 3 倍余量，网络抖一下不会闪。
LIVE_WINDOW = 60

_LOCK = threading.RLock()
# 由 app.py 在启动时注入（避免循环 import：这个模块不认识索引、设置、日志怎么写）
_ST = {"load": None, "save": None, "log": None, "restart": None}

_PAIR = {"code": "", "expires": 0.0}
_TRIES = {}                      # ip -> [失败次数, 解锁时间]
_ADV = {"proc": None, "port": 0, "name": "", "txt": []}
_SEEN = {}                       # token -> 已落盘的 last_seen
_LIVE = {}                       # 设备 id -> 最近一次收到请求的时间（内存，不落盘）


def init(load_settings, save_settings, op_log=None, restart_server=None):
    """注入宿主能力。app.py 启动时调一次。

    restart_server(on) 是可选的：局域网开关会把监听地址在
    `127.0.0.1` 和 `0.0.0.0` 之间切换，而绑定地址是**建监听时定死的**，
    所以必须让宿主把服务停掉再架起来。
    """
    _ST.update({"load": load_settings, "save": save_settings,
                "log": op_log, "restart": restart_server})


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _log(action, detail="", level="info"):
    fn = _ST.get("log")
    if fn:
        try:
            fn(action, detail, level=level)
        except Exception:
            pass


def _settings():
    fn = _ST.get("load")
    if not fn:
        return {}
    try:
        return fn() or {}
    except Exception:
        return {}


def _save(patch):
    fn = _ST.get("save")
    if not fn:
        return
    try:
        fn(patch)
    except Exception as e:
        _log("局域网", "设置保存失败：%s" % e, level="warn")


def is_loopback(ip):
    """回环地址判定。

    ★ 不用 `ip == "127.0.0.1"` 这种写法：IPv6 客户端是 `::1`，
      某些情况下还会见到 `127.0.0.2`（整个 127/8 都是回环）。
    """
    try:
        return ipaddress.ip_address(str(ip or "").split("%")[0]).is_loopback
    except ValueError:
        return False


def lan_enabled():
    """局域网开放开关。默认关 —— 安全第一（见规划文档 §14-2）。"""
    return bool(_settings().get("mobile_lan"))


def lan_ip():
    """本机在局域网里的地址。

    用一个「连不出去」的 UDP connect 让内核替我们挑路由 —— 不会真的发包
    （192.0.2.0/24 是 RFC 5737 的 TEST-NET-1，专门留给文档示例用的）。
    比遍历网卡猜哪个是 Wi-Fi 可靠。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 设备与令牌
# --------------------------------------------------------------------------
def _devices_raw():
    d = _settings().get("mobile_devices") or []
    return [x for x in d if isinstance(x, dict) and x.get("token")]


def mask_token(tok):
    tok = str(tok or "")
    return (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "…"


def public_device(d, online=None):
    """给界面看的设备信息 —— ★ 永远不带完整 token。

    `online` 不传就自己按 `_LIVE` 算（见 is_online）。配对成功那一刻回给手机的
    响应里也会用这个函数，那时 `_LIVE` 刚被 touch 写过，所以自然是在线的。
    """
    if online is None:
        online = is_online(d)
    return {"id": d.get("id"), "name": d.get("name"),
            "created": d.get("created"), "last_seen": d.get("last_seen"),
            "from": d.get("from"), "token_hint": mask_token(d.get("token")),
            "online": bool(online), "kind": device_kind(d)}


def is_online(d, now=None):
    """这台设备「此刻」是不是连着。

    ★ 和 `last_seen` 是两件事：`last_seen` 是**落盘**的「最近来过」，节流 10 分钟，
      所以它只能回答「今天连过没有」；界面要的是「现在还在不在」，那个必须走内存
      的 `_LIVE`（每个请求都会刷新，见 touch），不然手机关掉后图标还会亮十分钟。
    """
    if not d:
        return False
    now = time.time() if now is None else now
    ts = _LIVE.get(d.get("id"), 0.0)
    return (now - ts) < LIVE_WINDOW


# 设备类型：界面按这个换图标（iPhone 和安卓各一套）。★ 只影响画哪个图标，
# 不参与任何鉴权判断。
KINDS = ("ios", "android", "other")
# 名字里出现这些词就按对应类型算。★ 这是**兜底**：老的已配对设备（内存里没存
# kind）和用户手改过名字的都靠它；手机连上来时会自己报 kind，那条最准。
_KIND_HINTS = (
    ("ios", ("ipad", "iphone", "ipod", "ios")),
    ("android", ("android", "红米", "redmi", "小米", "xiaomi", "华为", "huawei",
                 "honor", "荣耀", "oppo", "vivo", "一加", "oneplus", "三星",
                 "samsung", "galaxy", "pixel", "魅族", "meizu", "realme", "努比亚")),
)


def normalize_kind(raw):
    """把手机报上来的类型收成 ios/android/other。

    ★ 认不出来的一律返回 **空串**（不是 "other"）。空串是「手机没说 / 说的词我们
    不认识」，后面 `device_kind` 才有机会退回按名字猜；要是这里直接兜成 "other"，
    就等于替手机**断言**了「它是别家手机」，名字兜底永远轮不上（踩过这个坑：
    Redmi Note 12 明明能靠名字认出安卓，却被存成 other）。
    """
    k = str(raw or "").strip().lower()
    if not k:
        return ""
    if k in ("ios", "iphone", "ipad", "ipados", "苹果"):
        return "ios"
    if k in ("android", "安卓", "鸿蒙", "harmony", "harmonyos"):
        return "android"
    if k in ("other", "其他", "其它"):
        return "other"
    return ""


def device_kind(d):
    """这台设备画哪个图标。显式 kind 优先，没有就按名字猜，再不行算 other。"""
    if not d:
        return "other"
    k = normalize_kind(d.get("kind"))
    if k:
        return k
    name = str(d.get("name") or "").lower()
    for kind, words in _KIND_HINTS:
        if any(w in name for w in words):
            return kind
    return "other"


def devices():
    return [public_device(d) for d in _devices_raw()]


def check_token(tok):
    """令牌校验。命中返回设备字典，否则 None。

    ★ 用 compare_digest 而不是 `==`：后者会在第一个不同的字节就返回，
      比较耗时和「猜对了几位」相关，理论上能被计时攻击一点点试出来。
    """
    if not tok:
        return None
    # ★ 必须比 bytes 而不是 str：compare_digest 对含非 ASCII 的字符串会直接抛
    #   TypeError（实测手机侧随便带一个中文令牌，整个请求就变成一个空响应）。
    #   编成 bytes 后这个限制没了，防计时攻击的作用一点不减。
    try:
        want = str(tok).encode("utf-8", "ignore")
    except Exception:
        return None
    if not want:
        return None
    hit = None
    for d in _devices_raw():
        try:
            got = str(d.get("token") or "").encode("utf-8", "ignore")
        except Exception:
            continue
        # 不提前 return：提前退出会暴露「是第几台设备匹配上的」
        if got and secrets.compare_digest(got, want):
            hit = d
    return hit


def touch(dev):
    """记一次「设备还活着」。

    ★ 分两层：内存的 `_LIVE` **每次**都写（界面靠它判断「此刻在不在」），
      settings.json 则节流 —— 手机每张缩略图都是一次请求，每次都落盘会把磁盘写烂。
    """
    if not dev:
        return
    did = dev.get("id")
    if did:
        _LIVE[did] = time.time()
    tok = str(dev.get("token") or "")
    now = int(time.time())
    if now - _SEEN.get(tok, 0) < SEEN_THROTTLE:
        return
    _SEEN[tok] = now
    try:
        devs = _devices_raw()
        for d in devs:
            if d.get("id") == dev.get("id"):
                d["last_seen"] = now
        _save({"mobile_devices": devs})
    except Exception:
        pass


def mark_offline(dev):
    """把一台设备**立刻**标成离线（范老板 2026-10-13）。

    手机上点了「断开连接」时调。`is_online` 看的是内存 `_LIVE` 的时间戳，
    把它抹掉即可当帧生效，不用等 `LIVE_WINDOW`（60 秒）自然过期。

    为什么不能只推 `last_seen`：那只是落盘的「来过没有」，界面判定用的是
    内存窗口。实测只动 `phone_sources` 的话，侧栏绿点立刻灭、顶栏那枚手机
    图标还会继续亮满一分钟 —— 两处给出不一样的答案。

    `dev` 就是校验令牌返回的那条配对记录，所以不会误伤别的手机。
    """
    if not dev:
        return
    did = dev.get("id")
    if did:
        _LIVE.pop(did, None)


def needs_auth(client_ip):
    """这个来源要不要带令牌。

    ★ 回环永远不要 —— 桌面端自己的界面、菜单栏助手、MCP 都走 127.0.0.1，
      给它们加一道鉴权等于把自家门锁上。
    """
    return lan_enabled() and not is_loopback(client_ip)


def revoke(device_id=None):
    """撤销一个设备；不给 id 就是全撤（「断开所有手机」）。"""
    devs = _devices_raw()
    if device_id:
        keep = [d for d in devs if d.get("id") != device_id]
        name = next((d.get("name") for d in devs if d.get("id") == device_id), device_id)
    else:
        keep, name = [], "全部设备"
    n = len(devs) - len(keep)
    _save({"mobile_devices": keep})
    _SEEN.clear()
    # 内存里的在线表也要跟着清，否则被撤掉的设备在「当前在线」里阴魂不散
    for d in devs:
        _LIVE.pop(d.get("id"), None)
    if n:
        _log("配对", "已断开 %s（%d 台）" % (name, n), level="warn")
    return {"ok": True, "removed": n, "devices": devices()}


# --------------------------------------------------------------------------
# 配对
# --------------------------------------------------------------------------
def pair_code(force=False):
    """当前配对码；没有 / 过期 / force 就现生成一个。

    ★ 惰性生成：没人看的时候**不存在**配对码。开机就摆一个「482913」在内存里
      等于给了攻击者一个固定的靶子。
    """
    with _LOCK:
        now = time.time()
        if force or not _PAIR["code"] or now >= _PAIR["expires"]:
            _PAIR["code"] = "%06d" % secrets.randbelow(1000000)
            _PAIR["expires"] = now + CODE_TTL
            _TRIES.clear()
        return {"code": _PAIR["code"], "expires": int(_PAIR["expires"]),
                "ttl": max(0, int(_PAIR["expires"] - now))}


def pair_request(code, name="", ip="", kind=""):
    """用配对码换令牌。这是**唯一**一个免鉴权的写接口。

    `kind` 是手机自报的类型（ios/android/other），只用来决定主界面画哪个图标。
    手机端不一定传（老版本客户端没有这个字段），那就退回按名字猜，见 device_kind。
    """
    now = time.time()
    with _LOCK:
        n, until = _TRIES.get(ip, [0, 0.0])
        if now < until:
            left = int(until - now) + 1
            return {"ok": False, "retry_after": left,
                    "error": "试错太多次了，请等 %d 秒后再试" % left}

        if not _PAIR["code"]:
            # ★ 「没生成过」和「过期了」要分开说：前者是用户还没去电脑上点，
            #    笼统说「已过期」会让人反复重试同一个不存在的码。
            return {"ok": False, "need_code": True,
                    "error": "电脑上还没生成配对码，请在「设置 → 连接」里点「显示配对码」"}
        if now >= _PAIR["expires"]:
            return {"ok": False, "need_code": True,
                    "error": "配对码已过期，请在电脑上重新获取"}

        want = str(code or "").strip()
        ok = (len(want) == 6 and want.isdigit()
              and secrets.compare_digest(want, _PAIR["code"]))
        if not ok:
            n += 1
            if n >= TRY_MAX:
                _TRIES[ip] = [0, now + TRY_COOLDOWN]
                _log("配对", "来源 %s 试错 %d 次，锁定 %d 秒" % (ip, TRY_MAX, TRY_COOLDOWN),
                     level="warn")
                return {"ok": False, "retry_after": TRY_COOLDOWN,
                        "error": "配对码不对，已锁定 %d 秒" % TRY_COOLDOWN}
            _TRIES[ip] = [n, 0.0]
            left = TRY_MAX - n
            return {"ok": False, "error": "配对码不对（还可以试 %d 次）" % left}

        # ★ 通过之后立刻作废：配对码是一次性的，不然一台手机配对成功后，
        #   同一个码在剩余有效期内还能再配第二台。
        _PAIR["code"] = ""
        _PAIR["expires"] = 0.0
        _TRIES.pop(ip, None)

    dev = {"id": secrets.token_hex(8),
           "name": (str(name or "").strip()[:40] or "未命名手机"),
           # ★ 这里只存「手机自己说的那一句」，认不出就留空 —— 不能 `or "other"`。
           #   落了盘就意味着板上钉钉，而 device_kind 是「显式优先、名字兜底」，
           #   一旦存了 "other" 兜底那层就再也走不到（见 normalize_kind 注释）。
           "kind": normalize_kind(kind),
           "token": secrets.token_urlsafe(32),
           "created": int(now), "last_seen": int(now), "from": ip or ""}
    # ★ 手机还没发过第一个请求，但配对这一刻它显然就在线 —— 先写进 _LIVE，
    #   否则刚配对完主界面的图标是灰的，得等手机下一次请求才亮。
    _LIVE[dev["id"]] = now
    _save({"mobile_devices": _devices_raw() + [dev]})
    _log("配对", "%s 已配对（来自 %s）" % (dev["name"], ip or "局域网"))
    return {"ok": True, "token": dev["token"], "device": public_device(dev)}


# --------------------------------------------------------------------------
# mDNS 广播
# --------------------------------------------------------------------------
def _stop_advertise():
    with _LOCK:
        p = _ADV.get("proc")
        _ADV.update({"proc": None, "port": 0, "name": "", "txt": []})
    if p is None:
        return
    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=2)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _start_advertise(port, name, version, items):
    """挂上 Bonjour 广播。信息放在 TXT 里，手机 browse 时不用先连上就能看到。"""
    _stop_advertise()
    exe = shutil.which("dns-sd")
    if not exe:
        return {"ok": False, "error": "系统里找不到 dns-sd（macOS 自带，一般不该缺）"}
    txt = ["name=%s" % name, "ver=%s" % version,
           "items=%d" % int(items or 0), "api=%d" % API_VERSION]
    cmd = [exe, "-R", name, SERVICE_TYPE, "local", str(int(port))] + txt
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except Exception as e:
        return {"ok": False, "error": "dns-sd 起不来：%s" % e}
    time.sleep(0.6)
    if proc.poll() is not None:
        return {"ok": False, "error": "dns-sd 立刻退出了（端口 %s 可能被占？）" % port}
    with _LOCK:
        _ADV.update({"proc": proc, "port": int(port), "name": name, "txt": txt})
    return {"ok": True, "service": SERVICE_TYPE, "name": name, "txt": txt}


# --------------------------------------------------------------------------
# 广播时用的名字
# --------------------------------------------------------------------------
_HOST_LABEL = {"value": None}


def _run_text(cmd):
    """跑一条命令、要它的 stdout 去空白。拿不到就给空串（绝不抛）。"""
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=3)
        return (out.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def host_label():
    """自动发现时给手机看的名字 —— **这台电脑的名字**，不是统一的「FXseek」。

    为什么要它：同一个 Wi-Fi 下有几台电脑时，「附近的电脑」列表里每一条都写着
    「FXseek」，手机端根本分不出谁是谁，点错了就配错机器。

    取法（依次退回，全都拿不到才用 "FXseek"）：
      1. macOS 的「电脑名称」（系统设置 → 通用 → 共享 里那个，默认就叫
         「XXX的MacBook Pro」）；
      2. 登录用户的全名（`id -F`，中文系统下就是「范老板」）；
      3. 简写用户名 / Windows 的 %USERNAME%；
      4. 主机名。

    ★ 电脑名称如果**没带上用户名**，就拼成「<用户名>的<电脑名>」——
      这样两头都占着：看着像机器名，又能一眼认出是谁的账号。
    """
    if _HOST_LABEL["value"]:
        return _HOST_LABEL["value"]

    machine = _run_text(["scutil", "--get", "ComputerName"])
    full = _run_text(["id", "-F"])
    short = full or _run_text(["id", "-un"]) or os.environ.get("USER") \
        or os.environ.get("USERNAME") or ""
    host = (socket.gethostname() or "").split(".")[0]

    if machine and short and short not in machine:
        label = "%s的%s" % (short, machine)
    else:
        label = machine or short or host or "FXseek"

    # Bonjour 服务名一个标签最多 63 字节，中文按 3 字节算，留点余量。
    label = " ".join(label.split())[:40] or "FXseek"
    _HOST_LABEL["value"] = label
    return label


# --------------------------------------------------------------------------
# 开关与状态
# --------------------------------------------------------------------------
def state(port=0, name="", version="", items=0):
    """设置页要的全部信息。"""
    on = lan_enabled()
    devs = devices()
    out = {"enabled": on, "devices": devs, "service": SERVICE_TYPE,
           "api_version": API_VERSION, "advertising": _ADV.get("proc") is not None}
    if on:
        ip = lan_ip()
        out.update({"ip": ip, "port": int(port or _ADV.get("port") or 0),
                    "url": "http://%s:%s/" % (ip, port or _ADV.get("port") or "")})
        if not out["advertising"] and port:
            r = _start_advertise(port, name or host_label(), version, items)
            if not r.get("ok"):
                out["advertise_error"] = r.get("error")
            else:
                out["advertising"] = True
    return out


def set_lan(on, port=0, name="", version="", items=0):
    """开 / 关局域网。会顺便切换监听地址（需要宿主重启监听）。"""
    on = bool(on)
    _save({"mobile_lan": on})
    if on:
        if port:
            _start_advertise(port, name or host_label(), version, items)
    else:
        _stop_advertise()
        with _LOCK:
            _PAIR.update({"code": "", "expires": 0.0})
        _TRIES.clear()
    restart = _ST.get("restart")
    if restart:
        try:
            restart(on)
        except Exception as e:
            _log("局域网", "重开监听失败：%s" % e, level="warn")
    _log("局域网", "局域网访问已%s" % ("开启" if on else "关闭"),
         level="info" if on else "warn")
    return state(port=port, name=name, version=version, items=items)


def shutdown():
    _stop_advertise()
