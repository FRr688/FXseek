#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""生成一对 Ed25519 密钥，用于签发离线许可证。

**只在开发者机器上跑一次。** 私钥永远不要进 git、不要进安装包、不要发给任何人。

    python3 tools/gen_keys.py                  # 写到 ~/.fxseek-keys/
    python3 tools/gen_keys.py --dir /somewhere

跑完会打印 Ed25519 公钥的 base64 —— 把它填进 `license.py` 的 `PUBLIC_KEY_B64`。
私钥文件（`private.key`）请自己备份好：**丢了就再也签不出能用的码了**
（已经发出去的码不受影响，它们验的是公钥）。
"""

from __future__ import annotations

import argparse
import base64
import os
import stat
import sys

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".fxseek-keys")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 FXseek 许可证签名密钥")
    ap.add_argument("--dir", default=DEFAULT_DIR, help=f"存哪儿（默认 {DEFAULT_DIR}）")
    ap.add_argument("--force", action="store_true", help="已存在也覆盖（危险）")
    a = ap.parse_args()

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        print("缺少 cryptography。请用打包用的解释器跑，或 pip install cryptography",
              file=sys.stderr)
        return 2

    priv_path = os.path.join(a.dir, "private.key")
    if os.path.exists(priv_path) and not a.force:
        print(f"已经有一把了：{priv_path}\n（要重做加 --force，但那会让已发出的码全部失效）",
              file=sys.stderr)
        return 1

    os.makedirs(a.dir, exist_ok=True)
    sk = Ed25519PrivateKey.generate()
    raw_priv = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    raw_pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)

    with open(priv_path, "wb") as f:
        f.write(raw_priv)
    # 只有自己能读，少一个手滑泄露的机会
    os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)

    pub_b64 = base64.b64encode(raw_pub).decode("ascii")
    print(f"私钥：{priv_path}   （★ 备份好，别提交，别外传）")
    print(f"公钥（复制进 license.py 的 PUBLIC_KEY_B64）：\n\n    {pub_b64}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
