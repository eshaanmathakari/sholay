from context_window import compact_messages, mark_rolling_cache


def _image_block(tag):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": tag}}


def test_keeps_last_k_images():
    messages = [
        {"role": "user", "content": [_image_block("a")]},
        {"role": "user", "content": [_image_block("b")]},
        {"role": "user", "content": [_image_block("c")]},
        {"role": "user", "content": [_image_block("d")]},
    ]
    compact_messages(messages, keep_last_images=2)
    types = [msg["content"][0]["type"] for msg in messages]
    assert types == ["text", "text", "image", "image"]
    assert messages[2]["content"][0]["source"]["data"] == "c"
    assert messages[3]["content"][0]["source"]["data"] == "d"


def test_handles_tool_result_nested_images():
    messages = [
        {"role": "assistant", "content": [_image_block("old1")]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tu_1",
            "content": [_image_block("old2"), _image_block("old3")],
        }]},
        {"role": "user", "content": [_image_block("newest")]},
    ]
    compact_messages(messages, keep_last_images=1)
    assert messages[0]["content"][0]["type"] == "text"
    inner = messages[1]["content"][0]["content"]
    assert inner[0]["type"] == "text"
    assert inner[1]["type"] == "text"
    assert messages[2]["content"][0]["type"] == "image"
    assert messages[2]["content"][0]["source"]["data"] == "newest"


def test_keep_zero_redacts_all():
    messages = [{"role": "user", "content": [_image_block("a"), _image_block("b")]}]
    compact_messages(messages, keep_last_images=0)
    for item in messages[0]["content"]:
        assert item["type"] == "text"


def test_rolling_cache_marks_only_last_block():
    messages = [
        {"role": "user", "content": [_image_block("a")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
    ]
    mark_rolling_cache(messages)
    assert "cache_control" not in messages[0]["content"][0]
    assert messages[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_rolling_cache_clears_prior_marker():
    messages = [
        {"role": "user", "content": [dict(_image_block("a"), cache_control={"type": "ephemeral"})]},
        {"role": "user", "content": [_image_block("b")]},
    ]
    mark_rolling_cache(messages)
    assert "cache_control" not in messages[0]["content"][0]      # old breakpoint moved off
    assert messages[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_rolling_cache_skips_non_dict_blocks():
    class SdkBlock:  # mimics an SDK content object on an assistant turn
        type = "text"
    messages = [
        {"role": "assistant", "content": [SdkBlock()]},
        {"role": "user", "content": [_image_block("newest")]},
    ]
    mark_rolling_cache(messages)  # must not raise on the non-dict block
    assert messages[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_rolling_cache_empty_is_safe():
    mark_rolling_cache([])  # no messages yet — must not raise


def test_preserves_tool_use_id_pairing():
    """Tool result IDs remain paired with their tool_use blocks after compaction."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "computer", "input": {"action": "screenshot"}},
        ]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tu_1",
            "content": [_image_block("old")],
        }]},
        {"role": "user", "content": [_image_block("newest")]},
    ]
    compact_messages(messages, keep_last_images=1)
    assert messages[0]["content"][0]["id"] == "tu_1"
    assert messages[1]["content"][0]["tool_use_id"] == "tu_1"
