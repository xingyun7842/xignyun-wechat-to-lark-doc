#!/usr/bin/env python3
"""Shared identity, duplicate-evidence, and failure-classification helpers."""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping


SN_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SHORT_PATH_RE = re.compile(r"^/s/([^/?#]+)")
CANONICAL_KEYS = ("__biz", "mid", "idx", "sn")


def _clean_url(url: str) -> str:
    return html.unescape(url).replace(r"\_", "_").strip()


def wechat_url_parts(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(_clean_url(url))
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    sn = (query.get("sn") or [""])[0].lower()
    short_match = SHORT_PATH_RE.match(parsed.path)
    values = {key: (query.get(key) or [""])[0] for key in CANONICAL_KEYS}
    values["sn"] = sn if SN_RE.fullmatch(sn) else ""
    return {
        "scheme": parsed.scheme.lower(),
        "host": (parsed.hostname or "").lower(),
        "short": short_match.group(1) if short_match else "",
        **values,
    }


def normalized_wechat_url(url: str) -> str:
    """Drop tracking parameters and fragments while preserving article identity."""
    cleaned = _clean_url(url)
    parsed = urllib.parse.urlsplit(cleaned)
    parts = wechat_url_parts(cleaned)
    if parts["sn"]:
        query = [(key, parts[key]) for key in CANONICAL_KEYS if parts.get(key)]
        return urllib.parse.urlunsplit(
            ("https", "mp.weixin.qq.com", "/s", urllib.parse.urlencode(query, safe="="), "")
        )
    if parts["short"]:
        return f'https://mp.weixin.qq.com/s/{parts["short"]}'
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), (parsed.netloc or "").lower(), parsed.path, parsed.query, "")
    )


def load_alias_cache(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return {}
    value = json.loads(source.read_text(encoding="utf-8-sig"))
    raw = value.get("aliases", {}) if isinstance(value, dict) else {}
    aliases: dict[str, str] = {}
    for short, sn in raw.items():
        short_value = str(short).strip()
        sn_value = str(sn).strip().lower()
        if short_value and SN_RE.fullmatch(sn_value):
            aliases[short_value] = sn_value
    return aliases


def save_alias_cache(path: str | Path | None, aliases: Mapping[str, str]) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "aliases": dict(sorted((short, sn.lower()) for short, sn in aliases.items())),
    }
    handle, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def merge_alias_evidence(
    urls: Iterable[str], aliases: Mapping[str, str] | None = None
) -> dict[str, str]:
    merged = dict(aliases or {})
    parts = [wechat_url_parts(url) for url in urls if url]
    sns = {part["sn"] for part in parts if part["sn"]}
    if len(sns) > 1:
        raise ValueError(f"conflicting WeChat sn values: {sorted(sns)}")
    if sns:
        sn = next(iter(sns))
        for part in parts:
            if part["short"]:
                previous = merged.get(part["short"])
                if previous and previous != sn:
                    raise ValueError(
                        f'short alias {part["short"]!r} maps to both {previous} and {sn}'
                    )
                merged[part["short"]] = sn
    return merged


def stable_article_identity(
    url: str,
    aliases: Mapping[str, str] | None = None,
    related_urls: Iterable[str] = (),
) -> str:
    related = [url, *related_urls]
    merged = merge_alias_evidence(related, aliases)
    for candidate in related:
        parts = wechat_url_parts(candidate)
        if parts["sn"]:
            return f'wx:sn:{parts["sn"]}'
    parts = wechat_url_parts(url)
    if parts["short"]:
        mapped = merged.get(parts["short"])
        return f"wx:sn:{mapped}" if mapped else f'wx:short:{parts["short"]}'
    digest = hashlib.sha256(normalized_wechat_url(url).encode("utf-8")).hexdigest()[:24]
    return f"wx:url:{digest}"


def normalize_match_text(value: str) -> str:
    value = html.unescape(value).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def body_fingerprint(value: str) -> dict[str, Any]:
    normalized = normalize_match_text(value)
    return {
        "chars": len(normalized),
        "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "head": normalized[:160],
        "tail": normalized[-160:] if normalized else "",
    }


def compare_article_fingerprints(
    *,
    incoming_title: str,
    incoming_account: str,
    incoming_body: str,
    candidate_title: str,
    candidate_account: str,
    candidate_body: str,
    min_coverage: float = 0.98,
) -> dict[str, Any]:
    """Confirm that the shorter article body is embedded in the longer capture."""
    title_match = normalize_match_text(incoming_title) == normalize_match_text(candidate_title)
    account_match = bool(incoming_account and candidate_account) and (
        normalize_match_text(incoming_account) == normalize_match_text(candidate_account)
    )
    incoming = normalize_match_text(incoming_body)
    candidate = normalize_match_text(candidate_body)
    if len(incoming) <= len(candidate):
        shorter, longer, shorter_side = incoming, candidate, "incoming"
    else:
        shorter, longer, shorter_side = candidate, incoming, "candidate"
    if len(shorter) < 80:
        return {
            "exact": False,
            "title_match": title_match,
            "account_match": account_match,
            "coverage": 0.0,
            "reason": "body too short for reliable duplicate evidence",
        }
    matcher = difflib.SequenceMatcher(None, shorter, longer, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    matched = sum(block.size for block in blocks)
    coverage = matched / len(shorter)
    first = blocks[0] if blocks else None
    last = blocks[-1] if blocks else None
    edge_allowance = max(30, int(len(shorter) * 0.02))
    longer_edge_allowance = max(600, int(len(longer) * 0.20))
    first_matches = bool(
        first and first.a <= edge_allowance and first.b <= longer_edge_allowance
    )
    last_matches = bool(
        last
        and len(shorter) - (last.a + last.size) <= edge_allowance
        and len(longer) - (last.b + last.size) <= longer_edge_allowance
    )
    return {
        "exact": bool(
            title_match and account_match and first_matches and last_matches and coverage >= min_coverage
        ),
        "title_match": title_match,
        "account_match": account_match,
        "coverage": round(coverage, 6),
        "threshold": min_coverage,
        "first_matches": first_matches,
        "last_matches": last_matches,
        "shorter_side": shorter_side,
        "shorter_chars": len(shorter),
        "longer_chars": len(longer),
        "largest_exact_block": max((block.size for block in blocks), default=0),
        "shorter_fingerprint": body_fingerprint(shorter),
    }


def is_wechat_verification_page(value: str) -> bool:
    compact = re.sub(r"\s+", "", html.unescape(value))
    markers = (
        "\u73af\u5883\u5f02\u5e38",
        "\u5b8c\u6210\u9a8c\u8bc1\u540e\u5373\u53ef\u7ee7\u7eed\u8bbf\u95ee",
        "js_verify",
        "wappoc_appmsgcaptcha",
        "secitptpage/template/verify.js",
        "register_code",
    )
    return any(marker in compact for marker in markers)


def failure_metadata(step: str, message: str, **extra: Any) -> dict[str, Any]:
    haystack = " ".join([step, message, json.dumps(extra, ensure_ascii=False, default=str)]).lower()
    if is_wechat_verification_page(haystack) or any(
        marker in haystack for marker in ("captcha", "verification required", "wechat verification")
    ):
        return {
            "failure_class": "wechat_verification",
            "retryable": False,
            "retry_after": "after-user-verification",
            "attempt_stage": step,
        }
    if step in {"parse", "input", "html_file"} or any(
        marker in haystack
        for marker in ("no article text", "cover image alone", "does not exist", "source_deleted")
    ):
        return {
            "failure_class": "deterministic_parse",
            "retryable": False,
            "retry_after": None,
            "attempt_stage": step,
        }
    transient_markers = (
        "timed out", "timeout", "temporarily unavailable", "connection reset", "connection aborted",
        "remote end closed", "http 429", "http 500", "http 502", "http 503", "http 504",
    )
    lark_markers = transient_markers + ("rpc", "rate limit", "too many requests")
    if step.startswith("lark") and any(marker in haystack for marker in lark_markers):
        return {
            "failure_class": "lark_transient",
            "retryable": True,
            "retry_after": "respect-retry-after-or-10s",
            "attempt_stage": step,
        }
    if any(marker in haystack for marker in transient_markers):
        return {
            "failure_class": "transient_network",
            "retryable": True,
            "retry_after": "20s",
            "attempt_stage": step,
        }
    return {
        "failure_class": "deterministic_parse" if step == "parse" else "non_retryable",
        "retryable": False,
        "retry_after": None,
        "attempt_stage": step,
    }
