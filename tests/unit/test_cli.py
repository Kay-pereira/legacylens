"""CLI: analyze, build and serve wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylenscli.cli import main
from legacylenscli.ir import FactStore

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_web"


def test_analyze_writes_a_health_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "report.json"

    main(
        [
            "analyze",
            "--root",
            str(FIXTURE),
            "--db",
            str(tmp_path / "facts.db"),
            "--output",
            str(output),
            "--top",
            "2",
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["files"] > 0
    assert report["summary"]["entry_points"] == 10
    assert report["status"] in {"ok", "attention", "critical"}
    assert "LegacyLens static report" in capsys.readouterr().out


def test_build_writes_the_documentation_tree(tmp_path: Path) -> None:
    out = tmp_path / "site"

    main(
        [
            "build",
            "--root",
            str(FIXTURE),
            "--db",
            str(tmp_path / "facts.db"),
            "--out",
            str(out),
            "--no-build",
        ]
    )

    assert (out / "docs" / "index.md").is_file()
    assert (out / "mkdocs.yml").is_file()
    assert not (out / "site").exists()


def test_serve_reuses_an_existing_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "facts.db"
    with FactStore(db):
        pass
    calls: list[tuple[Path, int]] = []

    def fake_serve(path: Path, *, port: int, **kwargs: object) -> None:
        calls.append((Path(path), port))

    monkeypatch.setattr("legacylenscli.cli.serve", fake_serve)
    main(["serve", "--db", str(db), "--port", "8123"])

    assert calls == [(db, 8123)]


def test_serve_requires_a_root_or_database() -> None:
    with pytest.raises(SystemExit):
        main(["serve"])
