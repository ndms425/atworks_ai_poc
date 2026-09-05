#!/usr/bin/env python3
"""python scripts/smoke_chat.py [--base http://127.0.0.1:8010] [--turns 1,2]  — 키 필요.
발화 4개의 카드가 나오는지 본다."""
import argparse
import json
import sys
import urllib.request

TURNS = [
    ("최근 실패한 api들 중 risk가 있다고 판단하는 것들을 가져와봐", {"run_digest"}),
    ("이거 왜 실패했어", set()),
    (
        "지난 1주일간 새롭게 update된 api들을 모아서 오늘부터 3일간 매일 오전 9시에 전부 수행하고 리포트를 남겨줘",
        {"job_preview", "question_form"},
    ),
    (
        "오늘 업데이트한 API를 개발서버와 이관서버에서 동일한 테스트 데이터로 수행하고 결과를 비교해줘",
        {"job_preview", "question_form"},
    ),
    ("이번 주 실패 원인별로 묶어줘", {"run_groups"}),
    ("환불 금액 refundAmount는 0 이상이어야 한다는 규칙 만들어줘", {"rule_preview"}),
]


def post(base, path, body=None, sid=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else b"",
        method="POST",
        headers={"Content-Type": "application/json", **({"X-Session-Id": sid} if sid else {})},
    )
    return urllib.request.urlopen(req, timeout=180)


def turn_ok(complete: bool, seen: set[str], want: set[str]) -> bool:
    """Determine if a turn's result is acceptable.

    Args:
        complete: Whether the turn completed successfully.
        seen: Components that were observed in the response.
        want: Components that should be present (if empty, no components required).

    Returns:
        True if the turn passed: it completed and either no components are wanted
        or all wanted components were seen.
    """
    return bool(complete and (not want or (seen & want)))


def run_turn(base, sid, i, text, want):
    body = {"message": text}
    if i == 2:
        body["attached_items"] = [
            {
                "order": 1,
                "kind": "run",
                "ref_id": "run-0001",
                "label": "POST /v1/payments/refund",
                "field": "refundAmount",
                "expected": "refundAmount >= 0",
                "comment": text,
            }
        ]
    seen, tools, complete, event = set(), [], False, None
    for raw in post(base, "/api/atworks/chat", body, sid):
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event == "ui":
            seen.add(json.loads(line[6:])["component"])
        elif line.startswith("data: ") and event == "turn_complete":
            complete = True
        elif line.startswith("data: ") and event == "tool_call":
            tools.append(json.loads(line[6:])["tool"])
        elif line.startswith("data: ") and event == "tool_result":
            d = json.loads(line[6:])
            if d.get("status") == "blocked":
                print(f"  [gate] {d['tool']} held by {d.get('reason')}")
    good = turn_ok(complete, seen, want)
    print(f"turn {i}: {'OK ' if good else 'FAIL'} components={sorted(seen)}")
    print(f"  tool_calls={tools}")
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    ap.add_argument(
        "--turns",
        default="1,2,3,4",
        help="comma list of which of the four utterances to run (default: all)",
    )
    a = ap.parse_args()
    indices = [int(x) for x in a.turns.split(",") if x.strip()]

    sid = json.load(post(a.base, "/api/atworks/session"))["session_id"]
    ok = True
    for i, (text, want) in enumerate(TURNS, 1):
        if i not in indices:
            continue
        ok = ok and run_turn(a.base, sid, i, text, want)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
