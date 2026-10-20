from __future__ import annotations

import pytest

from apigrunn_archiver import archiver as archiver_module
from apigrunn_archiver import cli
from apigrunn_archiver.fetcher import build_client

from .conftest import fixture_bytes


@pytest.fixture(autouse=True)
def offline(monkeypatch, site):
    def fake_client(**kwargs):
        kwargs["transport"] = site.transport
        return build_client(**kwargs)

    monkeypatch.setattr(archiver_module, "build_client", fake_client)


def test_run_runs_show(db_path, capsys):
    assert cli.main(["--db", str(db_path), "run"]) == 0
    assert "8 changed" in capsys.readouterr().out

    assert cli.main(["--db", str(db_path), "run"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db_path), "runs"]) == 0
    out = capsys.readouterr().out
    assert out.count(" ok ") == 2

    assert cli.main(["--db", str(db_path), "show", "tech"]) == 0
    assert capsys.readouterr().out == fixture_bytes("tech").decode("utf-8")


def test_run_exits_nonzero_when_a_page_fails(db_path, site, capsys):
    site.gone.add("tech")
    assert cli.main(["--db", str(db_path), "run"]) == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_show_missing_snapshot_exits_2(db_path, capsys):
    assert cli.main(["--db", str(db_path), "show", "tech"]) == 2
    assert "ARCHIVER-E400" in capsys.readouterr().err


def test_history_diff_verify_export(db_path, site, tmp_path, capsys):
    db = ["--db", str(db_path)]
    cli.main([*db, "run"])
    site.override["tech"] = b"<p>v2</p>\n"
    cli.main([*db, "run"])
    capsys.readouterr()

    assert cli.main([*db, "history", "tech"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[3] for line in out] == ["first", "changed"]

    assert cli.main([*db, "diff", "tech", "1", "2"]) == 1
    assert "+<p>v2</p>" in capsys.readouterr().out
    assert cli.main([*db, "diff", "business", "1", "2"]) == 0

    assert cli.main([*db, "verify"]) == 0
    assert "ok — 9 blobs verified" in capsys.readouterr().out

    assert cli.main([*db, "export", str(tmp_path / "out"), "--all"]) == 0
    assert "exported 2 run(s), 16 files" in capsys.readouterr().out
