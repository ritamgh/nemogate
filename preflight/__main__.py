"""`python -m preflight --out FILE [--tries N] [--yes]`: run the C0 probes, propose a safe config.

Live and opt-in. Without NEMOGATE_LIVE=1 it only prints how many model calls it would make. With
it, it also needs --yes. It never writes config.yaml: the proposal goes to --out.
"""

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from common.env import is_live
from common.tokenfactory import list_model_ids
from preflight.probes import ProbeResult
from preflight.run import (
    CALLS_PER_ROLE_PER_TRY,
    ModelFactory,
    planned_calls,
    run_probes,
    variant_models,
)
from preflight.safe_config import Proposal, derive, diff
from schemas.config import load_config

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {text}")
    return value


def _refused_out(out: Path, config: Path) -> str | None:
    """Why `out` may not be written, or None. The proposal never replaces an existing config or
    any file git tracks."""
    target = out.resolve()
    if target in (config.resolve(), DEFAULT_CONFIG.resolve()):
        return "is a config.yaml"
    try:
        tracked = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", target.name],
                cwd=target.parent,
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return None  # no git here, nothing to be tracked by
    return "is a file git tracks" if tracked else None


def _result_json(result: ProbeResult) -> dict:
    return {
        "probe": result.probe,
        "role": result.role.value,
        "response_format": result.response_format,
        "thinking": result.thinking,
        "tries": result.tries,
        "ok": result.ok,
        "passed": result.passed,
        "counts": result.counts,
        "evidence": list(result.evidence),
    }


def _print_results(results: Sequence[ProbeResult]) -> None:
    for r in results:
        setting = f"{r.response_format or '-'}/thinking={'-' if r.thinking is None else r.thinking}"
        counts = " ".join(f"{k}={v}" for k, v in r.counts.items())
        verdict = "ok  " if r.passed else "FAIL"
        print(f"{verdict} {r.probe:<10} {r.role.value:<8} {setting}  {r.ok}/{r.tries}  {counts}")
        for note in r.evidence:
            print(f"       {note}")


def _print_proposal(proposal: Proposal, changes: list[tuple[str, object, object]]) -> None:
    print(
        f"proposed safe config: response_format={proposal.response_format} "
        f"thinking={proposal.thinking} hash={proposal.hash}"
    )
    if not changes:
        print("config.yaml already matches the proposal")
    for name, current, proposed in changes:
        print(f"  differs: {name}: config.yaml has {current}, proposal is {proposed}")
    for warning in proposal.warnings:
        print(f"WARNING: {warning}")


def main(
    argv: Sequence[str] | None = None,
    *,
    model_factory: ModelFactory | None = None,
    list_ids: Callable[[str], list[str]] = list_model_ids,
) -> int:
    """`model_factory` and `list_ids` let tests run the whole flow without a model or a request."""
    parser = argparse.ArgumentParser(prog="python -m preflight", description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="where to write the proposal JSON")
    parser.add_argument("--tries", type=_positive, default=3, help="calls per probe (default 3)")
    parser.add_argument("--yes", action="store_true", help="confirm the planned number of calls")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    refused = _refused_out(args.out, args.config)
    if refused:
        parser.error(f"--out {refused}: write the proposal to a new file and review it")
    config = load_config(args.config)
    n_roles = len(config.models)
    print(
        f"planned: {planned_calls(n_roles, args.tries)} model calls ({n_roles} roles x "
        f"{CALLS_PER_ROLE_PER_TRY} per try x {args.tries} tries) and one free GET /models"
    )
    if not is_live():
        print("NEMOGATE_LIVE is not 1: nothing was run. These calls spend credit.")
        return 1
    if not args.yes:
        print("Nothing was run. Re-run with --yes to make these calls.")
        return 1

    served, list_error = None, None
    try:
        if config.token_factory.base_url is None:
            raise RuntimeError("config.yaml has no token_factory.base_url")
        served = set(list_ids(config.token_factory.base_url))
    except RuntimeError as exc:
        list_error = str(exc)
    with variant_models(args.config) as built:
        results = run_probes(config, args.tries, model_factory or built, served, list_error)

    proposal = derive(config, results)
    changes = diff(config, proposal)
    _print_results(results)
    _print_proposal(proposal, changes)
    document = {
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "tries": args.tries,
        "model_ids": {role.value: model.id for role, model in config.models.items()},
        "served_models_listed": served is not None,
        "proposal": {
            "safe_config": {
                "response_format": proposal.response_format,
                "thinking": proposal.thinking,
                "hash": proposal.hash,
            },
            "differs_from_config": [
                {"field": name, "config": current, "proposed": proposed}
                for name, current, proposed in changes
            ],
            "per_role": proposal.per_role,
            "warnings": list(proposal.warnings),
        },
        "results": [_result_json(r) for r in results],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 1 if proposal.warnings or proposal.hash is None else 0


if __name__ == "__main__":
    raise SystemExit(main())
