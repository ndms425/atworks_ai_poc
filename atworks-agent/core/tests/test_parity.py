from atworks_agent.parity import BodyDiff, DiffCluster, apply_ignore, cluster_diffs, compare_bodies


def test_equal_bodies_ignoring_a_noisy_field():
    a = {"a": 1, "t": "x"}
    b = {"a": 1, "t": "y"}
    result = compare_bodies(a, b, ["$.t"])
    assert isinstance(result, BodyDiff)
    assert result.equal is True
    assert result.diff_paths == []


def test_unequal_bodies_without_ignoring_the_noisy_field():
    a = {"a": 1, "t": "x"}
    b = {"a": 1, "t": "y"}
    result = compare_bodies(a, b, [])
    assert result.equal is False
    assert result.diff_paths == ["$.t"]


def test_nested_object_path_reported_with_dotted_form():
    a = {"a": {"b": 1}}
    b = {"a": {"b": 2}}
    result = compare_bodies(a, b, [])
    assert result.diff_paths == ["$.a.b"]


def test_array_index_path_reported_with_bracket_form():
    a = {"arr": [1, 2, 3]}
    b = {"arr": [1, 9, 3]}
    result = compare_bodies(a, b, [])
    assert result.diff_paths == ["$.arr[1]"]


def test_array_of_objects_reports_nested_index_path():
    a = {"arr": [{"x": 1}]}
    b = {"arr": [{"x": 2}]}
    result = compare_bodies(a, b, [])
    assert result.diff_paths == ["$.arr[0].x"]


def test_path_present_in_only_one_body_is_a_diff():
    a = {"a": 1}
    b = {"a": 1, "b": 2}
    result = compare_bodies(a, b, [])
    assert result.diff_paths == ["$.b"]


def test_ignore_exact_leaf_path():
    a = {"meta": {"serverTime": "t1"}}
    b = {"meta": {"serverTime": "t2"}}
    result = compare_bodies(a, b, ["$.meta.serverTime"])
    assert result.equal is True


def test_ignore_dotted_prefix_drops_nested_children():
    a = {"meta": {"serverTime": "t1", "other": "x"}}
    b = {"meta": {"serverTime": "t2", "other": "y"}}
    result = compare_bodies(a, b, ["$.meta"])
    assert result.equal is True


def test_ignore_bracket_prefix_drops_array_children():
    a = {"meta": ["t1", "keep-a"]}
    b = {"meta": ["t2", "keep-b"]}
    result = compare_bodies(a, b, ["$.meta[0]"])
    assert result.diff_paths == ["$.meta[1]"]


def test_ignore_prefix_does_not_match_unrelated_sibling_with_shared_prefix():
    # "$.meta" must not accidentally swallow "$.metaExtra" (no "." or "[" boundary).
    a = {"meta": {"x": 1}, "metaExtra": 1}
    b = {"meta": {"x": 1}, "metaExtra": 2}
    result = compare_bodies(a, b, ["$.meta"])
    assert result.diff_paths == ["$.metaExtra"]


def test_apply_ignore_removes_ignored_leaves_from_a_dict():
    body = {"a": 1, "meta": {"serverTime": "t1", "other": "x"}}
    result = apply_ignore(body, ["$.meta.serverTime"])
    assert result == {"a": 1, "meta": {"other": "x"}}


def test_cluster_diffs_groups_identical_diff_path_sets():
    rows = [
        {"row_key": "row1", "diff_paths": ["$.meta.serverTime"]},
        {"row_key": "row2", "diff_paths": ["$.meta.serverTime"]},
        {"row_key": "row3", "diff_paths": ["$.meta.serverTime"]},
        {"row_key": "row4", "diff_paths": ["$.a"]},
    ]
    clusters = cluster_diffs(rows)
    assert len(clusters) == 2
    assert isinstance(clusters[0], DiffCluster)
    assert clusters[0].paths == ["$.meta.serverTime"]
    assert clusters[0].count == 3
    assert clusters[0].row_keys == ["row1", "row2", "row3"]
    assert clusters[1].paths == ["$.a"]
    assert clusters[1].count == 1


def test_cluster_diffs_biggest_first_message():
    rows = [
        {"row_key": "row1", "diff_paths": ["$.meta.serverTime"]},
        {"row_key": "row2", "diff_paths": ["$.meta.serverTime"]},
    ]
    clusters = cluster_diffs(rows)
    message = f"{clusters[0].count} rows differ only in {clusters[0].paths}"
    assert message == "2 rows differ only in ['$.meta.serverTime']"
