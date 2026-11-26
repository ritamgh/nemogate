# A third-party app under the guard
A small Deep Agents app with its own tools (`send_payment`, `delete_record`) and its own `rules.yaml`.
It takes only `Guard` from NemoGate: `guard.middleware()` goes in the main agent and a declared `general-purpose` subagent.
`build_agent(model, rules_path, calls)` returns the agent; `calls` lists the tool runs that really happened.
`tests/test_third_party.py` drives it with a scripted model, offline. No `approval_before` rule: the app has no approval tool.
