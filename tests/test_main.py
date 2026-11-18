import os
import time

from evo.__main__ import frontend_stale


def make_frontend(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("x")
    (tmp_path / "package.json").write_text("{}")
    return tmp_path


def test_missing_build_is_stale(tmp_path):
    assert frontend_stale(make_frontend(tmp_path))


def test_build_newer_than_sources_is_fresh_until_a_source_changes(tmp_path):
    frontend = make_frontend(tmp_path)
    (frontend / "dist").mkdir()
    built = frontend / "dist" / "index.html"
    built.write_text("<html>")
    past = time.time() - 60
    for path in (frontend / "src" / "App.tsx", frontend / "package.json"):
        os.utime(path, (past, past))
    assert not frontend_stale(frontend)
    (frontend / "src" / "App.tsx").write_text("changed")
    os.utime(frontend / "src" / "App.tsx", (time.time() + 5, time.time() + 5))
    assert frontend_stale(frontend)
