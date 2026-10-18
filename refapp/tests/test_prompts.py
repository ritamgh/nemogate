"""The reference team's prompts describe a method, never a task (AGENTS.md rule 5)."""

import re

from refapp.prompts import (
    CODER_DESCRIPTION,
    CODER_PROMPT,
    PLANNER_PROMPT,
    REVIEWER_DESCRIPTION,
    REVIEWER_PROMPT,
)

# The generality pattern from AGENTS.md rule 5.
TASK_SPECIFIC = re.compile(
    r"\bT[1-8]\b|C-00[0-9]|migrations/|RISK|CHANGED:|delete_file|run_tests|http_get"
    r"|utils_old|descripton|export_csv"
)


def test_the_method_prompts_name_no_task_rule_or_seed_content():
    """Catches a task ID, rule ID, tool or seed-repo name creeping into a prompt."""
    for prompt in (
        PLANNER_PROMPT,
        CODER_PROMPT,
        REVIEWER_PROMPT,
        CODER_DESCRIPTION,
        REVIEWER_DESCRIPTION,
    ):
        assert TASK_SPECIFIC.search(prompt) is None
