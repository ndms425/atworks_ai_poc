# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""``commerce_common.memory.MemoryStore`` over the host's SQLite ``Store`` (self-growth spec §7).

Nothing of the reference machinery is copied: the Protocol, ``check_memory_store``,
``validate_fact``, the write filter and ``match_facts`` are IMPORTED, and this module is only the
six methods' SQL back. The subject is the **project**, not a person — this deployment's memory is
the team's shared vocabulary, so every fact under one ``subject_id`` belongs to everyone who can
open the portal. That is also why the reference's post-turn free-fact extraction stays off
(``memory_extract_facts=False``): a store shared by a team must hold only what a person confirmed.
"""

from __future__ import annotations

from commerce_common.memory import match_facts
from commerce_common.types import MemoryCategory, MemoryFact

from .store import Store, _parse_iso


class SqliteMemoryStore:
    """The MemoryStore contract on ``memory_facts`` / ``memory_meta``. Construct it over the same
    ``Store`` the backend runs on: the facts then live in the same file (and the same
    transactions) as the runs, the audit log and the vocabulary sidecar.

    ``check_memory_store(SqliteMemoryStore(store))`` is what proves it complete; the host runs it
    where the store enters the deployment (``MemoryRuntime.build``), so a method missing here
    fails at startup rather than inside a turn."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @staticmethod
    def _fact(row) -> MemoryFact:
        return MemoryFact(
            key=row["key"],
            value=row["value"],
            category=MemoryCategory(row["category"]),
            updated_at=_parse_iso(row["updated_at"]),
            source_session_id=row["source_session_id"],
        )

    async def get_facts(self, subject_id: str) -> list[MemoryFact]:
        return [self._fact(row) for row in self._store.memory_facts(subject_id)]

    async def upsert_facts(self, subject_id: str, facts: list[MemoryFact]) -> None:
        """The whole batch in one transaction. Facts arrive already validated —
        ``validate_fact`` (fence + write filter) runs at the caller, which is the single gate the
        spec puts in front of every write; this method does not second-guess it and does not
        filter, so there is exactly one place where "what may be stored" is decided."""
        self._store.upsert_memory_facts(
            subject_id,
            [(f.key, f.value, f.category.value, f.updated_at, f.source_session_id) for f in facts],
        )

    async def search_facts(self, subject_id: str, query: str) -> list[MemoryFact]:
        """The reference keyword match (``match_facts``) over this subject's facts, rather than a
        SQL LIKE of our own: the same query must select the same facts in every store, and the
        reference function IS that definition. The fact set of one project is bounded by the terms
        a team names, so reading it whole costs one indexed scan."""
        return match_facts(await self.get_facts(subject_id), query)

    async def delete_fact(self, subject_id: str, key: str) -> bool:
        return self._store.delete_memory_fact(subject_id, key)

    async def clear(self, subject_id: str) -> None:
        self._store.clear_memory_facts(subject_id)

    async def purge_generation(self, subject_id: str) -> int:
        return self._store.memory_purge_generation(subject_id)
