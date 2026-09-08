"""R49 — the two credential-failure error messages in streaming.py must point at the
repo-root .env and name a real environment variable; the COMMERCE_DEMO_AUTH=sdk sentence
told the operator to set a variable load_demo_env no longer reads, which does nothing."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "atworks_host" / "streaming.py"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_credential_messages_do_not_mention_the_dead_sentinel():
    assert "COMMERCE_DEMO_AUTH" not in _source()


def test_credential_messages_name_a_real_anthropic_env_var():
    text = _source()
    _, _, after_auth = text.partition("except anthropic.AuthenticationError")
    auth_block, _, after_generic = after_auth.partition("except Exception")
    generic_block = after_generic.split("def ", 1)[0]
    for block in (auth_block, generic_block):
        assert "ANTHROPIC_AUTH_TOKEN" in block or "ANTHROPIC_API_KEY" in block
