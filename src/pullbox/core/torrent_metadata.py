"""Bounded bencode validation and exact torrent metadata hashes."""

from __future__ import annotations

import hashlib


def torrent_info_hashes(content: bytes) -> frozenset[str]:
    """Validate one descriptor and hash its original info bytes without re-encoding."""
    info_start, info_end = _top_level_info_span(content)
    info = content[info_start:info_end]
    return frozenset(
        (
            hashlib.sha1(info, usedforsecurity=False).hexdigest(),
            hashlib.sha256(info).hexdigest(),
        )
    )


def _top_level_info_span(content: bytes) -> tuple[int, int]:
    if not content or content[0] != ord("d"):
        raise ValueError("torrent descriptor must be a dictionary")
    index = 1
    info_span: tuple[int, int] | None = None
    while index < len(content) and content[index] != ord("e"):
        key, index = _parse_bencoded_bytes(content, index)
        value_start = index
        index = _skip_bencoded_value(content, index, depth=1)
        if key == b"info":
            if info_span is not None:
                raise ValueError("torrent descriptor has duplicate info dictionaries")
            info_span = (value_start, index)
    if index >= len(content) or content[index] != ord("e") or index + 1 != len(content):
        raise ValueError("torrent descriptor is truncated or has trailing data")
    if info_span is None or content[info_span[0]] != ord("d"):
        raise ValueError("torrent descriptor has no info dictionary")
    return info_span


def _skip_bencoded_value(content: bytes, index: int, *, depth: int) -> int:
    if depth > 100 or index >= len(content):
        raise ValueError("invalid bencode nesting")
    marker = content[index]
    if 48 <= marker <= 57:
        _, end = _parse_bencoded_bytes(content, index)
        return end
    if marker == ord("i"):
        end = content.find(b"e", index + 1)
        if end < 0:
            raise ValueError("unterminated bencoded integer")
        value = content[index + 1 : end]
        digits = value[1:] if value.startswith(b"-") else value
        if (
            not digits
            or not digits.isdigit()
            or (len(digits) > 1 and digits.startswith(b"0"))
            or value == b"-0"
        ):
            raise ValueError("invalid bencoded integer")
        return end + 1
    if marker not in {ord("l"), ord("d")}:
        raise ValueError("invalid bencoded value")
    cursor = index + 1
    while cursor < len(content) and content[cursor] != ord("e"):
        if marker == ord("d"):
            _, cursor = _parse_bencoded_bytes(content, cursor)
        cursor = _skip_bencoded_value(content, cursor, depth=depth + 1)
    if cursor >= len(content):
        raise ValueError("unterminated bencoded collection")
    return cursor + 1


def _parse_bencoded_bytes(content: bytes, index: int) -> tuple[bytes, int]:
    colon = content.find(b":", index)
    if colon < 0:
        raise ValueError("invalid bencoded byte string")
    raw_length = content[index:colon]
    if (
        not raw_length
        or not raw_length.isdigit()
        or (len(raw_length) > 1 and raw_length.startswith(b"0"))
    ):
        raise ValueError("invalid bencoded byte-string length")
    length = int(raw_length)
    start = colon + 1
    end = start + length
    if end > len(content):
        raise ValueError("truncated bencoded byte string")
    return content[start:end], end
