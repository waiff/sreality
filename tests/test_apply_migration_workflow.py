"""apply_migration.yml is a lane that runs repo SQL against the database with
repo secrets, so its input guard is a security boundary — pin it, together with
the receipt script's mapping, hermetically."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import apply_migration_receipt as receipt

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "apply_migration.yml"


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_dispatch_only_with_the_three_inputs() -> None:
    doc = _doc()
    triggers = doc.get("on") or doc.get(True)
    assert list(triggers) == ["workflow_dispatch"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"file", "dry_run", "confirm"}
    assert inputs["dry_run"]["default"] is True
    assert doc["concurrency"]["group"] == "apply-migration"
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_inputs_never_interpolated_into_a_shell_script() -> None:
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if "${{ inputs." not in line:
            continue
        assert re.match(r"^\s+[A-Z_]+: \$\{\{ inputs\.\w+ \}\}$", line) or line.strip().startswith("if:"), line


def _validation_script() -> str:
    doc = _doc()
    steps = doc["jobs"]["apply"]["steps"]
    step = next(s for s in steps if s.get("name") == "Validate the filename")
    return step["run"]


@pytest.mark.parametrize(
    "name",
    ["../496_x.sql", "496 evil.sql", "496_x.SQL", "496_x.sql.sql", "496_x;rm.sql", "x496_a.sql", "496_a.b.sql"],
)
def test_filename_guard_rejects(tmp_path: Path, name: str) -> None:
    assert _run_guard(tmp_path, name) != 0


def test_filename_guard_accepts_an_existing_migration(tmp_path: Path) -> None:
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "496_images_rendition_provenance.sql").write_text("select 1;")
    assert _run_guard(tmp_path, "496_images_rendition_provenance.sql") == 0
    assert "MIGRATION_FILE=496_images_rendition_provenance.sql" in (tmp_path / "env").read_text()


def test_filename_guard_rejects_a_missing_file(tmp_path: Path) -> None:
    (tmp_path / "migrations").mkdir()
    assert _run_guard(tmp_path, "496_not_there.sql") != 0


def _run_guard(tmp_path: Path, name: str) -> int:
    env_file = tmp_path / "env"
    env_file.write_text("")
    proc = subprocess.run(
        ["bash", "-c", _validation_script()],
        cwd=tmp_path,
        env={**os.environ, "FILE": name, "GITHUB_ENV": str(env_file)},
        capture_output=True,
        text=True,
    )
    return proc.returncode


class _Cursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Conn:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql: str, params: dict | None = None) -> _Cursor:
        self.calls.append((sql, params or {}))
        return _Cursor(self.rows)


def test_probe_sends_every_safe_object_and_reports_presence() -> None:
    sql = (
        "alter table images add column if not exists rendition text;\n"
        "create or replace view images_public as select 1;\n"
        "create index concurrently if not exists images_x_idx on images (id);\n"
    )
    objects = receipt.parse_objects(sql)
    conn = _Conn([("column", "images.rendition", True), ("relation", "images_public", True), ("relation", "images_x_idx", False)])
    out = receipt.probe(conn, objects)
    assert conn.calls[0][1]["kinds"] == [o.kind for o in objects]
    assert conn.calls[0][1]["idents"] == [o.ident for o in objects]
    assert [present for _, _, present in out] == [True, True, False]


def test_statement_count_ignores_comments_and_trailing_whitespace() -> None:
    sql = "-- header; with a semicolon\nset lock_timeout = '5s';\n/* c; */\nselect 1;\n\n"
    assert receipt.statement_count(sql) == 2
