"""Trim screenshot payloads from API history to stay under request size limits.

The model only needs the last few screenshots to decide its next action; older
ones balloon the request body (Anthropic returned 413 RequestTooLargeError in an
early run) and consume cache. We replace the oldest image blocks with a short
text reference. Full PNGs always remain on disk in the run directory.
"""
from screen import text_block


def compact_messages(messages, keep_last_images: int):
    """Keep the newest `keep_last_images` image payloads; redact the rest."""
    refs = []

    def collect(container, key):
        value = container.get(key)
        if isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict) and item.get("type") == "image":
                    refs.append((value, idx))
                elif isinstance(item, dict) and item.get("type") == "tool_result":
                    collect(item, "content")

    for msg in messages:
        collect(msg, "content")

    redacted = text_block(
        "[older screenshot omitted from API history; full image is saved in the run directory]"
    )
    if keep_last_images <= 0:
        targets = refs
    else:
        targets = refs[:-keep_last_images]
    for container, idx in targets:
        container[idx] = redacted


def mark_rolling_cache(messages):
    """Move the prompt-cache breakpoint to the most recent message.

    Caching works on prefixes, so marking the latest message caches the whole
    tools + system + history prefix: the API serves the matching cached prefix
    and only writes the new turn. We clear any prior marker first so there is
    ever just one rolling breakpoint (Anthropic caps total breakpoints at 4).
    Blocks that are not plain dicts (e.g. SDK objects in assistant turns) are
    skipped — the most recent message is always a user dict-message at call time.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)
    if not messages:
        return
    content = messages[-1].get("content")
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = {"type": "ephemeral"}
