## Stable identity and retry safety

- Use `wx:sn:<sn>` as the primary article identity whenever `sn` is available. Normalize canonical URLs to `__biz`, `mid`, `idx`, and `sn`; discard `scene`, `srcid`, `chksm`, share-tracking fields, and fragments.
- Treat `/s/<short>` as `wx:short:<short>` until it is mapped. Supply equivalent URLs with manifest `canonical_url`/`aliases`, or use `--source-alias` and an optional `--alias-cache` JSON file. Never infer a short-to-`sn` mapping from title alone.
- Before creating a document, read title-matched Docx candidates. Reuse a candidate when stable identities match, or when title and account match, the shorter normalized body has matching first/last evidence, and its coverage is at least 98%. Record `EXACT_IDENTITY_MATCH` or `EXACT_CONTENT_MATCH`; do not delete older copies.
- Treat `No article text was found`, metadata-only pages, and unchanged failing SingleFile hashes as `deterministic_parse`; do not repeat the same static attempt. Promote them to browser-rendered SingleFile capture.
- Treat timeouts/connection resets/HTTP 429 or 5xx as `transient_network`; treat retryable Feishu RPC/rate-limit failures as `lark_transient`. Respect `retry_after` and re-run exact-source deduplication before any create retry.
- Treat WeChat `环境异常`、`完成验证后即可继续访问`、captcha pages, and `去验证` as `wechat_verification`. Stop that identity, do not save the error page, and never automate or bypass verification. Resume only after the user completes the official verification.


## Classification safety

- Inspect every accessible Wiki space; do not rely on a hard-coded category list.
- Classify at two levels: Wiki space first, then the best parent node inside it.
- Use existing hierarchy. Do not create a new Wiki space or category unless the user explicitly asks.
- Treat only existing hierarchy/category nodes as parents. A normal leaf article must not become the parent of another article merely because its title is similar.
- If an existing Docx has the same stable WeChat identity or passes the 98% title/account/body evidence check, return `mode: existing-document` and reuse it. Do not create a duplicate unless the user explicitly asks and `--force-create` is passed. When comment screenshots are supplied with an existing article, append and verify them on the existing Docx before returning.
- If the agent cannot confidently choose after reviewing the candidates, leave the Docx in `my_library` and ask the user to choose among 2–3 candidates.
- A stale/inaccessible Wiki returned by space discovery is skipped and must not abort classification.
- Verify the final node's `space_id` and `parent_node_token` exactly. A move command returning `ready=true` is insufficient if those fields do not match.


## Token discipline

- Let the script parse HTML, count blocks, map anchors, and call `lark-cli`.
- Keep only bounded summaries in the model context: title/account/snippet, top five Wiki candidates, top parent candidates, article/comment image counts, final URL, revision ID, and PASS/FAIL.
- If a failure occurs, include the failing step name and stderr excerpt, not the full HTML or document XML.

