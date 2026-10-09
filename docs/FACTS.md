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

Versions: deepagents 0.7.23, langchain 1.4.4, langgraph 1.2.14. Paths below are relative to site-packages. Offline probes (fake models, no network) live outside the repo at `~/.claude/orchestration/nemogate-spike/runs/D4/`: `task_probe.py` (the `task` boundary, scripted planner and coder) and `tools_probe.py` (tool exposure by backend, summarization, profiles). A real model's `tool_calls` shape is still re-checked live (Token Factory), see the last row.

| Fact | Value | Status | Evidence | Date | Who |
| --- | --- | --- | --- | --- | --- |
| `create_deep_agent` signature (middleware, subagents, backend parameters) | `create_deep_agent(model=None, tools=None, *, system_prompt=None, middleware=(), subagents=None, skills=None, memory=None, permissions=None, backend=None, interrupt_on=None, response_format=None, state_schema=None, context_schema=None, checkpointer=None, store=None, debug=False, name=None, cache=None)`. `model` is a `"provider:name"` string or a `BaseChatModel` instance (pass an instance: a `"openai:..."` string defaults to the Responses API, and `model=None` falls back to `claude-sonnet-4-6` and is deprecated). `checkpointer`, `store`, `context_schema`, `debug`, `name`, `cache` are passed straight to `langchain.agents.create_agent`. Returns a compiled graph with `recursion_limit=9999`. | VERIFIED (source) | `deepagents/graph.py:277-297` (signature), `:329-336` (Responses API note), `:989-1011` (`create_agent(...).with_config(recursion_limit=9_999)`), `:144-152` (default model) | 2026-10-10 | A (spike worker) |
| How a subagent is defined | A dict (`SubAgent` TypedDict) in `subagents=[...]`. Required: `name`, `description`. Optional: `system_prompt` (empty if omitted), `tools`, `model` (str or `BaseChatModel`), `middleware` (list), `interrupt_on`, `skills`, `permissions`, `response_format`, `mode` (`"isolated"` default, or experimental `"fork"`). `create_deep_agent` fills `model` from the main model and `tools` from the main agent's `tools=` argument when absent (filesystem tools come from `FilesystemMiddleware`, not from `tools`). Alternatives: `CompiledSubAgent {name, description, runnable, mode?}` (runnable's state needs a `messages` key) and `AsyncSubAgent` (has `graph_id`). A `general-purpose` subagent is added automatically unless one named that is passed or the harness profile disables it, so the planner's `task` tool lists it next to `coder` and `reviewer`. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/subagents.py:75-196` (SubAgent), `:199-282` (CompiledSubAgent), `deepagents/graph.py:697-787` (defaults filled), `:822-833` (general-purpose auto-add); offline: `runs/D4/task_probe.py` section 6 prints `- general-purpose: ...` and `- coder: writes code` in the `task` description | 2026-10-10 | A (spike worker) |
| Middleware base class and hook names available | `from langchain.agents.middleware import AgentMiddleware` (defined `langchain/agents/middleware/types.py:385`, exported `langchain/agents/middleware/__init__.py:32`). Generic `[StateT, ContextT, ResponseT]`. Hooks, each with an `a`-prefixed async twin: `before_agent(state, runtime) -> dict\|None`, `before_model(state, runtime) -> dict\|None`, `after_model(state, runtime) -> dict\|None`, `wrap_model_call(request: ModelRequest, handler) -> ModelResponse\|AIMessage\|ExtendedModelResponse`, `after_agent(state, runtime) -> dict\|None`, `wrap_tool_call(request: ToolCallRequest, handler) -> ToolMessage\|Command`. Class attributes: `state_schema`, `tools` (extra tools it registers), `name` (property, defaults to class name; used for replace-by-name), `trace_policy`, `transformers`. Decorator forms exist (`@wrap_tool_call`, `@before_model`, ...). `ToolCallRequest` is `langgraph/prebuilt/tool_node.py:133`, importable from `langchain.agents.middleware`. | VERIFIED (source) | `langchain/agents/middleware/types.py:385-760` (hooks at :431 before_agent, :455 before_model, :479 after_model, :503 wrap_model_call, :598 awrap_model_call, :650 after_agent, :674 wrap_tool_call, :756 awrap_tool_call), `langgraph/prebuilt/tool_node.py:133-150` (ToolCallRequest fields `tool_call`, `tool`, `state`, `runtime`) | 2026-10-10 | A (spike worker) |
| Hook used to wrap the `task` tool call | `wrap_tool_call(self, request, handler)`; also implement `awrap_tool_call` (below). It sees the main agent's `task` calls: `request.tool_call` is `{"name": "task", "args": {"description": ..., "subagent_type": ...}, "id": "call_...", "type": "tool_call"}`, `request.tool` is the `StructuredTool`, `request.state`, `request.runtime`. To rewrite the args before the subagent sees them, call `handler(request.override(tool_call={**call, "args": {...}}))`; the subagent received the rewritten text. To block, return a `ToolMessage(content=..., tool_call_id=call["id"], name="task", status="error")` without calling `handler`; the subagent never ran and the planner saw that message. Where middleware given to `create_deep_agent(middleware=[...])` runs: only in the main agent's stack (after Filesystem, SubAgent, Summarization, PatchToolCalls; before prompt-caching and UnsupportedContent). It does NOT run inside declarative subagents: the main probe never logged the coder's `write_file`, while a probe placed in the subagent spec's `middleware` did. Exceptions: a middleware whose `name` equals a default slot (e.g. `SummarizationMiddleware`, `FilesystemMiddleware`) replaces that slot in the general-purpose subagent too, and `mode="fork"` subagents inherit all of it. For the gate: put it in `create_deep_agent(middleware=[...])` for the delegation and return boundaries (the planner), and in each subagent's `middleware` only if inner tool calls must be gated. Composition: first in the list is outermost. A sync-only `wrap_tool_call` fails under `ainvoke` with `NotImplementedError`, so implement both `wrap_tool_call` and `awrap_tool_call`. | VERIFIED (offline run) | `runs/D4/task_probe.py` runs 1-5. Run 1 log: `{"mw":"main","phase":"before","name":"task","args":{"description":"Edit /app.py. RULE: ...","subagent_type":"coder"},"id":"call_task_1","type":"tool_call","tool_obj":"StructuredTool"}`; run 2: `coder saw HumanMessage: [GATE-PINNED] Edit /app.py...`; run 3: `coder invoked? 0 \| planner saw: ['BLOCKED by gate']`; run 5: `sync-only under ainvoke: FAILED NotImplementedError`. Stack printout: `['FilesystemMiddleware','SubAgentMiddleware','SummarizationMiddleware','PatchToolCallsMiddleware','Probe_main','AnthropicPromptCachingMiddleware','UnsupportedContentMiddleware']`. Source: `deepagents/graph.py:959` (user middleware merged into main stack only), `:753-757` (subagent gets spec `middleware`), `:849-850` (GP subagent inherits only default-slot overrides), `langchain/agents/middleware/types.py:683` (first = outermost), `:756-815` (async default raises). A real model's tool_calls shape is re-checked live (Token Factory). | 2026-10-10 | A (spike worker) |
| Hook used to wrap a tool result | The same hook, after `handler(request)` returns. For `task` the returned object is a `Command`, not a `ToolMessage`. Rewrite it by returning a new `Command(update={**res.update, "messages": [new_tool_message]})`; the planner's next model call then saw the rewritten text (`[GATE-RET] CODER REPORT: wrote /app.py`). For other tools (`write_file` etc.) the same hook returned a `ToolMessage` (`Updated file /app.py`). So the gate must handle both `ToolMessage` and `Command` results. The built-in `FilesystemMiddleware.wrap_tool_call` is outermost, so our middleware sees the raw `task` result before large-result eviction (see eviction row). | VERIFIED (offline run) | `runs/D4/task_probe.py` run 2: `planner 2nd call saw ToolMessage: ['[GATE-RET] CODER REPORT: wrote /app.py']`; sub-probe line `{"mw":"sub","phase":"after","name":"write_file","result_type":"ToolMessage","content":"Updated file /app.py"}`; source `langchain/agents/middleware/types.py:674-700` (return type `ToolMessage \| Command`) | 2026-10-10 | A (spike worker) |
| The `task` tool: name | `task` | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/subagents.py:852` (`StructuredTool.from_function(name="task", ...)`); offline: `request.tool_call["name"] == "task"` in every run, planner bound tools `['ls','read_file','write_file','edit_file','delete','glob','grep','task']` | 2026-10-10 | A (spike worker) |
| The `task` tool: payload fields | Exactly two model-supplied fields: `description: str` (the full instructions for the subagent) and `subagent_type: str` (one of the listed subagent names). Unknown keys (for example `prompt`) are rejected with a validation error returned to the model as a tool error. The tool node injects `runtime` alongside. The subagent receives only `[HumanMessage(content=description)]` as its messages (plus its own `system_prompt` as a system message), and the parent state minus the keys `messages, todos, structured_response, skills_metadata, pinned_skills` and private keys. So the text of `description` is the whole delegation boundary. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/subagents.py:404-436` (TaskToolSchema, unknown-key rejection), `:371-379` (`_EXCLUDED_STATE_KEYS`), `:780-782` (`HumanMessage(content=description)`); offline: `coder first-model-call messages: [('SystemMessage','You are coder.'),('HumanMessage','Edit /app.py. RULE: never touch migrations/. Return CHANGED: list.')]` | 2026-10-10 | A (spike worker) |
| The `task` tool: result shape | `Command(update={**subagent_final_state_minus_excluded_and_private_keys, "messages": [ToolMessage(content, tool_call_id=<the task call id>)]})`. `content` is the text of the LAST non-empty `AIMessage` of the subagent (right-stripped), or the JSON of `structured_response` when the subagent has `response_format`. Intermediate subagent messages and tool calls are not returned. Other subagent state keys are merged into the parent, observed: `files`. Observed message: `ToolMessage(name="task", tool_call_id="call_task_1", status="success", content="CODER REPORT: wrote /app.py")`; `Command.update` keys `['files','messages']`. Error paths (unknown `subagent_type`, recursion refusal) return a plain `str` instead. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/subagents.py:696-733` (`_return_command_with_state_update`), `:785-813` (sync `task`; str returns at :787-793), offline `runs/D4/task_probe.py` run 1: `{"phase":"after","name":"task","result_type":"Command","update_keys":["files","messages"],"msgs":[["ToolMessage","CODER REPORT: wrote /app.py","call_task_1","task","success"]]}`. A real model's tool_calls shape is re-checked live (Token Factory). | 2026-10-10 | A (spike worker) |
| Built-in file tool names and argument names | `ls(path)`, `read_file(file_path, offset, limit)`, `write_file(file_path, content)`, `edit_file(file_path, old_string, new_string, replace_all=False)`, `delete(file_path)`, `glob(pattern, path=None)`, `grep(pattern, path=None, glob=None, output_mode="files_with_matches", max_count=None)`, `execute(command, timeout=None)`. Paths must be absolute. Tool order is `ls, read_file, write_file, edit_file, delete, glob, grep` then `execute`. All sit in `FilesystemMiddleware` (the first middleware of main agent and subagents). | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/filesystem.py:1237-1357` (schemas), `:1481-1485` (names and order), `:1894-1906` (tool factories); offline: coder bound tools `['ls','read_file','write_file','edit_file','delete','glob','grep']` on a `StateBackend` | 2026-10-10 | A (spike worker) |
| Does a built-in delete exist? (If yes, drop the custom `delete_file` and point C-003 at it) | YES. The built-in is named `delete`, argument `file_path`; it deletes a file or directory (recursive, `rm -rf` on sandboxes). It is exposed to the model only when the backend overrides `delete` (`StateBackend`, `FilesystemBackend`, `StoreBackend`, `CompositeBackend`, `BaseSandbox` subclasses all do). Custom `delete_file` is not needed; point the constraint at `delete`. Note its tool name is `delete`, not `delete_file`. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/filesystem.py:1300-1303` (DeleteSchema), `:1414-1420` (description), `:2910` (hidden if unsupported), `deepagents/backends/protocol.py:985-1000` (`_supports_delete`), `deepagents/backends/sandbox.py:1960` (`BaseSandbox.delete`, `rm -rf`); offline `runs/D4/tools_probe.py` A and B: `delete` bound for both `StateBackend` and a `BaseSandbox` subclass | 2026-10-10 | A (spike worker) |
| Does a built-in shell `execute` exist, and how is it turned off | Exists: `execute(command, timeout=None)`. It is exposed to the model only when the backend is a `SandboxBackendProtocol` (for `CompositeBackend`, when its default backend is one); on any other backend it is filtered out of the model's tools. A `BaseSandbox` subclass (the Contree backend, if built that way) turns it on for BOTH the planner and the subagents. To turn it off: pass `FilesystemMiddleware(backend=<same backend>, tools=[...])` with a list that omits `"execute"` (`read_file` must be included; `"all"` is the default) in `create_deep_agent(middleware=[...])` for the main agent AND in each subagent's `middleware` list (replaces the default by name). Main-only override left the coder with `execute` and `delete`. Alternative, not run: a `HarnessProfile(excluded_tools=frozenset({"execute"}))`. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/filesystem.py:1579-1598` (`supports_execution`), `:2907-2912` and `:3208-3216` (filter at `wrap_model_call`), `:1771`+`:1800-1812` (`tools=` allowlist, `read_file` required), `deepagents/graph.py:753-760`; offline `runs/D4/tools_probe.py`: A planner tools have no `execute`; B (BaseSandbox) adds `execute`; D main-only override -> coder tools still end `..., 'grep', 'execute'`; E override in subagent spec -> coder tools `['ls','read_file','write_file','edit_file','glob','grep']` | 2026-10-10 | A (spike worker) |
| `SummarizationMiddleware` default trigger, and how to lower it | On by default in the main agent and in every declarative subagent (one `SummarizationMiddleware` each, built by `create_summarization_middleware(model, backend)`). Default trigger: if `model.profile["max_input_tokens"]` is an int, `("fraction", 0.85)` with `keep=("fraction", 0.10)`; otherwise `("tokens", 170000)` with `keep=("messages", 6)`. A `ChatOpenAI(model="nvidia/nemotron-...")` has `profile = None`, so it gets 170000 tokens and 6 messages. To lower: `from deepagents.middleware.summarization import SummarizationMiddleware; SummarizationMiddleware(model=<chat model>, backend=<backend>, trigger=("tokens", 4000), keep=("messages", 6))`, passed in `create_deep_agent(middleware=[...])` (replaces the default in place by the name `SummarizationMiddleware`) and in a subagent's `middleware` list for subagents. `trigger` accepts a tuple, a list (OR) or a dict (AND). The constructor's `trim_tokens_to_summarize` defaults to 4000 (the factory uses None). Summarization makes an LLM call with the `model` you give it, so a low trigger spends credit in live runs. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/middleware/summarization.py:262-299` (`compute_summarization_defaults`), `:1772-1856` (factory), `:523-600` (class, constructor), `:1765` (public alias), `deepagents/graph.py:724,830,912` (installed in subagents, GP, main); offline `runs/D4/tools_probe.py` F: user instance replaced the default in place (stack order unchanged), `lc helper trigger/keep: ('tokens', 4000) ('messages', 6)`; G/H: `profile None` -> `('tokens', 170000)` | 2026-10-10 | A (spike worker) |
| How the backend is passed to the agent | `create_deep_agent(backend=<BackendProtocol instance>)`; default `StateBackend()` (files live in graph state). Factories (callables) were removed in 0.7 and raise `TypeError`. The same instance is given to the main and every subagent's `FilesystemMiddleware`, so a sandbox backend is shared by planner and subagents. Backends in the package: `StateBackend`, `FilesystemBackend`, `LocalShellBackend`, `StoreBackend`, `CompositeBackend`, and `BaseSandbox` (abstract: `execute`, `id`, `upload_files`, `download_files`) for remote sandboxes. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/graph.py:287,653` (param and default), `deepagents/middleware/filesystem.py:1837-1844` (factory TypeError), `deepagents/backends/sandbox.py:1536,2096-2111` (`BaseSandbox` abstract members); offline `runs/D4/tools_probe.py` used a `FakeSandbox(BaseSandbox)` instance | 2026-10-10 | A (spike worker) |
| Large tool results are evicted to a file (affects `task` and `execute` results) | `FilesystemMiddleware.wrap_tool_call` writes any tool result over 20000 tokens (about 80000 chars, `NUM_CHARS_PER_TOKEN = 4`) to `/large_tool_results/...` and replaces the message with a pointer. Excluded from eviction: `ls, glob, grep, read_file, edit_file, write_file, delete`. So `task` and `execute` results are subject to it. This wrapper is outermost, so our middleware sees the raw result first. | VERIFIED (source) | `deepagents/middleware/filesystem.py:1771` (default 20000), `:966` (4 chars per token), `:1626-1634` (`TOOLS_EXCLUDED_FROM_EVICTION`), `:3682-3710` (`wrap_tool_call`) | 2026-10-10 | A (spike worker) |
| Built-in harness profile for Nemotron 3 Ultra (a hidden extra middleware stack) | `deepagents` ships a harness profile keyed per model spec for `nvidia:nvidia/nemotron-3-ultra-550b-a55b` and `nebius:nvidia/Nemotron-3-Ultra-550b-a55b` (and other hosts). When it matches it adds a prompt suffix, a `read_file` description override and 12 extra middleware (`NemotronProgressBudgetMiddleware`, `NemotronPolicyNudgeMiddleware`, `NemotronToolCallShim`, `ToolRetryMiddleware`, `NemotronTextToolCallParser`, `FinalAnswerGuardMiddleware`, ...) that rewrite tool calls, nudge the model and can inject fallback final text. It matches a model STRING spec, or an instance whose reported provider and id form such a key. A `ChatOpenAI(model="nvidia/Nemotron-3-Ultra-550b-a55b", base_url=...)` instance got NO profile (no extra middleware); the same id with the spec string `nebius:...` got all 12. Super and Nano have no profile. Passing `get_model(role)` instances therefore keeps the stack plain. | VERIFIED (source) + VERIFIED (offline run) | `deepagents/profiles/harness/_nvidia_nemotron_3_ultra.py:43-53` (specs), `:1824-1857` (stack and registration), `deepagents/profiles/harness/harness_profiles.py:1319-1364` (lookup); offline `runs/D4/tools_probe.py` H, I: `harness profile extra_middleware: []` for the instance, 12 names for the `nebius:` spec | 2026-10-10 | A (spike worker) |
| Real model `tool_calls` shape for `task` and a nemotron `description` argument | The offline probe used scripted `AIMessage.tool_calls` dicts. Whether Nemotron on Token Factory emits `task` calls with exactly `description` and `subagent_type`, and as valid JSON arguments, is not known. | NOT YET CHECKED | needs live: run a one-subagent deep agent with `get_model("sut")` on a trivial task and print `request.tool_call` from a `wrap_tool_call` probe (same middleware as `runs/D4/task_probe.py`) | | |

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
