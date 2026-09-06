"""Keyset pagination cursor: base64url(JSON({"t": iso timestamp, "id": row id})), no padding.
The cursor is server-made and opaque to every caller (model, portal, REST client alike) --
nothing outside this module ever constructs or interprets one. A REST adapter must produce and
consume cursors the same way so a page boundary survives the wire round trip unchanged."""
from __future__ import annotations

import base64
import json
from datetime import datetime


def encode_cursor(executed_at: datetime, run_id: str) -> str:
    payload = json.dumps({"t": executed_at.isoformat(), "id": run_id}, ensure_ascii=False)
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Raises ``ValueError("malformed cursor: ...")`` on ANY malformed input: bad base64, bad
    JSON, missing keys, bad datetime, undecodable bytes.

    The uniformity matters, because the paged routes turn this one exception type into a 400. The
    earlier shape re-raised a bare ``ValueError`` untouched, and most of what goes wrong here IS
    already a ValueError -- ``binascii.Error``, ``UnicodeDecodeError`` and ``JSONDecodeError`` all
    subclass it. So ``?cursor=zzz`` escaped as an opaque "Invalid base64-encoded string", nothing
    recognised it, and the route answered 500 for what is plainly a bad request (final review I5).
    Catching ``Exception`` and re-wrapping gives every failure one type and one message."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        data = json.loads(payload)
        ts = datetime.fromisoformat(data["t"])
        row_id = data["id"]
        if not isinstance(row_id, str):
            raise TypeError(f"cursor id must be a string, got {type(row_id).__name__}")
        return ts, row_id
    except Exception as error:
        raise ValueError(f"malformed cursor: {cursor!r} ({type(error).__name__})") from error
