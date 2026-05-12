"""Tests for chat HTTP upload file kind + allowlist."""

from __future__ import annotations

import pytest

from dooers.storage.chat_upload_file_policy import (
    enforce_allowed_chat_file_kind,
    infer_chat_upload_kind,
)


def test_infer_kind_image() -> None:
    assert infer_chat_upload_kind("image/png", "x.png") == "image"


def test_infer_kind_unknown() -> None:
    assert infer_chat_upload_kind("application/zip", "x.zip") is None


def test_enforce_skips_when_no_allowlist() -> None:
    enforce_allowed_chat_file_kind(filename="a.png", mime_type="image/png", allowed_raw=None)


def test_enforce_rejects_unknown_kind_with_allowlist() -> None:
    with pytest.raises(ValueError, match="reconhecido"):
        enforce_allowed_chat_file_kind(
            filename="a.zip",
            mime_type="application/zip",
            allowed_raw=frozenset({"image"}),
        )


def test_enforce_rejects_disallowed_kind() -> None:
    with pytest.raises(ValueError, match="image"):
        enforce_allowed_chat_file_kind(
            filename="a.png",
            mime_type="image/png",
            allowed_raw=frozenset({"document"}),
        )
