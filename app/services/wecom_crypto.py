"""WeCom callback signature verification + AES-CBC decrypt.

Implements the spec at:
  https://developer.work.weixin.qq.com/document/path/90968

Used by callback verification (GET ?msg_signature=&timestamp=&nonce=&echostr=)
and incoming message decryption (POST body).

The encryption is:
  - AES-256-CBC, PKCS#7 padding, IV = first 16 bytes of decoded AES key
  - decoded plaintext layout: 16B random + 4B big-endian length + content + corpid
  - signature = sha1(sorted([token, timestamp, nonce, msg_encrypt]).join(""))
"""
from __future__ import annotations

import base64
import hashlib
import struct

try:
    from Crypto.Cipher import AES  # pycryptodome
except ImportError:  # pragma: no cover
    AES = None  # type: ignore


def _b64_aes_key(encoding_aes_key: str) -> bytes:
    """企微 EncodingAESKey is 43 chars · need to add '=' padding then b64decode."""
    return base64.b64decode(encoding_aes_key + "=")


def verify_signature(token: str, timestamp: str, nonce: str, msg_encrypt: str, expected: str) -> bool:
    """SHA1 of sorted [token, timestamp, nonce, msg_encrypt] joined."""
    arr = sorted([token, timestamp, nonce, msg_encrypt])
    s = "".join(arr)
    actual = hashlib.sha1(s.encode("utf-8")).hexdigest()
    return actual == expected


def aes_decrypt(encoding_aes_key: str, msg_encrypt_b64: str) -> tuple[str, str]:
    """Decrypt and return (plaintext_xml_or_string, receive_id_corpid).

    Returns the inner string content (could be plain XML if message; or echostr
    if URL verification).
    """
    if AES is None:
        raise RuntimeError("pycryptodome not installed · pip install pycryptodome")

    aes_key = _b64_aes_key(encoding_aes_key)
    cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
    decrypted = cipher.decrypt(base64.b64decode(msg_encrypt_b64))

    # Strip PKCS#7 padding
    pad = decrypted[-1]
    if isinstance(pad, str):
        pad = ord(pad)
    if 1 <= pad <= 32:
        decrypted = decrypted[:-pad]

    # Layout: [16B random] [4B length BE] [content (length bytes)] [corpid]
    if len(decrypted) < 20:
        raise ValueError("decrypted payload too short")
    content_len = struct.unpack(">I", decrypted[16:20])[0]
    content = decrypted[20 : 20 + content_len].decode("utf-8")
    corpid = decrypted[20 + content_len :].decode("utf-8")
    return content, corpid
