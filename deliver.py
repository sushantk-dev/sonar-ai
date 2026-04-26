"""
SonarAI — PR Delivery & Escalation
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

from sonar_ai.config import settings
from sonar_ai.state import AgentState


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
    generator = state.get("generator_output", {})
    validation = state.get("validation", {})

    confidence_score: float = planner.get("confidence", 0.0)
    confidence_label = _confidence_label(confidence_score)
    validation_passed = validation.get("diff_ok") and validation.get("compile_ok")

    logger.info(
        f"[Deliver] confidence={confidence_label}({confidence_score:.2f}) "
        f"validation_passed={validation_passed}"
    )

    if confidence_label == "LOW" or not validation_passed:
        path = _write_escalation(state, confidence_label)
        return {**state, "escalation_path": path, "done": True}

    # Commit the fix
    try:
        _commit_fix(state)
        _push_branch(state)
    except Exception as exc:
        logger.error(f"[Deliver] Git commit/push failed: {exc}")
        path = _write_escalation(state, confidence_label, extra_note=str(exc))
        return {**state, "escalation_path": path, "done": True}

    # Open the PR
    try:
        pr_url = _open_pr(state, confidence_label)
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
    commit_msg = f"fix(sonar): resolve {rule_short} in {class_name}.java\n\n{issue['message']}"

    repo.index.add([file_path])
    repo.index.commit(commit_msg)
    logger.info(f"[Deliver] Committed fix: {commit_msg[:60]!r}")


def _push_branch(state: AgentState) -> None:
    repo = git.Repo(state["repo_local_path"])
    branch = state.get("fix_branch", "")
    repo.remotes.origin.push(refspec=f"{branch}:{branch}")
    logger.info(f"[Deliver] Pushed branch {branch}")


# ── GitHub PR ─────────────────────────────────────────────────────────────────

def _open_pr(state: AgentState, confidence_label: str) -> str:
    """Open a GitHub PR and return the PR URL."""
    gh = Github(settings.github_token, base_url=settings.github_base_url)

    repo_url = state["repo_url"]
    repo_name = _repo_name_from_url(repo_url)
    gh_repo = gh.get_repo(repo_name)

    issue = state["current_issue"]
    fix_branch = state["fix_branch"]
    planner = state.get("planner_output", {})
    generator = state.get("generator_output", {})
    validation = state.get("validation", {})

    rule_short = issue["rule_key"].split(":")[-1] if ":" in issue["rule_key"] else issue["rule_key"]
    class_name = Path(state["file_path"]).stem

    title = f"fix(sonar): resolve {rule_short} in {class_name}.java [{confidence_label}]"
    body = _build_pr_body(state, confidence_label)

    is_draft = confidence_label == "MEDIUM"

    pr = gh_repo.create_pull(
        title=title,
        body=body,
        head=fix_branch,
        base=gh_repo.default_branch,
        draft=is_draft,
    )
    logger.info(f"[Deliver] PR opened: {pr.html_url} (draft={is_draft})")

    # Auto-assign from CODEOWNERS on HIGH confidence
    if confidence_label == "HIGH":
        _assign_codeowner(gh_repo, pr, state["file_path"])

    if confidence_label == "MEDIUM":
        pr.create_issue_comment(
            "⚠️ **Medium confidence fix** — automated analysis suggests a review is needed "
            "before merging. Please inspect the diff carefully."
        )

    return pr.html_url


def _build_pr_body(state: AgentState, confidence_label: str) -> str:
    issue = state["current_issue"]
    planner = state.get("planner_output", {})
    generator = state.get("generator_output", {})
    validation = state.get("validation", {})

    badge = _confidence_badge(confidence_label)
    compile_icon = "✅" if validation.get("compile_ok") else "❌"
    test_icon = "✅" if validation.get("tests_ok") else "⚠️"

    patch = generator.get("patch_hunks", "")
    # Trim patch for readability in PR body
    if len(patch) > 3000:
        patch = patch[:3000] + "\n... (truncated)"

    return f"""\
{badge}

## 🔍 SonarQube Issue
| Field | Value |
|-------|-------|
| Rule | `{issue['rule_key']}` |
| Severity | `{issue['severity']}` |
| File | `{Path(state['file_path']).name}` |
| Line | {issue['line']} |
| Message | {issue['message']} |

## 🤖 Agent Reasoning
{planner.get('reasoning', '_No reasoning captured._')}

**Strategy:** {planner.get('strategy', '_N/A_')}

## 📄 Patch
```diff
{patch}
```

## ✅ Validation
| Check | Result |
|-------|--------|
| Diff Applied | ✅ |
| Maven Compile | {compile_icon} |
| Maven Tests | {test_icon} |

---
*Generated by SonarAI — automated Sonar remediation pipeline*
"""


def _assign_codeowner(gh_repo, pr, file_path: str) -> None:
    """Read CODEOWNERS and request review from the matching owner."""
    try:
        codeowners_content = gh_repo.get_contents("CODEOWNERS").decoded_content.decode()
        owner = _match_codeowner(codeowners_content, file_path)
        if owner:
            pr.create_review_request(reviewers=[owner.lstrip("@")])
            logger.info(f"[Deliver] Requested review from CODEOWNER: {owner}")
    except Exception as exc:
        logger.warning(f"[Deliver] CODEOWNERS lookup failed: {exc}")


def _match_codeowner(content: str, file_path: str) -> Optional[str]:
    """Find the last matching CODEOWNERS rule for the given file path."""
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
        # Simple glob: *.java, specific path, or wildcard
        if _glob_match(pattern, file_path) or _glob_match(pattern, file_name):
            matched_owner = owner

    return matched_owner


def _glob_match(pattern: str, target: str) -> bool:
    """Very simple glob matching (fnmatch-style for CODEOWNERS)."""
    import fnmatch
    return fnmatch.fnmatch(target, pattern) or fnmatch.fnmatch(Path(target).name, pattern)


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
    filename = f"{issue['key'][:8]}_{rule_short}.md"
    esc_path = esc_dir / filename

    content = f"""\
# Escalation — {issue['rule_key']} in {Path(state.get('file_path', 'unknown')).name}

**Reason:** Confidence = {confidence_label} / Validation failed — human review required.

## Issue Details
- **Rule:** {issue['rule_key']}
- **Severity:** {issue['severity']}
- **File:** {state.get('file_path', 'unknown')}
- **Line:** {issue['line']}
- **Message:** {issue['message']}

## Agent Reasoning
{planner.get('reasoning', '_Not available._')}

## Suggested Fix Strategy
{planner.get('strategy', '_Not available._')}

## Generated Patch (for reference)
```diff
{generator.get('patch_hunks', '_No patch generated._')}
```

## Validation Results
- Diff applied: {validation.get('diff_ok')}
- Compile OK: {validation.get('compile_ok')}
- Tests OK: {validation.get('tests_ok')}

### Compiler Error
```
{validation.get('compiler_error') or 'None'}
```

### Test Error
```
{validation.get('test_error') or 'None'}
```

{('## Additional Note\n' + extra_note) if extra_note else ''}

---
*Generated by SonarAI escalation handler*
"""

    esc_path.write_text(content, encoding="utf-8")
    logger.info(f"[Deliver] Escalation written: {esc_path}")
    return str(esc_path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _repo_name_from_url(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL."""
    # https://github.com/owner/repo[.git]
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)
    raise ValueError(f"Cannot extract repo name from URL: {url}")
