"""What is true NOW, read from aTworks at the start of every turn (``state_now`` in the context).

A conversation remembers the jobs, rules, profiles and format batches it staged or listed
(``AtworksSessionState.seen_*``), but those copies are as old as the turn that saw them: a job
approved on the Jobs page five minutes ago still reads ``staged`` in the session. A reply built on
that copy tells the operator to approve something that is already approved. So before the model
says a word, the turn re-reads, through the backend ABC only (the REST adapter answers the same
calls):

- every queue that waits on a person (``get_pending_*``) — the ONLY things that may be called
  "awaiting approval", and
- the current record of each item this conversation already knows (``get_job`` / ``get_rule`` /
  ``get_profile`` / ``get_format_batch``), most recent first, capped. The fresh record also
  REPLACES the stale copy in ``seen_*``, so cards rendered later this turn show today's status.

Read-only, deterministic, no model in it; a read that fails is left out rather than guessed.
"""

from __future__ import annotations

import logging
from typing import Any

from .types import AtworksSessionContext, AtworksSessionState

logger = logging.getLogger(__name__)

#: Items listed per pending queue, and items re-read per kind of known record.
PENDING_CAP = 8
KNOWN_CAP = 10


def _status(item: Any) -> str:
    status = getattr(item, "status", None)
    return str(getattr(status, "value", status) or "")


def _cut(text: str | None, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _job_line(job: Any) -> dict[str, Any]:
    line: dict[str, Any] = {"id": job.job_id, "status": _status(job), "summary": _cut(job.summary)}
    total = getattr(job, "total_executions", None)
    remaining = getattr(job, "remaining_executions", None)
    if isinstance(total, int) and isinstance(remaining, int) and _status(job) == "applied":
        line["executions_done"] = f"{total - remaining}/{total}"
    return line


def _rule_line(rule: Any) -> dict[str, Any]:
    return {"id": rule.rule_id, "status": _status(rule), "api_id": rule.api_id, "summary": _cut(rule.message)}


def _profile_line(profile: Any) -> dict[str, Any]:
    return {"id": profile.profile_id, "status": _status(profile), "job_id": profile.job_id,
            "summary": _cut(profile.summary)}


def _batch_line(batch: Any) -> dict[str, Any]:
    return {"id": batch.batch_id, "status": _status(batch), "entries": len(getattr(batch, "entries", []) or [])}


# kind -> (pending reader, by-id reader, state bucket, line builder)
_KINDS: tuple[tuple[str, str, str, str, Any], ...] = (
    ("jobs", "get_pending_jobs", "get_job", "seen_jobs", _job_line),
    ("rules", "get_pending_rules", "get_rule", "seen_rules", _rule_line),
    ("profiles", "get_pending_profiles", "get_profile", "seen_profiles", _profile_line),
    ("format_batches", "get_pending_format_batches", "get_format_batch", "seen_format_batches", _batch_line),
)


async def live_state(backend: Any, session: AtworksSessionContext, state: AtworksSessionState) -> dict[str, Any]:
    pending: dict[str, Any] = {}
    known: dict[str, list[dict[str, Any]]] = {}
    total_pending = 0
    for kind, pending_reader, by_id, bucket, line in _KINDS:
        reader = getattr(backend, pending_reader, None)
        if reader is not None:
            try:
                items = list(await reader(session))
                total_pending += len(items)
                pending[kind] = [line(item) for item in items[:PENDING_CAP]]
                if len(items) > PENDING_CAP:
                    pending[f"{kind}_more"] = len(items) - PENDING_CAP
            except Exception:
                logger.exception("state_now: %s failed; left out", pending_reader)
        seen: dict[str, Any] | None = getattr(state, bucket, None)
        lookup = getattr(backend, by_id, None)
        if not seen or lookup is None:
            continue
        lines = []
        for item_id in list(seen)[-KNOWN_CAP:][::-1]:
            try:
                current = await lookup(session, item_id)
            except Exception:
                logger.exception("state_now: %s(%s) failed; left out", by_id, item_id)
                continue
            if current is None:
                continue
            seen[item_id] = current   # cards rendered later this turn show the status as it is now
            lines.append(line(current))
        if lines:
            known[kind] = lines
    out: dict[str, Any] = {"pending_approvals": {"total": total_pending, **pending}}
    if known:
        out["this_conversation_items"] = known
    return out
