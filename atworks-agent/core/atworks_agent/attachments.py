"""화면→채팅 구조화 첨부. open-design renderCommentAttachmentHint 이식: selector/position 대신
run_id/api_id/field/actual/expected. 하드 스코프 문장으로 "이 항목만 다뤄라"를 못 박는다.
값은 전부 펜스 sanitizer를 거친다 — 응답 body에서 온 텍스트일 수 있다."""
from __future__ import annotations

from collections.abc import Sequence

from .fencing import ATWORKS_FENCE
from .types import AttachedItem

MAX_ITEMS = 8


def _s(value: str | None, max_chars: int) -> str:
    return ATWORKS_FENCE.sanitize_text(value or "", max_chars) or "(none)"


def render_attached_items_hint(items: Sequence[AttachedItem]) -> str:
    if not items:
        return ""
    lines = [
        "",
        "",
        "<attached-result-items>",
        "Hard scope: answer about ONLY the items identified below by ref_id. Do NOT re-run, "
        "re-rank, or stage anything for other APIs or runs even if you notice issues there — "
        "mention those as a follow-up note instead. The status and failed_rules on each item are "
        "the deterministic verdict; explain them, never re-judge them. If the user's request "
        "needs something outside this scope, ask before proceeding.",
    ]
    for item in sorted(items, key=lambda i: i.order)[:MAX_ITEMS]:
        lines += [
            "",
            f"{item.order}. {_s(item.ref_id, 64)}",
            f"kind: {item.kind}",
            f"label: {_s(item.label, 120)}",
            f"field: {_s(item.field, 80)}",
            f"actual: {_s(item.actual, 200)}",
            f"expected: {_s(item.expected, 200)}",
        ]
        if item.comment:
            lines.append(f"comment: {_s(item.comment, 300)}")
    lines.append("</attached-result-items>")
    return "\n".join(lines)
