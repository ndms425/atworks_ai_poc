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
