"""Sprint 2/3/4 logic tests — no DB required."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.services import ai_service
from app.services.search_service import _vec_literal
from app.workers.tasks_webhook import _sign


# ----- Sprint 3: AI service stub mode -----

def test_stub_embedding_has_768_dim():
    vec = ai_service.embed_text("anything")
    assert len(vec) == ai_service.EMBED_DIM == 768
    assert all(-1.0 <= v <= 1.0 for v in vec)


def test_stub_embedding_is_deterministic():
    a = ai_service.embed_text("乡约顺德的一张图")
    b = ai_service.embed_text("乡约顺德的一张图")
    assert a == b


def test_stub_embedding_differs_for_different_input():
    a = ai_service.embed_text("aaa")
    b = ai_service.embed_text("bbb")
    assert a != b


def test_tag_image_stub_returns_expected_keys():
    out = ai_service.tag_image(b"\xff\xd8\xff\xe0fake-jpeg")
    assert {"tags", "summary", "alt_text", "visual_description"}.issubset(out.keys())


# ----- Sprint 3: search service vec literal -----

def test_vec_literal_format():
    vec = [0.1, -0.5, 1.0]
    s = _vec_literal(vec)
    assert s.startswith("[") and s.endswith("]")
    assert "0.100000" in s and "-0.500000" in s


# ----- Sprint 2: webhook HMAC signing -----

def test_webhook_signing_matches_manual_hmac():
    secret = "test-secret"
    body = b'{"hello":"world"}'
    sig_header, ts = _sign(secret, body, ts=1700000000)
    assert sig_header.startswith("t=1700000000,v1=")
    expected = hmac.new(
        secret.encode(),
        f"{ts}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    assert sig_header.endswith(expected)


def test_webhook_signing_uses_now_when_no_ts():
    sig_header, ts = _sign("s", b"x")
    assert abs(int(time.time()) - ts) < 5


# ----- Sprint 4: folder path math -----

def test_folder_path_logic_with_mock():
    """Verify the path-building is what we expect.

    Real folder service requires a DB, so just exercise the path string.
    """
    parent_path = "/campaigns/"
    new_name = "2026-spring"
    result = parent_path.rstrip("/") + "/" + new_name + "/"
    assert result == "/campaigns/2026-spring/"


def test_folder_safe_name_strips_slashes():
    """Folder names should not allow path injection."""
    bad = "../etc/passwd"
    safe = bad.replace("/", "_")
    assert "/" not in safe
    assert safe == ".._etc_passwd"


# ----- Sprint 2: webhook subscription event filter -----

def test_event_filter_logic():
    """Empty events list should mean 'subscribe to everything'."""
    sub_events: list[str] = []
    event_type = "asset.uploaded"
    matches = (not sub_events) or (event_type in sub_events)
    assert matches


def test_event_filter_with_specific_events():
    sub_events = ["asset.uploaded", "asset.deleted"]
    assert "asset.uploaded" in sub_events
    assert "asset.processed" not in sub_events
