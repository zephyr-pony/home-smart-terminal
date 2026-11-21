"""Web Push 发送适配器（外部渠道边界）。

pywebpush 是同步阻塞的 requests 调用，调用方要用 asyncio.to_thread 包；
第三方异常一律转成 PushResult，上层只判断 ok / gone，不 import 推送库。
"""
import base64
import json


def _prefer_ipv4():
    """让 requests/urllib3 只走 IPv4。

    本机到 Apple 推送的 IPv6 路由是黑洞：getaddrinfo 先给 IPv6，连接要卡满超时
    才回退 IPv4，表现为推送慢十几秒甚至直接超时。家庭网络基本只有 IPv4，
    关掉 IPv6 解析没有副作用。
    """
    import socket

    import urllib3.util.connection as urllib3_conn

    urllib3_conn.allowed_gai_family = lambda: socket.AF_INET


class PushResult(object):
    def __init__(self, ok, status_code=None, gone=False, error=""):
        self.ok = ok
        self.status_code = status_code
        self.gone = gone            # 订阅已失效（404/410），应当删除
        self.error = error

    def __repr__(self):
        return "PushResult(ok=%s, status=%s, gone=%s)" % (
            self.ok, self.status_code, self.gone,
        )


def _safe_error(exc):
    """第三方异常的 __str__ 也可能炸（缺 message 属性），这里兜一层。"""
    try:
        return str(exc)[:300]
    except Exception:
        return exc.__class__.__name__


def _decode_key(private_key_b64):
    """私钥在 .env 里是 base64(PEM)，补回 padding 再解。"""
    text = "".join(str(private_key_b64 or "").split())
    if not text:
        return None
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class WebPushSender(object):
    def __init__(self, private_key_b64, subject, ttl=43200, timeout=10):
        self._subject = subject or "mailto:noreply@example.com"
        self._ttl = int(ttl)
        self._timeout = float(timeout)
        self._vapid = None
        pem = _decode_key(private_key_b64)
        if pem:
            from py_vapid import Vapid

            self._vapid = Vapid.from_pem(pem)
            _prefer_ipv4()

    @property
    def available(self):
        return self._vapid is not None

    def send(self, subscription, payload):
        """推送一条 JSON 负载；失败不抛异常，返回 PushResult。"""
        if not self._vapid:
            return PushResult(False, error="未配置 VAPID 私钥")

        endpoint = (subscription or {}).get("endpoint")
        keys = (subscription or {}).get("keys") or {
            "p256dh": subscription.get("p256dh"),
            "auth": subscription.get("auth"),
        }
        if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
            return PushResult(False, error="订阅信息不完整")

        info = {
            "endpoint": endpoint,
            "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
        }
        claims = {"sub": self._subject}
        data = json.dumps(payload or {}, ensure_ascii=False)

        try:
            from pywebpush import WebPushException, webpush

            response = webpush(
                subscription_info=info,
                data=data,
                vapid_private_key=self._vapid,
                vapid_claims=claims,
                ttl=self._ttl,
                timeout=self._timeout,
            )
            return PushResult(
                True, getattr(response, "status_code", 201)
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return PushResult(
                False, status, gone=status in (404, 410), error=_safe_error(exc)
            )
        except Exception as exc:  # 网络错误、密钥错误等
            return PushResult(False, None, error=_safe_error(exc))
