"""上网查：搜索 + 抓页面正文（D62）。

为什么要它：模型遇到没见过的文件格式、想找现成的工具或库，得能像人一样
先查再说，而不是凭训练时的印象编一个库名出来。

边界：
- 只允许 http/https；不跟到内网地址（避免被诱导去扫家里的路由器）；
- 超时、限制响应大小，失败降级成一句话，不抛 500；
- 搜索结果不缓存太久，避免同一句话反复请求。
"""
import html as html_lib
import ipaddress
import re
import time
from urllib.parse import quote, urlparse

import requests

TIMEOUT_SECONDS = 15
MAX_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 6000
CACHE_SECONDS = 600

_search_cache = {}
_fetch_cache = {}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class WebError(RuntimeError):
    """上网失败；消息直接给用户看。"""


def _proxies():
    """上网走哪个代理：只认 WEB_PROXY，空着就直连。

    刻意不继承 LLM_PROXY：那是用户自己机器的代理，不该变成小马的依赖。
    地址写成 `127.0.0.1:10808` 也认；设 WEB_PROXY=none 强制直连。
    """
    from config import WEB_PROXY

    value = str(WEB_PROXY or "").strip()
    if not value or value.lower() in ("none", "off", "0", "direct"):
        return None
    if "://" not in value:
        value = "http://" + value
    return {"http": value, "https": value}


def _is_private_host(host):
    """内网/环回地址不抓：提示词注入不该能把家里网络扫一遍。"""
    try:
        info = ipaddress.ip_address(host)
    except ValueError:
        return False
    return info.is_private or info.is_loopback or info.is_link_local


def _strip_html(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def fetch(url, max_chars=MAX_TEXT_CHARS):
    """抓一个页面，返回去掉标签的正文。"""
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("只支持 http/https 地址")
    if _is_private_host(parsed.hostname):
        raise WebError("内网地址不抓")

    cached = _fetch_cache.get(url)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]

    try:
        response = requests.get(
            url, headers=_HEADERS, timeout=TIMEOUT_SECONDS, stream=True,
            allow_redirects=True, proxies=_proxies(),
        )
    except requests.RequestException as exc:
        raise WebError("抓取失败：%s" % str(exc)[:160])

    if response.status_code >= 400:
        raise WebError("对方返回 %d，打不开这个地址" % response.status_code)
    content_type = response.headers.get("Content-Type", "")
    chunks = []
    total = 0
    try:
        for chunk in response.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BYTES:
                break
    finally:
        response.close()
    raw = b"".join(chunks)
    encoding = response.encoding or "utf-8"
    text = raw.decode(encoding, errors="replace")
    if "html" in content_type.lower() or "<html" in text[:500].lower():
        text = _strip_html(text)
    text = text.strip()
    if not text:
        text = "（页面没有可读正文）"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（已截断）"
    _fetch_cache[url] = (time.time(), text)
    return text


def _search_bing(query, n):
    """必应中国站：国内直连可用，结果页结构稳定。"""
    url = "https://cn.bing.com/search?q=" + quote(query) + "&setlang=zh-CN"
    try:
        response = requests.get(
            url, headers=_HEADERS, timeout=TIMEOUT_SECONDS, proxies=_proxies()
        )
    except requests.RequestException as exc:
        raise WebError("搜索失败：%s" % str(exc)[:160])
    if response.status_code >= 400:
        raise WebError("搜索服务返回 %d" % response.status_code)

    results = []
    for block in re.findall(r'<li class="b_algo".*?</li>', response.text, re.S):
        match = re.search(
            r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
        )
        if not match:
            continue
        snippet_match = re.search(
            r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', block, re.S
        ) or re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        results.append({
            "title": _strip_html(match.group(2)),
            "url": match.group(1),
            "snippet": _strip_html(snippet_match.group(1)) if snippet_match else "",
        })
        if len(results) >= n:
            break
    return results


def _search_ddg(query, n):
    """DuckDuckGo 的免密钥 HTML 版；被限流时返回空，由上层换引擎。"""
    url = "https://html.duckduckgo.com/html/?q=" + quote(query)
    try:
        response = requests.get(
            url, headers=_HEADERS, timeout=TIMEOUT_SECONDS, proxies=_proxies()
        )
    except requests.RequestException as exc:
        raise WebError("搜索失败：%s" % str(exc)[:160])
    if response.status_code >= 400:
        raise WebError("搜索服务返回 %d" % response.status_code)

    results = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
        re.S,
    )
    for match in pattern.finditer(response.text):
        href, title_html, rest = match.group(1), match.group(2), match.group(3)
        snippet_match = re.search(
            r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S
        )
        results.append({
            "title": _strip_html(title_html),
            "url": href,
            "snippet": _strip_html(snippet_match.group(1)) if snippet_match else "",
        })
        if len(results) >= n:
            break
    return results


def search(query, n=5):
    """搜网页，返回 [{title, url, snippet}]。

    先必应（国内可直连），拿不到结果再试 DuckDuckGo；两个都空就如实报错，
    不要编一个「网上说……」出来。
    """
    query = str(query or "").strip()
    if not query:
        raise WebError("没有要搜的内容")
    n = max(1, min(int(n or 5), 10))

    cached = _search_cache.get(query)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1][:n]

    results = []
    last_error = None
    for engine in (_search_bing, _search_ddg):
        try:
            results = engine(query, n)
        except WebError as exc:
            last_error = exc
            continue
        if results:
            break
    if not results:
        raise WebError(str(last_error) if last_error else "没搜到结果")
    _search_cache[query] = (time.time(), results)
    return results


def format_results(results):
    """给模型看的搜索结果；只给事实，不下结论。"""
    lines = []
    for index, item in enumerate(results, 1):
        lines.append("%d. %s\n   %s" % (index, item["title"], item["url"]))
        if item.get("snippet"):
            lines.append("   %s" % item["snippet"])
    return "\n".join(lines)
