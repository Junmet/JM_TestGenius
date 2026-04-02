"""
从 HTTP(S) URL、Atlassian Confluence、飞书文档拉取正文，供与 Wiki/在线需求对接。

凭证通过环境变量配置（见 .env.example）；未配置时 Confluence/飞书 相关行会报错提示。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

try:
    import trafilatura
except ImportError:
    trafilatura = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment]


def _load_dotenv() -> None:
    from dotenv import load_dotenv

    load_dotenv(override=True)


def _safe_stem(name: str, max_len: int = 80) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", (name or "").strip())
    s = re.sub(r"\s+", "_", s)
    if not s:
        s = "remote"
    return s[:max_len]


def _html_to_text(html: str) -> str:
    if BeautifulSoup is None:
        return re.sub(r"<[^>]+>", " ", html)
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text("\n")


def _extract_main_text(html: str) -> str:
    if trafilatura is not None:
        t = trafilatura.extract(html)
        if t and len(t.strip()) > 20:
            return t.strip()
    return _html_to_text(html).strip()


def fetch_http_page(url: str, *, timeout: float = 60.0) -> tuple[str, str]:
    """任意 HTTP(S) 页面：抽取正文；返回 (用于文件名的 stem, 正文)。"""
    _load_dotenv()
    headers = {
        "User-Agent": "JM_TestGenius/1.0 (+https://github.com/)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        body = r.text
        ct = (r.headers.get("content-type") or "").lower()
    if "text/html" in ct or body.lstrip().startswith("<"):
        text = _extract_main_text(body)
    else:
        text = body.strip()
    parsed = urlparse(url)
    stem = _safe_stem((parsed.path or "/").rstrip("/").split("/")[-1] or parsed.netloc or "page")
    return stem, text


def _confluence_auth_header() -> str:
    email = (os.getenv("CONFLUENCE_EMAIL") or "").strip()
    token = (os.getenv("CONFLUENCE_API_TOKEN") or os.getenv("CONFLUENCE_PAT") or "").strip()
    if not email or not token:
        raise ValueError(
            "Confluence：请在 .env 中配置 CONFLUENCE_EMAIL 与 CONFLUENCE_API_TOKEN（或 CONFLUENCE_PAT）"
        )
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _confluence_base() -> str:
    base = (os.getenv("CONFLUENCE_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise ValueError("Confluence：请配置 CONFLUENCE_BASE_URL，例如 https://your-domain.atlassian.net")
    return base


def _parse_confluence_page_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    m = re.search(r"/pages/(\d+)", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    m2 = re.search(r"(\d{6,})", s)
    if m2:
        return m2.group(1)
    raise ValueError(f"无法从 Confluence 链接中解析页面 ID：{url_or_id!r}")


def fetch_confluence_page(url_or_id: str, *, timeout: float = 60.0) -> tuple[str, str]:
    """
    Atlassian Confluence Cloud REST：GET /wiki/rest/api/content/{id}?expand=body.storage,title
    """
    _load_dotenv()
    page_id = _parse_confluence_page_id(url_or_id)
    base = _confluence_base()
    api = f"{base}/wiki/rest/api/content/{page_id}"
    params = {"expand": "body.storage,title"}
    headers = {
        "Authorization": _confluence_auth_header(),
        "Accept": "application/json",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(api, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
    title = (data.get("title") or f"confluence_{page_id}").strip()
    html = ""
    try:
        html = data["body"]["storage"]["value"] or ""
    except (KeyError, TypeError):
        pass
    text = _html_to_text(html) if html else ""
    if not text.strip():
        text = json.dumps(data, ensure_ascii=False)[:5000]
        logger.warning("Confluence 页面未解析出 storage HTML，已回退为 JSON 片段")
    stem = _safe_stem(title)
    return stem, text.strip()


def _feishu_tenant_token() -> str:
    app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise ValueError("飞书：请配置 FEISHU_APP_ID 与 FEISHU_APP_SECRET")
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": app_id, "app_secret": app_secret}
    with httpx.Client(timeout=30.0) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书 tenant_access_token 失败：{data}")
    tok = data.get("tenant_access_token")
    if not tok:
        raise RuntimeError("飞书：响应中无 tenant_access_token")
    return str(tok)


def _feishu_bearer_token() -> str:
    """
    优先使用 FEISHU_USER_ACCESS_TOKEN（用户 OAuth，适合仅本人可见的文档）；
    否则 tenant_access_token（需在文档「添加文档应用」授权）。
    """
    _load_dotenv()
    u = (os.getenv("FEISHU_USER_ACCESS_TOKEN") or "").strip()
    if u:
        return u
    return _feishu_tenant_token()


def _feishu_error_message(r: httpx.Response, data: dict | None) -> str:
    """解析飞书错误；HTTP 400 时 body 里常有 code/msg。"""
    if data is None:
        return f"HTTP {r.status_code}，{r.text[:600]}"
    code = data.get("code")
    msg = data.get("msg", "")
    extra = ""
    if code == 1770033:
        extra = " 排查：文档纯文本体积超过 raw_content 限制（错误码 1770033），可拆分文档。"
    elif code == 1770032:
        extra = (
            " 排查：当前身份无文档读取权限（错误码 1770032）。"
            "在云文档右上角「…」→「添加文档应用」并授予读取；或配置 FEISHU_USER_ACCESS_TOKEN。"
        )
    elif code == 99991400:
        extra = " 排查：触发接口限频（每秒 5 次），请稍后重试。"
    elif r.status_code in (400, 403) and code not in (0, None):
        extra = (
            " 若与权限相关：请「添加文档应用」或使用用户 token；"
            "官方说明无权限时可能返回 HTTP 400。"
        )
    return f"HTTP {r.status_code}，feishu code={code}，msg={msg}{extra} 完整响应：{data}"


def _parse_feishu_document_token(url_or_token: str) -> str:
    s = url_or_token.strip()
    m = re.search(r"/docx/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{10,}$", s):
        return s
    raise ValueError(f"无法解析飞书文档 document_id：{url_or_token!r}")


def _parse_feishu_wiki_token(url_or_token: str) -> str:
    """飞书知识库节点：URL 中 /wiki/ 后的 token。"""
    s = url_or_token.strip()
    m = re.search(r"/wiki/([a-zA-Z0-9_-]+)", s, re.I)
    if m:
        return m.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{10,}$", s):
        return s
    raise ValueError(f"无法解析飞书 Wiki 节点 token：{url_or_token!r}")


def _fetch_feishu_docx_raw_content(
    doc_id: str,
    *,
    timeout: float,
    title_fallback: str,
) -> tuple[str, str]:
    """飞书云文档 docx：GET /open-apis/docx/v1/documents/{document_id}/raw_content"""
    token = _feishu_bearer_token()
    api = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/raw_content"
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(api, headers=headers)
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(
                f"飞书 raw_content 返回非 JSON：HTTP {r.status_code} {r.text[:500]}"
            ) from None
    if r.status_code >= 400 or data.get("code") != 0:
        raise RuntimeError(f"飞书 raw_content 失败：{_feishu_error_message(r, data)}")
    d = data.get("data") or {}
    text = d.get("content")
    if isinstance(text, str):
        body = text
    else:
        body = json.dumps(d, ensure_ascii=False)
    title = str(d.get("document_title") or title_fallback or f"feishu_{doc_id}")
    stem = _safe_stem(title)
    return stem, body.strip()


def fetch_feishu_docx(url_or_token: str, *, timeout: float = 60.0) -> tuple[str, str]:
    """飞书云文档（/docx/ 链接或 document_id）。"""
    _load_dotenv()
    doc_id = _parse_feishu_document_token(url_or_token)
    return _fetch_feishu_docx_raw_content(
        doc_id, timeout=timeout, title_fallback=f"feishu_{doc_id}"
    )


def fetch_feishu_wiki_node(url_or_token: str, *, timeout: float = 60.0) -> tuple[str, str]:
    """
    飞书知识库 Wiki 节点：先 wiki/v2/spaces/get_node 取 obj_token，再拉 docx raw_content。
    见：https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node
    """
    _load_dotenv()
    wiki_token = _parse_feishu_wiki_token(url_or_token)
    tenant = _feishu_bearer_token()
    api = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
    headers = {"Authorization": f"Bearer {tenant}"}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(api, headers=headers, params={"token": wiki_token})
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(
                f"飞书 Wiki get_node 非 JSON：HTTP {r.status_code} {r.text[:500]}"
            ) from None
    if r.status_code >= 400 or data.get("code") != 0:
        raise RuntimeError(
            f"飞书 Wiki get_node 失败：{_feishu_error_message(r, data)}"
        )
    inner = data.get("data") or {}
    node = inner.get("node") if isinstance(inner.get("node"), dict) else None
    if not isinstance(node, dict):
        node = inner.get("record") if isinstance(inner.get("record"), dict) else inner
    obj_token = node.get("obj_token") or node.get("obj_token_str")
    obj_type = str(node.get("obj_type") or node.get("obj_type_str") or "").lower()
    title_hint = (node.get("title") or node.get("name") or "").strip() or f"wiki_{wiki_token}"
    if not obj_token:
        raise RuntimeError("飞书 Wiki 未返回 obj_token，请检查节点是否存在或应用权限。")
    if obj_type not in ("docx", "doc"):
        raise RuntimeError(
            f"飞书 Wiki 节点类型为 {obj_type!r}，当前仅支持云文档 docx。"
            "若为旧版文档或其它类型，请在飞书中转为新版云文档或使用 /docx/ 链接。"
        )
    return _fetch_feishu_docx_raw_content(
        obj_token, timeout=timeout, title_fallback=title_hint
    )


def resolve_remote_line(line: str, *, timeout: float = 60.0) -> tuple[str, str]:
    """
    解析一行「远程来源」说明：
    - 以 confluence: 开头 → 后接页面 URL 或页面数字 ID
    - 以 feishu: 开头 → 后接文档 URL 或 document_id
    - 否则若 URL 含 atlassian.net/wiki 且已配置 Confluence 环境 → 走 Confluence API
    - 否则若 URL 含 feishu.cn 且已配置飞书应用 → 走飞书 API
    - 其它 http(s) → 通用网页抓取
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        raise ValueError("空行")
    lower = raw.lower()
    if lower.startswith("confluence:"):
        return fetch_confluence_page(raw.split(":", 1)[1].strip(), timeout=timeout)
    if lower.startswith("feishu:"):
        payload = raw.split(":", 1)[1].strip()
        if "/wiki/" in payload.lower():
            return fetch_feishu_wiki_node(payload, timeout=timeout)
        return fetch_feishu_docx(payload, timeout=timeout)

    if not raw.startswith(("http://", "https://")):
        raise ValueError(f"非 URL 行（可用 confluence:/feishu: 前缀）：{raw[:80]}")

    if "atlassian.net" in lower and "/wiki/" in lower:
        _load_dotenv()
        if (os.getenv("CONFLUENCE_EMAIL") or "").strip() and (
            os.getenv("CONFLUENCE_API_TOKEN") or os.getenv("CONFLUENCE_PAT") or ""
        ).strip():
            return fetch_confluence_page(raw, timeout=timeout)

    if "feishu.cn" in lower and "/wiki/" in lower:
        _load_dotenv()
        if (os.getenv("FEISHU_APP_ID") or "").strip():
            return fetch_feishu_wiki_node(raw, timeout=timeout)

    if "feishu.cn" in lower and "/docx/" in lower:
        _load_dotenv()
        if (os.getenv("FEISHU_APP_ID") or "").strip():
            return fetch_feishu_docx(raw, timeout=timeout)

    return fetch_http_page(raw, timeout=timeout)
