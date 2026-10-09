# FACTS.md

Verified facts about the libraries, platform and models this project depends on. Filled in during the day-0 spike, then kept current.

## How to use this file

- **Trust only what is marked VERIFIED.** That means someone ran it or read the installed source, and recorded how.
- Statuses: `VERIFIED`, `UNVERIFIED` (from docs or a blog, not run by us), `NOT YET CHECKED`.
- Every VERIFIED row needs **evidence**: a command and its output, or a file and line in the installed package, plus the date and who checked.
- Never put a key, token or secret in this file. Record the name of an environment variable, not its value.
- If a fact you need is missing or not VERIFIED, do not guess. Read the installed package source (`uv run python -c "import <pkg>; print(<pkg>.__file__)"`), verify it, and add it here. If you cannot verify it, stop and ask.
- When a pinned version changes, re-check every row in that package's section.

Row format used in every table: **Fact | Value | Status | Evidence | Date | Who**

## Day-0 spike, mapped to sections

| Spike item | Fills section |
| --- | --- |
| Shared project, Sandboxes access, keys | 2, 3 |
| `GET /v1/models`, exact Nano, Super and Ultra IDs | 2 |
| Hello-world Deep Agent with one subagent in ContreeSandbox on Token Factory | 3, 4 |
| LangGraph checkpoint and Contree image: one fork from each | 3, 5 |
| Built-in tool names and the shape of the `task` call payload | 4, 7 |
| Pin versions, commit the lockfile | 1 |
| Serving probes and the cost probe (C0) | 6, 8 |

## 1. Environment

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| Python version | 3.12.13 (`.python-version` pins 3.12) | VERIFIED | `uv run python --version` | 2026-10-10 | A |
| `uv` version | 0.11.17 | VERIFIED | `uv --version` | 2026-10-10 | A |
| OS, each machine | A: Arch Linux, kernel 7.2.8-arch1-2. B: not yet recorded | VERIFIED (A only) | `/etc/os-release`, `uname -r` | 2026-10-10 | A |
| Hash of `uv.lock` | Not a fixed fact: every run record computes `lockfile_hash` itself. First 16 hex of sha256 at commit 4145d51: `c55889feba4a62e2` | VERIFIED | `sha256sum uv.lock` | 2026-10-10 | A |

Pinned packages (exact versions, from the lockfile; `uv pip list` on 2026-10-10):

| Package | Pinned version | Notes |
| --- | --- | --- |
| `deepagents` | 0.7.23 | |
| `langgraph` | 1.2.14 | `langgraph-checkpoint` 4.2.0 comes with it |
| LangGraph SQLite checkpointer package | `langgraph-checkpoint-sqlite` 3.1.1 | |
| `langchain-openai` | 1.7.0 | Pulls `langchain-core` 1.6.9, `openai` 3.27.0 |
| Contree SDK: `contree-sdk` | 0.3.6 | Pinned exactly (`==`) to the version the Python quickstart names |
| `pydantic` | 2.14.0 | |
| `pytest` | 9.1.1 | |

## 2. Token Factory

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| Base URL | | NOT YET CHECKED | | | |
| Env var that holds the key (name only) | `NEBIUS_API_KEY`; the same key serves Sandboxes | UNVERIFIED | docs.tokenfactory.nebius.com/sandboxes/start/set-up-access | 2026-10-09 | A |
| Nano model ID, from `GET /v1/models` | | NOT YET CHECKED | | | |
| Super model ID | | NOT YET CHECKED | | | |
| Ultra model ID | | NOT YET CHECKED | | | |
| Context window, each model | | NOT YET CHECKED | | | |
| Input price per 1M tokens, each model, and the date copied | | NOT YET CHECKED | | | |
| Output price per 1M tokens, each model | | NOT YET CHECKED | | | |
| `json_schema` supported | | NOT YET CHECKED | | | |
| `json_object` supported | | NOT YET CHECKED | | | |
| Tool calls come back as native `tool_calls` | | NOT YET CHECKED | | | |
| How thinking mode is switched on or off (parameter, default) | | NOT YET CHECKED | | | |
| Is `seed` honoured | | NOT YET CHECKED | | | |
| Rate limits | | NOT YET CHECKED | | | |
| Credits: total, per person, whether they pool into the shared project | | NOT YET CHECKED | | | |

## 3. Sandboxes and Contree

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| Sandboxes access granted, per person and per project | Beta: access must be requested at tokenfactory.nebius.com/sandboxes/about | UNVERIFIED | docs.tokenfactory.nebius.com/sandboxes/overview | 2026-10-09 | A |
| SDK credentials | Env vars `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID`, read when `Contree()` is constructed. No constructor auth params are documented. `NEBIUS_AI_PROJECT` (CLI only) does not replace `NEBIUS_PROJECT_ID`. | UNVERIFIED | docs.tokenfactory.nebius.com/sandboxes/start/set-up-access and python-quickstart | 2026-10-09 | A |
| SDK import path | `from contree_sdk import Contree`; async API, `client.images.use(...)` | UNVERIFIED | docs.tokenfactory.nebius.com/sandboxes/start/python-quickstart | 2026-10-09 | A |
| Import path of the Deep Agents backend (docs mention `contree_sdk.langchain.sandbox`) | | UNVERIFIED | | | |
| How to create a sandbox | | NOT YET CHECKED | | | |
| How to run a command in it | | NOT YET CHECKED | | | |
| How to get the image or checkpoint ID after a run | | NOT YET CHECKED | | | |
| How to fork from an image | | NOT YET CHECKED | | | |
| How to roll back to an image | | NOT YET CHECKED | | | |
| Time per fork | | NOT YET CHECKED | | | |
| Network access from inside the sandbox | | NOT YET CHECKED | | | |
| CPU, memory and time limits | | NOT YET CHECKED | | | |
| Data restrictions (no sensitive data per the docs) | | UNVERIFIED | | | |

## 4. Deep Agents

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| `create_deep_agent` signature (middleware, subagents, backend parameters) | | NOT YET CHECKED | | | |
| How a subagent is defined | | NOT YET CHECKED | | | |
| Middleware base class and hook names available | | NOT YET CHECKED | | | |
| Hook used to wrap the `task` tool call | | NOT YET CHECKED | | | |
| Hook used to wrap a tool result | | NOT YET CHECKED | | | |
| The `task` tool: name | | NOT YET CHECKED | | | |
| The `task` tool: payload fields | | NOT YET CHECKED | | | |
| The `task` tool: result shape | | NOT YET CHECKED | | | |
| Built-in file tool names and argument names | | NOT YET CHECKED | | | |
| Does a built-in delete exist? (If yes, drop the custom `delete_file` and point C-003 at it) | | NOT YET CHECKED | | | |
| Does a built-in shell `execute` exist, and how is it turned off | | NOT YET CHECKED | | | |
| `SummarizationMiddleware` default trigger, and how to lower it | | NOT YET CHECKED | | | |
| How the backend is passed to the agent | | NOT YET CHECKED | | | |

## 5. LangGraph checkpoints and forking

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| SQLite checkpointer import path | | NOT YET CHECKED | | | |
| How to list checkpoint IDs (docs mention `get_state_history`) | | UNVERIFIED | | | |
| How to fork from a checkpoint (docs mention `update_state`) | | UNVERIFIED | | | |
| Can we resume from a checkpoint just before the node that makes the `task` call | | NOT YET CHECKED | | | |
| Is the state serialisable with the sandbox backend attached | | NOT YET CHECKED | | | |
| Where the checkpoint ID is captured at a delegation boundary | | NOT YET CHECKED | | | |

## 6. C0 probe results

One row per probe per configuration. "Config" means model, `response_format`, and thinking on or off.

| Probe | Config | Result | Notes |
| --- | --- | --- | --- |
| Escape round-trip (`\n`, `\"`, `\\`) under `json_schema` | | | |
| Escape round-trip under `json_object` | | | |
| Forced tool call returns native `tool_calls`, not text | | | |
| Thinking on plus structured output returns non-empty, parseable content | | | |
| Model IDs resolve via `GET /v1/models` | | | |

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| Safe config chosen | | NOT YET CHECKED | | | |
| Hash of the safe config | | NOT YET CHECKED | | | |

## 7. Reference app

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| Seed repo commit SHA | | NOT YET CHECKED | | | |
| Tool names exactly as the agents see them | | NOT YET CHECKED | | | |
| Custom tools: `run_tests`, `delete_file`, `request_approval`, `http_get` | | NOT YET CHECKED | | | |
| How `request_approval` is auto-answered | | NOT YET CHECKED | | | |
| Typical agent steps per task | | NOT YET CHECKED | | | |
| Typical tokens per run, by role | | NOT YET CHECKED | | | |

## 8. Cost measurements

From the C0 cost probe. The budget-check script derives the run count per arm from this table.

| Task | Nano tokens | Super tokens | Ultra tokens | Cost per run (USD) | Measured on |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| Credit remaining at last check | | NOT YET CHECKED | | | |
| Runs per arm the budget allows | | NOT YET CHECKED | | | |

## 9. Gotchas log

Add a row whenever something surprises you. Newest first.

| Date | What surprised us | Workaround | Who |
| --- | --- | --- | --- |
| | | | |

## 10. Open questions

| Question | Owner | Needed by |
| --- | --- | --- |
| | | |
