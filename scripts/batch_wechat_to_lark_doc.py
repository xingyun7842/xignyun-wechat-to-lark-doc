#!/usr/bin/env python3
"""Run the single-article WeChat-to-Lark workflow from a JSON manifest.

The batch runner deliberately treats the existing single-article script as the
only migration implementation.  It adds stable-identity validation,
manifest-level duplicate suppression, bounded process concurrency, isolated
working directories, and failure containment without changing document semantics.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from wechat_reliability import (
    failure_metadata,
    load_alias_cache,
    merge_alias_evidence,
    normalized_wechat_url,
    save_alias_cache,
    stable_article_identity,
)


MAX_WORKERS = 4
SINGLE_SCRIPT = Path(__file__).with_name("wechat_to_lark_doc.py")


class ManifestError(ValueError):
    """Raised when a manifest cannot be executed safely."""


def emit(payload: dict[str, Any]) -> None:
    """Emit one compact UTF-8 JSON object per line."""
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(rendered.encode("utf-8", errors="replace"))
        buffer.flush()
    else:
        sys.stdout.write(rendered)
        sys.stdout.flush()


def decode_manifest(text: str, source: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest from {source} is not valid JSON: {exc}") from exc
    if isinstance(value, dict):
        value = value.get("items")
    if not isinstance(value, list):
        raise ManifestError('manifest must be a JSON array or an object with an "items" array')
    return value


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest does not exist: {path}") from exc
    return decode_manifest(text, str(path))


def require_text(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"item {index}: {key} must be a non-empty string")
    return value.strip()


def optional_text(item: dict[str, Any], key: str, index: int) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"item {index}: {key} must be a non-empty string when supplied")
    return value.strip()


def resolve_file(raw: str, manifest_dir: Path, index: int, key: str) -> str:
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        path = manifest_dir / path
    path = path.resolve()
    if not path.is_file():
        raise ManifestError(f"item {index}: {key} file does not exist: {path}")
    return str(path)


def canonical_url_key(url: str, aliases: dict[str, str] | None = None) -> str:
    """Backward-compatible wrapper around the stable article identity."""
    return stable_article_identity(url, aliases)


def validate_wechat_url(url: str, index: int, key: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "mp.weixin.qq.com":
        raise ManifestError(f"item {index}: {key} must be an mp.weixin.qq.com HTTP(S) article URL")
    return url


def validate_item(raw: Any, index: int, manifest_dir: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError(f"item {index}: each item must be an object")
    url = require_text(raw, "url", index)
    validate_wechat_url(url, index, "url")

    item: dict[str, Any] = {
        "index": index,
        "id": optional_text(raw, "id", index) or str(index),
        "url": url,
        "normalized_url": normalized_wechat_url(url),
    }
    canonical_url = optional_text(raw, "canonical_url", index)
    if canonical_url:
        item["canonical_url"] = validate_wechat_url(canonical_url, index, "canonical_url")
    aliases = raw.get("aliases", raw.get("source_aliases", []))
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list) or not all(
        isinstance(value, str) and value.strip() for value in aliases
    ):
        raise ManifestError(f"item {index}: aliases must be an array of WeChat URLs")
    item["source_aliases"] = [
        validate_wechat_url(value.strip(), index, "aliases") for value in aliases
    ]
    html_file = optional_text(raw, "html_file", index)
    if html_file:
        item["html_file"] = resolve_file(html_file, manifest_dir, index, "html_file")

    comments = raw.get("comment_images", raw.get("comment_image", []))
    if isinstance(comments, str):
        comments = [comments]
    if not isinstance(comments, list) or not all(isinstance(value, str) and value.strip() for value in comments):
        raise ManifestError(f"item {index}: comment_images must be an array of file paths")
    item["comment_images"] = [
        resolve_file(value.strip(), manifest_dir, index, "comment_images") for value in comments
    ]

    text_keys = [
        "parent_position", "target_space_id", "target_space_name",
        "target_parent_token", "target_parent_path",
    ]
    for key in text_keys:
        value = optional_text(raw, key, index)
        if value:
            item[key] = value
    reason = raw.get("classification_reason", raw.get("reason"))
    if reason is not None:
        if not isinstance(reason, str) or not reason.strip():
            raise ManifestError(f"item {index}: reason must be a non-empty string when supplied")
        item["classification_reason"] = reason.strip()

    width = raw.get("width")
    if width is not None:
        if not isinstance(width, int) or isinstance(width, bool) or not 100 <= width <= 2000:
            raise ManifestError(f"item {index}: width must be an integer between 100 and 2000")
        item["width"] = width
    confidence = raw.get("min_classification_confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            raise ManifestError(f"item {index}: min_classification_confidence must be between 0 and 1")
        item["min_classification_confidence"] = float(confidence)
    for key in ["no_wiki_archive", "force_create", "keep_temp"]:
        value = raw.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise ManifestError(f"item {index}: {key} must be true or false")
            item[key] = value
    return item


def validate_manifest(raw_items: list[dict[str, Any]], manifest_dir: Path) -> list[dict[str, Any]]:
    return [validate_item(item, index, manifest_dir) for index, item in enumerate(raw_items, start=1)]


def child_command(item: dict[str, Any], dry_run: bool, classify_only: bool = False) -> list[str]:
    command = [sys.executable, str(SINGLE_SCRIPT), item["url"]]
    scalar_flags = {
        "html_file": "--html-file",
        "parent_position": "--parent-position",
        "width": "--width",
        "target_space_id": "--target-space-id",
        "target_space_name": "--target-space-name",
        "target_parent_token": "--target-parent-token",
        "target_parent_path": "--target-parent-path",
        "classification_reason": "--classification-reason",
        "min_classification_confidence": "--min-classification-confidence",
        "alias_cache": "--alias-cache",
    }
    for key, flag in scalar_flags.items():
        if key in item:
            command.extend([flag, str(item[key])])
    for path in item.get("comment_images", []):
        command.extend(["--comment-image", path])
    for alias in [item.get("canonical_url"), *item.get("source_aliases", [])]:
        if alias:
            command.extend(["--source-alias", alias])
    boolean_flags = {
        "no_wiki_archive": "--no-wiki-archive",
        "force_create": "--force-create",
        "keep_temp": "--keep-temp",
    }
    for key, flag in boolean_flags.items():
        if item.get(key):
            command.append(flag)
    if dry_run:
        command.append("--dry-run")
    if classify_only:
        command.append("--classify-only")
    return command


def parse_child_output(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def compact_child_result(child: dict[str, Any]) -> dict[str, Any]:
    """Keep batch JSONL useful without repeating large URL/candidate arrays."""
    if child.get("ok") is not True:
        return child
    compact = {
        key: child[key]
        for key in [
            "ok", "mode", "title", "account", "doc_id", "url", "wiki_url",
            "article_identity", "normalized_url",
            "text_blocks", "xml_chars", "images_saved", "comment_images_saved",
            "total_images_saved", "revision_id",
        ]
        if key in child
    }
    if isinstance(child.get("images"), list):
        compact["image_count"] = len(child["images"])
    if isinstance(child.get("comment_images"), list):
        compact["comment_image_count"] = len(child["comment_images"])
    # The authenticated user's identity is not needed for migration evidence
    # and must not be persisted in durable batch JSONL results.
    for key in ["verification", "archive_plan", "archive"]:
        if key in child:
            compact[key] = child[key]
    classification = child.get("classification")
    if isinstance(classification, dict):
        compact["classification"] = {
            key: classification[key]
            for key in [
                "verdict", "confidence", "spaces_seen", "selected_space",
                "selected_parent", "duplicate",
            ]
            if key in classification
        }
    return compact


def run_item(
    item: dict[str, Any],
    dry_run: bool,
    classify_only: bool,
    work_root: Path,
) -> dict[str, Any]:
    work_dir = work_root / f"item_{item['index']:05d}"
    work_dir.mkdir(parents=True, exist_ok=False)
    command = child_command(item, dry_run, classify_only)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        cwd=work_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    child = parse_child_output(completed.stdout)
    result: dict[str, Any] = {
        "type": "item",
        "index": item["index"],
        "id": item["id"],
        "url": item["url"],
        "article_identity": item["article_identity"],
        "ok": completed.returncode == 0 and bool(child and child.get("ok") is True),
        "exit_code": completed.returncode,
    }
    if child is not None:
        result["result"] = compact_child_result(child)
        for key in ["failure_class", "retryable", "retry_after", "attempt_stage"]:
            if key in child:
                result[key] = child[key]
    else:
        result["error"] = "single-article worker did not return one JSON object"
        result.update(failure_metadata("worker_output", result["error"]))
        if completed.stdout:
            result["stdout_excerpt"] = completed.stdout[-1000:]
    if completed.stderr:
        result["stderr_excerpt"] = completed.stderr[-1000:]
    return result


def write_result_file(path: Path, results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for result in sorted(results, key=lambda value: value["index"]):
            handle.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help='JSON array or {"items": [...]} manifest; use - for UTF-8 stdin')
    parser.add_argument("--dry-run", action="store_true", help="Run every item with the single script's --dry-run mode")
    parser.add_argument(
        "--classify-only",
        action="store_true",
        help="Parse and query duplicate/classification evidence without creating or moving documents",
    )
    parser.add_argument("--validate-only", action="store_true", help="Validate and deduplicate the manifest without invoking workers")
    parser.add_argument("--workers", type=int, default=2, help=f"Concurrent workers, 1-{MAX_WORKERS} (default: 2)")
    parser.add_argument("--result-file", help="Optional durable JSONL output; relative paths resolve beside the manifest")
    parser.add_argument("--keep-work-dir", action="store_true", help="Keep isolated worker directories for diagnostics")
    parser.add_argument(
        "--alias-cache",
        help="Optional persistent JSON cache mapping /s/<short> aliases to WeChat sn identities",
    )
    args = parser.parse_args()

    if sum(bool(value) for value in [args.dry_run, args.classify_only, args.validate_only]) > 1:
        emit({
            "type": "fatal",
            "ok": False,
            "error": "--dry-run, --classify-only, and --validate-only are mutually exclusive",
        })
        return 2

    from_stdin = args.manifest == "-"
    manifest_path = Path.cwd() / "stdin-manifest.json" if from_stdin else Path(args.manifest).expanduser().resolve()
    if not 1 <= args.workers <= MAX_WORKERS:
        emit({"type": "fatal", "ok": False, "error": f"--workers must be between 1 and {MAX_WORKERS}"})
        return 2
    if not SINGLE_SCRIPT.is_file():
        emit({"type": "fatal", "ok": False, "error": f"single-article script not found: {SINGLE_SCRIPT}"})
        return 2
    try:
        if from_stdin:
            reconfigure = getattr(sys.stdin, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8-sig", errors="strict")
            raw_items = decode_manifest(sys.stdin.read(), "stdin")
        else:
            raw_items = load_manifest(manifest_path)
        items = validate_manifest(raw_items, manifest_path.parent)
        alias_cache_path = (
            str(Path(args.alias_cache).expanduser().resolve()) if args.alias_cache else None
        )
        alias_map = load_alias_cache(alias_cache_path)
        for item in items:
            related = [
                value
                for value in [item.get("canonical_url"), *item.get("source_aliases", [])]
                if value
            ]
            alias_map = merge_alias_evidence([item["url"], *related], alias_map)
        for item in items:
            related = [
                value
                for value in [item.get("canonical_url"), *item.get("source_aliases", [])]
                if value
            ]
            item["article_identity"] = stable_article_identity(
                item["url"], alias_map, related
            )
            item["url_key"] = item["article_identity"]
            if alias_cache_path:
                item["alias_cache"] = alias_cache_path
    except (ManifestError, ValueError, OSError, json.JSONDecodeError) as exc:
        emit({"type": "fatal", "ok": False, "error": str(exc)})
        return 2

    first_index_by_key: dict[str, int] = {}
    runnable: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for item in items:
        duplicate_of = first_index_by_key.get(item["url_key"])
        if duplicate_of is not None:
            result = {
                "type": "item", "index": item["index"], "id": item["id"],
                "url": item["url"], "ok": True, "mode": "skipped-manifest-duplicate",
                "article_identity": item["article_identity"],
                "duplicate_of_index": duplicate_of,
            }
            results.append(result)
            emit(result)
        else:
            first_index_by_key[item["url_key"]] = item["index"]
            runnable.append(item)

    if args.validate_only:
        for item in runnable:
            result = {
                "type": "item", "index": item["index"], "id": item["id"],
                "url": item["url"], "ok": True, "mode": "validated",
                "article_identity": item["article_identity"],
                "normalized_url": item["normalized_url"],
                "html_file": item.get("html_file"),
                "comment_images": len(item.get("comment_images", [])),
            }
            results.append(result)
            emit(result)
    else:
        work_root = Path(tempfile.mkdtemp(prefix="_wechat_lark_batch_", dir=str(Path.cwd())))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(run_item, item, args.dry_run, args.classify_only, work_root): item
                    for item in runnable
                }
                for future in concurrent.futures.as_completed(futures):
                    item = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # contain unexpected worker failures
                        error = f"batch worker failed: {type(exc).__name__}: {exc}"
                        result = {
                            "type": "item", "index": item["index"], "id": item["id"],
                            "url": item["url"], "ok": False,
                            "article_identity": item["article_identity"],
                            "error": error,
                            **failure_metadata("batch_worker", error),
                        }
                    results.append(result)
                    emit(result)
        finally:
            if args.keep_work_dir:
                emit({"type": "work-dir", "ok": True, "path": str(work_root)})
            else:
                shutil.rmtree(work_root, ignore_errors=True)

    succeeded = sum(1 for result in results if result.get("ok") is True)
    failed = len(results) - succeeded
    failures_by_class: dict[str, int] = {}
    for result in results:
        failure_class = result.get("failure_class")
        if failure_class:
            failures_by_class[failure_class] = failures_by_class.get(failure_class, 0) + 1
    summary = {
        "type": "summary", "ok": failed == 0, "mode": (
            "validate-only" if args.validate_only else
            "dry-run" if args.dry_run else
            "classify-only" if args.classify_only else
            "migrate"
        ),
        "total": len(items), "runnable": len(runnable), "succeeded": succeeded,
        "failed": failed, "duplicates_skipped": len(items) - len(runnable),
        "workers": args.workers,
        "failures_by_class": failures_by_class,
    }
    emit(summary)
    if args.result_file:
        result_path = Path(args.result_file).expanduser()
        if not result_path.is_absolute():
            result_path = manifest_path.parent / result_path
        write_result_file(result_path.resolve(), results, summary)
    if alias_cache_path and not args.validate_only and not args.dry_run and not args.classify_only:
        save_alias_cache(alias_cache_path, alias_map)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
