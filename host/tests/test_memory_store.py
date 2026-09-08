"""SqliteMemoryStore — the commerce_common MemoryStore contract on SQLite (self-growth §7).

The point of these tests is that this store is INTERCHANGEABLE with the reference's own stores:
the protocol check passes, and every method behaves the way `InMemoryMemoryStore` does, purge
generation included.
"""
from datetime import UTC, datetime

import pytest
from commerce_common.memory import InMemoryMemoryStore, check_memory_store
from commerce_common.types import MemoryCategory, MemoryFact

from atworks_host.memory_store import SqliteMemoryStore
from atworks_host.store import Store

PROJECT = "mes-demo"
T0 = datetime(2026, 9, 8, 9, tzinfo=UTC)


def _store() -> SqliteMemoryStore:
    return SqliteMemoryStore(Store(":memory:"))


def _fact(key: str, value: str, *, category=MemoryCategory.CONTEXT, at: datetime | None = None) -> MemoryFact:
    return MemoryFact(key=key, value=value, category=category, updated_at=at or T0,
                      source_session_id="sess")


def test_check_memory_store_accepts_it_and_rejects_a_partial_one():
    # The whole reason this runs where the store enters the deployment: a partial store must fail
    # at startup, not inside a turn. `is not None` proved neither half -- it passes for anything
    # the checker returns, and it would still pass if the checker had quietly become `identity`.
    store = _store()
    assert check_memory_store(store) is store          # the SAME object, not a wrapper

    class _Partial:
        """Everything but `purge_facts` -- the shape a half-finished port actually has."""
        async def get_facts(self, subject, keys=None): return []
        async def upsert_facts(self, subject, facts): return None
        async def delete_facts(self, subject, keys): return None

    with pytest.raises(TypeError):
        check_memory_store(_Partial())


async def test_upsert_and_get_round_trip_every_field():
    store = _store()
    await store.upsert_facts(PROJECT, [_fact("결제_계열", '{"path_prefix": "/v1/payment"}')])
    (back,) = await store.get_facts(PROJECT)
    assert back.key == "결제_계열" and back.value == '{"path_prefix": "/v1/payment"}'
    assert back.category is MemoryCategory.CONTEXT and back.source_session_id == "sess"
    assert back.updated_at == T0


async def test_upsert_replaces_a_fact_with_the_same_key():
    store = _store()
    await store.upsert_facts(PROJECT, [_fact("k", "old")])
    await store.upsert_facts(PROJECT, [_fact("k", "new", at=T0.replace(hour=10))])
    facts = await store.get_facts(PROJECT)
    assert len(facts) == 1 and facts[0].value == "new"


async def test_subjects_do_not_see_each_other():
    store = _store()
    await store.upsert_facts(PROJECT, [_fact("k", "ours")])
    await store.upsert_facts("other-project", [_fact("k", "theirs")])
    assert [f.value for f in await store.get_facts(PROJECT)] == ["ours"]
    assert [f.value for f in await store.get_facts("other-project")] == ["theirs"]


async def test_search_matches_key_value_and_category_like_the_reference():
    store, reference = _store(), InMemoryMemoryStore()
    facts = [_fact("결제_계열", '{"path_prefix": "/v1/payment"}'),
             _fact("계약_계열", '{"path_prefix": "/v1/contract"}')]
    await store.upsert_facts(PROJECT, facts)
    await reference.upsert_facts(PROJECT, facts)
    for query in ("payment", "결제_계열", "context", ""):
        assert sorted(f.key for f in await store.search_facts(PROJECT, query)) == \
               sorted(f.key for f in await reference.search_facts(PROJECT, query))


async def test_delete_reports_whether_it_deleted():
    store = _store()
    await store.upsert_facts(PROJECT, [_fact("k", "v")])
    assert await store.delete_fact(PROJECT, "k") is True
    assert await store.delete_fact(PROJECT, "k") is False
    assert await store.get_facts(PROJECT) == []


async def test_clear_purges_and_advances_the_generation():
    # The generation is what lets an in-flight extraction pass discard a batch written against a
    # subject that was purged meanwhile -- so it must survive the delete, not go with it.
    store = _store()
    assert await store.purge_generation(PROJECT) == 0
    await store.upsert_facts(PROJECT, [_fact("k", "v")])
    await store.clear(PROJECT)
    assert await store.get_facts(PROJECT) == [] and await store.purge_generation(PROJECT) == 1
    await store.clear(PROJECT)
    assert await store.purge_generation(PROJECT) == 2
    # Another subject's counter is untouched.
    assert await store.purge_generation("other-project") == 0


async def test_facts_survive_a_reopen_of_the_same_file(tmp_path):
    path = tmp_path / "facts.sqlite"
    await SqliteMemoryStore(Store(path)).upsert_facts(PROJECT, [_fact("k", "v")])
    reopened = SqliteMemoryStore(Store(path))
    assert [f.value for f in await reopened.get_facts(PROJECT)] == ["v"]
