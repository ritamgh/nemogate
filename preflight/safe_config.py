"""Derive the safe config from probe results and compare it with `config.yaml`.

Rules (docs/SPEC.md, C0):
- `response_format`: `json_schema` if it round-trips valid JSON for every role with thinking off,
  else `json_object` if that does, else none.
- `thinking`: off whenever a response format works with it off. On only as a last resort: no
  format works with thinking off, and `json_schema` works with it on for every role in all of
  the thinking probe, the escape probe and native tool calls, each run with thinking on.
Nothing here writes `config.yaml`; the caller writes the proposal elsewhere.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from common.llm import safe_config_hash
from preflight.probes import RESPONSE_FORMATS, ProbeResult, ResponseFormat
from schemas.config import AppConfig, SafeConfig
from schemas.enums import Role


@dataclass(frozen=True)
class Proposal:
    response_format: ResponseFormat | None
    thinking: bool | None
    hash: str | None  # None until both settings are decided
    per_role: dict[str, dict[str, bool]]  # role -> check name -> passed
    warnings: tuple[str, ...]


def _passed(results: Iterable[ProbeResult], probe: str, role: Role, fmt, thinking) -> bool:
    return any(
        r.passed
        for r in results
        if (r.probe, r.role, r.response_format, r.thinking) == (probe, role, fmt, thinking)
    )


def derive(config: AppConfig, results: Sequence[ProbeResult]) -> Proposal:
    roles = list(config.models)

    def all_roles(probe: str, fmt: ResponseFormat | None, thinking: bool | None) -> bool:
        return all(_passed(results, probe, role, fmt, thinking) for role in roles)

    per_role = {
        role.value: {
            **{
                f"{fmt}_thinking_off": _passed(results, "escape", role, fmt, False)
                for fmt in RESPONSE_FORMATS
            },
            "json_schema_thinking_on": _passed(results, "thinking", role, "json_schema", True),
            "escape_json_schema_thinking_on": _passed(results, "escape", role, "json_schema", True),
            "tool_calls_native_thinking_off": _passed(
                results, "tool_call", role, "json_schema", False
            ),
            "tool_calls_native_thinking_on": _passed(
                results, "tool_call", role, "json_schema", True
            ),
            "model_id_served": _passed(results, "model_ids", role, None, None),
        }
        for role in roles
    }

    warnings: list[str] = []
    response_format = next((f for f in RESPONSE_FORMATS if all_roles("escape", f, False)), None)
    thinking: bool | None = None
    if response_format is not None:
        thinking = False
    else:
        # Thinking on changes what the model sends, so every property the agents rely on is
        # proven again with it on; thinking-off results say nothing about it.
        unproven = [
            f"{role_name} {what} with thinking on"
            for role_name, checks in per_role.items()
            for key, what in (
                ("json_schema_thinking_on", "structured output"),
                ("escape_json_schema_thinking_on", "escape"),
                ("tool_calls_native_thinking_on", "tool calls"),
            )
            if not checks[key]
        ]
        if unproven:
            warnings.append(
                "no response format works with thinking off, and thinking on is not proven for "
                f"every role ({', '.join(unproven)}): there is no safe config"
            )
        else:
            response_format, thinking = "json_schema", True
            warnings.append("no response format works with thinking off; proposing thinking on")
    tool_key = f"tool_calls_native_thinking_{'on' if thinking else 'off'}"
    for role_name, checks in per_role.items():
        if not checks[tool_key]:
            warnings.append(f"{role_name}: tool calls did not all come back native")
        if not checks["model_id_served"]:
            warnings.append(f"{role_name}: the configured model id is not confirmed as served")

    digest = None
    if response_format is not None and thinking is not None:
        proposed = config.model_copy(
            update={"safe_config": SafeConfig(response_format=response_format, thinking=thinking)}
        )
        digest = safe_config_hash(proposed)
    return Proposal(response_format, thinking, digest, per_role, tuple(warnings))


def diff(config: AppConfig, proposal: Proposal) -> list[tuple[str, object, object]]:
    """`(field, config.yaml value, proposed value)` for each safe-config field that differs."""
    current = config.safe_config
    pairs = [
        ("response_format", current.response_format, proposal.response_format),
        ("thinking", current.thinking, proposal.thinking),
        ("hash", current.hash, proposal.hash),
    ]
    return [pair for pair in pairs if pair[1] != pair[2]]
