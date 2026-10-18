# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
#
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。
"""离线许可证：签名验真 + 免费版配额。

为什么是签名码，不是明文码
--------------------------
明文码（`FXSEEK-PRO-2026` 这种）有三个致命伤：

  ① 流出去一份，全网通用 —— 没人会去举报，你也拦不住；
  ② 用户把本地 `license.txt` 里的档位改成 pro 就绕过了 —— 一个记事本搞定；
  ③ 没法按客户发不同的码，「这码是谁泄露的」永远查不出来。

签名码把「授权什么」写进载荷，再用**只有开发者手上有的私钥**签一遍，
App 里只放公钥。用户能改本地文件、能删文件，但**改不出一个合法签名**。
要绕过就只能动代码（改 `app.py` 或重新签名整个包），门槛高一个数量级。

★ 说句实话：`app.py` 在 `.app` 里是明文 Python，真有决心的人还是能改。
  这套东西的目标是**拦住顺手牵羊**、让愿意付钱的人付得心服口服，
  不是「绝对锁得住」。所以别按「锁得住」来定价。

码长什么样
----------
    FXK1-XXXXX-XXXXX-…-XXXXX

就是 `base32(载荷 ‖ 签名)`，连字符纯粹给眼睛分组、解析时一律丢掉。
载荷 12 字节 + 签名 64 字节（Ed25519）= 76 字节 → 122 个字符。
**以粘贴为主**，不用指望用户手抄。

载荷布局（12 字节定长，比 JSON 省一半多）
----------------------------------------
    偏移   长度   字段        说明
    0      1     版本        固定 1（将来换格式好认）
    1      1     档位        1 = pro（0 预留给 free，一般不会签发）
    2..3   2     签发日      uint16 大端，自 2020-01-01 起的天数
    4..5   2     有效天数    uint16 大端，**0 = 永久**
    6..11  6     序列号      随机，用来区分客户（也是「谁泄露了」的线索）

到期判定 = `签发日 + 有效天数`，所以载荷里不用再单独存到期日。
"""

from __future__ import annotations

import base64
import json
import os
import time

# ── 免费版配额 ──────────────────────────────────────────────────────────────
#
# ★★ 这几个数字就是「免费版能用到什么程度」，改这里就够了 —— 全项目只有这一处。
#
# 2026-10-13 范老板定的新方案（推翻了原来的「3 个索引源 + 5000 件」）：
#
#   ① 索引源个数**不再限制** —— 原来卡 3 个，是因为怕用户拿免费版当生产工具；
#      但实际用下来，「几个源」跟「值不值 19.9」几乎无关，反而最先撞墙、
#      最容易让人觉得「这软件是坏的」。取消。
#   ② 素材件数从 5000 收到 **2000**，而且口径变了：
#      **本机索引 + 所有联机终端的素材，合计**。原来只算电脑本机，
#      手机相册几千张不计入 —— 等于给了个后门。
#   ③ 新增「最多绑 1 台移动终端」。历史绑定（配过就算，离线也算），
#      要换手机得先在设置里把旧的解绑。
#
# 为什么 2000 而不是 5000：2000 张照片对「随便试试」依然完全够用
# （一个手机相册就这个量级），但真把整个照片库挂上去就会撞墙 ——
# 那时候他已经知道这东西好用了，而不是还在判断「这软件是不是坏的」。
FREE_MAX_SOURCES = None      # None = 不限（保留键名，免得 UI / 旧逻辑取不到）
FREE_MAX_ITEMS = 2000
FREE_MAX_TERMINALS = 1       # 免费版最多绑几台移动终端；None = 不限

# ── 分享奖励 ────────────────────────────────────────────────────────────────
#
# 规则（2026-10-13 范老板拍板，当天下午·十一 又收紧了口径）：
#   分享给朋友 +15 天、转发朋友圈 +30 天；**攒到多少补多少**（2026-10-18 改了：
#   原来「每天最多计 1 次」，范老板反馈「贴了码不涨时间」——一天只补一次太反直觉）；
#   两档合计累计封顶 90 天；
#   累计封顶 **3 个月（90 天）** —— 原来是「一次 30 天、封顶 12 个月」，
#   范老板说「一年期限太长了」。
#   手机端和电脑端的分享共用同一套额度池。
#
#   ★ 封顶为什么是 90 而不是 30×3=90……它就是 90：3 个月按 30 天算。
#     注意 90 天在「15 天档」和「30 天档」混着来的时候**不整除**，
#     所以 `credit_share` 对贴顶的那一次只给**剩余额度**（80 → 只加 10），
#     而不是整档拒绝 —— 否则用户会在 75 天时发现「分享没反应」。
#
# ★ 为什么奖励**不经过发卡平台**：发卡平台只在「有真实交易」时抽成。
#   分享奖励不产生交易、钱没有从作者手里过，平台看不见也抽不着 ——
#   所以给免费用户发分享奖励，边际成本是 0。
#
# ★ 奖励只存**本机**（防君子）：用户能删掉 bonus.json 回到免费版，
#   但删了对他没好处（本来就想要额度），而伪造又需要作者私钥。
#   和签名码一样，目标是拦住顺手牵羊，不是绝对锁得住。
# ★ 范老板 2026-10-13（下午·十一）改的口径：
#   「一年期限太长了，改到最多 3 个月，每次分享给朋友得 15 天，转发朋友圈才是一个月。」
#   所以①封顶从 360 天砍到 **90 天**；②奖励**分两档** —— 点对点发给朋友 15 天，
#   发朋友圈（曝光更大）30 天。两档共用同一个「每天 1 次」的额度池。
#
#   ⚠️ 2026-10-18 起 `FREE_SHARE_PER_DAY = 0`（不限次）。冷却那段代码留着，
#      传非 0 就恢复按天限次，别再当成死代码删掉。
#
#   ⚠️ `credit_share` 的默认档是 **moments（30 天）**，不是 friend：
#      默认值是给「没传 kind 的老调用」用的，宁可多给也别少给 ——
#      少给了用户会来问，多给了只是少赚。
SHARE_KIND_FRIEND = "friend"     # 分享给朋友（一对一）
SHARE_KIND_MOMENTS = "moments"   # 转发朋友圈（一对多）
FREE_SHARE_DAYS_FRIEND = 15      # 每次「分享给朋友」加多少天
FREE_SHARE_DAYS_MOMENTS = 30     # 每次「转发朋友圈」加多少天
FREE_SHARE_DAYS = FREE_SHARE_DAYS_MOMENTS   # 兼容旧调用
FREE_SHARE_MAX_DAYS = 90         # 累计封顶 = 3 个月（原 360 = 12 个月）
FREE_SHARE_PER_DAY = 0           # 每天最多计几次（两档合计）；0 = 不限（2026-10-18）


def share_days(kind: str = SHARE_KIND_MOMENTS) -> int:
    """这一档每次加多少天。认不出的 kind 按最多的那档给。"""
    return FREE_SHARE_DAYS_FRIEND if str(kind or "") == SHARE_KIND_FRIEND \
        else FREE_SHARE_DAYS_MOMENTS

TIER_FREE = "free"
TIER_PRO = "pro"

_DAY = 86400                 # 一天多少秒
_EPOCH = 1577836800          # 2020-01-01 00:00:00 UTC —— 载荷里那个「天」的零点
_PAYLOAD_LEN = 12
_CODE_PREFIX = "FXK1"
_SIG_LEN = 64

#: Ed25519 公钥（32 字节原始，base64）。对应的私钥在开发者机器上，
#: **永远不进仓库** —— 用 `tools/gen_keys.py` 生成、`tools/make_license.py` 签发。
#: 换密钥会让**所有已发出的码失效**，所以一旦发过货就别再动它。
PUBLIC_KEY_B64 = "3S3oZ8ajrkn2eMhLHas7Q843mJrnmOOPEGjAocIie9A="

_LICENSE_PATH = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "FXseek", "license.txt")

#: 分享奖励 / 终端绑定 的落盘位置，跟 license.txt 放在一起。
#: 单独一个文件是为了**别把签名码覆盖掉** —— 用户在「许可证」面板里
#: 解绑个手机不该动到他的激活码。
#:
#: ★ 可用 `FXSEEK_BONUS_PATH` 覆盖 —— **写测试必须覆盖**。
#:   自测时踩过一次：直接调 `credit_share()` 把开发机真实的 bonus.json 写脏了
#:   （当场多出 30 天专业版 + 一条假的手机分享记录）。
#:   这个环境变量就是给「跑一遍看看」用的，生产路径不受影响。
_BONUS_PATH = os.environ.get("FXSEEK_BONUS_PATH") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "FXseek", "bonus.json")

_cache: dict | None = None
_bonus: dict | None = None


# ── 载荷 / 编码 ─────────────────────────────────────────────────────────────

def _b32_nopad(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=")


def _b32_dec(text: str) -> bytes:
    """宽松解码：丢掉连字符/空格，补回长度对齐 —— 用户粘贴时什么样都认。"""
    t = "".join(ch for ch in text if ch.isalnum()).upper()
    if t.startswith(_CODE_PREFIX):
        t = t[len(_CODE_PREFIX):]
    pad = (-len(t)) % 8
    return base64.b32decode(t + "=" * pad)


def _unpack(payload: bytes) -> dict | None:
    if len(payload) != _PAYLOAD_LEN or payload[0] != 1:
        return None
    tier = TIER_PRO if payload[1] == 1 else TIER_FREE
    issued = int.from_bytes(payload[2:4], "big")
    valid = int.from_bytes(payload[4:6], "big")
    return {
        "v": payload[0],
        "tier": tier,
        "issued_days": issued,
        "valid_days": valid,
        "serial": _b32_nopad(payload[6:12]),
        # 0 = 永久；否则从签发那天算起
        "expires_at": None if not valid else _EPOCH + (issued + valid) * _DAY,
    }


def _verify(code: str) -> dict | None:
    """验签。任何一步不对就返回 None（调用方一律当成免费版）。"""
    try:
        raw = _b32_dec(code)
    except Exception:
        return None
    if len(raw) != _PAYLOAD_LEN + _SIG_LEN:
        return None
    payload, sig = raw[:_PAYLOAD_LEN], raw[_PAYLOAD_LEN:]
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
        pub.verify(sig, payload)
    except Exception:
        # ★ 加密库缺失、公钥没配、签名不对 —— 一律按「不通过」处理。
        #   绝不能让「验不了」变成「放行」。
        return None
    info = _unpack(payload)
    if not info:
        return None
    exp = info.get("expires_at")
    if exp and time.time() > exp:
        info["expired"] = True
    return info


# ── 落盘 / 读盘 ─────────────────────────────────────────────────────────────

def license_path() -> str:
    return _LICENSE_PATH


def load(force: bool = False) -> dict:
    """当前生效的许可证。**每次读盘都重新验签** —— 不信任文件里的任何字段。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    info = {"tier": TIER_FREE, "serial": None, "expires_at": None,
            "expired": False, "source": "none"}
    try:
        with open(_LICENSE_PATH, "r", encoding="utf-8") as f:
            code = f.read().strip()
    except OSError:
        code = ""
    if code:
        got = _verify(code)
        if got:
            got["source"] = "file"
            info = got
        else:
            info["source"] = "invalid"      # 文件在，但码不合法（改过 / 换过密钥）
    _cache = info
    return info


def tier() -> str:
    return load().get("tier") or TIER_FREE


# ── 分享奖励 / 终端绑定 的落盘 ──────────────────────────────────────────────
#
# bonus.json 长这样：
#   {
#     "bonus_expires_at":  1790000000.0,   # 奖励攒出来的专业版到期时间戳
#     "share_days_total":  60,             # 已累计的奖励天数（封顶 FREE_SHARE_MAX_DAYS）
#     "credited_days":     ["2026-10-13"], # 最近计过数的日期（用来做「每天 1 次」冷却）
#     "phone_shares":      {"<device_id>": 3},   # 每台手机已计入的分享次数
#     "terminals":         [{"id","name","bound_at"}],  # 绑定的移动终端
#   }
#
# ★ 「每天最多 1 次」用的是**本地日期字符串**而不是「距上次多少秒」：
#   用户想要的是「一天一次」这个直觉，不是「间隔 24 小时」——
#   23:59 分享一次、00:01 再分享一次，按直觉就是两天，该给两次。

def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _bonus_load(force: bool = False) -> dict:
    global _bonus
    if _bonus is not None and not force:
        return _bonus
    data = {"bonus_expires_at": 0.0, "share_days_total": 0,
            "credited_days": [], "phone_shares": {}, "terminals": [],
            "unbound": []}
    try:
        with open(_BONUS_PATH, "r", encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            data.update({k: got.get(k, v) for k, v in data.items()})
    except (OSError, ValueError):
        pass
    # 类型兜底：文件被手改坏了也別让整个 App 起不来
    if not isinstance(data.get("terminals"), list):
        data["terminals"] = []
    if not isinstance(data.get("unbound"), list):
        data["unbound"] = []
    if not isinstance(data.get("phone_shares"), dict):
        data["phone_shares"] = {}
    if not isinstance(data.get("credited_days"), list):
        data["credited_days"] = []
    try:
        data["bonus_expires_at"] = float(data.get("bonus_expires_at") or 0)
        data["share_days_total"] = int(data.get("share_days_total") or 0)
    except (TypeError, ValueError):
        data["bonus_expires_at"], data["share_days_total"] = 0.0, 0
    _bonus = data
    return _bonus


def _bonus_save(data: dict) -> None:
    global _bonus
    _bonus = data
    try:
        os.makedirs(os.path.dirname(_BONUS_PATH), exist_ok=True)
        tmp = _BONUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _BONUS_PATH)      # 原子替换：别写到一半断电留个半截文件
    except OSError:
        pass


def bonus_expires_at() -> float:
    """奖励攒出来的到期时间戳。0 = 没有奖励。"""
    d = _bonus_load()
    exp = float(d.get("bonus_expires_at") or 0)
    return exp if exp > time.time() else 0.0


def bonus_active() -> bool:
    return bonus_expires_at() > time.time()


def credit_share(source: str = "desktop", count: int = 1,
                 kind: str = SHARE_KIND_MOMENTS) -> dict:
    """记 `count` 次分享（每次按 `kind` 那一档算天数）。返回真正计进去了几次。

    ★ 三道闸：① 没到今天的额度就不计（每天 FREE_SHARE_PER_DAY 次）；
      ② 累计天数封顶 FREE_SHARE_MAX_DAYS；
      ③ 只有真的 +了天数才算「今天用掉了」—— 被 ①② 挡掉的不消耗今天的额度。
      最后这条很重要：要是「撞了封顶白扣掉今天的一次」，
      用户会看到「我分享了啊，怎么没加」而完全不知道是封顶了。

    ★ 封顶改成 90 之后**不再整除**（15 / 30 混着来，最后总差一截），
      所以对着顶的那一次**给剩余额度**而不是整档拒绝 —— 比如已经 80 天、
      再来一次 30 天的，就只加 10 天；攒到正好 90 之后才报 capped。
      不这么做的话用户会在 75 天的时候发现「分享没反应」，那更难解释。
    """
    d = _bonus_load()
    now = time.time()
    today = _today()
    days = d.get("credited_days") or []
    used_today = days.count(today)
    total = int(d.get("share_days_total") or 0)
    days_each = share_days(kind)

    credited, reasons, granted = 0, [], 0
    for _ in range(max(0, int(count))):
        if FREE_SHARE_PER_DAY > 0 and used_today >= FREE_SHARE_PER_DAY:
            reasons.append("cooling")
            break
        room = FREE_SHARE_MAX_DAYS - total
        if room <= 0:
            reasons.append("capped")
            break
        give = min(days_each, room)          # 贴顶那次只补剩余额度
        base = max(now, float(d.get("bonus_expires_at") or 0))
        # 封顶口径 = 「从现在起最多还能有 FREE_SHARE_MAX_DAYS 天」，
        # 所以第二次分享是在**已有到期日**上顺延，而不是从今天重算。
        new_exp = min(base + give * _DAY, now + FREE_SHARE_MAX_DAYS * _DAY)
        if new_exp <= float(d.get("bonus_expires_at") or 0):
            reasons.append("capped")
            break
        d["bonus_expires_at"] = new_exp
        total += give
        granted += give
        used_today += 1
        credited += 1

    if credited:
        d["share_days_total"] = total
        d["credited_days"] = (days + [today] * credited)[-60:]   # 只留最近 60 天就够判冷却
        _bonus_save(d)

    return {"ok": credited > 0, "credited": credited, "per_day": FREE_SHARE_PER_DAY,
            "last_reason": (reasons[-1] if reasons else ""),
            "share_days_total": int(d.get("share_days_total") or 0),
            "max_days": FREE_SHARE_MAX_DAYS, "days_each": days_each,
            "days_granted": granted, "kind": kind,
            "expires_at": float(d.get("bonus_expires_at") or 0) or None,
            "source": source}


def _share_norm(v) -> dict:
    """把 `phone_shares` 里的一个值归一到 `{"friend": int, "moments": int}`。

    ★ 旧格式是个**整数**（那时候只有一档、每次 30 天）。这里当成 moments：
      它也是 30 天，这样老用户升上来「已经计过的次数」不会被当成 0 重算一遍
      —— 重算就等于白送，而且用户会看到「怎么又加了一次」。
    """
    if isinstance(v, dict):
        try:
            return {"friend": max(0, int(v.get("friend") or 0)),
                    "moments": max(0, int(v.get("moments") or 0))}
        except (TypeError, ValueError):
            return {"friend": 0, "moments": 0}
    try:
        return {"friend": 0, "moments": max(0, int(v or 0))}
    except (TypeError, ValueError):
        return {"friend": 0, "moments": 0}


def phone_share_sync(device_id: str, reported) -> dict:
    """手机自报「分享给朋友 N 次 / 发朋友圈 M 次」—— 只把**新增**的拿去记奖励。

    `reported` 可以是：
      · dict `{"friend": n, "moments": m}` —— 现在手机发的形状；
      · 整数 `n`                            —— 老调用，当成 moments（30 天那档）。

    ★ 为什么记「每台手机已计入的累计值」而不是「上次的原始值」：
      被冷却挡掉的那几次不能就此丢掉。用户上午连了一次、只计了 1 次，
      下午再连、手机上还是 3 次 —— 如果只比对原始值，那剩下的 2 次就永远补不上，
      用户会看到「我分享 3 次只给了 1 次」却不知道为什么。
      记「已计入累计」，那剩下的会等到明天冷却解除后自动补。

    ★ 补的时候**先补朋友圈（30 天）再补朋友（15 天）**：一天只计一次，
      顺序决定了用户先拿到哪一档 —— 先给多的那档对他有利，也少一轮来回。
    """
    device_id = str(device_id or "").strip()
    if not device_id:
        return {"ok": False, "credited": 0, "reason": "no_device"}

    if isinstance(reported, dict):
        want = _share_norm(reported)
    else:
        try:
            want = {"friend": 0, "moments": max(0, int(reported or 0))}
        except (TypeError, ValueError):
            want = {"friend": 0, "moments": 0}

    d = _bonus_load()
    seen = _share_norm((d.get("phone_shares") or {}).get(device_id))
    pending = {k: max(0, want[k] - seen[k]) for k in ("friend", "moments")}
    if not any(pending.values()):
        return {"ok": True, "credited": 0, "days_granted": 0,
                "pending": 0, "pending_friend": 0, "pending_moments": 0,
                "granted": {"friend": 0, "moments": 0},
                "reason": "nothing_new"}

    got = {"friend": 0, "moments": 0}
    last = {}
    days_got = 0
    for kind in ["moments"] * pending["moments"] + ["friend"] * pending["friend"]:
        r = credit_share("phone", 1, kind)
        last = r
        if r.get("credited"):
            got[kind] += 1
            days_got += int(r.get("days_granted") or 0)
        else:
            # 冷却 / 封顶了 —— 这一档和后面还没轮到的全部留到下次。
            break

    # ★ 只有真的计进去的才算「已计入」；被挡下的留到下次。
    if any(got.values()):
        d = _bonus_load(force=True)
        ps = d.setdefault("phone_shares", {})
        cur = _share_norm(ps.get(device_id))
        for k in ("moments", "friend"):
            cur[k] = cur.get(k, 0) + got[k]
        ps[device_id] = cur
        _bonus_save(d)

    left = {k: pending[k] - got[k] for k in ("friend", "moments")}
    out = dict(last)
    out.update({"ok": any(got.values()), "credited": sum(got.values()),
                # ★ `days_granted` 必须用**累加值**，不能跟着 `last` 走 ——
                #   `last` 是循环里最后那一次的结果，而那一次多半是「冷却被挡」
                #   的那一下（granted=0）。照抄它会让接口回「口径 +30 天」
                #   却报 `days_granted: 0`，界面上就变成「明明加了天数却显示 +0 天」。
                "days_granted": days_got,
                "pending": sum(left.values()),
                "pending_friend": left["friend"], "pending_moments": left["moments"],
                "granted": got})
    out["reason"] = out.get("last_reason") or ("ok" if any(got.values()) else "")
    return out


# ── 移动终端绑定（免费版 1 台，历史绑定，可解绑）────────────────────────────

def terminals() -> list:
    return list(_bonus_load().get("terminals") or [])


def max_terminals():
    return None if is_pro() else FREE_MAX_TERMINALS


def can_pair() -> dict:
    """免费版要配对**新**终端前先问一句。Pro / 未达上限一律放行。

    ★ 判据是「已经绑了几台」，不是「现在在线几台」—— 范老板定的「历史绑定」。
      否则用户关掉旧手机就能白嫖多一台，那这条限制就没意义了。
    """
    cap = max_terminals()
    if cap is None:
        return {"ok": True, "max": None, "bound": len(terminals())}
    bound = terminals()
    if len(bound) >= cap:
        names = "、".join(str(t.get("name") or "未命名手机") for t in bound[:3])
        return {"ok": False, "max": cap, "bound": len(bound), "terminals": bound,
                "error": f"免费版只能连 {cap} 台移动终端（已绑：{names}）。"
                         f"要换手机，请先在「设置 → 许可证」里把旧的解绑。"}
    return {"ok": True, "max": cap, "bound": len(bound)}


def bind_terminal(dev_id: str, name: str = "", explicit: bool = False) -> dict:
    """登记一台移动终端。

    `explicit=True`  = 用户**刚刚配对成功**（配对那条路调），一定绑。
    `explicit=False` = 手机自报家门时顺手补绑（announce 那条路调），
                       但用户**主动解绑过**的那台不补 —— 见 `unbound` 的说明。
    """
    dev_id = str(dev_id or "").strip()
    if not dev_id:
        return {"ok": False, "error": "no_id"}
    d = _bonus_load()
    terms = d.get("terminals") or []
    for t in terms:
        if str(t.get("id") or "") == dev_id:
            t["name"] = str(name or t.get("name") or "")[:40]
            t["last_bound_at"] = time.time()
            _bonus_save(d)
            return {"ok": True, "terminals": terms, "reused": True}
    # ★ 解绑过的设备不许在 announce 里偷偷爬回来。
    #   否则「解绑旧手机 → 配新手机」这条路根本走不通：旧手机还在跑、
    #   20 秒一次的心跳里就把它自己又绑上了，新手机的配对立刻被挡。
    #   用户主动解绑 = 我要把这一台请出去，那它就是出去了 ——
    #   除非它**重新配对**（explicit=True），那才说明是真要用它。
    if not explicit and dev_id in set(d.get("unbound") or []):
        return {"ok": False, "error": "unbound", "terminals": terms, "skipped": True}
    if explicit:
        # 重新配对：撤销以前那条「别再自己爬回来」的记录
        d["unbound"] = [x for x in (d.get("unbound") or []) if str(x) != dev_id]
    terms.append({"id": dev_id, "name": str(name or "未命名手机")[:40],
                  "bound_at": time.time()})
    d["terminals"] = terms
    _bonus_save(d)
    return {"ok": True, "terminals": terms}


def unbind_terminal(dev_id: str) -> dict:
    """解绑一台（范老板要的「解绑换机」）。

    ★ 两件事必须一起做：
      ① 从绑定名单里摘掉，腾出名额；
      ② 记进 `unbound` 黑名单 —— 不然它下一次 announce 又把自己绑回来，
         名额永远腾不出来，「换机」就成了个摆设。
    ★ 但**不动**它的分享计数值：万一用户是误点，重新绑回来分享记录还在。
    """
    dev_id = str(dev_id or "").strip()
    if not dev_id:
        return {"ok": False, "error": "no_id", "terminals": terminals()}
    d = _bonus_load()
    before = d.get("terminals") or []
    d["terminals"] = [t for t in before if str(t.get("id") or "") != dev_id]
    un = [str(x) for x in (d.get("unbound") or []) if str(x) != dev_id]
    un.append(dev_id)
    d["unbound"] = un[-20:]          # 只留最近 20 条，别让文件无限长
    _bonus_save(d)
    return {"ok": len(d["terminals"]) != len(before), "terminals": d["terminals"]}


# ── 分享凭证码（手机没连电脑时手抄的那条路）────────────────────────────────
#
# 手机连过电脑，双方就有一枚共享密钥（就是配对令牌，电脑存在 mobile_devices 里、
# 手机存在 Keychain 里）。手机拿它签自己的分享次数：
#
#     ticket = "FXT1-" + 次数 + "-" + HMAC-SHA256(令牌, "fxseek-share:" + 次数)[:12]
#
# 用户在电脑上贴这段 → 电脑拿**每一台已绑定终端**的令牌各试一遍 →
# 对得上就按这个次数补奖励。
#
# ★ 为什么不用 Ed25519 那种非对称签名：私钥在作者手里，手机上没有；
#   给每台手机发一枚专属私钥又要多一套密钥管理。用配对时已经交换过的
#   对称密钥最省事，而且**只在「这台手机确实配过对」的前提下才验得过** ——
#   没配过对的手机算不出任何一个已绑终端的 HMAC。
# ★ 防重放：次数写进签名里，而且电脑记着「这台已经计入到第几次了」，
#   老码重贴只会得到 nothing_new。

_TICKET_PREFIX = "FXT1"
_TICKET_CTX = "fxseek-share:"


def make_ticket(token: str, friend: int, moments: int) -> str:
    """手机侧调用（电脑侧只在自测里用）。

    ★ 范老板 2026-10-13（下午·十一）把奖励分成两档（朋友 15 天 / 朋友圈 30 天）之后，
      凭证码里**只写一个总次数就不够了** —— 同样「分享 3 次」，全是朋友的
      是 45 天、全是朋友圈的是 90 天。所以码里带两个数。
      形状从三段变四段：`FXT1-<朋友次数>-<朋友圈次数>-<签名>`。
    """
    import hashlib
    import hmac
    f, m = max(0, int(friend)), max(0, int(moments))
    sig = hmac.new(str(token).encode(), _ticket_msg(f, m),
                   hashlib.sha256).hexdigest()[:12].upper()
    return "%s-%d-%d-%s" % (_TICKET_PREFIX, f, m, sig)


def _ticket_msg(friend: int, moments: int) -> bytes:
    return (_TICKET_CTX + "%d:%d" % (int(friend), int(moments))).encode()


def _ticket_split(text: str):
    t = (text or "").strip().upper().replace(" ", "")
    parts = t.split("-")
    if len(parts) != 4 or parts[0] != _TICKET_PREFIX:
        return None
    if not parts[1].isdigit() or not parts[2].isdigit() or len(parts[3]) != 12:
        return None
    return int(parts[1]), int(parts[2]), parts[3]


def redeem_ticket(text: str, tokens: dict) -> dict:
    """`tokens` = {device_id: 配对令牌}。验签成功就按次数补奖励。

    ★ 失败时返回的 `error` 是**给人看的原因码**，调用方（app.py 的 license_ticket）
      会把它翻成中文提示。所以这几个值是有语义的，别随手改名：
        format  —— 格式不对（不是 FXT1-朋友-朋友圈-签名 的形状）
        range   —— 次数离谱（负数 / 单个 >10000 / 两个都是 0）
        nokey   —— 本机一台手机都没配对过，拿什么验签
        invalid —— 形状对、但没有任何一台已配对手机的令牌能签出这个签名
    """
    import hashlib
    import hmac
    got = _ticket_split(text)
    if not got:
        return {"ok": False, "error": "format", "credited": 0}
    friend, moments, sig = got
    if friend < 0 or moments < 0 or friend > 10000 or moments > 10000:
        return {"ok": False, "error": "range", "credited": 0}
    if friend + moments <= 0:
        return {"ok": False, "error": "range", "credited": 0}
    if not any(tok for tok in (tokens or {}).values()):
        # 一台都没配过对：这时候「验不过」和「码是假的」都无从谈起，
        # 得告诉用户先连一次手机，否则他会以为是自己抄错了。
        return {"ok": False, "error": "nokey", "credited": 0}
    for dev_id, tok in (tokens or {}).items():
        if not tok:
            continue
        want = hmac.new(str(tok).encode(), _ticket_msg(friend, moments),
                        hashlib.sha256).hexdigest()[:12].upper()
        if hmac.compare_digest(want, sig):
            r = phone_share_sync(dev_id, {"friend": friend, "moments": moments})
            return {**r, "ok": True, "device_id": dev_id,
                    "count": friend + moments,
                    "friend": friend, "moments": moments,
                    "credited": int(r.get("credited") or 0)}
    return {"ok": False, "error": "invalid", "credited": 0}


def is_pro() -> bool:
    """专业版 = 签名码有效，**或者**分享奖励还在有效期内。"""
    info = load()
    if info.get("tier") == TIER_PRO and not info.get("expired"):
        return True
    return bonus_active()


def limits() -> dict:
    """这一档的配额。`None` = 不限。"""
    if is_pro():
        return {"max_sources": None, "max_items": None, "max_terminals": None}
    return {"max_sources": None, "max_items": FREE_MAX_ITEMS,
            "max_terminals": FREE_MAX_TERMINALS}


def activate(code: str) -> dict:
    """粘贴一个码 → 验签 → 通过就落盘。返回给 UI 用的结果。"""
    global _cache
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "empty"}
    got = _verify(code)
    if not got:
        return {"ok": False, "error": "invalid"}
    if got.get("expired"):
        return {"ok": False, "error": "expired", "expires_at": got["expires_at"]}
    try:
        os.makedirs(os.path.dirname(_LICENSE_PATH), exist_ok=True)
        with open(_LICENSE_PATH, "w", encoding="utf-8") as f:
            f.write(code + "\n")
    except OSError as e:
        return {"ok": False, "error": "io", "detail": str(e)}
    _cache = None
    return {"ok": True, "license": load(force=True)}


def deactivate() -> dict:
    """把本机许可证删掉（换机器 / 退货时用）。不联网，不通知任何人。

    ★ 只删签名码，**不碰** bonus.json —— 分享攒的额度和终端绑定是另一回事，
      「换个激活码」不该把用户的手机也解绑了。
    """
    global _cache
    try:
        os.remove(_LICENSE_PATH)
    except OSError:
        pass
    _cache = None
    return {"ok": True, "license": load(force=True)}


# ── 给界面看的一眼状态 ──────────────────────────────────────────────────────

def status(used_sources: int | None = None, used_items: int | None = None,
           used_terminals: int | None = None) -> dict:
    info = load()
    lim = limits()
    bonus_exp = bonus_expires_at()
    sig_exp = info.get("expires_at")
    # 生效到期日 = 签名码和奖励谁晚算谁。签名码是「永久」时直接永久。
    if info.get("tier") == TIER_PRO and sig_exp is None and not info.get("expired"):
        effective = None
    else:
        effective = max([x for x in (sig_exp or 0, bonus_exp or 0)] or [0]) or None
    out = {
        "tier": TIER_PRO if is_pro() else (info.get("tier") or TIER_FREE),
        "serial": info.get("serial"),
        "expires_at": effective,
        "expired": bool(info.get("expired")) and not bonus_active(),
        "note": info.get("source"),          # none / file / invalid
        "limits": lim,
        "used": {"sources": used_sources, "items": used_items,
                 "terminals": used_terminals if used_terminals is not None
                 else len(terminals())},
        "bonus": {
            "active": bonus_active(),
            "expires_at": bonus_exp or None,
            "days_total": int(_bonus_load().get("share_days_total") or 0),
            "max_days": FREE_SHARE_MAX_DAYS,
            # ★ 两档都要报出来：界面得写「分享给朋友 +15 天 / 发朋友圈 +30 天」。
            #   `days_each` 留着是为了不让老界面（只认一个数）算出 0。
            "days_each": FREE_SHARE_DAYS_MOMENTS,
            "days_friend": FREE_SHARE_DAYS_FRIEND,
            "days_moments": FREE_SHARE_DAYS_MOMENTS,
            "per_day": FREE_SHARE_PER_DAY,
            "credited_days": _bonus_load().get("credited_days") or [],
            "today": _today(),
        },
        "terminals": terminals(),
        "from_bonus": bonus_active() and not (info.get("tier") == TIER_PRO
                                              and not info.get("expired")),
    }
    return out
