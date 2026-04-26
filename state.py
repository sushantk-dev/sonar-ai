"""
SonarAI — Shared Agent State
Passed through every node of the LangGraph state graph.
All fields are optional to allow partial population at different stages.
"""

from __future__ import annotations

from typing import Any, Optional
from typing_extensions import TypedDict


class SonarIssue(TypedDict):
    """A single parsed issue from sonar-report.json."""
    key: str               # Sonar issue key / UUID
    rule_key: str          # e.g. "java:S2259"
    severity: str          # BLOCKER | CRITICAL | MAJOR | MINOR | INFO
    component: str         # e.g. "my-project:src/main/java/Foo.java"
    line: int              # Flagged line number
    message: str           # Human-readable issue message
    status: str            # OPEN | CONFIRMED | etc.
    effort: str            # Remediation effort string


class PlannerOutput(TypedDict):
    """Output from LLM·1 (Planner)."""
    reasoning: str
    strategy: str
    confidence: float      # 0.0 – 1.0


class GeneratorOutput(TypedDict):
    """Output from LLM·2 (Generator)."""
    patch_hunks: str       # Unified diff text
    changed_methods: list[str]


class CriticOutput(TypedDict):
    """Output from LLM·3 (Critic)."""
    approved: bool
    concerns: list[str]


class ValidationResult(TypedDict):
    diff_ok: bool
    compile_ok: bool
    tests_ok: bool
    compiler_error: str
    test_error: str


class AgentState(TypedDict, total=False):
    # ── Input ─────────────────────────────────────────────────────────────────
    sonar_report_path: str          # Path to sonar-report.json
    repo_url: str                   # GitHub clone URL
    commit_sha: str                 # Exact commit SHA from Sonar scan

    # ── Parsed issues ─────────────────────────────────────────────────────────
    issues: list[SonarIssue]        # All parsed, filtered, sorted issues
    current_issue_index: int        # Pointer into issues[]
    current_issue: SonarIssue       # Convenience alias

    # ── Repo state ────────────────────────────────────────────────────────────
    repo_local_path: str            # Absolute path to cloned repo
    fix_branch: str                 # Git branch name for this fix
    file_path: str                  # Absolute path to the .java file
    method_context: str             # Extracted method source (or ±50 line slice)

    # ── Rule KB ───────────────────────────────────────────────────────────────
    rule_kb: dict[str, Any]         # rule_key → rule metadata dict

    # ── LLM outputs ───────────────────────────────────────────────────────────
    planner_output: PlannerOutput
    generator_output: GeneratorOutput
    critic_output: CriticOutput
    retry_count: int                # How many critic→generator retries so far

    # ── Validation ────────────────────────────────────────────────────────────
    validation: ValidationResult

    # ── Delivery ──────────────────────────────────────────────────────────────
    pr_url: Optional[str]           # PR URL if opened
    escalation_path: Optional[str]  # Path to escalation .md if not PRed

    # ── Pipeline metadata ─────────────────────────────────────────────────────
    errors: list[str]               # Accumulated non-fatal errors / warnings
    done: bool                      # Signals terminal state to the graph
