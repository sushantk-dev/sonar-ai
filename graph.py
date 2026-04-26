"""
SonarAI — LangGraph State Graph
6-node pipeline:
  ingest → load_repo → plan → generate → critique → validate → deliver
                                    ↑______________|  (retry edge, max 1)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from loguru import logger
from langgraph.graph import StateGraph, END

from sonar_ai.state import AgentState, SonarIssue
from sonar_ai.config import settings
from sonar_ai.parser import parse_sonar_report, load_rule_kb
from sonar_ai.repo_loader import clone_repo, create_fix_branch, resolve_java_file, extract_method_context
from sonar_ai.agents import plan_fix, generate_fix, critique_fix
from sonar_ai.validator import validate
from sonar_ai.deliver import deliver


# ── Node: ingest ──────────────────────────────────────────────────────────────

def node_ingest(state: AgentState) -> AgentState:
    """Parse the Sonar report, load the Rule KB, set up the issue queue."""
    logger.info("[Ingest] Parsing Sonar report...")
    issues = parse_sonar_report(state["sonar_report_path"])
    rule_kb = load_rule_kb()

    if not issues:
        logger.warning("[Ingest] No actionable issues found — pipeline will exit")
        return {**state, "issues": [], "rule_kb": rule_kb, "current_issue_index": 0, "errors": [], "done": True}

    first_issue: SonarIssue = issues[0]
    logger.info(
        f"[Ingest] {len(issues)} issues queued. First: {first_issue['rule_key']} "
        f"(line {first_issue['line']}) in {first_issue['component']}"
    )

    return {
        **state,
        "issues": issues,
        "rule_kb": rule_kb,
        "current_issue_index": 0,
        "current_issue": first_issue,
        "errors": state.get("errors", []),
        "done": False,
    }


# ── Node: load_repo ───────────────────────────────────────────────────────────

def node_load_repo(state: AgentState) -> AgentState:
    """Clone the repo, checkout commit SHA, resolve file path, extract method context."""
    issue = state["current_issue"]
    logger.info(f"[LoadRepo] Loading repo for issue {issue['rule_key']}")

    repo = clone_repo(
        repo_url=state["repo_url"],
        clone_base_dir=settings.clone_dir,
        github_token=settings.github_token,
        commit_sha=state["commit_sha"],
    )

    repo_local_path = str(repo.working_dir)

    fix_branch = create_fix_branch(repo, issue["rule_key"], issue["key"])

    file_path = resolve_java_file(repo_local_path, issue["component"])
    if not file_path:
        error_msg = f"Cannot resolve file for component: {issue['component']}"
        logger.error(f"[LoadRepo] {error_msg}")
        errors = state.get("errors", []) + [error_msg]
        return {**state, "errors": errors, "done": True}

    method_context = extract_method_context(file_path, issue["line"])

    logger.info(f"[LoadRepo] Ready. file={Path(file_path).name} branch={fix_branch}")

    return {
        **state,
        "repo_local_path": repo_local_path,
        "fix_branch": fix_branch,
        "file_path": file_path,
        "method_context": method_context,
        "retry_count": 0,
    }


# ── Node: plan ────────────────────────────────────────────────────────────────

def node_plan(state: AgentState) -> AgentState:
    """LLM·1 Planner — analyse issue and produce fix strategy."""
    return plan_fix(state)


# ── Node: generate ────────────────────────────────────────────────────────────

def node_generate(state: AgentState) -> AgentState:
    """LLM·2 Generator — produce unified diff patch."""
    return generate_fix(state)


# ── Node: critique ────────────────────────────────────────────────────────────

def node_critique(state: AgentState) -> AgentState:
    """LLM·3 Critic — review the generated patch."""
    return critique_fix(state)


# ── Node: validate ────────────────────────────────────────────────────────────

def node_validate(state: AgentState) -> AgentState:
    """Apply diff and run mvn compile + test."""
    return validate(state)


# ── Node: deliver ─────────────────────────────────────────────────────────────

def node_deliver(state: AgentState) -> AgentState:
    """Commit, push, open PR or write escalation."""
    return deliver(state)


# ── Conditional edges ─────────────────────────────────────────────────────────

def route_after_critique(state: AgentState) -> Literal["validate", "generate"]:
    """
    After Critic runs:
    - approved → validate
    - rejected AND retry_count < max → back to generate (increment counter)
    - rejected AND retries exhausted → validate anyway (will likely escalate)
    """
    critic_out = state.get("critic_output", {})
    approved = critic_out.get("approved", False)
    retry_count = state.get("retry_count", 0)

    if approved:
        logger.info("[Router] Critic approved — proceeding to validate")
        return "validate"

    if retry_count < settings.max_critic_retries:
        logger.info(
            f"[Router] Critic rejected — retry {retry_count + 1}/{settings.max_critic_retries}"
        )
        # Increment retry count in state (mutate via a wrapper)
        # LangGraph nodes must return full state dicts; we do this via a side effect here
        # The increment is done inside node_generate (it reads retry_count and planner uses it)
        state["retry_count"] = retry_count + 1
        return "generate"

    logger.warning("[Router] Critic rejected and retries exhausted — proceeding to validate")
    return "validate"


def route_after_ingest(state: AgentState) -> Literal["load_repo", END]:
    """Skip rest of pipeline if no issues were found."""
    if state.get("done") or not state.get("issues"):
        return END
    return "load_repo"


def route_after_load_repo(state: AgentState) -> Literal["plan", END]:
    """Skip if file resolution failed."""
    if state.get("done"):
        return END
    return "plan"


# ── Graph assembly ────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """Assemble and compile the SonarAI LangGraph."""
    graph = StateGraph(AgentState)

    # Register nodes
    graph.add_node("ingest", node_ingest)
    graph.add_node("load_repo", node_load_repo)
    graph.add_node("plan", node_plan)
    graph.add_node("generate", node_generate)
    graph.add_node("critique", node_critique)
    graph.add_node("validate", node_validate)
    graph.add_node("deliver", node_deliver)

    # Entry point
    graph.set_entry_point("ingest")

    # Linear edges
    graph.add_conditional_edges("ingest", route_after_ingest, {"load_repo": "load_repo", END: END})
    graph.add_conditional_edges("load_repo", route_after_load_repo, {"plan": "plan", END: END})
    graph.add_edge("plan", "generate")
    graph.add_edge("generate", "critique")

    # Conditional retry edge: critique → (validate | generate)
    graph.add_conditional_edges(
        "critique",
        route_after_critique,
        {"validate": "validate", "generate": "generate"},
    )

    graph.add_edge("validate", "deliver")
    graph.add_edge("deliver", END)

    return graph.compile()


# ── Public runner ─────────────────────────────────────────────────────────────

def run_pipeline(
    sonar_report_path: str,
    repo_url: str,
    commit_sha: str,
) -> AgentState:
    """
    Run the full SonarAI pipeline for the highest-priority issue in the report.

    Args:
        sonar_report_path: Path to sonar-report.json
        repo_url:          GitHub HTTPS clone URL
        commit_sha:        Exact commit SHA used during the Sonar scan

    Returns:
        Final AgentState after the pipeline completes.
    """
    app = build_graph()

    initial_state: AgentState = {
        "sonar_report_path": sonar_report_path,
        "repo_url": repo_url,
        "commit_sha": commit_sha,
    }

    logger.info("=" * 60)
    logger.info("SonarAI pipeline starting")
    logger.info(f"  report : {sonar_report_path}")
    logger.info(f"  repo   : {repo_url}")
    logger.info(f"  commit : {commit_sha}")
    logger.info("=" * 60)

    final_state = app.invoke(initial_state)

    logger.info("=" * 60)
    if final_state.get("pr_url"):
        logger.info(f"✅ PR opened: {final_state['pr_url']}")
    elif final_state.get("escalation_path"):
        logger.warning(f"⚠️  Escalation: {final_state['escalation_path']}")
    else:
        logger.info("Pipeline completed (no PR or escalation)")
    logger.info("=" * 60)

    return final_state
