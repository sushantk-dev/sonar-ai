"""
SonarAI — LLM Agent Nodes  (Iteration 2)
Three LangGraph node functions wired into the state graph:
  plan_fix      → LLM·1 Planner  → PlannerOutput
  generate_fix  → LLM·2 Generator → GeneratorOutput
  critique_fix  → LLM·3 Critic   → CriticOutput

Iteration 2 changes:
  - plan_fix now passes rag_context (prior fix examples) to the Planner prompt.
  - retrieve_rag_context() is exposed as a standalone node for the graph to call
    before plan_fix, enabling pre-fetch of ChromaDB results.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from langchain_google_vertexai import ChatVertexAI
from loguru import logger

from config import settings
from prompts import planner_prompt, generator_prompt, critic_prompt, format_rag_context
from state import AgentState, PlannerOutput, GeneratorOutput, CriticOutput, RAGContext


# ── LLM factory ──────────────────────────────────────────────────────────────

def _make_llm(temperature: float = 0.2) -> ChatVertexAI:
    """Return a ChatVertexAI instance wired to the configured model."""
    return ChatVertexAI(
        model_name=settings.vertex_model,
        project=settings.gcp_project,
        location=settings.gcp_location,
        max_output_tokens=settings.max_tokens,
        temperature=temperature,
    )


# ── JSON parsing helper ───────────────────────────────────────────────────────

def _parse_json_response(raw: str, node_name: str) -> dict[str, Any]:
    """
    Parse a JSON response from an LLM.  Handles common LLM habits like:
    - Wrapping JSON in ```json ... ``` fences
    - Leading/trailing whitespace
    """
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error(f"[{node_name}] JSON parse failed: {exc}\nRaw output:\n{raw[:500]}")
        raise


def _clean_patch_hunks(patch: str) -> str:
    """
    Strip markdown code fences that the LLM embeds INSIDE the patch_hunks JSON value.
    """
    if not patch:
        return patch

    patch = patch.replace("\\r\\n", "\n").replace("\\n", "\n")
    patch = re.sub(r"^```[a-zA-Z]*\s*\n?", "", patch.lstrip(), flags=re.MULTILINE)
    patch = re.sub(r"\n?^```\s*$", "", patch.rstrip(), flags=re.MULTILINE)

    return patch.strip()


# ── Node helpers ──────────────────────────────────────────────────────────────

def _rule_kb_entry_text(state: AgentState) -> str:
    """Format the Rule KB entry for the current issue, or a generic note if missing.

    Supports both legacy schema (name/short/severity/description/fix_strategy/examples)
    and the extended FindBugs-compatible schema that adds type, tags, and impacts.
    """
    rule_kb: dict = state.get("rule_kb", {})
    issue = state.get("current_issue", {})
    rule_key = issue.get("rule_key", "")
    entry = rule_kb.get(rule_key)
    if not entry:
        return f"No KB entry for rule {rule_key}. Apply generic best-practice remediation."

    # -- Core fields (always present) ------------------------------------------
    lines = [
        f"Name: {entry.get('name', '')}",
        f"Description: {entry.get('description', '')}",
        f"Fix Strategy: {entry.get('fix_strategy', '')}",
        f"Example Before: {entry.get('example_before', '')}",
        f"Example After: {entry.get('example_after', '')}",
    ]

    # -- Extended fields (FindBugs-compatible schema) ---------------------------
    rule_type = entry.get("type")
    if rule_type:
        lines.append(f"Type: {rule_type}")

    tags = entry.get("tags")
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")

    impacts = entry.get("impacts")
    if impacts:
        impact_strs = [
            f"{imp.get('softwareQuality', '')} ({imp.get('severity', '')})"
            for imp in impacts
        ]
        lines.append(f"Impacts: {'; '.join(impact_strs)}")

    return "\n".join(lines)


# ── RAG node ─────────────────────────────────────────────────────────────────

def retrieve_rag_context(state: AgentState) -> AgentState:
    """
    LangGraph node — retrieve similar prior fixes from ChromaDB.
    Populates state['rag_context'].
    Silently no-ops (empty context) if RAG is disabled or unavailable.
    """
    if not settings.enable_rag:
        empty: RAGContext = {"rule_key": "", "similar_fixes": [], "retrieved_count": 0}
        return {**state, "rag_context": empty}

    issue = state.get("current_issue", {})
    rule_key = issue.get("rule_key", "")
    message = issue.get("message", "")
    method_context = state.get("method_context", "")

    logger.info(f"[RAG] Retrieving prior fixes for rule={rule_key}")

    try:
        from rag_store import retrieve_similar_fixes
        similar_fixes = retrieve_similar_fixes(
            rule_key=rule_key,
            method_context=method_context,
            message=message,
            top_k=settings.rag_top_k,
        )
    except Exception as exc:
        logger.warning(f"[RAG] Retrieval failed (non-fatal): {exc}")
        similar_fixes = []

    rag_ctx: RAGContext = {
        "rule_key": rule_key,
        "similar_fixes": similar_fixes,
        "retrieved_count": len(similar_fixes),
    }

    if similar_fixes:
        logger.info(f"[RAG] Found {len(similar_fixes)} similar fix(es) to use as context")
    else:
        logger.info("[RAG] No similar prior fixes found")

    return {**state, "rag_context": rag_ctx}


# ── LLM·1  Planner ────────────────────────────────────────────────────────────

def plan_fix(state: AgentState) -> AgentState:
    """
    Analyse the current Sonar issue with chain-of-thought reasoning.
    Populates state['planner_output'].
    Includes RAG few-shot examples in the prompt if available.
    """
    issue = state["current_issue"]
    logger.info(
        f"[Planner] rule={issue['rule_key']} severity={issue['severity']} "
        f"line={issue['line']}"
    )

    # Build RAG few-shot block
    rag_ctx = state.get("rag_context", {})
    similar_fixes = rag_ctx.get("similar_fixes", []) if rag_ctx else []
    rag_block = format_rag_context(similar_fixes)
    if similar_fixes:
        logger.info(f"[Planner] Including {len(similar_fixes)} RAG example(s) in prompt")

    llm = _make_llm(temperature=0.1)
    chain = planner_prompt | llm

    prompt_vars = {
        "rule_key": issue["rule_key"],
        "severity": issue["severity"],
        "message": issue["message"],
        "file_path": state.get("file_path", "unknown"),
        "flagged_line": issue["line"],
        "rule_kb_entry": _rule_kb_entry_text(state),
        "method_context": state.get("method_context", ""),
        "rag_context": rag_block,
    }

    t0 = time.time()
    response = chain.invoke(prompt_vars)
    elapsed = time.time() - t0

    raw = response.content if hasattr(response, "content") else str(response)
    logger.info(f"[Planner] LLM call completed in {elapsed:.2f}s")

    parsed: PlannerOutput = _parse_json_response(raw, "Planner")  # type: ignore[assignment]

    parsed.setdefault("reasoning", "")
    parsed.setdefault("strategy", "")
    parsed.setdefault("confidence", 0.5)

    logger.info(
        f"[Planner] confidence={parsed['confidence']:.2f} "
        f"strategy_preview={parsed['strategy'][:80]!r}"
    )

    return {**state, "planner_output": parsed}


# ── LLM·2  Generator ─────────────────────────────────────────────────────────

def generate_fix(state: AgentState) -> AgentState:
    """
    Generate a minimal unified diff fixing the Sonar issue.
    Populates state['generator_output'].
    Appends critic feedback to the prompt on retry iterations.
    """
    issue = state["current_issue"]
    retry_count = state.get("retry_count", 0)
    planner_out = state.get("planner_output", {})

    logger.info(f"[Generator] retry={retry_count} rule={issue['rule_key']}")

    retry_feedback = ""
    if retry_count > 0:
        critic_out = state.get("critic_output", {})
        concerns = critic_out.get("concerns", [])
        concern_text = "\n".join(f"  - {c}" for c in concerns)
        validation = state.get("validation", {})

        compiler_error = validation.get("compiler_error", "")
        test_error = validation.get("test_error", "")

        retry_feedback = (
            "## ⚠ Previous Attempt Was Rejected — Fix These Issues\n"
            f"Critic concerns:\n{concern_text}\n"
        )
        if compiler_error:
            retry_feedback += f"\nCompiler error:\n```\n{compiler_error[:800]}\n```\n"
        if test_error:
            retry_feedback += f"\nTest failure:\n```\n{test_error[:800]}\n```\n"

    llm = _make_llm(temperature=0.3)
    chain = generator_prompt | llm

    repo_root = state.get("repo_local_path", "")
    abs_path = state.get("file_path", "")
    try:
        rel_path = Path(abs_path).relative_to(repo_root).as_posix() if repo_root else abs_path
    except ValueError:
        rel_path = Path(abs_path).name

    full_file_context = _numbered_file(abs_path)

    prompt_vars = {
        "rule_key": issue["rule_key"],
        "severity": issue["severity"],
        "message": issue["message"],
        "file_path": rel_path,
        "flagged_line": issue["line"],
        "strategy": planner_out.get("strategy", ""),
        "method_context": full_file_context or state.get("method_context", ""),
        "retry_feedback": retry_feedback,
    }

    t0 = time.time()
    response = chain.invoke(prompt_vars)
    elapsed = time.time() - t0

    raw = response.content if hasattr(response, "content") else str(response)
    logger.info(f"[Generator] LLM call completed in {elapsed:.2f}s")

    parsed: GeneratorOutput = _parse_json_response(raw, "Generator")  # type: ignore[assignment]
    parsed.setdefault("patch_hunks", "")
    parsed.setdefault("changed_methods", [])

    raw_patch = parsed["patch_hunks"]
    cleaned_patch = _clean_patch_hunks(raw_patch)
    if cleaned_patch != raw_patch:
        logger.info(
            f"[Generator] Stripped markdown fences from patch_hunks "
            f"(was {len(raw_patch)} chars, now {len(cleaned_patch)} chars)"
        )
    parsed["patch_hunks"] = cleaned_patch

    logger.info(
        f"[Generator] patch_lines={len(parsed['patch_hunks'].splitlines())} "
        f"changed_methods={parsed['changed_methods']}"
    )

    return {**state, "generator_output": parsed}


# ── LLM·3  Critic ─────────────────────────────────────────────────────────────

def critique_fix(state: AgentState) -> AgentState:
    """
    Adversarially review the generated patch.
    Populates state['critic_output'].
    """
    issue = state["current_issue"]
    generator_out = state.get("generator_output", {})

    logger.info(f"[Critic] reviewing patch for rule={issue['rule_key']}")

    llm = _make_llm(temperature=0.1)
    chain = critic_prompt | llm

    changed_methods = generator_out.get("changed_methods", [])

    prompt_vars = {
        "rule_key": issue["rule_key"],
        "severity": issue["severity"],
        "message": issue["message"],
        "file_path": state.get("file_path", "unknown"),
        "flagged_line": issue["line"],
        "method_context": state.get("method_context", ""),
        "patch_hunks": generator_out.get("patch_hunks", ""),
        "changed_methods": ", ".join(changed_methods) if changed_methods else "unknown",
    }

    t0 = time.time()
    response = chain.invoke(prompt_vars)
    elapsed = time.time() - t0

    raw = response.content if hasattr(response, "content") else str(response)
    logger.info(f"[Critic] LLM call completed in {elapsed:.2f}s")

    parsed: CriticOutput = _parse_json_response(raw, "Critic")  # type: ignore[assignment]
    parsed.setdefault("approved", False)
    parsed.setdefault("concerns", [])

    logger.info(
        f"[Critic] approved={parsed['approved']} "
        f"concerns={len(parsed['concerns'])}"
    )
    if not parsed["approved"]:
        for concern in parsed["concerns"]:
            logger.warning(f"[Critic] concern: {concern}")

    return {**state, "critic_output": parsed}


# ── File helpers ──────────────────────────────────────────────────────────────

def _numbered_file(file_path: str, max_lines: int = 300) -> str:
    """
    Return the full file content with 1-based line numbers prepended.
    Capped at max_lines to stay within context limits.
    """
    if not file_path:
        return ""
    try:
        lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            head = lines[:150]
            tail = lines[-50:]
            gap = len(lines) - 200
            numbered = (
                "\n".join(f"{i+1:4d}  {l}" for i, l in enumerate(head))
                + f"\n... ({gap} lines omitted) ...\n"
                + "\n".join(f"{len(lines)-49+i:4d}  {l}" for i, l in enumerate(tail))
            )
        else:
            numbered = "\n".join(f"{i+1:4d}  {l}" for i, l in enumerate(lines))
        return f"// {Path(file_path).name} — {len(lines)} lines total\n" + numbered
    except OSError:
        return ""