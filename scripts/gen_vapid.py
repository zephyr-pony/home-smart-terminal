"""生成 Web Push 用的 VAPID 密钥对。

输出三行，直接粘到 `.env` 末尾即可（私钥是 base64(PEM) 单行，方便放进 .env）。
生成一次就够，重新生成会让已订阅设备全部失效。

用法：
    python scripts/gen_vapid.py
"""
import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def main():
    vapid = Vapid()
    vapid.generate_keys()
    public_raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    print("# ---- Web Push ----")
    print("PUSH_ENABLED=1")
    print("VAPID_PUBLIC_KEY=" + _b64(public_raw))
    print("VAPID_PRIVATE_KEY=" + _b64(vapid.private_pem()))
    print("VAPID_SUBJECT=mailto:noreply@example.com")


if __name__ == "__main__":
    main()
