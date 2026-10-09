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
| SDK credentials | `Contree()` uses `IAMAuth` by default. Its `token` field defaults to the string `"NEBIUS_API_KEY"` and `project_id` to `"NEBIUS_PROJECT_ID"`: each is treated as an env var NAME and replaced by `os.environ[name]` when that variable exists, at construction. Requests carry headers `Authorization: Bearer <token>` and `Project: <project_id>`. Constructor: `Contree(config: ContreeConfig or None = None, *, base_url=None, token=None)`; there is no `project_id` shorthand, so a project from somewhere other than the env needs `ContreeConfig(auth=IAMAuth(project_id=...))`. This confirms the old row. Correction: `NEBIUS_AI_PROJECT` is not read anywhere in the SDK (grep of every `environ`/`getenv`), and the SDK ships no CLI (`contree_sdk/shell/__main__.py` is empty, no console_scripts entry), so that variable matters only to a separate `contree` CLI if one is installed. | VERIFIED (source) | contree_sdk/auth.py:25-41 (resolve), :60-72 (IAMAuth), contree_sdk/sdk/client/_base.py:50-79, contree_sdk/config.py:22; grep: only auth.py:20,30,49,51,62,64 and _internals/utils/auth_ini.py:24,26,45 touch the environment; runs/D3/offline_d3.py line `env set    -> headers: {'Authorization': 'Bearer FAKE-TOKEN', 'Project': 'FAKE-PROJECT'}` (fake values, `IAMAuth().resolve()` only, no client) | 2026-10-10 | A (spike worker) |
| What happens when the credential env vars are missing | No error at construction: the literal names `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID` are used as the token and project, so the first API call fails (expect 401/403). Check that both variables are set in `common/` before building the client. | VERIFIED (offline run) | runs/D3/offline_d3.py line `env unset  -> token: NEBIUS_API_KEY \| project_id: NEBIUS_PROJECT_ID`; contree_sdk/auth.py:30-41 | 2026-10-10 | A (spike worker) |
| Fallback credential file `auth.ini` | If the env var is not set and the field still equals its default, the SDK reads `$CONTREE_HOME/auth.ini`, else `$XDG_CONFIG_HOME/contree/auth.ini`, else `~/.config/contree/auth.ini`; section `[profile:<name>]` (name from `CONTREE_PROFILE`, else the file's `profile` default key, else `default`); keys `token`, `url`, `type`, `project`. A stale file can silently supply credentials, so a test that must not reach the platform should set `CONTREE_HOME` to an empty directory. The legacy `JWTAuth` reads `CONTREE_TOKEN` and `CONTREE_BASE_URL`, but `ContreeConfig` uses it only if passed explicitly. | VERIFIED (source) | contree_sdk/_internals/utils/auth_ini.py:15-51, contree_sdk/auth.py:34-40, :47-57 | 2026-10-10 | A (spike worker) |
| Default base URL | `https://api.tokenfactory.nebius.com/sandboxes/` (`ContreeEndpoint.TOKEN_FACTORY_SANDBOXES`). Other constants: `PROD_NORTH` `https://eu-north.nebius.computer`, `STAGE_NORTH` `https://eu-north-stage.nebius.computer`. Override only with `Contree(base_url=...)`, `IAMAuth(base_url=...)` or the ini `url` key; `CONTREE_BASE_URL` is ignored by `IAMAuth`. | VERIFIED (source) | contree_sdk/_internals/utils/config.py:9-12, contree_sdk/auth.py:66; runs/D3/offline_d3.py line `CONTREE_BASE_URL honoured by IAMAuth? base_url = https://api.tokenfactory.nebius.com/sandboxes/` | 2026-10-10 | A (spike worker) |
| SDK import path and API style | `from contree_sdk import Contree` (async client) and `from contree_sdk import ContreeSync` (sync client): confirmed. `client.images.use(ref, strict=False)` exists; it is `async def` on `Contree` and a plain method on `ContreeSync`. `ref` is a UUID, a UUID string, or an OCI/tag string. With `strict=False` it makes no API call and returns `ContreeImage(uuid=<uuid>, tag=None)` for a UUID (or `uuid=None, tag=<tag>` for a tag); `strict=True` fetches it first. Other image calls: `images.oci(ref, tag=, username=, password=, timeout=)` (aliases `docker`, `podman`; uses an existing tag, else imports from the registry), `images.import_from(...)`, `images(number=100, kind=, tagged=, since=, until=)`. | VERIFIED (offline run) | contree_sdk/__init__.py:1-5, contree_sdk/sdk/managers/images/_async.py:21-22,39-46, _sync.py:21-22, _base.py:124-143,289-331; runs/D3/offline_d3.py line `ImagesManager.use is coroutine fn: True \| ImagesManagerSync.use: False` | 2026-10-10 | A (spike worker) |
| Import path and class of the Deep Agents sandbox backend | `from contree_sdk.langchain.sandbox import ContreeSandbox`. `__init__(self, session: ContreeSession or ContreeSessionSync)`; no other parameters. Class chain: `ContreeSandbox` -> `deepagents.backends.sandbox.BaseSandbox` -> `SandboxBackendProtocol` -> `BackendProtocol`, so it implements the sandbox backend protocol (`id`, `execute`/`aexecute`, `upload_files`/`download_files`; the file operations come from `BaseSandbox`). `execute` accepts `timeout`; `enable_capture_offload` stays `False`. `deepagents` is only a dev extra in contree-sdk's metadata; the deepagents pinned in this repo supplies the base class. | VERIFIED (offline run) | contree_sdk/langchain/sandbox.py:5-6,14-22; runs/D3/offline_d3.py lines `ContreeSandbox MRO: ['ContreeSandbox', 'BaseSandbox', 'SandboxBackendProtocol', 'BackendProtocol', 'ABC', 'object']` and `issubclass SandboxBackendProtocol: True \| BaseSandbox: True \| execute accepts timeout: True \| enable_capture_offload: False` | 2026-10-10 | A (spike worker) |
| How to create a sandbox | There is no "create sandbox" call. A sandbox is a session on an image: `image = await client.images.use(uuid_or_tag)` (or `await client.images.oci("python:3.12")`), `session = image.session()`, `sandbox = ContreeSandbox(session)`. Pass the async `ContreeSession` and keep your own reference to it: a `ContreeSessionSync` is copied into a new async session, so your handle would never advance. Sync equivalent: `ContreeImageSync.session()` gives a `ContreeSessionSync`. `sandbox.id` is `contree-<uuid4>-from-<start image uuid>`, fixed at construction and not updated after runs. | VERIFIED (offline run) | contree_sdk/sdk/objects/image/_async.py:6-8, _sync.py:6-8, contree_sdk/langchain/sandbox.py:15-22; runs/D3/offline_d3.py lines `async session shared (not copied): True`, `sandbox id: contree-c250ba41-e52 ...-from-<start-uuid>: True`, `sync session copied into async ContreeSession: True` (fake client object, no network) | 2026-10-10 | A (spike worker) |
| How to run a command in it | Through the sandbox: `await sandbox.aexecute(cmd, timeout=None)` or `sandbox.execute(cmd, timeout=None)` returns `ExecuteResponse(output: str, exit_code: int or None, truncated: bool)`. `output` is stdout followed by stderr (concatenated, not interleaved). It calls `session.run(shell=cmd, timeout=timeout, disposable=False, truncate_output_at=10*1024*1024)`. A non-zero exit code is not an exception. Directly on the SDK: `s = await session.run(shell=..., args=, env=, cwd=, hostname=, stdin=, stdout=, stderr=, tag=, files=, timeout=, disposable=True, truncate_output_at=, preserve_env=False)` (or `command=` instead of `shell=`); the awaited result is the session itself in state `SUCCEEDED` with `.exit_code`, `.stdout` / `.stderr` (str), `.elapsed` (timedelta), `.result.cost` (float), `.result.truncated`. Sync: `session.run(...).wait()`. | VERIFIED (offline run) | contree_sdk/langchain/sandbox.py:54-78, contree_sdk/sdk/objects/image_like/_base.py:126-144,294-341,415-423, result.py:13-41, _sync.py:20-27; runs/D3/offline_d3.py lines `ExecuteResponse: ExecuteResponse(output='OUTERR', exit_code=3, truncated=False)` and `spawn request: image==start: True disposable: False timeout: 7 truncate_output_at: 10485760 shell: True` (fake client) | 2026-10-10 | A (spike worker) |
| How to get the image or checkpoint ID after a run | `session.uuid` (a `UUID`) is replaced in place by the new image after every successful `disposable=False` run, because `ContreeSession._copy_self` returns `self`. `ContreeSandbox` has no public accessor (`sandbox._session` is private), so keep your own reference to the session you passed in and read `session.uuid` at the boundary. `ContreeImage.run(...)` on a plain image (not a session) returns a new image object and leaves the original untouched. A failed run leaves `session.uuid` at the last good image. | VERIFIED (offline run) | contree_sdk/sdk/objects/session/_base.py:15-16, contree_sdk/sdk/objects/image_like/_base.py:78-82,334-337; runs/D3/offline_d3.py lines `session.uuid advanced after run: True`, `2nd run image == 1st run's output image: True`, `session.uuid unchanged by failed run: True` | 2026-10-10 | A (spike worker) |
| How to fork from an image | No fork method exists. Images are immutable and every run yields a new image, so a fork is a new session on the snapshot UUID: `ContreeSandbox((await client.images.use(snapshot_uuid)).session())`. The `use` call is local with `strict=False`. Each fork has its own session, so forks are independent of the parent and of each other; never hand one session to two sandboxes (their locks do not coordinate). Snapshot = copy `session.uuid` at the delegation boundary. | VERIFIED (offline run) | contree_sdk/sdk/managers/images/_base.py:136-139; runs/D3/offline_d3.py line `fork run used snapshot image: True \| parent session unaffected: True` (fake client; real fork behaviour is in the time-per-fork row) | 2026-10-10 | A (spike worker) |
| How to roll back to an image | Same operation as fork: drop the current session and build a new one from the earlier UUID. There is no in-place rollback call. After a `ContreeError` during a run (operation failed, cancelled or timed out) the session is left in state `FAILED`, and every later `run` raises `ContreeImageStateError`; an error while starting the operation leaves it in `EXECUTING`, which also has no way out. So a failed run forces the same rebuild, from the last good `session.uuid`. A non-zero exit code does not trigger this. | VERIFIED (offline run) | contree_sdk/sdk/objects/image_like/_base.py:38-44 (no transition out of FAILED or EXECUTING-to-PREPARED), :299, :330-332; runs/D3/offline_d3.py lines `session state after failure: FAILED`, `next run on failed session raises ContreeImageStateError: Image state "..." cannot have state FAILED`, `recovered ok: 3`. The EXECUTING case is from source only. | 2026-10-10 | A (spike worker) |
| Every sandbox operation creates an image | `execute` runs with `disposable=False`, so each command makes a new image. A file write costs two images: a `python3 -c` preflight execute (mkdir) plus `upload_files`, which runs `true` with the files attached (`apply_files`). `download_files` only reads and makes no image. Expect hundreds of images per agent run. | VERIFIED (source) | contree_sdk/langchain/sandbox.py:24-31,62-65, contree_sdk/sdk/objects/image_like/_base.py:232-236, deepagents/backends/sandbox.py:1718-1750 | 2026-10-10 | A (spike worker) |
| Image cleanup, deletion and lifetime | The SDK has no call to delete an image. Its image API surface is list, get by uuid or tag, import, tag, untag, plus inspect (list files, download a file). Operations can be cancelled (`DELETE /v1/operations/{id}`, sent automatically if the wait is cancelled). Retention and any quota on stored images are server side. | VERIFIED (source) | contree_sdk/_internals/client/v1/images.py:10-29, inspect.py:9-20, operations.py:7-12, contree_sdk/sdk/client/_base.py:137-148 | 2026-10-10 | A (spike worker) |
| Image retention and image-count quota on the platform | | NOT YET CHECKED | needs live: after a few runs, `await client.images(number=None, kind=ImageKind.INSTANCES)` and see whether old instance images disappear or a quota error appears | | |
| Tags | `image.tag_as(tag)` / `image.untag()` (async on `Contree`, plain on `ContreeSync`) and `run(tag=...)` name an image; `images.use("tag")` resolves the tag at first run (`uuid=None` until then). Whether a tag can be reused (overwritten) is server side. UUIDs are enough for snapshot and fork; tags are optional. | VERIFIED (source) | contree_sdk/sdk/objects/image_like/_async.py:55-61, _base.py:339-340,425-452, sdk/managers/images/_base.py:140-143 | 2026-10-10 | A (spike worker) |
| Sandbox file upload and download semantics | `upload_files([(abs_path, bytes)])`: a path that does not start with `/` returns error `invalid_path` for that entry. Uploaded files get mode 0644, uid 0, gid 0, and the sandbox cannot pass a mode, so scripts need `chmod` through `execute`. `download_files([paths])` returns `content` or error `file_not_found` / `invalid_path`, read from the session's current image. To seed a repo directly: `await session.apply_files({"/repo/a.py": b"..."})`, which also accepts `UploadFileSpec(mode=, uid=, gid=, source=)`. | VERIFIED (source) | contree_sdk/langchain/sandbox.py:24-52, contree_sdk/utils/models/file.py:14-21, contree_sdk/sdk/objects/image_like/_base.py:205-236 | 2026-10-10 | A (spike worker) |
| Image requirements for the Deep Agents file tools | `BaseSandbox` implements `ls`, `read`, `glob`, `grep` (path and glob route), `edit` and the write preflight by running `python3 -c "..."` inside the sandbox; `delete` uses `test -e` and `rm -rf`. The base image must have `python3` or those tools fail. | VERIFIED (source) | deepagents/backends/sandbox.py:56,381,462,498,583,660,855 (python3 templates), :1960-2000 (delete) | 2026-10-10 | A (spike worker) |
| Sync versus async and event loops | `Contree` is async; `ContreeSync` wraps the same code with `coro_sync`, which runs coroutines on one shared daemon-thread event loop and blocks the calling thread. HTTP uses one `httpx.AsyncClient` per running loop. `ContreeSandbox.execute` is the sync path (`coro_sync(aexecute)`) and `aexecute` the native one. Each sandbox holds an `asyncio.Lock` that serialises `aexecute`, `aupload_files` and `adownload_files`, so one sandbox runs one command at a time. | VERIFIED (source) | contree_sdk/_internals/utils/wrapper.py:26-123, contree_sdk/_internals/lib/client_base.py:24-33, contree_sdk/langchain/sandbox.py:18,29,48,60,77-78 | 2026-10-10 | A (spike worker) |
| Timeout parameters and defaults the SDK exposes | `ContreeConfig`: `transport_timeout=10.0` s (HTTP, all phases), `operation_timeout=1000.0` s (default for any operation), `operation_run_timeout=None` and `operation_import_timeout=None` (fall back to `operation_timeout`), poll interval `operation_poll_secs_min=0.1` to `operation_poll_secs_max=10.0` with `operation_poll_secs_backoff_grow=1.75`, `operation_poll_not_found_limit=10`, `default_truncate_output_at=65535` bytes (the sandbox overrides it to 10 MiB), `file_upload_chunk_size=1 MiB`, `images_list_batch_size=100`. Per command: `run(timeout=seconds or timedelta)`, rounded up to whole seconds; `ContreeSandbox.execute(timeout=None)` therefore uses 1000 s, and that one value is both the server-side command timeout and the SDK wait limit. Exceeding the wait limit raises `OperationTimedOutError`. Only after `get_token_info()` has been called does the SDK log a warning when a timeout exceeds the server's `instance_max_timeout`. | VERIFIED (offline run) | contree_sdk/config.py:25-53, contree_sdk/sdk/objects/image_like/_base.py:181-184,303-307,319, contree_sdk/sdk/client/_base.py:117-122,160-168; runs/D3/offline_d3.py lines `ContreeConfig defaults: {...}` and `2nd run image == 1st run's output image: True \| default timeout: 1000` | 2026-10-10 | A (spike worker) |
| SDK parameters for CPU, memory and network | None. The spawn request carries only `command, image, hostname, args, shell, env, cwd, disposable, stdin, timeout, truncate_output_at, files, preserve_env`. There is no CPU, memory, disk or network setting anywhere in the SDK, so NemoGate cannot configure them; the platform fixes them. Results report `max_rss`, `user_cpu_time`, `system_cpu_time`, `elapsed_time` and `cost` per run. | VERIFIED (source) | contree_sdk/_internals/models/instance.py:8-24,78-92 | 2026-10-10 | A (spike worker) |
| Defaults the sandbox does not set | `ContreeSandbox` passes no `cwd`, `env` or `hostname`: cwd is sent as `""` (server default), hostname is `"hostname"`, env is empty, `preserve_env=False`. | VERIFIED (offline run) | runs/D3/offline_d3.py line `spawn request: ... cwd: '' hostname: hostname`; contree_sdk/sdk/objects/image_like/_base.py:190-193,316-318 | 2026-10-10 | A (spike worker) |
| Retries and concurrency control in the SDK | Starting a run or an import is retried on HTTP 429 (`TooManyRequestsError`) and `ApiTimeoutError` through one shared `CircuitRetrier` per client: up to 100 retries, 1 to 30 s between attempts, until the operation timeout elapses, circuit-wide cap of 1000 failures. There is no client-side limit on concurrent runs (no semaphore). Other errors are not retried. | VERIFIED (source) | contree_sdk/sdk/client/_base.py:84-96, contree_sdk/_internals/utils/circuit_retrier.py:67-71,146-177; grep `Semaphore` finds only the retrier's internal one | 2026-10-10 | A (spike worker) |
| Output decoding | Stdout and stderr are decoded as strict UTF-8 (`.decode()`); a command that prints non-UTF-8 bytes makes `aexecute` raise `UnicodeDecodeError` after the run has already advanced `session.uuid`. | VERIFIED (source) | contree_sdk/sdk/objects/image_like/result.py:44-49, contree_sdk/langchain/sandbox.py:61-70 | 2026-10-10 | A (spike worker) |
| Time per fork | | NOT YET CHECKED | needs live: time `ContreeSandbox((await client.images.use(snapshot_uuid)).session()).aexecute("true")` over 10 forks of one snapshot, sequentially, then 5 at once with `asyncio.gather` | | |
| Network access from inside the sandbox | | NOT YET CHECKED | needs live: `await sandbox.aexecute("python3 -c \"import urllib.request as u;print(u.urlopen('https://example.com',timeout=5).status)\"")` | | |
| CPU, memory, disk and server-side time limits | | NOT YET CHECKED | needs live: `await client.get_token_info()` (prints `limits`, `permissions`, `operations_stat`; keys the SDK reads: `instance_max_timeout`, `images_import_max_timeout`) and `await sandbox.aexecute("nproc; free -m; df -h /; ulimit -a")` | | |
| Command timeout behaviour | | NOT YET CHECKED | needs live: `await sandbox.aexecute("sleep 30", timeout=3)`; record the exit code, `truncated`, elapsed time, whether an exception is raised, and whether the session survives (the raw result has `state.timed_out`, which `ExecuteResponse` does not expose) | | |
| Maximum concurrent runs per project, rate limit values | | NOT YET CHECKED | needs live: run 20 `aexecute("true")` on 20 forks of one snapshot with `asyncio.gather`, with `logging` at INFO for `contree_sdk`; look for 429 retries and record total wall time | | |
| Cost of a sandbox run | `result.cost` (float) is reported per run; unit, price and whether idle sandboxes bill are not known. | NOT YET CHECKED | needs live: print `s.result.cost` and `s.elapsed` after `s = await session.run(shell="true", disposable=False)` and compare with the Token Factory billing page | | |
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
