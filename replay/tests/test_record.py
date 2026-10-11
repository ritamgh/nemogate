"""replay/record.py: run ids, git checks, hashes, scoped ledger and run-folder files."""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from replay import record
from schemas.config import load_config
from schemas.enums import Arm, Authority, Operator, Role
from schemas.ledger import Constraint, Ledger, ToolForbidden, load_ledger_yaml
from schemas.run import FsChange, RunConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with two commits; tag v1 is the first one, HEAD the second."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "first")
    _git(path, "tag", "v1")
    (path / "a.txt").write_text("two\n")
    _git(path, "commit", "-q", "-am", "second")
    return path


def _constraint(cid: str = "C-001", text: str = "Never run rm.") -> Constraint:
    return Constraint(
        id=cid,
        text=text,
        kind="constraint",
        source="user",
        authority="binding",
        scope=("coder",),
        origin="declared",
        check=ToolForbidden(name="rm"),
    )


def _ledger(*constraints: Constraint) -> Ledger:
    return Ledger(version=1, constraints=constraints)


# 1. run ids


def test_new_run_id_has_the_documented_shape_and_is_unique():
    first, second = record.new_run_id(), record.new_run_id()
    assert re.fullmatch(r"r-\d{8}T\d{6}-[0-9a-f]{6}", first)
    assert first != second


# 2. git


def test_git_sha_resolves_head_and_a_tag_to_full_commit_shas(repo: Path):
    head, first = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "v1")
    assert head != first
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    assert record.git_sha("HEAD", repo) == head
    assert record.git_sha("main", repo) == head
    assert record.git_sha("v1", repo) == first


def test_git_sha_unknown_ref_is_a_value_error(repo: Path):
    with pytest.raises(ValueError, match="no-such-ref"):
        record.git_sha("no-such-ref", repo)


def test_git_sha_outside_a_repo_is_a_value_error(tmp_path: Path):
    with pytest.raises(ValueError):
        record.git_sha("HEAD", tmp_path)


def test_require_checkout_at_head_returns_the_head_sha(repo: Path):
    head = _git(repo, "rev-parse", "HEAD")
    assert record.require_checkout_at("main", repo) == head
    assert record.require_checkout_at("HEAD", repo) == head


def test_require_checkout_at_another_ref_is_a_value_error(repo: Path):
    with pytest.raises(ValueError, match="v1"):
        record.require_checkout_at("v1", repo)


def test_require_checkout_refuses_modified_tracked_file_unless_allowed(repo: Path):
    (repo / "a.txt").write_text("edited\n")
    with pytest.raises(ValueError, match="a.txt"):
        record.require_checkout_at("HEAD", repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert record.require_checkout_at("HEAD", repo, allow_dirty=True) == head


def test_untracked_file_that_is_not_ignored_is_dirty(repo: Path):
    # Changed from "untracked file alone is not dirty": the run executes this checkout, so a
    # new, unignored source file is code the recorded repo_sha does not describe.
    (repo / "scratch.txt").write_text("x\n")
    with pytest.raises(ValueError, match="scratch.txt"):
        record.require_checkout_at("HEAD", repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert record.require_checkout_at("HEAD", repo, allow_dirty=True) == head


def test_untracked_file_in_a_new_directory_is_dirty(repo: Path):
    (repo / "newpkg").mkdir()
    (repo / "newpkg" / "mod.py").write_text("x = 1\n")
    with pytest.raises(ValueError, match="newpkg"):
        record.require_checkout_at("HEAD", repo)


def test_ignored_files_are_not_dirty(repo: Path):
    (repo / ".gitignore").write_text("runs/\n*.sqlite\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    (repo / "runs").mkdir()
    (repo / "runs" / "r-1.json").write_text("{}\n")
    (repo / "cache.sqlite").write_text("x")
    assert record.require_checkout_at("HEAD", repo) == _git(repo, "rev-parse", "HEAD")


def test_staged_change_counts_as_dirty(repo: Path):
    (repo / "a.txt").write_text("staged\n")
    _git(repo, "add", "a.txt")
    with pytest.raises(ValueError):
        record.require_checkout_at("HEAD", repo)


# 3. lockfile hash


def test_lockfile_hash_is_the_first_12_hex_of_sha256_of_the_bytes(tmp_path: Path):
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"version = 1\n\xff\x00lock\n")
    expected = hashlib.sha256(b"version = 1\n\xff\x00lock\n").hexdigest()[:12]
    assert len(expected) == 12
    assert record.lockfile_hash(lock) == expected


def test_lockfile_hash_default_is_the_repo_lockfile():
    expected = hashlib.sha256((REPO_ROOT / "uv.lock").read_bytes()).hexdigest()[:12]
    assert record.lockfile_hash() == expected


# 4. model ids


def test_model_ids_returns_every_roles_configured_id():
    config = load_config(REPO_ROOT / "config.yaml")
    ids = record.model_ids(config)
    assert set(ids) == set(Role)
    for role in Role:
        assert ids[role] == config.models[role].id
        assert ids[role]


def test_model_ids_rejects_a_role_without_an_id():
    config = load_config(REPO_ROOT / "config.yaml")
    models = dict(config.models)
    models[Role.sut] = models[Role.sut].model_copy(update={"id": None})
    broken = config.model_copy(update={"models": models})
    with pytest.raises(ValueError, match="sut"):
        record.model_ids(broken)


# 5. scoped ledger and ledger hash


def test_scoped_ledger_keeps_exactly_the_requested_entry():
    ledger = _ledger(_constraint("C-001"), _constraint("C-002", "Other."), _constraint("C-003"))
    scoped = record.scoped_ledger(ledger, "C-002")
    assert [c.id for c in scoped.constraints] == ["C-002"]
    assert scoped.constraints[0].text == "Other."
    assert scoped.version == 1


def test_scoped_ledger_unknown_id_is_a_key_error():
    with pytest.raises(KeyError):
        record.scoped_ledger(_ledger(_constraint("C-001")), "C-009")


def test_ledger_hash_is_12_hex_and_stable_for_equal_ledgers():
    a, b = _ledger(_constraint()), _ledger(_constraint())
    assert re.fullmatch(r"[0-9a-f]{12}", record.ledger_hash(a))
    assert record.ledger_hash(a) == record.ledger_hash(b)


def test_ledger_hash_changes_when_any_field_changes():
    base = _ledger(_constraint())
    variants = [
        _ledger(_constraint(text="Never run rm -rf.")),
        _ledger(_constraint(cid="C-002")),
        _ledger(_constraint().model_copy(update={"authority": Authority.advisory})),
        _ledger(_constraint().model_copy(update={"check": ToolForbidden(name="mv")})),
        _ledger(_constraint(), _constraint("C-002")),
    ]
    hashes = {record.ledger_hash(v) for v in variants} | {record.ledger_hash(base)}
    assert len(hashes) == len(variants) + 1


def test_ledger_hash_matches_a_hand_written_compact_sorted_json():
    ledger = _ledger(_constraint())
    literal = (
        '{"constraints":[{"authority":"binding","check":{"name":"rm","type":"tool_forbidden"},'
        '"id":"C-001","kind":"constraint","origin":"declared","scope":["coder"],'
        '"source":"user","text":"Never run rm."}],"version":1}'
    )
    expected = hashlib.sha256(literal.encode()).hexdigest()[:12]
    assert record.ledger_hash(ledger) == expected


# 6. files


def test_write_ledger_round_trips_through_load_ledger_yaml(tmp_path: Path):
    ledger = _ledger(
        _constraint("C-001"),
        _constraint("C-002", "Other.").model_copy(update={"check": None}),
        _constraint("C-003").model_copy(update={"scope": ("coder", "planner")}),
    )
    path = tmp_path / "sub" / "ledger.yaml"
    record.write_ledger(ledger, path)
    loaded = load_ledger_yaml(path.read_text())
    assert loaded == ledger
    assert isinstance(loaded.constraints, tuple)
    assert record.ledger_hash(loaded) == record.ledger_hash(ledger)


@dataclass(frozen=True)
class _Task:
    id: str
    constraint: str
    boundary: str
    user_message: str


def test_make_run_config_takes_target_from_the_task_and_stores_paths_as_strings(tmp_path: Path):
    task = _Task("task-a", "C-004", "delegation:planner->coder", "do it")
    run_dir = tmp_path / "runs" / "r-1"
    cfg = record.make_run_config(
        run_id="r-1",
        task=task,
        arm=Arm.mutated,
        operator=Operator.drop,
        run_dir=run_dir,
        ledger_path=run_dir / "ledger.yaml",
        seed=3,
        repo_ref="main",
    )
    assert isinstance(cfg, RunConfig)
    assert cfg.run_id == "r-1"
    assert cfg.task_id == "task-a"
    assert cfg.arm == Arm.mutated
    assert cfg.operator == Operator.drop
    assert cfg.target_constraint == "C-004"
    assert cfg.target_boundary == "delegation:planner->coder"
    assert cfg.trace_path == str(run_dir / "trace.jsonl")
    assert cfg.ledger_path == str(run_dir / "ledger.yaml")
    assert cfg.seed == 3
    assert cfg.repo_ref == "main"


def test_write_json_writes_one_model_pretty_with_trailing_newline(tmp_path: Path):
    path = tmp_path / "deep" / "dir" / "change.json"
    record.write_json(path, FsChange(path="a/b.py", op="added"))
    assert path.read_text() == '{\n  "path": "a/b.py",\n  "op": "added"\n}\n'


def test_write_json_writes_a_list_of_models(tmp_path: Path):
    path = tmp_path / "fs_diff.json"
    record.write_json(path, [FsChange(path="a", op="added"), FsChange(path="b", op="deleted")])
    assert json.loads(path.read_text()) == [
        {"path": "a", "op": "added"},
        {"path": "b", "op": "deleted"},
    ]
    assert path.read_text().endswith("]\n")


def test_write_json_empty_list_is_an_empty_json_array(tmp_path: Path):
    path = tmp_path / "v.json"
    record.write_json(path, [])
    assert path.read_text() == "[]\n"
