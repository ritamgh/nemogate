# Repair: Rejected
- Fix: `carry_rules` on `planner`
- Why: The planner must copy the user's rule into every task it delegates.
- Result: guard: old_string must occur exactly once in app/prompts.py (it occurs 0 times: 'PROMPT = ""') (2 tries)
- Diff: `repair-carry_rules-round1.patch`
