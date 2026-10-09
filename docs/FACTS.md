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
| SQLite checkpointer import path and constructors | Sync: `from langgraph.checkpoint.sqlite import SqliteSaver`; `SqliteSaver(conn: sqlite3.Connection, *, serde=None)` or the context manager `SqliteSaver.from_conn_string(path_or_":memory:")` (a `@contextmanager` classmethod, so it must be used in `with`; it opens `sqlite3.connect(..., check_same_thread=False)`). Async: `from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver`; `AsyncSqliteSaver(conn: aiosqlite.Connection)` (its `__init__` calls `asyncio.get_running_loop()`, so build it inside a running loop) or `async with AsyncSqliteSaver.from_conn_string(path) as s`. `SqliteSaver` docstring says it "does not scale to multiple threads" | VERIFIED (source) | `langgraph/checkpoint/sqlite/__init__.py:45,85-96,98-125`; `langgraph/checkpoint/sqlite/aio.py:38,118-130,132-146`. Imports and a with-block ran in `runs/D5/fork_offline.py` and `fork_offline2.py` | 2026-10-10 | A (spike worker) |
| Which saver goes with sync vs async invocation | `create_deep_agent(..., checkpointer=saver)` takes any `Checkpointer` (param at `graph.py:292`, passed to `create_agent(checkpointer=...)` at `graph.py:996`). Sync `agent.invoke/stream/get_state_history/update_state` need `SqliteSaver`; async `ainvoke/aget_state_history/aupdate_state` need `AsyncSqliteSaver`. `SqliteSaver` + `ainvoke` raises `NotImplementedError: The SqliteSaver does not support async methods`. `AsyncSqliteSaver` + sync `invoke` called from the loop thread raises `asyncio.InvalidStateError` (sync calls are only allowed from a different thread). Related: a custom `AgentMiddleware` that defines only `wrap_tool_call` fails under `ainvoke` with `NotImplementedError`; define `awrap_tool_call` too | VERIFIED (offline run) | `runs/D5/fork_offline2.py` prints `SqliteSaver + ainvoke ERR: NotImplementedError ...` and `AsyncSqliteSaver + sync invoke ERR: InvalidStateError ...`; source `langgraph/checkpoint/sqlite/__init__.py:585-624`, `langgraph/checkpoint/sqlite/aio.py:160-170`, `langchain/agents/middleware/types.py:823` | 2026-10-10 | A (spike worker) |
| Listing checkpoints | `agent.get_state_history(config, *, filter=None, before=None, limit=None) -> Iterator[StateSnapshot]` (async twin `aget_state_history`). `config = {"configurable": {"thread_id": ...}}`. Newest first. Includes every checkpoint of the thread in the root namespace, including fork branches (all siblings, flat list) | VERIFIED (source) | `langgraph/pregel/main.py:1510-1548` (async at `:1550`). Ran in `runs/D5/fork_offline.py` (6 snapshots for one recorded run) | 2026-10-10 | A (spike worker) |
| What a `StateSnapshot` carries | NamedTuple fields: `values` (dict of channels, e.g. `messages`, `files`), `next` (tuple of node names that run next), `config` (`{"configurable": {"thread_id", "checkpoint_ns", "checkpoint_id"}}`: the checkpoint id is `snap.config["configurable"]["checkpoint_id"]`), `metadata` (`source` seen: `input`, `loop`, `update`, `step`, `parents`, plus `counters_since_delta_snapshot`), `created_at`, `parent_config` (same shape, the parent checkpoint), `tasks` (tuple of `PregelTask(id, name, path, error, interrupts, state, result)`), `interrupts`. A task's `.result` holds the already-computed output of that task from the recorded run | VERIFIED (source) | `langgraph/types.py:711-735`. Printed in `runs/D5/fork_offline.py` (`pre_tools config`, `pre_tools metadata`, `pre_tools tasks`) | 2026-10-10 | A (spike worker) |
| Node names in a Deep Agents graph | Graph nodes: `__start__`, `PatchToolCallsMiddleware.before_agent`, `model`, `tools`, `__end__`. The model node is `"model"` and the tool node (which runs `task`) is `"tools"`; both are added by langchain `create_agent` | VERIFIED (offline run) | `runs/D5/fork_offline.py` prints `graph nodes: ['__start__', 'model', 'tools', 'PatchToolCallsMiddleware.before_agent', '__end__']`; source `langchain/agents/factory.py:1611,1615` | 2026-10-10 | A (spike worker) |
| Which snapshot is the pre-`task` checkpoint | Two useful ones (steps from a run with one `task` call: 0 before_agent, 1 model, 2 tools, 3 model, 4 end). (a) **Just before the model node that emits `task`**: `snap.next == ("model",)` and the last message is a HumanMessage/ToolMessage (step 1 here); resuming here re-calls the model, so spends a model call and is not deterministic. (b) **Just after the AIMessage with the `task` tool_call, before the tools node runs**: `snap.next == ("tools",)` and `snap.values["messages"][-1].tool_calls[0]["name"] == "task"` (step 2 here); resuming here makes no model call. Selection rule, newest-first history: filter `next == ("tools",)` and last message has a tool call named `task` (with several delegations there are several matches: pick by the tool call id or by order); the checkpoint before the model is the chronologically previous snapshot. The `task` tool call args are `description` and `subagent_type` only | VERIFIED (offline run) | `runs/D5/fork_offline.py` and `fork_offline2.py` (`find_pre_task`): output `{'step': 2, 'next': ('tools',), 'last': 'AIMessage', 'last_tc': ['task']}`; `TaskToolSchema` at `deepagents/middleware/subagents.py:404-414` | 2026-10-10 | A (spike worker) |
| `update_state` signature and semantics | `agent.update_state(config, values, as_node=None, task_id=None) -> RunnableConfig` (async `aupdate_state`; also `bulk_update_state`). Writes `values` through node `as_node`'s writers, as if that node had produced them, and saves a **new checkpoint** (new `checkpoint_id`, `metadata.source == "update"`, `metadata.step` = parent step + 1) whose `parent_config` is the checkpoint named in `config`. The original checkpoint is not modified. Returns the config of the new checkpoint (`{"thread_id", "checkpoint_ns", "checkpoint_id"}`). `messages` uses the id-keyed `add_messages` reducer: a message with an existing `id` replaces it, one without appends. `as_node` is inferred when unambiguous (it worked without `as_node` at the pre-tools checkpoint); otherwise raises `InvalidUpdateError("Ambiguous update, specify as_node")`; the node must exist in `agent.nodes`. Use `as_node="model"` to say "the model node produced this AIMessage" so `next` stays `("tools",)` | VERIFIED (offline run) | `langgraph/pregel/main.py:2593-2604,1615,1594,1963-1971`. `runs/D5/fork_offline.py` prints `update_state returned: {... 'checkpoint_id': '...-8003-3c1a34242dce'}`, `forked snapshot: {'step': 3, 'source': 'update', 'next': ('tools',), ...} parent==pre: True`; `fork_offline2.py` prints `update_state w/o as_node OK -> ('tools',)` | 2026-10-10 | A (spike worker) |
| Resuming from an earlier checkpoint | Pass `{"configurable": {"thread_id": T, "checkpoint_id": C}}` and input `None`: `agent.invoke(None, snap.config)` replays forward from that checkpoint and re-executes the pending tasks. `snap.config` and the config returned by `update_state` are directly usable. Resuming the unchanged pre-tools checkpoint re-ran the subagent (new subagent call, same description) | VERIFIED (offline run) | `runs/D5/fork_offline.py` prints `FORK A (unchanged, same thread) SUB_SEEN: ['ORIGINAL: write hello.txt ...']`; `pregel/_loop.py:330` (`is_replaying = CONFIG_KEY_CHECKPOINT_ID in config[CONF]`) | 2026-10-10 | A (spike worker) |
| Does a fork stay on the same thread, and how to make many independent forks | Forking by `checkpoint_id` stays on the **same `thread_id`** and branches the history: each `update_state(pre.config, ...)` adds a sibling child of the same parent checkpoint, and `get_state_history` lists all branches flat (6 children of the pre-tools checkpoint after 1 original + 5 forks). Original checkpoints are untouched. Many forks = call `update_state(pre.config, new_values, as_node="model")` once per variant (or none for the "natural" arm, just `invoke(None, pre.config)`) and `invoke(None, returned_config)` for each; 3 forks run in parallel via `ThreadPoolExecutor` on one shared `SqliteSaver` all succeeded with correct, distinct outputs. A separate thread is possible but is not a copy: `SqliteSaver.copy_thread` is **not implemented** (raises `NotImplementedError`; the sqlite classes do not override the base). A new thread can be seeded with `update_state({"configurable": {"thread_id": new}}, {...}, as_node="model")`, but then you must supply every state channel yourself (including `files`) and the history is not carried over. Recommended: fork on the same thread, keep returned configs per arm | VERIFIED (offline run) | `runs/D5/fork_offline2.py` prints `copy_thread ERR: NotImplementedError`, `parallel forks: ['final: worker saw: FORK0', ...FORK1, FORK2] | distinct fork ids: 3`, `children of pre_tools checkpoint on thread 'rec': 6`; `runs/D5/fork_offline.py` prints `threads in db: [('forkC',), ('rec',)]` and the seeded-thread run; source `langgraph/checkpoint/base/__init__.py:350-366` (base `copy_thread` raises), no override in `langgraph/checkpoint/sqlite/*.py` | 2026-10-10 | A (spike worker) |
| Can we resume just before the tools node executes `task` after rewriting the tool_call args in the last AIMessage | **Yes.** Take the pre-tools snapshot `pre`; build `AIMessage(content=ai.content, tool_calls=[{**tc, "args": {**tc["args"], "description": NEW}}], id=ai.id)` (same `id` so the reducer replaces rather than appends; keep the tool_call `id` so the ToolMessage matches); `cfg = agent.update_state(pre.config, {"messages": [new_ai]}, as_node="model")`; `agent.invoke(None, cfg)`. The subagent received the rewritten `description` as its first HumanMessage, and the original checkpoint still holds the original args. The rewritten AIMessage is also what the parent sees in its history afterwards | VERIFIED (offline run) | `runs/D5/fork_offline.py` prints `forked last tool_call args: {'description': 'MUTATED: write hello.txt', ...} | n_msgs 2`, `FORK B (mutated, same thread) SUB_SEEN: ['MUTATED: write hello.txt']`, `original pre snapshot still has: ORIGINAL: write hello.txt ...`. How `task` builds the subagent input: `deepagents/middleware/subagents.py:785-813` (`HumanMessage(content=description)` at the non-fork branch of `_validate_and_prepare_state`) | 2026-10-10 | A (spike worker) |
| Is the backend part of graph state | **No.** The backend is a plain attribute of the middleware (`FilesystemMiddleware.backend`, and `SubAgentMiddleware`/summarization hold it too), i.e. in the agent's closure, not in a state channel, so it is never serialised into a checkpoint. The graph state keys of a Deep Agent are `messages` and (with `StateBackend`) `files`. Run with `FilesystemBackend` attached: checkpointing and forking worked and the checkpoint blobs contained neither the backend class name nor its root path | VERIFIED (offline run) | `deepagents/middleware/filesystem.py:1837` (`self.backend = ...`), `:1174` (`files` state channel); `runs/D5/fork_offline3.py` prints `state keys: ['messages']` and `'FilesystemBackend' in checkpoint blobs: False | root path in blobs: False` | 2026-10-10 | A (spike worker) |
| Where files live with the default (state) backend, and do they survive a fork | Default backend is `StateBackend()` (`graph.py:653`). Files live in the **`files` state key** (`dict[path, FileData]`, a `DeltaChannel` with a reducer and a snapshot every 50 updates), read and written through Pregel internals (`CONFIG_KEY_READ`/`CONFIG_KEY_SEND`), so they are inside the checkpoint. They are visible in `snap.values["files"]`, and a fork on the same thread inherits the files as of the forked-from checkpoint (a `write_file` before the `task` call was present in the forked snapshot and in the final state). The `task` tool passes the parent state minus `_EXCLUDED_STATE_KEYS` (`messages`, `todos`, `structured_response`, ...; `files` is NOT excluded) to the subagent, and merges the subagent's non-excluded state keys (so its file writes) back via `Command(update=...)` (source-read, not run with a subagent that writes). Seeding a new thread with `update_state` does not carry `files` unless you pass them | VERIFIED (offline run) | `runs/D5/fork_offline2.py` prints `files in pre_tools state: ['/notes.txt']`, `files in fork: ['/notes.txt']`, `files after: ['/notes.txt']`; source `deepagents/backends/state.py:38-120`, `deepagents/middleware/filesystem.py:1174-1175`, `deepagents/middleware/subagents.py:371-379,696-733` | 2026-10-10 | A (spike worker) |
| With a non-state (sandbox/disk) backend, do files survive or rewind on fork | They live **outside** the checkpoint (`state keys: ['messages']`, no `files`), so a fork does not restore them: the external file system keeps whatever the recorded run left there, and a resumed fork re-executes only the nodes after the chosen checkpoint against that live state. Offline proof used `FilesystemBackend` on a temp dir as a stand-in: after deleting `b.txt` and resuming from the checkpoint after the first write, the fork re-created `b.txt`. A Contree sandbox must therefore be snapshotted/forked separately (image fork per arm), see section 3 | VERIFIED (offline run) | `runs/D5/fork_offline3.py` prints `state keys: ['messages'] | files on disk: ['a.txt', 'b.txt']` and `disk after fork-run: ['a.txt', 'b.txt']`. The Contree backend itself was not exercised: needs live: run the fork against a `ContreeSandbox` backend and check whether files written after the fork point are still present | 2026-10-10 | A (spike worker) |
| Where the checkpoint ID is captured at the delegation boundary at run time | Three ways, all offline-verified. (1) **Inside middleware** (`wrap_tool_call` when `request.tool_call["name"] == "task"`): `request.runtime.config["configurable"]["checkpoint_map"][""]` is the id of the checkpoint the `tools` step started from, which equals the pre-tools snapshot id. `configurable["checkpoint_id"]` is `None` there and `checkpoint_ns` is `tools:<task-id>`, so do not use `checkpoint_id`. `langgraph.config.get_config()` returns the same dict. The id string is the full UUID. (2) **While streaming**: `agent.stream(input, cfg, stream_mode="checkpoints")` yields one event per checkpoint with keys `config, metadata, next, parent_config, tasks, values`; take the event with `next == ["tools"]` whose last message calls `task`, id in `event["config"]["configurable"]["checkpoint_id"]`. (3) **After the run**: scan `get_state_history` as above (needs no hook; recommended for the recorder) | VERIFIED (offline run) | `runs/D5/fork_offline2.py` prints `CAPTURED inside wrap_tool_call: ... 'checkpoint_map': "{'': '1f1c420e-790f-63c3-8004-bfe9756fdac2'}", 'checkpoint_id': 'None'` and `pre_tools full checkpoint_id: 1f1c420e-790f-63c3-8004-bfe9756fdac2`; `stream_mode=checkpoints event keys: [...]`; source `langgraph/pregel/_algo.py:917-922`, `langgraph/pregel/main.py:2760` | 2026-10-10 | A (spike worker) |
| Subagent runs are checkpointed too | The subagent graph is invoked inside the `task` tool as `subagent.invoke(state, {"configurable": {"ls_agent_type": "subagent"}})` and inherits the parent's checkpointer, so its steps are saved under the same `thread_id` with `checkpoint_ns = "tools:<task-id>"`. Root-namespace `get_state_history` does not list them; forking is done at the root namespace only. Each re-run of the `task` tool (original run and each fork) adds a new `tools:<task-id>` namespace | VERIFIED (offline run) | `runs/D5/fork_offline.py` prints `namespaces for thread rec: [('',), ('tools:cec2abd7-...',), ('tools:d44c4ae0-...',), ('tools:e8a2abcb-...',)]`; source `deepagents/middleware/subagents.py:812,842` | 2026-10-10 | A (spike worker) |

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
