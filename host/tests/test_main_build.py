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
    monkeypatch.setattr("atworks_host.main.AtworksAgent", lambda **kwargs: None)
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
    monkeypatch.setattr("atworks_host.main.AtworksAgent", lambda **kwargs: None)
    monkeypatch.setattr("atworks_host.main.MockAtworks", lambda *args, **kwargs: type('MockBackend', (), {})())
    monkeypatch.setattr("atworks_host.main.create_app", lambda **kwargs: type('FastAPI', (), {})())
    monkeypatch.setattr("atworks_host.main.Scheduler", lambda *args, **kwargs: type('Scheduler', (), {})())

    # Call build() - this should read ATWORKS_TRUST_OS_CA=1 from .env
    app, backend, scheduler = main.build()

    # With ATWORKS_TRUST_OS_CA=1 in .env, inject_into_ssl MUST be called
    assert calls == [1], f"Expected one truststore.inject_into_ssl() call with ATWORKS_TRUST_OS_CA=1, but got {calls}"
