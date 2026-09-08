"""asklog.classify_turn -- the deterministic turn classifier (self-growth spec §6).

Every assertion here is about a rule the MODEL cannot influence: it calls tools, and what the
tools were decides outcome, intent and cluster key. Nothing in this module reaches a backend, a
clock or a session store, so a change in classification shows up here first.
"""
from datetime import UTC, datetime

from atworks_agent.asklog import classify_turn, cluster_key_for, normalized_tokens
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.masking import policy_from_config
from atworks_agent.types import AtworksSessionContext, AtworksSessionState, QueryFilters, QuerySpec

NOW = datetime(2026, 9, 8, 10, tzinfo=UTC)
POLICY = policy_from_config(AtworksAgentConfig(model="m"))
SESSION = AtworksSessionContext(session_id="s-1", project_id="mes", operator="minseong", role="qa")


def _spec(**kwargs) -> QuerySpec:
    base = {"dimensions": ["api"], "measures": ["non_pass"], "filters": QueryFilters(window_days=30)}
    return QuerySpec(**{**base, **kwargs})


def _classify(question="실패 좀 보여줘", tools=(), cards=0, unmet=None, spec=None, turn_id="t-1"):
    return classify_turn(
        question=question, tool_names=list(tools), cards=cards, unmet=unmet, spec=spec,
        policy=POLICY, session=SESSION, turn_id=turn_id, now=NOW,
    )


# -- outcome -------------------------------------------------------------------------------

def test_answered_needs_both_a_data_tool_and_a_card():
    entry = _classify(tools=["query_runs", "present_query_table"], cards=1, spec=_spec())
    assert entry.outcome == "answered"


def test_a_data_tool_without_a_card_is_partial():
    # The model read the numbers and narrated them in prose. That is the case the Growth view
    # exists to surface, so it must not read as an answer.
    assert _classify(tools=["query_runs"], cards=0, spec=_spec()).outcome == "partial"


def test_a_card_without_a_data_tool_is_partial():
    assert _classify(tools=["present_suggestions"], cards=1).outcome == "partial"


def test_a_turn_with_no_tools_at_all_is_partial():
    assert _classify(tools=[], cards=0).outcome == "partial"


def test_note_unmet_ask_makes_the_turn_unmet():
    entry = _classify(tools=["note_unmet_ask"], unmet=("no_dimension", "요청 헤더별로 묶어줘", "요청 헤더 차원"))
    assert entry.outcome == "unmet" and entry.unmet_reason == "no_dimension"
    assert entry.wanted == "요청 헤더 차원"


def test_action_wins_over_every_other_rule():
    # A staging turn is an action even when it also queried, drew a card and reported a gap:
    # approval turns are not growth material and must not dilute the answered/unmet ratios.
    entry = _classify(
        tools=["query_runs", "present_query_table", "note_unmet_ask", "stage_job"],
        cards=1, unmet=("refused", "…", "…"), spec=_spec(),
    )
    assert entry.outcome == "action"


def test_apply_and_discard_are_actions_too():
    assert _classify(tools=["apply_rule"], cards=1).outcome == "action"
    assert _classify(tools=["discard_profile"]).outcome == "action"


# -- intent --------------------------------------------------------------------------------

def test_intent_lookup_status_and_aggregate():
    assert _classify(tools=["search_apis"], cards=1).intent == "lookup"
    assert _classify(tools=["get_api"], cards=1).intent == "lookup"
    assert _classify(tools=["list_runs"], cards=1).intent == "status"
    assert _classify(tools=["rank_failed_runs"], cards=1).intent == "status"
    assert _classify(tools=["aggregate_runs"], cards=1).intent == "aggregate"


def test_status_wins_over_lookup_when_both_ran():
    # A triage turn calls search_apis to resolve the API and then list_runs; the QUESTION was
    # about run status, so the more specific intent has to come first in the map.
    assert _classify(tools=["search_apis", "list_runs"], cards=1).intent == "status"


def test_aggregate_refines_to_trend_on_a_day_dimension():
    assert _classify(tools=["query_runs"], cards=1, spec=_spec(dimensions=["day"])).intent == "trend"
    assert _classify(tools=["query_runs"], cards=1, spec=_spec(dimensions=["week"])).intent == "trend"


def test_aggregate_refines_to_compare_and_compare_wins_over_trend():
    spec = _spec(dimensions=["day"], compare_previous_window=True)
    assert _classify(tools=["query_runs"], cards=1, spec=spec).intent == "compare"


def test_query_runs_without_a_spec_stays_aggregate():
    # The turn called the tool and the tool failed validation, so no spec came back.
    assert _classify(tools=["query_runs"], cards=0).intent == "aggregate"


def test_screen_directives_alone_are_meta():
    assert _classify(tools=["navigate_screen", "highlight_screen"]).intent == "meta"


def test_a_screen_directive_beside_a_data_tool_is_not_meta():
    assert _classify(tools=["navigate_screen", "list_runs"], cards=1).intent == "status"


def test_intent_carries_the_unmet_reason_when_no_other_tool_ran():
    entry = _classify(tools=["note_unmet_ask"], unmet=("out_of_scope", "점심 메뉴", ""))
    assert entry.intent == "unmet:out_of_scope"


def test_action_intent():
    assert _classify(tools=["stage_rule"], cards=1).intent == "action"


# -- masking and truncation ----------------------------------------------------------------

def test_the_question_is_masked_before_it_is_stored():
    entry = _classify(question="900101-1234567 이 사람 실행 좀 찾아줘")
    assert "900101-1234567" not in entry.question and "***" in entry.question


def test_an_email_in_the_question_is_masked_too():
    assert "hong@example.com" not in _classify(question="hong@example.com 이 계정").question


def test_the_question_is_truncated_at_300_chars_after_masking():
    entry = _classify(question="가" * 500)
    assert len(entry.question) == 300


def test_masking_happens_before_truncation():
    # The pattern sits past the 300th character of the RAW message: truncating first would cut
    # the id in half, leave the fragment unmatched, and store raw digits.
    entry = _classify(question=("가" * 295) + " 900101-1234567 실패")
    assert "900101" not in entry.question


# -- cluster_key ---------------------------------------------------------------------------

def test_cluster_key_comes_from_the_spec_and_carries_no_values():
    a = _classify(question="/v1/payments 실패", tools=["query_runs"], cards=1,
                  spec=_spec(filters=QueryFilters(window_days=30, path_contains=["/v1/payments"])))
    b = _classify(question="/v1/orders 실패", tools=["query_runs"], cards=1, turn_id="t-2",
                  spec=_spec(filters=QueryFilters(window_days=7, path_contains=["/v1/orders"])))
    assert a.cluster_key == b.cluster_key
    assert "payments" not in a.cluster_key and "orders" not in b.cluster_key
    assert "30" not in a.cluster_key


def test_cluster_key_is_deterministic_for_the_same_spec():
    spec = _spec(dimensions=["api", "day"], measures=["non_pass", "fail_rate"])
    assert _classify(tools=["query_runs"], cards=1, spec=spec).cluster_key == \
        _classify(tools=["query_runs"], cards=1, spec=spec, turn_id="t-9").cluster_key


def test_an_unmet_cluster_key_is_the_reason_plus_sorted_tokens():
    entry = _classify(question="요청 헤더별로 실패를 묶어줘",
                      unmet=("no_dimension", "요청 헤더별 묶기", "요청 헤더 차원"))
    key = entry.cluster_key
    assert key.startswith("unmet:no_dimension|")
    tokens = key.split("|", 1)[1].split(",")
    assert tokens == sorted(tokens) and len(tokens) <= 6


def test_two_unmet_questions_with_the_same_words_in_another_order_share_a_cluster():
    # Tokens are sorted, so word order alone never splits a cluster. (Korean particles are NOT
    # stemmed -- "실패" and "실패를" are different tokens; that is a known limit of the §6 rule,
    # not something this test pretends away.)
    one = _classify(question="헤더별로 실패 묶어줘", unmet=("no_dimension", "a", "b"))
    two = _classify(question="실패 묶어줘 헤더별로", unmet=("no_dimension", "a", "b"), turn_id="t-2")
    assert one.cluster_key == two.cluster_key


def test_a_spec_wins_over_the_unmet_branch():
    # A turn that queried AND reported a gap clusters with the query it actually ran.
    entry = _classify(tools=["query_runs", "note_unmet_ask"], cards=1, spec=_spec(),
                      unmet=("no_evidence", "…", "…"))
    assert entry.cluster_key.startswith("dims=")


def test_a_spec_less_turn_clusters_on_outcome_and_intent():
    assert _classify(tools=["list_runs"], cards=1).cluster_key == "answered:status"


def test_normalized_tokens_drop_stopwords_short_tokens_and_duplicates():
    tokens = normalized_tokens("실패 실패 를 the failed_rule 별로!!")
    assert "the" not in tokens and "를" not in tokens
    assert tokens.count("실패") == 1 and tokens == sorted(tokens)


def test_normalized_tokens_keep_at_most_six():
    assert len(normalized_tokens("aaa bbb ccc ddd eee fff ggg hhh")) == 6


def test_cluster_key_for_is_reachable_without_building_an_entry():
    assert cluster_key_for(outcome="partial", intent="meta", question="", spec=None, unmet=None) \
        == "partial:meta"


# -- the row itself ------------------------------------------------------------------------

def test_the_entry_carries_the_session_the_counters_and_the_turn_id():
    entry = _classify(tools=["query_runs", "present_query_table"], cards=1, spec=_spec(), turn_id="turn-7")
    assert entry.session_id == "s-1" and entry.operator == "minseong" and entry.role == "qa"
    assert entry.tool_calls == 2 and entry.cards == 1 and entry.turn_id == "turn-7"
    assert entry.at == NOW and entry.spec == _spec() and entry.feedback is None


def test_a_turn_without_an_unmet_report_carries_neither_reason_nor_wanted():
    entry = _classify(tools=["list_runs"], cards=1)
    assert entry.unmet_reason is None and entry.wanted is None


def test_per_turn_scratch_fields_never_change_the_persisted_session_document():
    """turn_tool_names/turn_cards/turn_unmet are per-turn scratch (Field(exclude=True)): mutating
    them must leave `state.model_dump(mode="json")` -- the bytes SessionRecord.state_document
    persists under compare-and-set -- identical, or every chat turn would dirty the session row."""
    import json
    state = AtworksSessionState()
    before = json.dumps(state.model_dump(mode="json"), sort_keys=True)
    state.turn_tool_names.extend(["query_runs", "present_query_table"])
    state.turn_cards = 2
    state.turn_unmet = ("no_dimension", "endpoint 계열", "path_segment_3")
    after = json.dumps(state.model_dump(mode="json"), sort_keys=True)
    assert before == after
    assert "turn_tool_names" not in after and "turn_cards" not in after and "turn_unmet" not in after
