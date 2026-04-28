"""Sprint 1 smoke tests — touch each layer once."""
from __future__ import annotations

import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)
from app.services.asset_service import classify_kind, safe_extension


def test_password_round_trip():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h)
    assert not verify_password("nope", h)


def test_jwt_round_trip():
    token = create_access_token("user-id", extra_claims={"role": "member"})
    decoded = decode_access_token(token)
    assert decoded["sub"] == "user-id"
    assert decoded["role"] == "member"


def test_api_key_generate_and_hash():
    raw, prefix, digest = generate_api_key("test")
    assert raw.startswith("dam_test_")
    assert len(raw.split("_")[-1]) == 64
    assert hash_api_key(raw) == digest
    assert prefix.startswith("dam_test_")


@pytest.mark.parametrize(
    "mime,ext,expected",
    [
        ("image/jpeg", "jpg", "image"),
        ("image/png", "png", "image"),
        ("video/mp4", "mp4", "video"),
        ("audio/mpeg", "mp3", "audio"),
        ("application/pdf", "pdf", "document"),
        ("application/zip", "zip", "archive"),
        ("application/octet-stream", "fbx", "model3d"),
        ("application/octet-stream", "weird", "other"),
    ],
)
def test_classify_kind(mime, ext, expected):
    assert classify_kind(mime, ext) == expected


def test_safe_extension():
    assert safe_extension("photo.JPG", "image/jpeg") == "jpg"
    assert safe_extension("noext", "image/png") == "png"
    assert safe_extension("noext", "application/x-unknown") == "bin"


@pytest.mark.asyncio
async def test_health_endpoints(client):
    r = await client.get("/v1/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_root(client):
    r = await client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "QideDAM"
