# Repair: Accepted
- Fix: `carry_rules` on `planner`
- Why: The planner paraphrased the handoff and lost the prohibition.
- Result: violation rate 100% -> 0%, task success 0% -> 100% (2 baseline runs, 2 patched runs)
- Diff: `repair-carry_rules.patch`
