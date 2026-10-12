from pathlib import Path

import pytest

from common.env import REPO_ROOT
from ledger import LoadedLedger, ledger_hash, load_ledger, scope_to
from refapp.tasks import load_tasks

YAML = """\
version: 1
app:
  repo_root: /srv
  approval_tool: ask
constraints:
  - id: C-001
    text: "Keep out of vendor/"
    kind: constraint
    source: user
    authority: binding
    scope: [worker]
    origin: declared
    check: {type: path_forbidden, glob: "vendor/**", ops: [write]}
  - id: C-002
    text: "No network"
    kind: constraint
    source: user
    authority: binding
    scope: [worker]
    origin: declared
    check: {type: tool_forbidden, name: fetch}
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ledger.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_ledger_returns_ledger_and_12_hex_hash(tmp_path):
    loaded = load_ledger(write(tmp_path, YAML))
    assert isinstance(loaded, LoadedLedger)
    assert [c.id for c in loaded.ledger.constraints] == ["C-001", "C-002"]
    assert len(loaded.ledger_hash) == 12
    int(loaded.ledger_hash, 16)


def test_hash_ignores_comments_and_layout(tmp_path):
    # Breaks if reformatting the YAML would make every past run incomparable.
    reformatted = "# a comment\n" + YAML.replace("[worker]", "\n      - worker")
    a = load_ledger(write(tmp_path, YAML)).ledger_hash
    b = load_ledger(write(tmp_path, reformatted)).ledger_hash
    assert a == b


def test_hash_changes_when_a_rule_changes(tmp_path):
    a = load_ledger(write(tmp_path, YAML)).ledger_hash
    b = load_ledger(write(tmp_path, YAML.replace("vendor/**", "vendor/*"))).ledger_hash
    assert a != b


def test_hash_matches_ledger_hash_of_the_parsed_ledger(tmp_path):
    loaded = load_ledger(write(tmp_path, YAML))
    assert ledger_hash(loaded.ledger) == loaded.ledger_hash


def test_scope_to_keeps_only_the_target_and_the_app_block(tmp_path):
    full = load_ledger(write(tmp_path, YAML)).ledger
    scoped = scope_to(full, "C-002")
    assert [c.id for c in scoped.constraints] == ["C-002"]
    assert scoped.app == full.app


def test_scope_to_unknown_id_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        scope_to(load_ledger(write(tmp_path, YAML)).ledger, "C-009")


def test_repo_ledger_loads_and_covers_every_reference_task():
    # Breaks if a task names a constraint the ledger lacks (tasks are data plus a ledger entry).
    ledger = load_ledger(REPO_ROOT / "nemogate.yaml").ledger
    ids = {c.id for c in ledger.constraints}
    assert {task.constraint for task in load_tasks()} <= ids
    assert ledger.app.repo_root == "/repo"
    assert all(c.check is not None for c in ledger.constraints)
