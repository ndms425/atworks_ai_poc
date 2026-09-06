from datetime import UTC, datetime

import pytest

from atworks_agent.cursor import decode_cursor, encode_cursor


def test_round_trip_preserves_timestamp_and_id():
    ts = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    cursor = encode_cursor(ts, "run-0042")

    decoded_ts, decoded_id = decode_cursor(cursor)

    assert decoded_ts == ts
    assert decoded_id == "run-0042"


def test_cursor_has_no_base64_padding():
    cursor = encode_cursor(datetime(2026, 9, 3, tzinfo=UTC), "run-1")
    assert "=" not in cursor


@pytest.mark.parametrize("garbage", [
    "not-valid-base64!!!",
    "",
    "aGVsbG8",              # valid base64, not JSON
    "eyJmb28iOiAiYmFyIn0",  # valid base64 JSON, missing "t"/"id" keys
    "eyJ0IjogIm5vdC1hLWRhdGUiLCAiaWQiOiAicnVuLTEifQ",  # bad datetime for "t"
])
def test_garbage_raises_value_error(garbage):
    with pytest.raises(ValueError):
        decode_cursor(garbage)
