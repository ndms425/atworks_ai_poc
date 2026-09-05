"""Unit tests for smoke_chat.py verdict logic."""
import importlib.util
from pathlib import Path

# Import turn_ok from smoke_chat.py
spec = importlib.util.spec_from_file_location("smoke_chat", Path(__file__).parent.parent.parent / "scripts" / "smoke_chat.py")
smoke_chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke_chat)

turn_ok = smoke_chat.turn_ok


def test_turn_ok_complete_with_matching_component():
    """Test that turn passes when complete and wanted components are seen."""
    result = turn_ok(True, {"run_digest", "suggestions"}, {"run_digest"})
    assert result is True
    assert isinstance(result, bool)


def test_turn_ok_complete_with_no_components_wanted():
    """Test that turn passes when complete and no components are wanted."""
    result = turn_ok(True, set(), set())
    assert result is True
    assert isinstance(result, bool)


def test_turn_ok_complete_with_missing_components():
    """Test that turn fails when complete but not all wanted components seen."""
    result = turn_ok(True, {"suggestions"}, {"job_preview", "question_form"})
    assert result is False
    assert isinstance(result, bool)


def test_turn_ok_incomplete():
    """Test that turn fails when not complete, even if wanted components seen."""
    result = turn_ok(False, {"run_digest"}, {"run_digest"})
    assert result is False
    assert isinstance(result, bool)


def test_turn_ok_returns_bool():
    """Test that turn_ok always returns a bool, never a set."""
    result = turn_ok(True, {"a"}, {"a"})
    assert isinstance(result, bool)
    assert result is True


def test_turns_include_the_comparison_utterance():
    """The fourth utterance is the one that could not be staged as one job before this
    change; it must reach either a job preview or the slot-filling form."""
    assert len(smoke_chat.TURNS) == 6
    text, want = smoke_chat.TURNS[3]
    assert "개발서버와 이관서버" in text and "비교" in text
    assert want == {"job_preview", "question_form"}
