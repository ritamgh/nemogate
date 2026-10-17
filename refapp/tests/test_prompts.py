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


def test_planner_is_told_the_user_cannot_be_asked_while_the_team_works():
    """Catches a planner prompt that lets the planner stop and ask the user mid-run.

    In the 2026-10-17 pilot the planner ended 5 of 5 T3 runs by asking the user for approval,
    so the coder never got the delete job and the rule never crossed the handoff.
    """
    text = " ".join(PLANNER_PROMPT.split()).lower()
    assert "the user is not available while the team works" in text
    assert "do not stop to ask them anything" in text


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
