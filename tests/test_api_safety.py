import asyncio
import sqlite3
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.main import app  # noqa: E402
from app.config import PAPERS_DIR  # noqa: E402
from app.routers import annotations as annotations_router  # noqa: E402
from app.routers import settings as settings_router  # noqa: E402


def test_settings_read_does_not_return_secret_values(monkeypatch):
    values = {
        "translate_provider": "openai",
        "translate_api_key": "translation-secret",
        "mineru_token": "mineru-secret",
    }
    monkeypatch.setattr(settings_router, "get_settings", lambda: dict(values))
    response = settings_router.get_all()
    safe = response["settings"]
    assert safe["translate_api_key"] == ""
    assert safe["mineru_token"] == ""
    assert safe["translate_api_key_configured"] is True
    assert safe["mineru_token_configured"] is True
    assert "translation-secret" not in repr(response)
    assert "mineru-secret" not in repr(response)


def test_empty_secret_fields_preserve_existing_values(monkeypatch):
    values = {
        "translate_provider": "openai",
        "translate_api_key": "keep-this-key",
        "mineru_token": "keep-this-token",
    }

    def fake_set(key, value):
        values[key] = value

    monkeypatch.setattr(settings_router, "get_settings", lambda: dict(values))
    monkeypatch.setattr(settings_router, "set_setting", fake_set)
    safe = settings_router._save_settings({
        "translate_provider": "mock",
        "translate_api_key": "",
        "mineru_token": "",
    })
    assert values["translate_provider"] == "mock"
    assert values["translate_api_key"] == "keep-this-key"
    assert values["mineru_token"] == "keep-this-token"
    assert safe["translate_api_key"] == ""
    assert safe["translate_api_key_configured"] is True


def test_explicit_secret_clear_is_supported(monkeypatch):
    values = {"translate_api_key": "secret", "mineru_token": "token"}

    monkeypatch.setattr(settings_router, "get_settings", lambda: dict(values))
    monkeypatch.setattr(settings_router, "set_setting", lambda key, value: values.__setitem__(key, value))
    result = settings_router._save_settings({
        "clear_translate_api_key": True,
        "translate_api_key": "",
    })
    assert values["translate_api_key"] == ""
    assert result["translate_api_key_configured"] is False
    assert values["mineru_token"] == "token"


def test_static_mount_exposes_only_paper_asset_root():
    mounts = [route for route in app.routes if hasattr(route, "app") and hasattr(route, "path")]
    assert any(route.path == "/files/papers" for route in mounts)
    assert not any(route.path == "/files" for route in mounts)
    paper_mount = next(route for route in mounts if route.path == "/files/papers")
    assert Path(paper_mount.app.directory) == PAPERS_DIR


def test_annotation_cannot_be_updated_through_another_paper_id(monkeypatch):
    monkeypatch.setattr(annotations_router, "row", lambda *_args, **_kwargs: None)
    with pytest.raises(HTTPException) as error:
        annotations_router.update_annotation("paper-b", 10, {"content": "not mine"})
    assert error.value.status_code == 404


def test_backup_contains_consistent_database_snapshot_and_assets(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "library.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker(value) VALUES(?)", ("committed row",))
        connection.commit()
    # Windows 下 with 语句不会真正关闭连接（只结束事务），残留文件锁会阻塞临时目录清理
    connection.close()

    asset_dir = data_dir / "papers" / "p-test" / "assets"
    asset_dir.mkdir(parents=True)
    (asset_dir / "figure.png").write_bytes(b"fake-png-for-backup-test")

    monkeypatch.setattr(settings_router, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings_router, "DB_PATH", db_path)
    response = settings_router.backup()

    async def collect_body():
        return b"".join([chunk async for chunk in response.body_iterator])

    payload = asyncio.run(collect_body())
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert "library.db" in names
        assert "papers/p-test/assets/figure.png" in names
        assert "library.db-wal" not in names
        restored = tmp_path / "restored.db"
        restored.write_bytes(archive.read("library.db"))

    with sqlite3.connect(restored) as connection:
        row = connection.execute("SELECT value FROM marker").fetchone()
    assert row == ("committed row",)
