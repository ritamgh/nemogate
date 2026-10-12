"""Load nemogate.yaml, fingerprint it, and cut it down to one run's target constraint."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from schemas.ledger import Ledger, load_ledger_yaml


@dataclass(frozen=True)
class LoadedLedger:
    ledger: Ledger
    ledger_hash: str


def ledger_hash(ledger: Ledger) -> str:
    """12 hex of sha256 over the validated ledger as compact sorted JSON (comments don't count)."""
    text = json.dumps(ledger.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def load_ledger(path: str | Path) -> LoadedLedger:
    ledger = load_ledger_yaml(Path(path).read_text(encoding="utf-8"))
    return LoadedLedger(ledger, ledger_hash(ledger))


def scope_to(ledger: Ledger, constraint_id: str) -> Ledger:
    """One run's ledger: only its target constraint, keeping the app block."""
    return ledger.model_copy(update={"constraints": (ledger.get(constraint_id),)})
