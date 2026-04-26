"""
SonarAI — PR Delivery & Escalation  (Phase 05 — hardened)
Commits the fix, pushes the branch, and opens a GitHub PR
(or writes an escalation markdown file for LOW-confidence / failed patches).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import git
from github import Github, GithubException
from loguru import logger

from config import settings
from state import AgentState


# ── Confidence helpers ────────────────────────────────────────────────────────

def _confidence_label(score: float) -> str:
    if score >= settings.confidence_high_threshold:
        return "HIGH"
    if score >= settings.confidence_medium_threshold:
        return "MEDIUM"
    return "LOW"


def _confidence_badge(label: str) -> str:
    colour = {"HIGH": "brightgreen", "MEDIUM": "yellow", "LOW": "red"}.get(label, "grey")
    return f"![Confidence: {label}](https://img.shields.io/badge/confidence-{label}-{colour})"


# ── Public entry point ────────────────────────────────────────────────────────

def deliver(state: AgentState) -> AgentState:
    """
    LangGraph node — commit the fix, evaluate confidence, and either open a PR or escalate.
    """
    issue = state["current_issue"]
    planner = state.get("planner_output", {})
    validation = state.get("validation", {})

    confidence_score: float = planner.get("confidence", 0.0)
    confidence_label = _confidence_label(confidence_score)
    validation_passed = validation.get("diff_ok") and validation.get("compile_ok")

    logger.info(
        f"[Deliver] confidence={confidence_label}({confidence_score:.2f}) "
        f"validation_passed={validation_passed} "
        f"tests_ok={validation.get('tests_ok')}"
    )

    # ── Dry-run guard ────────────────────────────────────────────────────────
    import os as _os
    if _os.environ.get("SONAR_AI_DRY_RUN") == "1":
        logger.info("[Deliver] DRY RUN — skipping commit, push, and PR creation")
        patch_preview = state.get("generator_output", {}).get("patch_hunks", "")[:500]
        logger.info(f"[Deliver] DRY RUN patch preview:\n{patch_preview}")
        return {**state, "done": True}

    if confidence_label == "LOW" or not validation_passed:
        path = _write_escalation(state, confidence_label)
        return {**state, "escalation_path": path, "done": True}

    # Snapshot the file content *before* committing for the PR body
    before_snippet = _read_method_region(
        state.get("file_path", ""),
        state.get("current_issue", {}).get("line", 0),
        lines=15,
    )

    # Commit the fix
    try:
        _commit_fix(state)
        _push_branch(state)
    except Exception as exc:
        logger.error(f"[Deliver] Git commit/push failed: {exc}")
        path = _write_escalation(state, confidence_label, extra_note=str(exc))
        return {**state, "escalation_path": path, "done": True}

    # Snapshot after-commit content for PR body
    after_snippet = _read_method_region(
        state.get("file_path", ""),
        state.get("current_issue", {}).get("line", 0),
        lines=15,
    )

    # Open the PR
    try:
        pr_url = _open_pr(state, confidence_label, before_snippet, after_snippet)
        return {**state, "pr_url": pr_url, "done": True}
    except GithubException as exc:
        logger.error(f"[Deliver] GitHub PR creation failed: {exc}")
        path = _write_escalation(state, confidence_label, extra_note=str(exc))
        return {**state, "escalation_path": path, "done": True}


# ── Git commit & push ─────────────────────────────────────────────────────────

def _commit_fix(state: AgentState) -> None:
    repo = git.Repo(state["repo_local_path"])
    file_path = state["file_path"]
    issue = state["current_issue"]

    rule_short = issue["rule_key"].split(":")[-1] if ":" in issue["rule_key"] else issue["rule_key"]
    class_name = Path(file_path).stem
    commit_msg = (
        f"fix(sonar): resolve {rule_short} in {class_name}.java\n\n"
        f"SonarQube rule: {issue['rule_key']}\n"
        f"Severity: {issue['severity']}\n"
        f"Message: {issue['message']}\n\n"
        f"Auto-fixed by SonarAI"
    )

    # Use relative path for git index to avoid absolute-path issues
    repo_root = Path(repo.working_dir)
    try:
        rel_path = str(Path(file_path).relative_to(repo_root))
    except ValueError:
        rel_path = file_path

    repo.index.add([rel_path])
    repo.index.commit(commit_msg)
    logger.info(f"[Deliver] Committed: {commit_msg.splitlines()[0]!r}")


def _push_branch(state: AgentState) -> None:
    repo = git.Repo(state["repo_local_path"])
    branch = state.get("fix_branch", "")
    if not branch:
        raise ValueError("fix_branch is not set in state")

    # Use repo.git.push() which calls the git subprocess directly and
    # respects the push URL set on the remote (including the injected token).
    # This avoids the "no refspec" error from GitPython's high-level push().
    try:
        repo.git.push(
            "origin",
            f"refs/heads/{branch}:refs/heads/{branch}",
            "--set-upstream",
        )
        logger.info(f"[Deliver] Pushed branch {branch} → origin")
    except git.GitCommandError as exc:
        # Surface the actual git error message — much easier to diagnose
        raise git.GitCommandError(
            "push",
            exc.status,
            stderr=exc.stderr,
        ) from exc


# ── GitHub PR ─────────────────────────────────────────────────────────────────

def _open_pr(
    state: AgentState,
    confidence_label: str,
    before_snippet: str,
    after_snippet: str,
) -> str:
    """Open a GitHub PR and return the PR URL."""
    gh = Github(settings.github_token, base_url=settings.github_base_url)

    repo_url = state["repo_url"]
    repo_name = _repo_name_from_url(repo_url)
    gh_repo = gh.get_repo(repo_name)

    issue = state["current_issue"]
    fix_branch = state["fix_branch"]

    rule_short = issue["rule_key"].split(":")[-1] if ":" in issue["rule_key"] else issue["rule_key"]
    class_name = Path(state["file_path"]).stem

    title = f"fix(sonar): resolve {rule_short} in {class_name}.java [{confidence_label}]"
    body = _build_pr_body(state, confidence_label, before_snippet, after_snippet)
    is_draft = confidence_label == "MEDIUM"

    pr = gh_repo.create_pull(
        title=title,
        body=body,
        head=fix_branch,
        base=gh_repo.default_branch,
        draft=is_draft,
    )
    logger.info(f"[Deliver] PR #{pr.number} opened: {pr.html_url} (draft={is_draft})")

    # Apply sonar-ai label (create if missing)
    _ensure_label(gh_repo, pr)

    # Auto-assign from CODEOWNERS on HIGH confidence
    if confidence_label == "HIGH":
        _assign_codeowner(gh_repo, pr, state["file_path"])

    if confidence_label == "MEDIUM":
        pr.create_issue_comment(
            "⚠️ **Medium confidence fix** — this patch was automatically generated but "
            "the agent's confidence score is below the HIGH threshold. "
            "Please review the diff carefully before merging."
        )

    return pr.html_url


def _build_pr_body(
    state: AgentState,
    confidence_label: str,
    before_snippet: str,
    after_snippet: str,
) -> str:
    issue = state["current_issue"]
    planner = state.get("planner_output", {})
    generator = state.get("generator_output", {})
    validation = state.get("validation", {})

    badge = _confidence_badge(confidence_label)
    compile_icon = "✅" if validation.get("compile_ok") else "❌"
    test_icon = "✅" if validation.get("tests_ok") else "⚠️ skipped / failed"

    patch = generator.get("patch_hunks", "")
    if len(patch) > 4000:
        patch = patch[:4000] + "\n... (truncated — see commit diff for full patch)"

    reasoning = planner.get("reasoning", "_No reasoning captured._")
    strategy = planner.get("strategy", "_N/A_")
    confidence_score = planner.get("confidence", 0.0)

    concerns = state.get("critic_output", {}).get("concerns", [])
    concerns_md = (
        "\n".join(f"- {c}" for c in concerns)
        if concerns
        else "_None recorded._"
    )

    before_block = f"```java\n{before_snippet}\n```" if before_snippet else "_Not available._"
    after_block = f"```java\n{after_snippet}\n```" if after_snippet else "_Not available._"

    return f"""\
{badge}  **Confidence score: {confidence_score:.0%}**

## 🔍 SonarQube Issue
| Field | Value |
|-------|-------|
| Rule | `{issue['rule_key']}` |
| Severity | `{issue['severity']}` |
| File | `{Path(state['file_path']).name}` |
| Line | {issue['line']} |
| Message | {issue['message']} |

---

## 🤖 Agent Reasoning (Planner)
{reasoning}

**Fix strategy:** {strategy}

---

## 📸 Before / After

<details>
<summary>Before (around line {issue['line']})</summary>

{before_block}

</details>

<details>
<summary>After (around line {issue['line']})</summary>

{after_block}

</details>

---

## 📄 Full Patch
```diff
{patch}
```

---

## 🔎 Critic Notes
{concerns_md}

---

## ✅ Validation
| Check | Result |
|-------|--------|
| Diff applied cleanly | ✅ |
| Maven compile | {compile_icon} |
| Maven tests | {test_icon} |

---
*Generated by [SonarAI](https://github.com/sonar-ai) — automated Sonar remediation pipeline*
"""


def _ensure_label(gh_repo, pr) -> None:
    """Apply 'sonar-ai' label to the PR; create the label if it doesn't exist."""
    label_name = "sonar-ai"
    try:
        try:
            label = gh_repo.get_label(label_name)
        except GithubException:
            label = gh_repo.create_label(label_name, "0075ca", "Auto-fix by SonarAI")
        pr.add_to_labels(label)
    except Exception as exc:
        logger.warning(f"[Deliver] Could not apply label '{label_name}': {exc}")


def _assign_codeowner(gh_repo, pr, file_path: str) -> None:
    """Read CODEOWNERS and request review from the matching owner or team."""
    try:
        # Try both .github/CODEOWNERS and root CODEOWNERS
        for path in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
            try:
                content = gh_repo.get_contents(path).decoded_content.decode()
                break
            except GithubException:
                content = None
        if not content:
            logger.debug("[Deliver] No CODEOWNERS file found")
            return

        owner = _match_codeowner(content, file_path)
        if not owner:
            return

        handle = owner.lstrip("@")
        if "/" in handle:
            # Team: "org/team-name"
            org_name, team_slug = handle.split("/", 1)
            pr.create_review_request(team_reviewers=[team_slug])
            logger.info(f"[Deliver] Requested review from team: {handle}")
        else:
            pr.create_review_request(reviewers=[handle])
            logger.info(f"[Deliver] Requested review from: {handle}")
    except Exception as exc:
        logger.warning(f"[Deliver] CODEOWNERS assignment failed: {exc}")


def _match_codeowner(content: str, file_path: str) -> Optional[str]:
    """
    Find the last matching CODEOWNERS rule (later rules take precedence).
    Handles *.java, /path/pattern, and ** globs.
    """
    file_rel = file_path  # may be absolute; we match against the name too
    file_name = Path(file_path).name
    matched_owner: Optional[str] = None

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, owner = parts[0], parts[1]
        if _glob_match(pattern, file_rel) or _glob_match(pattern, file_name):
            matched_owner = owner  # last match wins

    return matched_owner


def _glob_match(pattern: str, target: str) -> bool:
    import fnmatch
    # Normalise: strip leading slash from pattern
    pat = pattern.lstrip("/")
    return (
        fnmatch.fnmatch(target, pat)
        or fnmatch.fnmatch(Path(target).name, pat)
        or fnmatch.fnmatch(target, f"**/{pat}")
    )


# ── Escalation ────────────────────────────────────────────────────────────────

def _write_escalation(
    state: AgentState, confidence_label: str, extra_note: str = ""
) -> str:
    """Write an escalation markdown file and return its path."""
    issue = state["current_issue"]
    planner = state.get("planner_output", {})
    generator = state.get("generator_output", {})
    validation = state.get("validation", {})

    esc_dir = Path(settings.escalation_dir)
    esc_dir.mkdir(parents=True, exist_ok=True)

    rule_short = issue["rule_key"].split(":")[-1] if ":" in issue["rule_key"] else issue["rule_key"]
    # Sanitise key for filename
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "", issue["key"])[:12]
    filename = f"{safe_key}_{rule_short}.md"
    esc_path = esc_dir / filename

    patch = generator.get("patch_hunks", "_No patch generated._")
    compiler_err = validation.get("compiler_error") or "None"
    test_err = validation.get("test_error") or "None"
    reasoning = planner.get("reasoning", "_Not available._")
    strategy = planner.get("strategy", "_Not available._")

    content = f"""\
# Escalation — {issue['rule_key']} in {Path(state.get('file_path', 'unknown')).name}

> **Action required:** This issue could not be auto-fixed with sufficient confidence.
> Reason: **Confidence = {confidence_label}** / Validation passed = {validation.get('diff_ok') and validation.get('compile_ok')}

---

## Issue Details
| Field | Value |
|-------|-------|
| Rule | `{issue['rule_key']}` |
| Severity | `{issue['severity']}` |
| File | `{state.get('file_path', 'unknown')}` |
| Line | {issue['line']} |
| Message | {issue['message']} |

## Agent Reasoning
{reasoning}

## Suggested Fix Strategy
{strategy}

## Generated Patch (for reference — may be partially correct)
```diff
{patch}
```

## Validation Results
| Check | Result |
|-------|--------|
| Diff applied | {validation.get('diff_ok', False)} |
| Maven compile | {validation.get('compile_ok', False)} |
| Maven tests | {validation.get('tests_ok', False)} |

### Compiler Error
```
{compiler_err}
```

### Test Failure
```
{test_err}
```
{chr(10) + '## Additional Note' + chr(10) + extra_note if extra_note else ''}

---
*Generated by SonarAI escalation handler — review and fix manually*
"""

    esc_path.write_text(content, encoding="utf-8")
    logger.info(f"[Deliver] Escalation written: {esc_path}")
    return str(esc_path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _repo_name_from_url(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL."""
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", url)
    if match:
        return match.group(1)
    raise ValueError(f"Cannot extract repo name from URL: {url}")


def _read_method_region(file_path: str, flagged_line: int, lines: int = 15) -> str:
    """
    Read ±lines lines around flagged_line from the file on disk.
    Returns a numbered snippet or empty string on any error.
    """
    if not file_path or not flagged_line:
        return ""
    try:
        source_lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(source_lines)
        start = max(0, flagged_line - lines - 1)
        end = min(total, flagged_line + lines)
        numbered = "\n".join(
            f"{start + i + 1:4d}  {line}" for i, line in enumerate(source_lines[start:end])
        )
        return numbered
    except Exception as exc:
        logger.debug(f"[Deliver] Could not read snippet from {file_path}: {exc}")
        return ""