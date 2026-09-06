"""Pins the ATWORKS_TRUST_OS_CA=0 switch path (R36): with the truststore injection
skipped, build() must still produce a working FastAPI app. No network call happens here —
this only proves the conditional branch and the rest of build() still wire up."""
import importlib


def test_build_without_truststore_injection(monkeypatch):
    monkeypatch.setenv("ATWORKS_TRUST_OS_CA", "0")
    import atworks_host.main as main

    importlib.reload(main)
    app, backend, scheduler = main.build()
    assert type(app).__name__ == "FastAPI"
    assert backend is not None
    assert scheduler is not None


def test_build_respects_env_file_atworks_trust_os_ca_0(monkeypatch, tmp_path):
    """Proves .env is consulted before ATWORKS_TRUST_OS_CA check: setting
    ATWORKS_TRUST_OS_CA=0 in .env must prevent truststore.inject_into_ssl() call."""
    # Ensure OS variable is not set (load_dotenv won't override an existing var)
    monkeypatch.delenv("ATWORKS_TRUST_OS_CA", raising=False)

    # Create .env with ATWORKS_TRUST_OS_CA=0
    env_file = tmp_path / ".env"
    env_file.write_text("ATWORKS_TRUST_OS_CA=0")

    # Track calls to truststore.inject_into_ssl
    calls = []

    # Import atworks_host.main
    import atworks_host.main as main

    # Point main.ROOT to tmp_path so load_dotenv finds our .env
    monkeypatch.setattr("atworks_host.main.ROOT", tmp_path)

    # Mock truststore.inject_into_ssl to track calls
    monkeypatch.setattr("truststore.inject_into_ssl", lambda: calls.append(1))

    # Mock out the dependencies that try to access the filesystem
    monkeypatch.setattr("atworks_host.main.AtworksAgent", lambda **kwargs: type('MockAgent', (), {'client': None})())
    monkeypatch.setattr("atworks_host.main.MockAtworks", lambda *args, **kwargs: type('MockBackend', (), {})())
    monkeypatch.setattr("atworks_host.main.create_app", lambda **kwargs: type('FastAPI', (), {})())
    monkeypatch.setattr("atworks_host.main.Scheduler", lambda *args, **kwargs: type('Scheduler', (), {})())

    # Call build() - this should read ATWORKS_TRUST_OS_CA=0 from .env
    app, backend, scheduler = main.build()

    # With ATWORKS_TRUST_OS_CA=0 in .env, inject_into_ssl must NOT be called
    assert calls == [], f"Expected no truststore.inject_into_ssl() calls with ATWORKS_TRUST_OS_CA=0, but got {calls}"


def test_build_respects_env_file_atworks_trust_os_ca_1(monkeypatch, tmp_path):
    """Proves .env setting ATWORKS_TRUST_OS_CA=1 causes truststore.inject_into_ssl() call."""
    # Ensure OS variable is not set
    monkeypatch.delenv("ATWORKS_TRUST_OS_CA", raising=False)

    # Create .env with ATWORKS_TRUST_OS_CA=1
    env_file = tmp_path / ".env"
    env_file.write_text("ATWORKS_TRUST_OS_CA=1")

    # Track calls to truststore.inject_into_ssl
    calls = []

    # Import atworks_host.main
    import atworks_host.main as main

    # Point main.ROOT to tmp_path so load_dotenv finds our .env
    monkeypatch.setattr("atworks_host.main.ROOT", tmp_path)

    # Mock truststore.inject_into_ssl to track calls
    monkeypatch.setattr("truststore.inject_into_ssl", lambda: calls.append(1))

    # Mock out the dependencies that try to access the filesystem
    monkeypatch.setattr("atworks_host.main.AtworksAgent", lambda **kwargs: type('MockAgent', (), {'client': None})())
    monkeypatch.setattr("atworks_host.main.MockAtworks", lambda *args, **kwargs: type('MockBackend', (), {})())
    monkeypatch.setattr("atworks_host.main.create_app", lambda **kwargs: type('FastAPI', (), {})())
    monkeypatch.setattr("atworks_host.main.Scheduler", lambda *args, **kwargs: type('Scheduler', (), {})())

    # Call build() - this should read ATWORKS_TRUST_OS_CA=1 from .env
    app, backend, scheduler = main.build()

    # With ATWORKS_TRUST_OS_CA=1 in .env, inject_into_ssl MUST be called
    assert calls == [1], f"Expected one truststore.inject_into_ssl() call with ATWORKS_TRUST_OS_CA=1, but got {calls}"


def test_build_deletes_empty_credential_env_vars(monkeypatch, tmp_path):
    """Proves empty credential variables are deleted after load_dotenv so the SDK
    falls back to the other auth method (e.g., empty ANTHROPIC_API_KEY doesn't block
    ANTHROPIC_AUTH_TOKEN Bearer auth). R38: empty credentials break auth fallback."""
    import os

    # Set empty API key and valid Bearer token in OS environment (before .env load)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok_valid_bearer")

    # Create empty .env so load_dotenv has nothing to add
    env_file = tmp_path / ".env"
    env_file.write_text("")

    # Import atworks_host.main
    import atworks_host.main as main

    # Point main.ROOT to tmp_path so load_dotenv finds our (empty) .env
    monkeypatch.setattr("atworks_host.main.ROOT", tmp_path)

    # Set ATWORKS_TRUST_OS_CA=0 to skip truststore injection
    monkeypatch.setenv("ATWORKS_TRUST_OS_CA", "0")

    # Mock out the dependencies that try to access the filesystem
    monkeypatch.setattr("atworks_host.main.AtworksAgent", lambda **kwargs: type('MockAgent', (), {'client': None})())
    monkeypatch.setattr("atworks_host.main.MockAtworks", lambda *args, **kwargs: type('MockBackend', (), {})())
    monkeypatch.setattr("atworks_host.main.create_app", lambda **kwargs: type('FastAPI', (), {})())
    monkeypatch.setattr("atworks_host.main.Scheduler", lambda *args, **kwargs: type('Scheduler', (), {})())

    # Call build() - should delete empty ANTHROPIC_API_KEY
    app, backend, scheduler = main.build()

    # After build(), empty ANTHROPIC_API_KEY must be gone (so SDK falls back to Bearer)
    assert "ANTHROPIC_API_KEY" not in os.environ, \
        f"Empty ANTHROPIC_API_KEY should be deleted, but found: {os.environ.get('ANTHROPIC_API_KEY')!r}"

    # ANTHROPIC_AUTH_TOKEN (Bearer) must still be set
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "tok_valid_bearer", \
        f"ANTHROPIC_AUTH_TOKEN should still be set, but got: {os.environ.get('ANTHROPIC_AUTH_TOKEN')!r}"


def test_build_env_overrides_inherited_shell_variables(monkeypatch, tmp_path):
    """Proves .env values take precedence over inherited shell variables (R39).
    Developer shells often export ANTHROPIC_BASE_URL for other tools; this test
    verifies the repo's .env file overrides those inherited values."""
    import os

    # Set shell environment variable (simulating inherited shell export)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://shell.invalid")
    monkeypatch.setenv("ATWORKS_TRUST_OS_CA", "0")

    # Create .env with different values
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_BASE_URL=https://file.invalid\nATWORKS_TRUST_OS_CA=0")

    # Import atworks_host.main
    import atworks_host.main as main

    # Point main.ROOT to tmp_path so load_dotenv finds our .env
    monkeypatch.setattr("atworks_host.main.ROOT", tmp_path)

    # Mock out the dependencies that try to access the filesystem
    monkeypatch.setattr("atworks_host.main.AtworksAgent", lambda **kwargs: type('MockAgent', (), {'client': None})())
    monkeypatch.setattr("atworks_host.main.MockAtworks", lambda *args, **kwargs: type('MockBackend', (), {})())
    monkeypatch.setattr("atworks_host.main.create_app", lambda **kwargs: type('FastAPI', (), {})())
    monkeypatch.setattr("atworks_host.main.Scheduler", lambda *args, **kwargs: type('Scheduler', (), {})())

    # Call build() - .env should override inherited ANTHROPIC_BASE_URL
    app, backend, scheduler = main.build()

    # After build(), ANTHROPIC_BASE_URL must be the .env value, not the shell value
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://file.invalid", \
        f"Expected .env value 'https://file.invalid', but got: {os.environ.get('ANTHROPIC_BASE_URL')!r}"


def test_build_atworks_insight_narration_env_switch(monkeypatch, tmp_path):
    """Proves ATWORKS_INSIGHT_NARRATION=0 disables enable_insight_narration on the built
    config (the deterministic-fallback demo switch), and that it defaults to enabled when
    unset — mirroring the ATWORKS_TRUST_OS_CA env-override tests above."""
    monkeypatch.setattr("atworks_host.main.ROOT", tmp_path)
    (tmp_path / ".env").write_text("")
    monkeypatch.setenv("ATWORKS_TRUST_OS_CA", "0")

    captured_configs = []

    import atworks_host.main as main

    monkeypatch.setattr("atworks_host.main.AtworksAgent", lambda **kwargs: type('MockAgent', (), {'client': None})())
    monkeypatch.setattr(
        "atworks_host.main.MockAtworks",
        lambda config, *args, **kwargs: captured_configs.append(config) or type('MockBackend', (), {})(),
    )
    monkeypatch.setattr("atworks_host.main.create_app", lambda **kwargs: type('FastAPI', (), {})())
    monkeypatch.setattr("atworks_host.main.Scheduler", lambda *args, **kwargs: type('Scheduler', (), {})())

    # Default (unset): enable_insight_narration stays True.
    monkeypatch.delenv("ATWORKS_INSIGHT_NARRATION", raising=False)
    main.build()
    assert captured_configs[-1].enable_insight_narration is True

    # ATWORKS_INSIGHT_NARRATION=0 disables it.
    monkeypatch.setenv("ATWORKS_INSIGHT_NARRATION", "0")
    main.build()
    assert captured_configs[-1].enable_insight_narration is False

    # Any other value (e.g. "1") keeps it enabled.
    monkeypatch.setenv("ATWORKS_INSIGHT_NARRATION", "1")
    main.build()
    assert captured_configs[-1].enable_insight_narration is True
