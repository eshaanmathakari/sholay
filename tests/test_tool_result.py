"""Regression: an errored tool_result must be text-only.

The Anthropic API rejects a tool_result with ``is_error: true`` whose content
carries a non-text block (e.g. a post-action screenshot):
  "messages.*.content.*.tool_result: all content must be type `text`
   if `is_error` is true"
This crashed the agent on the very first failed tool call. Guarded by
``screen.tool_result``; these tests pin the invariant.
"""
from screen import image_block, text_block, tool_result


def test_error_result_drops_image_blocks():
    content = [text_block("rc=1: file does not exist"), image_block("ZmFrZQ==")]
    tr = tool_result("toolu_1", content, is_error=True)
    assert tr["is_error"] is True
    assert all(b["type"] == "text" for b in tr["content"])


def test_error_result_keeps_the_text():
    content = [text_block("boom"), image_block("ZmFrZQ==")]
    tr = tool_result("toolu_1", content, is_error=True)
    assert tr["content"][0]["text"] == "boom"


def test_error_result_never_empty_when_only_image():
    tr = tool_result("toolu_1", [image_block("ZmFrZQ==")], is_error=True)
    assert tr["content"] and tr["content"][0]["type"] == "text"


def test_success_result_keeps_image():
    content = [text_block("ok"), image_block("ZmFrZQ==")]
    tr = tool_result("toolu_1", content, is_error=False)
    assert "is_error" not in tr
    assert any(b["type"] == "image" for b in tr["content"])


def test_string_content_passthrough():
    tr = tool_result("toolu_1", "rejected by human", is_error=True)
    assert tr["content"] == "rejected by human"
    assert tr["is_error"] is True
