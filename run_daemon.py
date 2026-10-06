#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""把 FXseek 主服务起成**真正脱离**的守护进程。

为什么不用 `nohup app.py &`：那样它还是当前 shell 的子进程，
DSH 一重启（或终端一关）整棵进程树被 SIGKILL，服务就没了——
这次就是这么挂的（ECONNREFUSED 127.0.0.1:8231）。

start_new_session=True 会调 setsid()，让它自成一个会话/进程组，
之后再也收不到上游的 SIGHUP/SIGKILL。
"""
import os, sys, subprocess, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PYBIN = os.path.join(HERE, "venv", "cpython-3.11", "bin", "python3.11")
LOG = "/tmp/qbh2/app.log"


def alive(port=8231):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def running():
    r = subprocess.run(["pgrep", "-f", "app.py --port"], capture_output=True, text=True)
    return r.stdout.strip()


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "8231"
    if running() or alive(int(port)):
        print("  已经在跑了，不重复启动")
        return 0
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "a", buffering=1)
    f.write("\n===== %s 重新拉起 =====\n" % time.strftime("%m-%d %H:%M:%S"))
    subprocess.Popen([PYBIN, "-u", "app.py", "--port", port],
                     cwd=HERE, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                     start_new_session=True)          # ← 关键：setsid()
    for i in range(30):
        time.sleep(1)
        if alive(int(port)):
            print("  ✓ 已起来（%d 秒），pid %s" % (i + 1, running()))
            return 0
    print("  ✗ 30 秒还没起来，看", LOG)
    return 1


if __name__ == "__main__":
    sys.exit(main())
