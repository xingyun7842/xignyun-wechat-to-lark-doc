#!/usr/bin/env python3
"""Offline regression tests for identity, duplicate evidence, and failure classes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import wechat_to_lark_doc as single  # noqa: E402
import batch_wechat_to_lark_doc as batch  # noqa: E402
from wechat_reliability import (  # noqa: E402
    failure_metadata,
    merge_alias_evidence,
    normalized_wechat_url,
    load_alias_cache,
    save_alias_cache,
    stable_article_identity,
)


PAIRS = [
    (
        "\u4e92\u8054\u7f51\u5df2\u6b7b\uff0cAgent \u6c38\u751f", "AGENT\u6a58",
        "https://mp.weixin.qq.com/s/cX3bYrI9Sq7sOJj0E6V9IQ",
        "https://mp.weixin.qq.com/s?__biz=MzkwMzY5NzU2Nw==&mid=2247488954&idx=1&sn=751172c4fa327e68d5e5c04b4844277d",
    ),
    (
        "\u4ece\u70e7\u8111\u795e\u4e66 GEB \u5230 Agent \u7684\u81ea\u6211\u610f\u8bc6", "AGENT\u6a58",
        "https://mp.weixin.qq.com/s/8yGCi-onB1xP4NNOZEdpkQ",
        "https://mp.weixin.qq.com/s?__biz=MzkwMzY5NzU2Nw==&mid=2247489606&idx=1&sn=725cb4e9339cad118ea3e0040a1d7f57",
    ),
    (
        "\u674e\u8bde\u7684\u867e\uff0c\u628a\u641e AI \u7684\u90fd\u770b\u6c89\u9ed8\u4e86", "\u5c39John",
        "https://mp.weixin.qq.com/s/sYS7EqeCp6yl1BLUxPkwOA",
        "https://mp.weixin.qq.com/s?__biz=MzA4NzgzMjA4MQ==&mid=2453481629&idx=1&sn=56984be47bcd34a999727ccab9e0b8b4",
    ),
    (
        "\u8ffd\u95ee\u5f20\u96ea\u5cf0", "\u6587\u5144\u8d5e\u4e00\u4e2a",
        "https://mp.weixin.qq.com/s/l_CvnY_k2e05eBLxPg8RVg",
        "https://mp.weixin.qq.com/s?__biz=MzAxNTYwMzcyNw==&mid=2247486150&idx=1&sn=5391ac96348a1f7e4fc4ca4af38c950b",
    ),
    (
        "Anthropic \u548c OpenAI \u540c\u65f6\u5f00\u59cb\u6492\u94b1\u503e\u9500\uff0cAI\u7684\u201c\u5916\u5356\u5927\u6218\u201d\u6765\u4e86", "\u5218\u5c0f\u6392",
        "https://mp.weixin.qq.com/s/l7ZSPkbWhmawOmLTGfe5tw",
        "https://mp.weixin.qq.com/s?__biz=MzI1MTUxNzgxMA==&mid=2247501561&idx=1&sn=8ef07c94a9a0728e49cc145a4e4efae3",
    ),
    (
        "Codex\u4e0d\u6253\u7b97\u8ba9Claude Code\u597d\u8fc7", "\u5468\u4e00\u7b11",
        "https://mp.weixin.qq.com/s/PT_ynFi_EKJC-Iy_uDNb7g",
        "https://mp.weixin.qq.com/s?__biz=MzkyNjU2ODM2NQ==&mid=2247626204&idx=1&sn=bb6b350ae665aa8040f72be8c1ebb2d6",
    ),
]


class ReliabilityTests(unittest.TestCase):
    def test_long_body_metadata_is_bounded_to_a_readable_title(self) -> None:
        body_like_title = "这是一个没有标点的微信短内容标题" * 20
        cleaned = single.clean_title_value(body_like_title)
        self.assertLessEqual(len(cleaned), 61)
        self.assertTrue(cleaned.endswith("…"))

    def test_six_short_aliases_resolve_to_sn_identity(self) -> None:
        aliases: dict[str, str] = {}
        for _title, _account, short, canonical in PAIRS:
            aliases = merge_alias_evidence([short, canonical], aliases)
            self.assertEqual(
                stable_article_identity(short, aliases),
                stable_article_identity(canonical, aliases),
            )
            self.assertTrue(stable_article_identity(canonical, aliases).startswith("wx:sn:"))

    def test_canonical_normalization_drops_tracking_and_fragment(self) -> None:
        canonical = PAIRS[0][3] + "&scene=24&srcid=x&chksm=y&shareinfo=z#rd"
        normalized = normalized_wechat_url(canonical)
        self.assertNotIn("scene=", normalized)
        self.assertNotIn("srcid=", normalized)
        self.assertNotIn("chksm=", normalized)
        self.assertNotIn("#", normalized)
        self.assertIn("sn=751172c4fa327e68d5e5c04b4844277d", normalized)

    def test_alias_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.json"
            aliases = merge_alias_evidence([PAIRS[0][2], PAIRS[0][3]])
            save_alias_cache(path, aliases)
            self.assertEqual(aliases, load_alias_cache(path))

    def test_six_title_candidates_are_blocked_by_read_only_content_evidence(self) -> None:
        for index, (title, account, short, canonical) in enumerate(PAIRS):
            core = "\u9996\u6bb5\u8bc1\u636e" + ("\u8fd9\u662f\u516c\u4f17\u53f7\u6b63\u6587\u6838\u5fc3\u5185\u5bb9" * 260) + "\u672b\u6bb5\u8bc1\u636e"
            incoming_core = core
            if index == 2:
                incoming_core = core[:1800] + ("\u5c0f\u5e45\u6392\u7248\u5dee\u5f02" * 4) + core[1840:]
            article = {
                "url": canonical,
                "title": title,
                "account": account,
                "events": [single.Event("text", "\u65b0\u7248\u4f5c\u8005\u5361", "p"), single.Event("text", incoming_core, "p"), single.Event("text", "\u65b0\u7248UI\u5c3e\u9879", "p")],
                "article_identity": stable_article_identity(canonical),
            }
            content = (
                f"# {title}\n\n"
                f"\u6765\u6e90\uff1a\u5fae\u4fe1\u516c\u4f17\u53f7\u300c{account}\u300d\uff1b\u539f\u6587\u94fe\u63a5\uff1a{short}\n\n"
                f"{core}\n"
            )
            calls: list[list[str]] = []

            def fake_run_lark(args: list[str], **_kwargs):
                calls.append(args)
                return {"ok": True, "data": {"document": {"content": content}}}

            with mock.patch.object(single, "run_lark", side_effect=fake_run_lark):
                duplicate = single.find_existing_article(
                    article,
                    [{"space_id": "space", "name": "fixture"}],
                    {"space": [{"title": title, "obj_type": "docx", "obj_token": f"doc-{index}", "node_token": f"node-{index}"}]},
                    {},
                )
            self.assertEqual("EXACT_CONTENT_MATCH", duplicate["verdict"])
            self.assertGreaterEqual(duplicate["fingerprint"]["coverage"], 0.98)
            self.assertTrue(all(call[:2] == ["docs", "+fetch"] for call in calls))

    def test_failure_classes(self) -> None:
        self.assertEqual("deterministic_parse", failure_metadata("parse", "No article text was found")["failure_class"])
        self.assertEqual("transient_network", failure_metadata("fetch", "connection timed out")["failure_class"])
        self.assertEqual("wechat_verification", failure_metadata("wechat_access", "\u73af\u5883\u5f02\u5e38\uff0c\u5b8c\u6210\u9a8c\u8bc1\u540e\u5373\u53ef\u7ee7\u7eed\u8bbf\u95ee")["failure_class"])
        self.assertEqual("lark_transient", failure_metadata("lark-cli", "RPC timeout")["failure_class"])

    def test_rich_media_class_and_lazy_images_are_parsed_without_js_content_id(self) -> None:
        fixture = """
        <html><head><meta property="og:title" content="富文本测试"></head><body>
        <div class="rich_media_content"><p>第一段正文</p>
        <img data-original="https://mmbiz.qpic.cn/a/640?wx_fmt=png">
        <svg style="background-image:url('https://mmbiz.qpic.cn/b/640?wx_fmt=jpeg')"></svg>
        <p>第二段正文</p></div></body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rich.html"
            path.write_text(fixture, encoding="utf-8")
            article = single.parse_article("https://mp.weixin.qq.com/s/rich", str(path))
        self.assertEqual(2, article["text_count"])
        self.assertEqual(2, article["image_count"])
        self.assertEqual(["第一段正文", "第二段正文"], [e.value for e in article["events"] if e.kind == "text"])

    def test_plain_text_body_is_decoded_from_script(self) -> None:
        fixture = r"""
        <html><head><meta property="og:title" content="纯文本测试"></head><body>
        <p id="js_text_desc"></p><script type="text/javascript">
        var TextContentNoEncode = true;
        var ContentNoEncode = window.a_value_which_never_exists || '\u7b2c\u4e00\u884c%0A\u7b2c\u4e8c\u884c';
        </script></body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain.html"
            path.write_text(fixture, encoding="utf-8")
            article = single.parse_article("https://mp.weixin.qq.com/s/plain", str(path))
        body = " ".join(e.value for e in article["events"] if e.kind == "text")
        self.assertIn("第一行", body)
        self.assertIn("第二行", body)

    def test_picture_page_list_and_fullscreen_text_are_parsed(self) -> None:
        fixture = r"""
        <html><head><meta property="og:title" content="全屏图文测试"></head><body>
        <div id="js_fullscreen_layout_padding"></div><script type="text/javascript">
        window.picture_page_info_list = [{cdn_url:'https://mmbiz.qpic.cn/p1/640'}, {cdn_url:'https://mmbiz.qpic.cn/p2/640'}];
        window.text_page_info = {content_noencode: JsDecode('\u56fe\u6587\u6b63\u6587')};
        </script></body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fullscreen.html"
            path.write_text(fixture, encoding="utf-8")
            article = single.parse_article("https://mp.weixin.qq.com/s/fullscreen", str(path))
        self.assertEqual(2, article["image_count"])
        self.assertIn("图文正文", [e.value for e in article["events"] if e.kind == "text"])

    def test_github_observed_verification_markers_stop_processing(self) -> None:
        self.assertTrue(single.is_wechat_verification_page('<script src="secitptpage/template/verify.js"></script>'))
        self.assertTrue(single.is_wechat_verification_page("register_code = 1"))
        self.assertFalse(single.is_wechat_verification_page("这是一篇讲解验证方法的正常文章"))

    def test_batch_classify_only_command_is_read_only(self) -> None:
        item = {
            "url": "https://mp.weixin.qq.com/s/example",
            "html_file": "C:/fixture/article.html",
            "target_space_id": "space",
        }
        command = batch.child_command(item, dry_run=False, classify_only=True)
        self.assertIn("--classify-only", command)
        self.assertNotIn("--dry-run", command)
        self.assertIn("--html-file", command)


if __name__ == "__main__":
    unittest.main()
