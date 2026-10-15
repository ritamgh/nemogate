# Repair: Accepted
- Fix: `carry_rules` on `planner`
- Why: The planner paraphrased the handoff and lost the prohibition.
- Result: violation rate 50% -> 10%, task success 80% -> 90% (10 baseline runs, 10 patched runs)
- Diff: `repair-carry_rules.patch`
