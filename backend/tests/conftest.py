import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("TRADESAFE_DB_PATH", str(db_path))
    from backend.store import db

    db.init_db()
    yield
