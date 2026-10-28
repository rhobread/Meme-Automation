from app import main


def test_celery_smoke_executes_dependency_check(monkeypatch):
    from app.worker import smoke

    expected = {"database": "ok", "redis": "ok", "schema": "0001"}
    monkeypatch.setattr(main, "check_dependencies", lambda: expected)
    assert smoke.apply().get() == expected


def test_celery_smoke_propagates_failure(monkeypatch):
    from app.worker import smoke

    def fail():
        raise RuntimeError("unavailable")
    monkeypatch.setattr(main, "check_dependencies", fail)
    assert smoke.apply().failed()
