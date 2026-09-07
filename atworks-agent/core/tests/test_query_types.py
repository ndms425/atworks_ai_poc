"""자가발전 질의 엔진의 타입/카탈로그/config 게이트 (self-growth, spec 2026-09-07, Task 1)."""
import json
from datetime import UTC, datetime
from typing import get_args

import pytest

from atworks_agent.catalog import (
    DIMENSIONS,
    MEASURES,
    catalog_hint,
    cluster_key_for_spec,
    title_for_spec,
)
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.types import Dimension, Measure, QueryFilters, QuerySpec


def _spec(**overrides) -> QuerySpec:
    defaults = dict(dimensions=["path_segment_2"], measures=["non_pass", "apis"],
                     filters=QueryFilters(status="non_pass", window_days=30), limit=20)
    defaults.update(overrides)
    return QuerySpec(**defaults)


# -- QuerySpec / QueryFilters round-trip and validation ---------------------------------

def test_query_spec_round_trips_through_json():
    spec = _spec()
    restored = QuerySpec.model_validate(json.loads(spec.model_dump_json()))
    assert restored == spec


def test_query_filters_and_query_spec_reject_unknown_keys():
    with pytest.raises(ValueError):
        QueryFilters(bogus=1)
    with pytest.raises(ValueError):
        QuerySpec(measures=["runs"], bogus=1)


def test_query_spec_rejects_three_dimensions():
    with pytest.raises(ValueError):
        _spec(dimensions=["api", "method", "target_env"])


def test_query_spec_rejects_duplicate_dimensions():
    with pytest.raises(ValueError):
        _spec(dimensions=["method", "method"])


def test_query_spec_rejects_limit_above_fifty():
    with pytest.raises(ValueError):
        _spec(limit=51)
    _spec(limit=50)


def test_query_filters_rejects_window_days_combined_with_since_or_until():
    with pytest.raises(ValueError):
        QueryFilters(window_days=30, since=datetime(2026, 8, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        QueryFilters(window_days=30, until=datetime(2026, 8, 1, tzinfo=UTC))
    # both absent, or either alone, is fine
    QueryFilters()
    QueryFilters(window_days=30)
    QueryFilters(since=datetime(2026, 8, 1, tzinfo=UTC), until=datetime(2026, 8, 31, tzinfo=UTC))


def test_query_spec_rejects_order_by_not_in_measures():
    with pytest.raises(ValueError):
        _spec(measures=["runs", "apis"], order_by="fail_rate")
    _spec(measures=["runs", "apis"], order_by="key")
    _spec(measures=["runs", "apis"], order_by="apis")


def test_query_spec_allows_zero_dimensions_for_one_summary_row():
    spec = _spec(dimensions=[])
    assert spec.dimensions == []


# -- cluster_key_for_spec: values never appear, kinds always do -------------------------

def test_cluster_key_ignores_filter_values_but_not_kinds():
    a = _spec(filters=QueryFilters(status="non_pass", window_days=30, path_contains=["payment"]))
    b = _spec(filters=QueryFilters(status="non_pass", window_days=30, path_contains=["refund", "cancel"]))
    assert cluster_key_for_spec(a) == cluster_key_for_spec(b)

    c = _spec(filters=QueryFilters(status="non_pass", window_days=30, method=["GET"]))
    assert cluster_key_for_spec(a) != cluster_key_for_spec(c)


def test_cluster_key_reflects_compare_previous_window():
    plain = _spec(compare_previous_window=False)
    compared = _spec(compare_previous_window=True)
    assert cluster_key_for_spec(compared) == cluster_key_for_spec(plain) + "|compare"


def test_cluster_key_is_dims_and_measures_sorted():
    spec = _spec(dimensions=["target_env", "method"], measures=["fail", "apis", "runs"],
                filters=QueryFilters(), order_by="key")
    key = cluster_key_for_spec(spec)
    assert key == "dims=method,target_env|measures=apis,fail,runs|filters="


# -- title_for_spec: deterministic Korean, bounded ---------------------------------------

def test_title_for_spec_is_deterministic_and_bounded():
    spec = _spec()
    a, b = title_for_spec(spec), title_for_spec(spec)
    assert a == b
    assert len(a) <= 120
    assert "상위 20" in a


def test_title_for_spec_uses_전체_for_no_dimensions_and_no_status():
    spec = _spec(dimensions=[], filters=QueryFilters(window_days=7))
    title = title_for_spec(spec)
    assert title.startswith("전체 · 전체 · 7일 · 상위")


# -- catalog_hint: covers every literal exactly once, byte-stable -----------------------

def test_catalog_hint_mentions_every_dimension_and_measure_exactly_once():
    # One line per item, dimensions first then measures, in literal declaration order -- checked
    # by line prefix rather than substring count, since "api" is itself a substring of "api_group"
    # /"apis"/other help text.
    hint = catalog_hint()
    lines = hint.split("\n")
    names = [line.split(" — ", 1)[0] for line in lines]
    assert names == [*get_args(Dimension), *get_args(Measure)]
    assert len(names) == len(set(names))


def test_catalog_hint_is_byte_identical_across_calls():
    assert catalog_hint() == catalog_hint()


def test_dimensions_and_measures_catalog_cover_every_literal():
    assert set(DIMENSIONS) == set(get_args(Dimension))
    assert set(MEASURES) == set(get_args(Measure))


# -- config gate --------------------------------------------------------------------------

def test_absent_tools_gate_on_enable_query_runs():
    on = AtworksAgentConfig(model="m").absent_tools()
    off = AtworksAgentConfig(model="m", enable_query_runs=False).absent_tools()
    assert not {"query_runs", "present_query_table", "note_unmet_ask", "propose_alias"} & on
    assert {"query_runs", "present_query_table", "note_unmet_ask", "propose_alias"} <= off


def test_self_growth_config_defaults():
    c = AtworksAgentConfig(model="m")
    assert c.enable_query_runs is True
    assert c.enable_growth is True
    assert c.memory_extract_facts is False
    assert c.max_query_dimensions == 2
    assert c.max_query_limit == 50
    assert c.slo_query_ms == 300
    assert c.ask_log_retention_days == 365
    assert c.promote_window_days == 7
    assert c.promote_min_users == 3
    assert c.promote_min_asks == 5
    assert c.vocabulary_max_inject == 8
    assert c.vocabulary_cooldown_days == 30
    assert c.vocabulary_auto_demote_rejections == 3
