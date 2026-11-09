#!/usr/bin/env python3
"""Save a WeChat Official Account article to a Feishu/Lark Docx.

The script is intentionally conservative:
- dry-run performs only network parsing, no Feishu writes;
- real mode creates one new Docx by default;
- images are uploaded through lark-cli and then moved to placeholders.
- wiki discovery is read-only; automatic moves require a confident classification.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from wechat_reliability import (
    compare_article_fingerprints,
    failure_metadata,
    is_wechat_verification_page,
    load_alias_cache,
    merge_alias_evidence,
    normalized_wechat_url,
    save_alias_cache,
    stable_article_identity,
    wechat_url_parts,
)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)


def make_temp_dir(cwd: Path) -> Path:
    """Create a process-safe temporary directory inside cwd for lark-cli paths."""
    return Path(tempfile.mkdtemp(prefix="_wechat_lark_tmp_", dir=str(cwd)))


def fail(step: str, message: str, **extra: Any) -> None:
    payload = {"ok": False, "step": step, "error": message}
    payload.update(failure_metadata(step, message, **extra))
    payload.update(extra)
    write_json(payload)
    raise SystemExit(1)


def write_json(payload: dict[str, Any]) -> None:
    """Emit UTF-8 JSON even when Windows inherited a legacy console code page."""
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(rendered.encode("utf-8"))
        buffer.flush()
        return
    sys.stdout.write(rendered)


def emit(payload: dict[str, Any]) -> None:
    write_json(payload)


def decode_js_escapes(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"\\x([0-9A-Fa-f]{2})", repl, text)
    text = re.sub(r"\\u([0-9A-Fa-f]{4})", repl, text)
    replacements = {
        r"\/": "/",
        r"\'": "'",
        r'\"': '"',
        r"\n": "\n",
        r"\r": "\n",
        r"\t": "\t",
        r"\\": "\\",
    }
    for escaped, decoded in replacements.items():
        text = text.replace(escaped, decoded)
    return text


def clean_text(value: str) -> str:
    value = html.unescape(decode_js_escapes(value))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clean_title_value(value: str) -> str:
    """Keep a usable title when WeChat text posts expose the full body as og:title."""
    value = html.unescape(decode_js_escapes(value)).replace("\\n", "\n")
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    first_line = clean_text(first_line)
    if len(first_line) > 80:
        match = re.match(r"^(.{1,80}?[。！？!?])", first_line)
        if match:
            first_line = match.group(1)
        else:
            first_line = first_line[:60].rstrip(" ，,；;：:-") + "…"
    return first_line


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def normalize_url(url: str) -> str:
    url = html.unescape(decode_js_escapes(url)).strip()
    if url.startswith("//"):
        url = "https:" + url
    if "mmbiz.qpic.cn" in url:
        parsed = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return url


def image_ext(url: str, content_type: str = "") -> str:
    # WeChat can advertise wx_fmt=png while content-negotiating the response to
    # WebP (for example when tp=webp). Prefer the bytes' declared media type so
    # lark-cli can detect dimensions from the actual file format.
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if media_type == "image/png":
        return ".png"
    if media_type == "image/gif":
        return ".gif"
    if media_type == "image/webp":
        return ".webp"

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    fmt = (query.get("wx_fmt") or [""])[0].lower()
    if fmt in {"jpeg", "jpg"}:
        return ".jpg"
    if fmt in {"png", "gif", "webp"}:
        return "." + fmt
    return ".jpg"


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read dimensions from common WeChat image formats without extra deps."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        offset = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while offset + 8 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                break
            segment_size = int.from_bytes(data[offset : offset + 2], "big")
            if marker in sof_markers and offset + 7 <= len(data):
                height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                return width, height
            if segment_size < 2:
                break
            offset += segment_size
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        offset = 12
        while offset + 8 <= len(data):
            chunk = data[offset : offset + 4]
            size = int.from_bytes(data[offset + 4 : offset + 8], "little")
            payload = offset + 8
            if chunk == b"VP8X" and payload + 10 <= len(data):
                width = 1 + int.from_bytes(data[payload + 4 : payload + 7], "little")
                height = 1 + int.from_bytes(data[payload + 7 : payload + 10], "little")
                return width, height
            if chunk == b"VP8 " and payload + 10 <= len(data):
                if data[payload + 3 : payload + 6] == b"\x9d\x01\x2a":
                    width = int.from_bytes(data[payload + 6 : payload + 8], "little") & 0x3FFF
                    height = int.from_bytes(data[payload + 8 : payload + 10], "little") & 0x3FFF
                    return width, height
            if chunk == b"VP8L" and payload + 5 <= len(data) and data[payload] == 0x2F:
                b1, b2, b3, b4 = data[payload + 1 : payload + 5]
                width = 1 + b1 + ((b2 & 0x3F) << 8)
                height = 1 + ((b2 & 0xC0) >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
                return width, height
            offset = payload + size + (size & 1)
    return None


def fetch_url(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if is_wechat_verification_page(body):
            fail(
                "wechat_access",
                "WeChat verification is required; do not automate or bypass it.",
                status=exc.code,
            )
        fail("fetch", f"HTTP {exc.code} while fetching WeChat article.", status=exc.code)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        fail("fetch", f"Network error while fetching WeChat article: {type(exc).__name__}: {exc}")
    enc = "utf-8"
    match = re.search(r"charset=([\w-]+)", ctype, re.I)
    if match:
        enc = match.group(1)
    return raw.decode(enc, errors="replace")


def extract_meta(content: str, prop: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.I | re.S)
        if match:
            return clean_text(match.group(1))
    return ""


def extract_between_tags(content: str, tag_id: str) -> str:
    match = re.search(
        rf'<[^>]+id=["\']{re.escape(tag_id)}["\'][^>]*>(.*?)</[^>]+>',
        content,
        re.I | re.S,
    )
    if not match:
        return ""
    return clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))


class PageMetadataParser(HTMLParser):
    """Extract metadata and visible text from quoted or SingleFile-minified HTML."""

    VOID_TAGS = WeChatContentParser.VOID_TAGS if "WeChatContentParser" in globals() else {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.targets = {"activity-name", "js_text_title", "js_name", "js_wx_follow_nickname"}
        self.values: dict[str, str] = {}
        self.capture_id = ""
        self.capture_depth = 0
        self.buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key and key not in self.meta:
                self.meta[key] = clean_text(attrs.get("content", ""))
        if not self.capture_id and attrs.get("id") in self.targets:
            self.capture_id = attrs["id"]
            self.capture_depth = 1
            self.buffer = []
        elif self.capture_id and tag not in self.VOID_TAGS:
            self.capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.capture_id:
            return
        self.capture_depth -= 1
        if self.capture_depth <= 0:
            value = clean_text(" ".join(self.buffer))
            if value and self.capture_id not in self.values:
                self.values[self.capture_id] = value
            self.capture_id = ""
            self.capture_depth = 0
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.capture_id:
            value = clean_text(data)
            if value:
                self.buffer.append(value)


def extract_page_metadata(content: str) -> PageMetadataParser:
    parser = PageMetadataParser()
    parser.feed(content)
    return parser


def extract_title(content: str, page: PageMetadataParser | None = None) -> str:
    page = page or extract_page_metadata(content)
    for value in [
        page.meta.get("og:title", ""),
        page.meta.get("twitter:title", ""),
        page.values.get("activity-name", ""),
        page.values.get("js_text_title", ""),
        extract_meta(content, "og:title"),
        extract_meta(content, "twitter:title"),
        extract_between_tags(content, "activity-name"),
        extract_between_tags(content, "js_text_title"),
    ]:
        if value:
            return clean_title_value(value)
    match = re.search(r"msg_title\s*=\s*['\"](.*?)['\"]", content, re.S)
    if match:
        return clean_text(match.group(1))
    return "微信公众号文章"


def extract_account(content: str, page: PageMetadataParser | None = None) -> str:
    page = page or extract_page_metadata(content)
    for value in [
        page.meta.get("og:article:author", ""),
        page.meta.get("author", ""),
        page.values.get("js_name", ""),
        page.values.get("js_wx_follow_nickname", ""),
    ]:
        if value:
            return value
    for key in ["og:article:author", "author"]:
        value = extract_meta(content, key)
        if value:
            return value
    value = extract_between_tags(content, "js_name")
    return value or "未知公众号"


def extract_cover_url(content: str) -> str:
    patterns = [
        r"cdn_url_235_1:\s*['\"]([^'\"]+)['\"]",
        r"cdn_url_1_1:\s*['\"]([^'\"]+)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return normalize_url(match.group(1))
    for prop in ["og:image", "twitter:image"]:
        value = extract_meta(content, prop)
        if value and "mmbiz.qpic.cn" in value:
            return normalize_url(value)
    return ""


def extract_slideshow_urls(content: str) -> list[str]:
    """Extract WeChat image-article carousel pages in their authored order.

    WeChat's desktop image-article renderer keeps the original full-size page
    URL on each ``.swiper_item`` as ``data-src``. SingleFile preserves those
    attributes even when only the current slide has been rasterized into CSS.
    """
    urls: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<div\b(?=[^>]*\bclass=["\']?[^>]*\bswiper_item\b)'
        r'(?=[^>]*\bdata-src=["\'](https://mmbiz\.qpic\.cn/[^"\']+))[^>]*>',
        re.I,
    )
    for match in pattern.finditer(content):
        url = normalize_url(match.group(1))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    # Newer image-carousel and fullscreen posts keep authored pages in a
    # JavaScript picture_page_info_list instead of .swiper_item elements.
    picture_scripts = re.findall(
        r"picture_page_info_list\s*=\s*(.*?)(?=\n\s*(?:window\.|var\s+|const\s+|let\s+)|</script>)",
        content,
        re.I | re.S,
    )
    for script in picture_scripts:
        for match in re.finditer(r"\bcdn_url\s*:\s*(['\"])((?:\\.|(?!\1).)*)\1", script, re.I | re.S):
            prefix = script[max(0, match.start() - 100) : match.start()].lower()
            if "watermark_info" in prefix or "share_cover" in prefix:
                continue
            url = normalize_url(match.group(2))
            if "mmbiz.qpic.cn" in url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def extract_script_texts(content: str) -> list[str]:
    """Extract body text embedded in WeChat's non-rich-text page scripts."""
    values: list[str] = []
    patterns = [
        # Plain-text and video-share posts.
        r"(?:Text)?ContentNoEncode\s*=.*?\|\|\s*(['\"])((?:\\.|(?!\1).)*)\1\s*;",
        # Fullscreen posts (appmsg_type 10002).
        r"(?:content_noencode|content)\s*:\s*(?:JsDecode\(\s*)?(['\"])((?:\\.|(?!\1).)*)\1",
        # Image-carousel description.
        r"window\.desc\s*=\s*(['\"])((?:\\.|(?!\1).)*)\1",
    ]
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, content, re.I | re.S):
            value = urllib.parse.unquote(decode_js_escapes(match.group(2)))
            value = clean_text(value.replace("<br>", "\n").replace("<br/>", "\n"))
            value = clean_text(re.sub(r"<[^>]+>", " ", value))
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


@dataclass
class Event:
    kind: str
    value: str
    tag: str = "p"


class WeChatContentParser(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_root = False
        self.depth = 0
        self.skip_depth = 0
        self.current_tag = "p"
        self.buffer: list[str] = []
        self.events: list[Event] = []

    @staticmethod
    def is_content_root(attrs: dict[str, str]) -> bool:
        content_ids = {
            "js_content",
            "js_text_desc",
            "js_common_share_desc_wrap",
            "js_fullscreen_layout_padding",
        }
        content_classes = {"rich_media_content", "original_page", "share_media_swiper_content"}
        classes = set(attrs.get("class", "").split())
        return attrs.get("id") in content_ids or bool(classes & content_classes)

    def append_image(self, source: str, tag: str = "img") -> None:
        source = normalize_url(source)
        if "mmbiz.qpic.cn" in source or source.startswith("data:image/"):
            self.events.append(Event("img", source, tag))

    def flush_text(self) -> None:
        text = clean_text(" ".join(self.buffer))
        self.buffer = []
        if not text:
            return
        if text in {"图片", "预览时标签不可点"}:
            return
        if self.events and self.events[-1].kind == "text" and self.events[-1].value == text:
            return
        self.events.append(Event("text", text, self.current_tag))

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if not self.in_root:
            if self.is_content_root(attrs):
                self.in_root = True
                self.depth = 1
            return

        if tag not in self.VOID_TAGS:
            self.depth += 1
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return

        if tag in {"p", "section", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush_text()
            self.current_tag = tag if tag.startswith("h") else "p"

        if tag == "br":
            self.flush_text()

        if tag == "img":
            self.flush_text()
            src = (
                attrs.get("data-src")
                or attrs.get("data-original")
                or attrs.get("data-url")
                or attrs.get("src")
                or attrs.get("data-backsrc")
                or ""
            )
            self.append_image(src)

        style = attrs.get("style", "")
        if "background-image" in style:
            for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", style, re.I | re.S):
                self.flush_text()
                self.append_image(match.group(2), "svg-background")

    def handle_endtag(self, tag: str) -> None:
        if not self.in_root:
            return
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        if not self.skip_depth and tag in {"p", "section", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush_text()
            self.current_tag = "p"
        self.depth -= 1
        if self.depth <= 0:
            self.flush_text()
            self.in_root = False
            self.depth = 0
            self.skip_depth = 0

    def handle_data(self, data: str) -> None:
        if self.in_root and not self.skip_depth:
            text = clean_text(data)
            if text:
                self.buffer.append(text)


def parse_article(url: str, html_file: str | None = None) -> dict[str, Any]:
    if html_file:
        source = Path(html_file).expanduser().resolve()
        if not source.is_file():
            fail("input", "Rendered HTML file does not exist.", path=str(source))
        raw = source.read_text(encoding="utf-8", errors="replace")
    else:
        raw = fetch_url(url)
    if is_wechat_verification_page(raw):
        fail(
            "wechat_access",
            "WeChat verification is required; do not automate or bypass it.",
        )
    decoded = html.unescape(decode_js_escapes(raw))
    parser = WeChatContentParser()
    parser.feed(decoded)

    page = extract_page_metadata(decoded)
    title = extract_title(decoded, page)
    account = extract_account(decoded, page)

    events: list[Event] = []
    seen_images: set[str] = set()
    slideshow_urls = extract_slideshow_urls(decoded)
    if slideshow_urls:
        for slide_url in slideshow_urls:
            events.append(Event("img", slide_url, "slide"))
            seen_images.add(slide_url)
    for event in parser.events:
        if event.kind == "img":
            if event.value.startswith("data:image/"):
                match = re.match(r"data:([^;,]+)(;base64)?,(.*)\Z", event.value, re.S)
                if match:
                    try:
                        payload = match.group(3)
                        data = (
                            base64.b64decode(payload, validate=False)
                            if match.group(2)
                            else urllib.parse.unquote_to_bytes(payload)
                        )
                        dimensions = image_dimensions(data)
                        if dimensions and max(dimensions) < 120:
                            continue
                    except Exception:
                        pass
            if event.value in seen_images:
                continue
            seen_images.add(event.value)
        if event.kind == "text" and not events and event.value == title:
            continue
        events.append(event)

    for text in extract_script_texts(decoded):
        if text != title and not any(event.kind == "text" and event.value == text for event in events):
            events.append(Event("text", text, "p"))

    cover = extract_cover_url(decoded)
    if cover and cover not in seen_images:
        events.insert(0, Event("img", cover, "cover"))
        seen_images.add(cover)

    text_count = sum(1 for event in events if event.kind == "text")
    image_count = sum(1 for event in events if event.kind == "img")
    special_image_layout = any(
        marker in decoded
        for marker in ("share_media_swiper_content", "js_fullscreen_layout_padding", "picture_page_info_list")
    )
    if text_count == 0 and not (slideshow_urls and special_image_layout):
        fail(
            "parse",
            "No article text was found. A cover image alone is not a complete article; the WeChat page may require browser rendering or login.",
        )

    return {
        "url": url,
        "title": title,
        "account": account,
        "events": events,
        "text_count": text_count,
        "image_count": image_count,
    }


def build_xml(article: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    title = article["title"]
    account = article["account"]
    source_url = article["url"]
    parts = [
        f"<title>{xml_escape(title)}</title>",
        f"<p>来源：微信公众号「{xml_escape(account)}」；原文链接：{xml_escape(source_url)}</p>",
    ]
    images: list[dict[str, Any]] = []
    img_idx = 0
    for event in article["events"]:
        if event.kind == "img":
            img_idx += 1
            placeholder = f"[[WECHAT_IMAGE_{img_idx:03d}]]"
            images.append({"index": img_idx, "url": event.value, "placeholder": placeholder, "tag": event.tag})
            parts.append(f"<p>{placeholder}</p>")
            continue

        text = event.value
        tag = "h2" if event.tag in {"h1", "h2", "h3"} and len(text) <= 80 else "p"
        if tag == "p" and len(text) <= 40 and text.endswith(("：", ":")):
            tag = "h2"
        parts.append(f"<{tag}>{xml_escape(text)}</{tag}>")

    return "\n".join(parts), images


def find_lark_cli() -> str:
    for name in ["lark-cli.cmd", "lark-cli.exe", "lark-cli"]:
        path = shutil.which(name)
        if path:
            return path
    fallback = Path(r"C:\Users\Public\Apps\npm-global\lark-cli.cmd")
    if fallback.exists():
        return str(fallback)
    fail("lark-cli", "lark-cli was not found in PATH.")


def run_lark(
    args: list[str], *, stdin: str | None = None, allow_failure: bool = False
) -> dict[str, Any]:
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    cp = subprocess.run(
        [find_lark_cli(), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if cp.returncode != 0:
        if allow_failure:
            return {"ok": False, "data": {}, "stderr": cp.stderr[-1000:]}
        fail(
            "lark-cli",
            f"Command failed: lark-cli {' '.join(args)}",
            exit_code=cp.returncode,
            stderr=cp.stderr[-2000:],
        )
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        if allow_failure:
            return {"ok": False, "data": {}, "stdout": cp.stdout[-1000:]}
        fail("lark-cli", "Command did not return JSON.", stdout=cp.stdout[-2000:])
    auth_status_success = (
        len(args) >= 2 and args[0] == "auth" and args[1] == "status"
        and data.get("verified") is True
    )
    if data.get("ok") is not True and not auth_status_success:
        if allow_failure:
            return data
        fail("lark-cli", "Command returned ok != true.", response=data)
    return data


def check_auth() -> dict[str, Any]:
    data = run_lark(["auth", "status", "--json", "--verify"])
    identities = data.get("data", {}).get("identities") or data.get("identities", {})
    user = identities.get("user", {})
    status = user.get("tokenStatus") or user.get("status")
    if status and str(status).lower() not in {"valid", "ok"}:
        fail("auth", "Feishu user token is not valid. Ask the user to authorize lark-cli user identity.", user=user)
    return user


def article_corpus(article: dict[str, Any], limit: int = 12000) -> str:
    body = " ".join(event.value for event in article["events"] if event.kind == "text")
    return clean_text(f'{article["title"]} {article["account"]} {body}')[:limit]


def article_body_text(article: dict[str, Any]) -> str:
    return "\n".join(event.value for event in article["events"] if event.kind == "text")


def document_article_evidence(content: str) -> dict[str, Any]:
    """Extract bounded duplicate evidence from a read-only Docx markdown fetch."""
    decoded = html.unescape(content).replace(r"\_", "_")
    title_match = re.search(r"(?m)^#\s+(.+?)\s*$", decoded)
    if not title_match:
        title_match = re.search(r"<title>(.*?)</title>", decoded, re.S | re.I)
    title = clean_text(re.sub(r"<[^>]+>", " ", title_match.group(1))) if title_match else ""
    urls = re.findall(r"https://mp\.weixin\.qq\.com/[^\s)<]+", decoded)
    account_match = re.search(
        r"\u5fae\u4fe1\u516c\u4f17\u53f7[\u300c\u300e]([^\u300d\u300f]+)[\u300d\u300f]",
        decoded,
    )
    account = clean_text(account_match.group(1)) if account_match else ""
    body_lines: list[str] = []
    for raw_line in decoded.splitlines():
        line = raw_line.strip()
        if not line or "mp.weixin.qq.com" in line or line.startswith("!["):
            continue
        if title and clean_text(re.sub(r"^#+\s*", "", line)) == title:
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", line)
        line = re.sub(r"<img\b[^>]*>", " ", line, flags=re.I)
        line = re.sub(r"</?(?:p|h[1-6]|fragment|title)\b[^>]*>", " ", line, flags=re.I)
        line = clean_text(line)
        if line:
            body_lines.append(line)
    return {"title": title, "account": account, "urls": urls, "body": "\n".join(body_lines)}


def normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def char_ngrams(value: str, size: int = 2) -> set[str]:
    value = normalized(value)
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[idx : idx + size] for idx in range(len(value) - size + 1)}


def label_score(label: str, corpus: str) -> float:
    """Cheap, explainable similarity for Chinese/English mixed labels."""
    label_norm = normalized(label)
    corpus_norm = normalized(corpus)
    if not label_norm:
        return 0.0
    exact = 12.0 if len(label_norm) >= 2 and label_norm in corpus_norm else 0.0
    grams = char_ngrams(label_norm)
    corpus_grams = char_ngrams(corpus_norm)
    coverage = len(grams & corpus_grams) / max(len(grams), 1)
    ascii_terms = re.findall(r"[a-z0-9]{2,}", label.lower())
    ascii_hits = sum(1 for term in ascii_terms if term in corpus.lower())
    mixed_terms = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", label.lower())
    mixed_hits = sum(1 for term in mixed_terms if term in corpus.lower())
    return exact + coverage * 8.0 + ascii_hits * 2.0 + mixed_hits * 2.5


def confidence_from_scores(scores: list[float], strong_at: float = 16.0) -> float:
    if not scores or scores[0] <= 0:
        return 0.0
    top = scores[0]
    second = scores[1] if len(scores) > 1 else 0.0
    quality = min(1.0, top / strong_at)
    margin = max(0.0, min(1.0, (top - second) / max(top, 1.0)))
    return round(0.6 * quality + 0.4 * margin, 3)


def list_wiki_spaces() -> list[dict[str, Any]]:
    response = run_lark(
        ["wiki", "+space-list", "--as", "user", "--page-all", "--page-limit", "0", "--format", "json"]
    )
    return response.get("data", {}).get("spaces", [])


def list_wiki_nodes(space_id: str, parent_token: str | None = None) -> list[dict[str, Any]]:
    argv = [
        "wiki", "+node-list", "--as", "user", "--space-id", space_id,
        "--page-all", "--page-limit", "2", "--format", "json",
    ]
    if parent_token:
        argv.extend(["--parent-node-token", parent_token])
    response = run_lark(argv, allow_failure=True)
    return response.get("data", {}).get("nodes", [])


def find_existing_article(
    article: dict[str, Any],
    spaces: list[dict[str, Any]],
    roots_by_space: dict[str, list[dict[str, Any]]],
    alias_map: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    wanted_title = normalized(article["title"])
    wanted_identity = article.get("article_identity") or stable_article_identity(
        article["url"], alias_map
    )
    space_name_by_id = {str(space.get("space_id", "")): space.get("name", "") for space in spaces}
    title_matches: list[dict[str, Any]] = []
    for space_id, nodes in roots_by_space.items():
        for node in nodes:
            if normalized(node.get("title", "")) != wanted_title:
                continue
            candidate = {
                "verdict": "TITLE_MATCH_ONLY",
                "space_id": space_id,
                "space_name": space_name_by_id.get(space_id, ""),
                "node_token": node.get("node_token", ""),
                "obj_token": node.get("obj_token", ""),
                "obj_type": node.get("obj_type", ""),
                "title": node.get("title", ""),
                "parent_node_token": node.get("parent_node_token", ""),
                "wiki_url": f'https://my.feishu.cn/wiki/{node.get("node_token", "")}',
            }
            title_matches.append(candidate)
            if node.get("obj_type") != "docx" or not node.get("obj_token"):
                continue
            response = run_lark([
                "docs", "+fetch", "--as", "user", "--doc", node["obj_token"],
                "--scope", "keyword", "--keyword", "原文链接",
                "--context-before", "0", "--context-after", "0",
                "--detail", "with-ids", "--format", "json",
            ], allow_failure=True)
            content = response.get("data", {}).get("document", {}).get("content", "")
            full_response = run_lark([
                "docs", "+fetch", "--as", "user", "--doc", node["obj_token"],
                "--doc-format", "markdown", "--format", "json",
            ], allow_failure=True)
            full_content = full_response.get("data", {}).get("document", {}).get("content", "")
            if full_content:
                content = full_content
            evidence = document_article_evidence(content)
            candidate_identities = {
                stable_article_identity(url, alias_map) for url in evidence["urls"]
            }
            if wanted_identity in candidate_identities:
                candidate["verdict"] = "EXACT_IDENTITY_MATCH"
                candidate["doc_url"] = f'https://my.feishu.cn/docx/{node["obj_token"]}'
                candidate["article_identity"] = wanted_identity
                candidate["source_urls"] = evidence["urls"]
                return candidate
            fingerprint = compare_article_fingerprints(
                incoming_title=article["title"],
                incoming_account=article["account"],
                incoming_body=article_body_text(article),
                candidate_title=evidence["title"] or node.get("title", ""),
                candidate_account=evidence["account"],
                candidate_body=evidence["body"],
            )
            candidate["fingerprint"] = fingerprint
            if fingerprint.get("exact"):
                candidate["verdict"] = "EXACT_CONTENT_MATCH"
                candidate["doc_url"] = f'https://my.feishu.cn/docx/{node["obj_token"]}'
                candidate["article_identity"] = wanted_identity
                candidate["source_urls"] = evidence["urls"]
                try:
                    discovered = merge_alias_evidence(
                        [article["url"], *evidence["urls"]], alias_map
                    )
                    candidate["alias_updates"] = {
                        key: value
                        for key, value in discovered.items()
                        if (alias_map or {}).get(key) != value
                    }
                except ValueError:
                    candidate["alias_updates"] = {}
                return candidate
    return title_matches[0] if title_matches else None


def classify_wiki(
    article: dict[str, Any], alias_map: dict[str, str] | None = None
) -> dict[str, Any]:
    corpus = article_corpus(article)
    spaces = list_wiki_spaces()
    if not spaces:
        return {"verdict": "NO_SPACES", "confidence": 0.0, "spaces_seen": 0, "candidates": []}

    roots_by_space: dict[str, list[dict[str, Any]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(spaces))) as pool:
        futures = {
            pool.submit(list_wiki_nodes, str(space["space_id"])): str(space["space_id"])
            for space in spaces
        }
        for future, space_id in futures.items():
            try:
                roots_by_space[space_id] = future.result()
            except SystemExit:
                roots_by_space[space_id] = []

    ranked: list[dict[str, Any]] = []
    for space in spaces:
        space_id = str(space.get("space_id", ""))
        roots = roots_by_space.get(space_id, [])
        base = label_score(space.get("name", ""), corpus)
        base += 0.35 * label_score(space.get("description", ""), corpus)
        root_signal = max((label_score(node.get("title", ""), corpus) for node in roots), default=0.0)
        ranked.append({
            "space_id": space_id,
            "space_name": space.get("name", ""),
            "description": space.get("description", ""),
            "score": round(base + 0.3 * root_signal, 3),
            "root_nodes_seen": len(roots),
        })
    ranked.sort(key=lambda item: (-item["score"], item["space_name"]))
    space_confidence = confidence_from_scores([item["score"] for item in ranked])
    selected = ranked[0]
    selected_roots = roots_by_space.get(selected["space_id"], [])

    container_roots = [node for node in selected_roots if node.get("has_child")][:12]
    child_groups: dict[str, list[dict[str, Any]]] = {}
    if container_roots:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(container_roots))) as pool:
            futures = {
                pool.submit(list_wiki_nodes, selected["space_id"], node["node_token"]): node["node_token"]
                for node in container_roots
            }
            for future, parent_token in futures.items():
                try:
                    child_groups[parent_token] = future.result()
                except SystemExit:
                    child_groups[parent_token] = []

    duplicate_nodes_by_space = dict(roots_by_space)
    duplicate_nodes_by_space[selected["space_id"]] = [
        *selected_roots,
        *(node for children in child_groups.values() for node in children),
    ]
    duplicate = find_existing_article(article, spaces, duplicate_nodes_by_space, alias_map)

    path_by_token = {node.get("node_token"): node.get("title", "") for node in selected_roots}
    parent_candidates: list[dict[str, Any]] = []
    for node in selected_roots:
        title = node.get("title", "")
        is_generic_container = normalized(title) in {
            "文章", "文章收藏", "收藏", "剪藏", "资料", "素材"
        }
        if not node.get("has_child") and not is_generic_container:
            continue
        score = label_score(title, corpus) + (1.5 if node.get("has_child") else 0.0)
        if is_generic_container:
            score += 8.0
        parent_candidates.append({
            "node_token": node.get("node_token", ""), "title": title, "path": title,
            "has_child": bool(node.get("has_child")), "score": round(score, 3),
        })
    for parent_token, children in child_groups.items():
        parent_title = path_by_token.get(parent_token, "")
        for node in children:
            title = node.get("title", "")
            is_generic_container = normalized(title) in {
                "文章", "文章收藏", "收藏", "剪藏", "资料", "素材"
            }
            if not node.get("has_child") and not is_generic_container:
                continue
            score = label_score(title, corpus) + (1.0 if node.get("has_child") else 0.0)
            if is_generic_container:
                score += 8.0
            parent_candidates.append({
                "node_token": node.get("node_token", ""), "title": title,
                "path": f"{parent_title} / {title}" if parent_title else title,
                "has_child": bool(node.get("has_child")), "score": round(score, 3),
            })
    parent_candidates.sort(key=lambda item: (-item["score"], item["path"]))
    useful_parents = [item for item in parent_candidates if item["score"] >= 5.0]
    parent_confidence = confidence_from_scores([item["score"] for item in useful_parents], strong_at=14.0)
    selected_parent = useful_parents[0] if useful_parents else None
    combined = round(space_confidence * (0.85 + 0.15 * parent_confidence), 3)
    return {
        "verdict": "RECOMMENDED" if combined >= 0.72 else "NEEDS_REVIEW",
        "confidence": combined,
        "space_confidence": space_confidence,
        "parent_confidence": parent_confidence,
        "spaces_seen": len(spaces),
        "selected_space": selected,
        "selected_parent": selected_parent,
        "duplicate": duplicate,
        "candidates": ranked[:5],
        "parent_candidates": parent_candidates[:8],
        "article_basis": {
            "title": article["title"], "account": article["account"], "snippet": corpus[:600],
        },
    }


def wiki_move_doc(doc_id: str, target_space_id: str, target_parent_token: str | None) -> dict[str, Any]:
    existing = run_lark([
        "wiki", "+node-get", "--as", "user", "--node-token", doc_id,
        "--obj-type", "docx", "--format", "json",
    ], allow_failure=True)
    existing_node = existing.get("data", {}) if existing.get("ok") is True else {}
    if existing_node.get("node_token"):
        argv = [
            "wiki", "+move", "--as", "user",
            "--node-token", existing_node["node_token"],
            "--source-space-id", str(existing_node.get("space_id", "")),
            "--target-space-id", target_space_id,
            "--format", "json",
        ]
    else:
        argv = [
            "wiki", "+move", "--as", "user", "--obj-type", "docx", "--obj-token", doc_id,
            "--target-space-id", target_space_id, "--format", "json",
        ]
    if target_parent_token:
        argv.extend(["--target-parent-token", target_parent_token])
    response = run_lark(argv)
    move_data = response.get("data", {})
    node_token = (
        move_data.get("node_token") or move_data.get("wiki_token")
        or existing_node.get("node_token")
    )
    if not node_token:
        return {"verdict": "PENDING", **move_data}
    verified = run_lark([
        "wiki", "+node-get", "--as", "user", "--node-token", node_token,
        "--space-id", target_space_id, "--format", "json",
    ]).get("data", {})
    return {
        "verdict": "PASS",
        "space_id": verified.get("space_id") or move_data.get("space_id") or target_space_id,
        "node_token": verified.get("node_token") or node_token,
        "parent_node_token": verified.get("parent_node_token") or move_data.get("parent_node_token", ""),
        "obj_token": verified.get("obj_token") or doc_id,
        "title": verified.get("title") or move_data.get("title", ""),
    }


def parse_blocks(content: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r'<(?P<tag>title|h[1-6]|p)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>|<img\b(?P<imgattrs>[^>]*)/?>',
        re.S,
    )
    attr_re = re.compile(r'([\w-]+)="([^"]*)"')
    blocks: list[dict[str, Any]] = []
    for match in pattern.finditer(content):
        if match.group("tag"):
            attrs = dict(attr_re.findall(match.group("attrs") or ""))
            text = clean_text(re.sub(r"<[^>]+>", " ", match.group("body") or ""))
            blocks.append({"type": match.group("tag"), "id": attrs.get("id"), "text": text})
        else:
            attrs = dict(attr_re.findall(match.group("imgattrs") or ""))
            blocks.append(
                {
                    "type": "img",
                    "id": attrs.get("id"),
                    "name": attrs.get("name"),
                    "caption": clean_text(attrs.get("caption", "")),
                }
            )
    return blocks


def fetch_doc(doc: str) -> dict[str, Any]:
    data = run_lark(["docs", "+fetch", "--doc", doc, "--detail", "with-ids", "--format", "json"])
    document = data.get("data", {}).get("document", {})
    document["blocks"] = parse_blocks(document.get("content", ""))
    return document


def create_doc(xml: str, parent_position: str) -> dict[str, Any]:
    return run_lark(
        [
            "docs",
            "+create",
            "--as",
            "user",
            "--content",
            "-",
            "--parent-position",
            parent_position,
            "--format",
            "json",
        ],
        stdin=xml,
    )


def download_images(images: list[dict[str, Any]], article_url: str, temp_dir: Path) -> None:
    temp_dir.mkdir(parents=True, exist_ok=True)
    for image in images:
        if image["url"].startswith("data:image/"):
            match = re.match(r"data:([^;,]+)(;base64)?,(.*)\Z", image["url"], re.S)
            if not match:
                fail("image_download", "Invalid embedded image data URI.", index=image["index"])
            ctype = match.group(1)
            payload = match.group(3)
            try:
                data = base64.b64decode(payload, validate=False) if match.group(2) else urllib.parse.unquote_to_bytes(payload)
            except Exception as exc:
                fail("image_download", "Could not decode embedded image.", index=image["index"], error_detail=str(exc))
        else:
            req = urllib.request.Request(
                image["url"],
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": article_url,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "")
        ext = image_ext(image["url"], ctype)
        name = f"wechat_img_{image['index']:03d}{ext}"
        path = temp_dir / name
        path.write_bytes(data)
        image["file_name"] = name
        image["file_path"] = path
        image["bytes"] = len(data)
        dimensions = image_dimensions(data)
        if dimensions:
            image["source_width"], image["source_height"] = dimensions


def prepare_comment_images(paths: list[str], temp_dir: Path) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    temp_dir.mkdir(parents=True, exist_ok=True)
    for index, raw_path in enumerate(paths, start=1):
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            fail("comment_images", "Comment image does not exist or is not a file.", path=str(source))
        suffix = source.suffix.lower() or ".jpg"
        name = f"comment_img_{index:03d}{suffix}"
        destination = temp_dir / name
        shutil.copy2(source, destination)
        prepared.append({
            "index": index,
            "total": len(paths),
            "source_name": source.name,
            "file_name": name,
            "file_path": destination,
            "bytes": destination.stat().st_size,
        })
    return prepared


def media_insert(
    doc: str,
    image: dict[str, Any],
    width: int,
    cwd: Path,
    caption: str | None = None,
) -> dict[str, Any]:
    rel_path = image["file_path"].relative_to(cwd).as_posix()
    command = [
        "docs", "+media-insert", "--as", "user", "--doc", doc,
        "--file", rel_path, "--type", "image", "--align", "center",
        "--width", str(width),
    ]
    source_width = int(image.get("source_width") or 0)
    source_height = int(image.get("source_height") or 0)
    if source_width > 0 and source_height > 0:
        display_width = width
        display_height = max(1, round(display_width * source_height / source_width))
        if display_height > 10000:
            display_width = max(1, round(display_width * 10000 / display_height))
            display_height = 10000
            command[command.index("--width") + 1] = str(display_width)
        command.extend(["--height", str(display_height)])
    command.extend([
        "--caption", caption or f"原文图片 {image['index']}/{image['total']}",
        "--format", "json",
    ])
    return run_lark(command)


def append_comment_images(
    doc_id: str,
    source_paths: list[Path],
    temp_dir: Path,
    cwd: Path,
    width: int,
) -> dict[str, Any]:
    if not source_paths:
        return {
            "verdict": "SKIPPED",
            "images_added": 0,
            "heading_created": False,
            "block_ids": [],
        }
    before = fetch_doc(doc_id)
    heading_exists = any(
        block.get("type", "").startswith("h") and block.get("text") == "评论区截图"
        for block in before["blocks"]
    )
    prepared = prepare_comment_images([str(path) for path in source_paths], temp_dir)
    if not heading_exists:
        run_lark([
            "docs", "+update", "--as", "user", "--doc", doc_id,
            "--command", "append", "--content", "<h2>评论区截图</h2>",
            "--format", "json",
        ])
    block_ids: list[str] = []
    for image in prepared:
        response = media_insert(
            doc_id,
            image,
            width,
            cwd,
            caption=f"评论区截图 {image['index']}/{image['total']}",
        )
        block_id = response.get("data", {}).get("block_id")
        if block_id:
            block_ids.append(block_id)
    after = fetch_doc(doc_id)
    final_ids = {block.get("id") for block in after["blocks"]}
    heading_count = sum(
        1 for block in after["blocks"]
        if block.get("type", "").startswith("h") and block.get("text") == "评论区截图"
    )
    verdict = (
        "PASS"
        if len(block_ids) == len(prepared)
        and all(block_id in final_ids for block_id in block_ids)
        and heading_count >= 1
        else "FAIL"
    )
    return {
        "verdict": verdict,
        "images_added": len(block_ids),
        "heading_created": not heading_exists,
        "heading_count": heading_count,
        "block_ids": block_ids,
        "revision_id": after.get("revision_id"),
    }


def first_doc_id(create_response: dict[str, Any]) -> str:
    data = create_response.get("data", {})
    document = data.get("document", {})
    for key in ["document_id", "token", "doc_token"]:
        if document.get(key):
            return document[key]
    if data.get("document_id"):
        return data["document_id"]
    fail("create_doc", "Could not find document_id in create response.", response=create_response)


def first_doc_url(create_response: dict[str, Any], doc_id: str) -> str:
    document = create_response.get("data", {}).get("document", {})
    return document.get("url") or document.get("document_url") or f"https://my.feishu.cn/docx/{doc_id}"


def automate(args: argparse.Namespace) -> None:
    article = parse_article(args.url, args.html_file)
    try:
        alias_map = merge_alias_evidence(
            [args.url, *args.source_alias], load_alias_cache(args.alias_cache)
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        fail("input", f"Invalid or conflicting short-alias evidence: {exc}")
    article["article_identity"] = stable_article_identity(
        args.url, alias_map, args.source_alias
    )
    article["normalized_url"] = normalized_wechat_url(args.url)
    xml, images = build_xml(article)
    comment_sources = [Path(path).expanduser().resolve() for path in args.comment_image]
    for path in comment_sources:
        if not path.is_file():
            fail("comment_images", "Comment image does not exist or is not a file.", path=str(path))
    for image in images:
        image["total"] = len(images)

    if args.dry_run:
        emit(
            {
                "ok": True,
                "mode": "dry-run",
                "article_identity": article["article_identity"],
                "normalized_url": article["normalized_url"],
                "title": article["title"],
                "account": article["account"],
                "text_blocks": article["text_count"],
                "images": [
                    {
                        "index": image["index"],
                        "tag": image["tag"],
                        "url": (
                            f"{image['url'].split(',', 1)[0]},... ({len(image['url'])} chars)"
                            if image["url"].startswith("data:image/")
                            else image["url"]
                        ),
                    }
                    for image in images
                ],
                "comment_images": [
                    {"index": index, "name": path.name, "bytes": path.stat().st_size}
                    for index, path in enumerate(comment_sources, start=1)
                ],
                "xml_chars": len(xml),
            }
        )
        return

    user = check_auth()
    classification = classify_wiki(article, alias_map)
    if args.classify_only:
        emit({
            "ok": True,
            "mode": "classify-only",
            "article_identity": article["article_identity"],
            "title": article["title"],
            "account": article["account"],
            "classification": classification,
        })
        return

    duplicate = classification.get("duplicate") or {}
    exact_verdicts = {"EXACT_URL_MATCH", "EXACT_IDENTITY_MATCH", "EXACT_CONTENT_MATCH"}
    if duplicate.get("verdict") in exact_verdicts and args.upgrade_existing:
        doc_id = duplicate.get("obj_token")
        if not doc_id:
            fail("upgrade_existing", "Exact duplicate did not provide a Docx token.")
        cwd = Path.cwd()
        temp_dir = make_temp_dir(cwd)
        try:
            # Rebuild the existing migration in place. This preserves the Wiki node
            # and stable identity while upgrading text/image fidelity without
            # creating a second document.
            run_lark(
                [
                    "docs", "+update", "--as", "user", "--doc", doc_id,
                    "--command", "overwrite", "--content", "-", "--format", "json",
                ],
                stdin=xml,
            )
            document = fetch_doc(doc_id)
            placeholder_ids: dict[str, str] = {}
            for block in document["blocks"]:
                text = block.get("text") or ""
                if text.startswith("[[WECHAT_IMAGE_") and text.endswith("]]" ) and block.get("id"):
                    placeholder_ids[text] = block["id"]
            missing = [image["placeholder"] for image in images if image["placeholder"] not in placeholder_ids]
            if missing:
                fail("placeholder_map", "Some image placeholders were not found in the upgraded document.", missing=missing)

            download_images(images, args.url, temp_dir)
            for image in images:
                media_insert(doc_id, image, args.width, cwd)

            document = fetch_doc(doc_id)
            image_block_by_name: dict[str, str] = {}
            for block in document["blocks"]:
                if block.get("type") == "img" and block.get("name") and block.get("id"):
                    image_block_by_name[block["name"]] = block["id"]
            for image in images:
                block_id = image_block_by_name.get(image["file_name"])
                if not block_id:
                    fail("image_block_map", "Uploaded image block was not found by file name.", image=image)
                run_lark(
                    [
                        "docs", "+update", "--as", "user", "--doc", doc_id,
                        "--command", "block_move_after",
                        "--block-id", placeholder_ids[image["placeholder"]],
                        "--src-block-ids", block_id, "--format", "json",
                    ]
                )
            if images:
                run_lark(
                    [
                        "docs", "+update", "--as", "user", "--doc", doc_id,
                        "--command", "block_delete", "--block-id",
                        ",".join(placeholder_ids[image["placeholder"]] for image in images),
                        "--format", "json",
                    ]
                )

            comment_result = append_comment_images(
                doc_id, comment_sources, temp_dir, cwd, args.width
            )
            final_doc = fetch_doc(doc_id)
            final_blocks = final_doc["blocks"]
            final_img_count = sum(1 for block in final_blocks if block.get("type") == "img")
            placeholders_left = [
                block.get("text") for block in final_blocks
                if (block.get("text") or "").startswith("[[WECHAT_IMAGE_")
            ]
            comment_heading_count = sum(
                1 for block in final_blocks
                if block.get("type", "").startswith("h") and block.get("text") == "评论区截图"
            )
            comment_img_count = sum(
                1 for block in final_blocks
                if block.get("type") == "img"
                and (block.get("name") or "").startswith("comment_img_")
            )
            expected_img_count = len(images) + len(comment_sources)
            comments_ok = (
                not comment_sources
                or (
                    comment_heading_count >= 1
                    and comment_result.get("verdict") == "PASS"
                    and comment_result.get("images_added") == len(comment_sources)
                )
            )
            verdict = (
                "PASS"
                if final_img_count >= expected_img_count and not placeholders_left and comments_ok
                else "FAIL"
            )
            alias_map.update(duplicate.get("alias_updates") or {})
            save_alias_cache(args.alias_cache, alias_map)
            emit({
                "ok": verdict == "PASS",
                "mode": "upgraded-existing-document",
                "article_identity": article["article_identity"],
                "title": article["title"],
                "account": article["account"],
                "doc_id": doc_id,
                "url": duplicate.get("doc_url"),
                "wiki_url": duplicate.get("wiki_url"),
                "images_saved": len(images),
                "comment_images_saved": len(comment_sources),
                "total_images_saved": expected_img_count,
                "revision_id": final_doc.get("revision_id"),
                "verification": {
                    "verdict": verdict,
                    "final_img_count": final_img_count,
                    "placeholders_left": placeholders_left,
                    "comment_heading_count": comment_heading_count,
                    "comment_img_count": comment_img_count,
                },
                "classification": classification,
                "archive": {
                    "verdict": "PASS",
                    "space_id": duplicate.get("space_id"),
                    "node_token": duplicate.get("node_token"),
                    "obj_token": doc_id,
                    "title": duplicate.get("title"),
                    "wiki_url": duplicate.get("wiki_url"),
                    "preserved_existing_node": True,
                },
                "identity": {
                    "userName": user.get("userName"),
                    "openId": user.get("openId"),
                },
            })
        finally:
            if temp_dir.exists() and not args.keep_temp:
                shutil.rmtree(temp_dir, ignore_errors=True)
        return
    if duplicate.get("verdict") in exact_verdicts and not args.force_create:
        alias_map.update(duplicate.get("alias_updates") or {})
        save_alias_cache(args.alias_cache, alias_map)
        comment_result = {
            "verdict": "SKIPPED", "images_added": 0, "heading_created": False, "block_ids": []
        }
        if comment_sources:
            cwd = Path.cwd()
            temp_dir = make_temp_dir(cwd)
            try:
                comment_result = append_comment_images(
                    duplicate["obj_token"], comment_sources, temp_dir, cwd, args.width
                )
            finally:
                if temp_dir.exists() and not args.keep_temp:
                    shutil.rmtree(temp_dir, ignore_errors=True)
        emit({
            "ok": comment_result.get("verdict") in {"PASS", "SKIPPED"},
            "mode": "existing-document",
            "article_identity": article["article_identity"],
            "title": article["title"],
            "account": article["account"],
            "url": duplicate.get("doc_url"),
            "wiki_url": duplicate.get("wiki_url"),
            "classification": classification,
            "verification": {
                "verdict": comment_result.get("verdict") if comment_sources else "PASS",
                "duplicate_prevented": True,
                "match": duplicate.get("verdict"),
                "comments": comment_result,
            },
        })
        return

    selected_space = classification.get("selected_space") or {}
    selected_parent = classification.get("selected_parent") or {}
    target_space_id = args.target_space_id or selected_space.get("space_id")
    target_parent_token = args.target_parent_token
    if target_parent_token is None and not args.target_space_id:
        target_parent_token = selected_parent.get("node_token")
    explicit_target = bool(args.target_space_id)
    archive_plan = {
        "enabled": not args.no_wiki_archive,
        "explicit_target": explicit_target,
        "target_space_id": target_space_id,
        "target_space_name": (
            args.target_space_name or selected_space.get("space_name", "")
        ),
        "target_parent_token": target_parent_token,
        "target_parent_path": (
            args.target_parent_path or selected_parent.get("path", "")
        ),
        "reason": args.classification_reason or (
            f'空间“{selected_space.get("space_name", "")}”与文章主题匹配'
            + (
                f'；父节点“{selected_parent.get("path", "")}”与作者/主题匹配'
                if selected_parent else "；归档到知识库根目录"
            )
        ),
        "confidence": 1.0 if explicit_target else classification.get("confidence", 0.0),
    }
    cwd = Path.cwd()
    temp_dir = make_temp_dir(cwd)
    try:
        create_response = create_doc(xml, args.parent_position)
        doc_id = first_doc_id(create_response)
        doc_url = first_doc_url(create_response, doc_id)

        document = fetch_doc(doc_id)
        placeholder_ids: dict[str, str] = {}
        for block in document["blocks"]:
            text = block.get("text") or ""
            if text.startswith("[[WECHAT_IMAGE_") and text.endswith("]]") and block.get("id"):
                placeholder_ids[text] = block["id"]
        missing = [image["placeholder"] for image in images if image["placeholder"] not in placeholder_ids]
        if missing:
            fail("placeholder_map", "Some image placeholders were not found in the created document.", missing=missing)

        download_images(images, args.url, temp_dir)
        for image in images:
            media_insert(doc_id, image, args.width, cwd)

        document = fetch_doc(doc_id)
        image_block_by_name: dict[str, str] = {}
        for block in document["blocks"]:
            if block.get("type") == "img" and block.get("name") and block.get("id"):
                image_block_by_name[block["name"]] = block["id"]

        for image in images:
            block_id = image_block_by_name.get(image["file_name"])
            if not block_id:
                fail("image_block_map", "Uploaded image block was not found by file name.", image=image)
            run_lark(
                [
                    "docs",
                    "+update",
                    "--as",
                    "user",
                    "--doc",
                    doc_id,
                    "--command",
                    "block_move_after",
                    "--block-id",
                    placeholder_ids[image["placeholder"]],
                    "--src-block-ids",
                    block_id,
                    "--format",
                    "json",
                ]
            )

        if images:
            run_lark(
                [
                    "docs",
                    "+update",
                    "--as",
                    "user",
                    "--doc",
                    doc_id,
                    "--command",
                    "block_delete",
                    "--block-id",
                    ",".join(placeholder_ids[image["placeholder"]] for image in images),
                    "--format",
                    "json",
                ]
            )

        comment_result = append_comment_images(
            doc_id, comment_sources, temp_dir, cwd, args.width
        )

        final_doc = fetch_doc(doc_id)
        final_blocks = final_doc["blocks"]
        final_img_count = sum(1 for block in final_blocks if block.get("type") == "img")
        placeholders_left = [
            block.get("text")
            for block in final_blocks
            if (block.get("text") or "").startswith("[[WECHAT_IMAGE_")
        ]
        comment_heading_count = sum(
            1 for block in final_blocks
            if block.get("type", "").startswith("h") and block.get("text") == "评论区截图"
        )
        comment_img_count = sum(
            1 for block in final_blocks
            if block.get("type") == "img"
            and (block.get("name") or "").startswith("comment_img_")
        )
        expected_img_count = len(images) + len(comment_sources)
        comments_ok = (
            not comment_sources
            or (
                comment_heading_count >= 1
                and comment_result.get("verdict") == "PASS"
                and comment_result.get("images_added") == len(comment_sources)
            )
        )
        verdict = (
            "PASS"
            if final_img_count >= expected_img_count and not placeholders_left and comments_ok
            else "FAIL"
        )
        if args.no_wiki_archive:
            archive_result = {"verdict": "SKIPPED", "reason": "--no-wiki-archive"}
        elif verdict != "PASS":
            archive_result = {"verdict": "SKIPPED", "reason": "document verification failed"}
        elif not target_space_id:
            archive_result = {"verdict": "NEEDS_REVIEW", "reason": "no accessible wiki target found"}
        elif not explicit_target and archive_plan["confidence"] < args.min_classification_confidence:
            archive_result = {
                "verdict": "NEEDS_REVIEW",
                "reason": "classification confidence below threshold; document remains in my_library",
                "threshold": args.min_classification_confidence,
            }
        else:
            archive_result = wiki_move_doc(doc_id, target_space_id, target_parent_token)
            if archive_result.get("node_token"):
                archive_result["wiki_url"] = f'https://my.feishu.cn/wiki/{archive_result["node_token"]}'
        emit(
            {
                "ok": verdict == "PASS" and archive_result.get("verdict") in {"PASS", "SKIPPED"},
                "article_identity": article["article_identity"],
                "title": article["title"],
                "account": article["account"],
                "doc_id": doc_id,
                "url": doc_url,
                "images_saved": len(images),
                "comment_images_saved": len(comment_sources),
                "total_images_saved": expected_img_count,
                "revision_id": final_doc.get("revision_id"),
                "verification": {
                    "verdict": verdict,
                    "final_img_count": final_img_count,
                    "placeholders_left": placeholders_left,
                    "comment_heading_count": comment_heading_count,
                    "comment_img_count": comment_img_count,
                },
                "classification": classification,
                "archive_plan": archive_plan,
                "archive": archive_result,
                "identity": {
                    "userName": user.get("userName"),
                    "openId": user.get("openId"),
                },
            }
        )
        save_alias_cache(args.alias_cache, alias_map)
    finally:
        if temp_dir.exists() and not args.keep_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="WeChat article URL, usually https://mp.weixin.qq.com/s/...")
    parser.add_argument(
        "--html-file",
        help="Optional SingleFile HTML captured after browser rendering; keeps the original URL for dedup/source metadata",
    )
    parser.add_argument(
        "--source-alias",
        action="append",
        default=[],
        help="Equivalent WeChat short/canonical URL; repeat to bind aliases to one stable identity",
    )
    parser.add_argument(
        "--alias-cache",
        help="Optional persistent JSON cache mapping /s/<short> aliases to WeChat sn identities",
    )
    parser.add_argument("--parent-position", default="my_library", help="Feishu create target, default: my_library")
    parser.add_argument("--width", type=int, default=800, help="Displayed image width in Feishu Docx")
    parser.add_argument(
        "--comment-image",
        action="append",
        default=[],
        help="Optional local comment screenshot; repeat to append multiple images in order",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not call lark-cli or write Feishu docs")
    parser.add_argument(
        "--classify-only",
        action="store_true",
        help="Read accessible Wiki spaces/nodes and recommend a target; create or move nothing",
    )
    parser.add_argument("--target-space-id", help="Override automatic Wiki space classification")
    parser.add_argument("--target-space-name", help="Human-readable target space name for the result")
    parser.add_argument("--target-parent-token", help="Override automatic target parent node")
    parser.add_argument("--target-parent-path", help="Human-readable target parent path for the result")
    parser.add_argument("--classification-reason", help="Record the agent's classification rationale")
    parser.add_argument(
        "--min-classification-confidence",
        type=float,
        default=0.72,
        help="Minimum heuristic confidence for an automatic Wiki move (default: 0.72)",
    )
    parser.add_argument(
        "--no-wiki-archive",
        action="store_true",
        help="Create the document but leave it in my_library",
    )
    parser.add_argument(
        "--force-create",
        action="store_true",
        help="Create a new document even when the stable identity or body fingerprint already exists",
    )
    parser.add_argument(
        "--upgrade-existing",
        action="store_true",
        help="Overwrite an exact-match existing Docx in place and restore source images without creating a duplicate",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep downloaded image temp directory")
    args = parser.parse_args()

    if "mp.weixin.qq.com" not in args.url:
        fail("input", "URL is not an mp.weixin.qq.com article link.", url=args.url)
    automate(args)


if __name__ == "__main__":
    main()
