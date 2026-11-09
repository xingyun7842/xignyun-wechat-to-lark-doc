## Batch workflow

Put collected articles in a UTF-8 JSON manifest. Use an array directly or an object with an `items` array. Relative HTML/comment paths resolve beside the manifest. `url` is required; the other fields are optional:

```json
{
  "items": [
    {
      "id": "queue-001",
      "url": "https://mp.weixin.qq.com/s/...",
      "html_file": "rendered/article-001.html",
      "target_space_id": "<space_id>",
      "target_space_name": "AI技术学习",
      "reason": "文章主题是 Agent 技术",
      "comment_images": ["comments/001-1.jpg"]
    }
  ]
}
```

Validate paths, field types, URL keys, and manifest duplicates without network or Feishu writes:

```powershell
python <SKILL_DIR>\scripts\batch_wechat_to_lark_doc.py queue.json --validate-only
```

Pass `-` as the manifest path to read UTF-8 JSON from standard input; relative file paths then resolve from the current directory.

Parse all entries without Feishu writes, then run real migrations after reviewing the per-item JSONL results:

```powershell
python <SKILL_DIR>\scripts\batch_wechat_to_lark_doc.py queue.json --dry-run --workers 4 --result-file dry-run-results.jsonl
python <SKILL_DIR>\scripts\batch_wechat_to_lark_doc.py queue.json --workers 2 --result-file migration-results.jsonl
```

Use `--workers 2` for normal Feishu writes; raise it only when API stability has been observed. The hard maximum is 4. Every worker invokes the existing single-article script in an isolated working directory. One failure is emitted for that item and does not stop other items. Manifest duplicates are skipped before concurrency starts to avoid duplicate-creation races. Treat the final summary and every item's normal `verification`/`archive` verdicts as evidence; a summary alone does not prove article fidelity.


## Multi-Agent pipeline

Only when delegation is authorized and useful for a large migration, separate responsibilities as follows (no fixed agent count is required):

1. Assign exactly one GUI workstation to the shared browser. Let it alternate serially between WeChat favorites inventory and Tabbit/SingleFile capture. It must pause when the user is typing or when login/verification appears.
2. Feed only validated URLs or SingleFile files to the Feishu workstation. Run at most two independent CLI workers for normal writes; each article identity gets one worker lease.
3. Keep a separate read-only failure/retry workstation. Let it classify failures, maintain retry timing and alias evidence, and verify possible duplicates without controlling the browser or writing Feishu.

Never run two GUI agents concurrently against WeChat or Tabbit. GUI collection is serial even when parsing, classification, duplicate reads, and Feishu work run in parallel.

