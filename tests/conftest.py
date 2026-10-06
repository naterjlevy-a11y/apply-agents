import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file and data dir."""
    from apply_agents import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(config, "LOG_PATH", tmp_path / "t.log")
    monkeypatch.setattr(config, "APPLICATIONS_DIR", tmp_path / "Applications")
    monkeypatch.setattr(config, "APPLIED_LOG", str(tmp_path / "applied-log.csv"))
    monkeypatch.setattr(db, "_conn", None)
    yield


@pytest.fixture
def profile(tmp_path):
    from apply_agents.profile import Profile
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "profile.test.yaml"
    text = fixture.read_text(encoding="utf-8").replace("RESUME_PLACEHOLDER", str(resume))
    p = tmp_path / "profile.yaml"
    p.write_text(text, encoding="utf-8")
    return Profile(p)
