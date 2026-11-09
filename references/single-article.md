## Operating rule

Prefer the scripted CLI route unless the user explicitly requires the real browser extension UI:

1. Fetch and parse the WeChat article outside the model context.
   If static parsing says the page requires browser rendering/login, open it in the user's browser and save the rendered page with SingleFile, then pass that HTML with `--html-file` while keeping the original WeChat URL as the positional argument. For WeChat image-article/carousel pages, the script extracts every `.swiper_item` full-size `data-src` URL in authored order; do not trust a metadata-only Feishu Clip result as a completed article.
2. Read all accessible Wiki spaces and their category/root nodes with user identity.
3. Detect an existing article by exact source URL before creating anything.
4. Classify a new article into a Wiki space and, when supported by the directory, a parent node.
5. Create a Feishu Docx with text and image placeholders.
6. Download article images locally with WeChat-friendly headers.
7. Upload images with `lark-cli docs +media-insert`.
8. Move uploaded image blocks to their original text anchors with `block_move_after`.
9. Delete placeholders and verify image count/order.
10. If the user supplied comment screenshots, append a “评论区截图” heading and upload them in the supplied order.
11. Move the verified Docx into the selected Wiki node and read the node back.

Do not paste full article HTML, full XML, or full article text into the chat. Report only the title, Feishu link, image count, verification result, and any failures.

For many already-collected links/SingleFile pages, use the batch wrapper instead of invoking the single-article script repeatedly. Keep GUI collection serial, but let the wrapper run independent CLI migrations concurrently.


## Before executing

- Use user identity for personal Feishu resources: `lark-cli ... --as user`.
- If auth is invalid or missing, stop and ask the user to complete Feishu authorization; do not switch to bot identity for user cloud documents.
- If manually editing documents instead of using the bundled script, also follow the `lark-doc` and `lark-shared` skills.
- Wiki discovery and moves must use the `lark-wiki` skill. Never substitute a Drive folder move for a Wiki target.
- A Docx created under `my_library` may already be a personal Wiki node. Resolve it with `wiki +node-get --obj-type docx`; when a node exists, move it with `wiki +move --node-token` rather than docs-to-wiki mode.
- If the user only asks for an explanation, do not create or modify Feishu documents.


## Scripted workflow

Run from any workspace, resolving the script path relative to this skill folder:

```powershell
python <SKILL_DIR>\scripts\wechat_to_lark_doc.py "https://mp.weixin.qq.com/s/..." --dry-run
```

If dry-run shows the expected article title and images, run:

```powershell
python <SKILL_DIR>\scripts\wechat_to_lark_doc.py "https://mp.weixin.qq.com/s/..." --classify-only
```

For a browser-rendered fallback saved by SingleFile:

```powershell
python <SKILL_DIR>\scripts\wechat_to_lark_doc.py "https://mp.weixin.qq.com/s/..." --html-file "<INPUT_DIR>\rendered-article.html" --dry-run
```

The local HTML route supports embedded `data:image/...` resources and WeChat image-article carousels. Carousel pages are recovered from their ordered full-size `mmbiz.qpic.cn` slide URLs even when SingleFile rasterized only the currently visible slide. The original URL is still used for exact deduplication and source metadata.

Review the bounded classification result as the agent. Use the article title, account, snippet, Wiki names/descriptions, and node paths. Prefer an existing semantic container such as “文章/收藏/资料”; otherwise choose a topic or author node. If the recommendation is sound, make it explicit in the real command so the agent's judgment is recorded:

```powershell
python <SKILL_DIR>\scripts\wechat_to_lark_doc.py "https://mp.weixin.qq.com/s/..." --parent-position my_library --target-space-id "<space_id>" --target-space-name "<知识库名称>" --target-parent-token "<node_token>" --target-parent-path "<父节点路径>" --classification-reason "<简短依据>"
```

Omit `--target-parent-token` when the Wiki root is the correct destination. The script can also auto-use its own recommendation when explicit targets are omitted, but it will refuse to move below the confidence threshold. Treat `ok: true`, `verification.verdict: PASS`, and `archive.verdict: PASS` as completion evidence.

When the user supplies comment screenshots, add one `--comment-image` per local file in the exact requested order:

```powershell
python <SKILL_DIR>\scripts\wechat_to_lark_doc.py "https://mp.weixin.qq.com/s/..." --comment-image "C:\path\comment-1.jpg" --comment-image "C:\path\comment-2.jpg"
```

Comment images are optional. Do not create an empty “评论区截图” section when none are supplied. The script copies external paths into its managed temporary directory, uploads them after the article body, verifies their order/count, and removes the temporary copies unless `--keep-temp` is set.


## Expected handoff

Final response should include:

- whether it was created by CLI equivalent workflow or the browser extension;
- Feishu document URL;
- number of images saved;
- whether images were inserted at original positions;
- number and order verification of optional comment screenshots;
- selected Wiki space and parent path, classification reason/confidence, and move verification;
- any known limitation, such as video/audio/interactive WeChat widgets not being preserved.
