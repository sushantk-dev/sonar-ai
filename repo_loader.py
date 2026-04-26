"""
SonarAI — Repository Loader
Clones the target repo, checks out the exact Sonar commit SHA, resolves .java file paths,
and extracts the method (or ±50-line slice) containing the flagged line.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import git
from loguru import logger

try:
    import javalang
    _JAVALANG_AVAILABLE = True
except ImportError:
    _JAVALANG_AVAILABLE = False
    logger.warning("javalang not available — will use raw line slices only")

from sonar_ai.state import SonarIssue


# ── Repo cloning ──────────────────────────────────────────────────────────────

def clone_repo(
    repo_url: str,
    clone_base_dir: str,
    github_token: str,
    commit_sha: str,
) -> git.Repo:
    """
    Clone ``repo_url`` into ``clone_base_dir/<repo-name>`` and check out ``commit_sha``.

    If the directory already exists (e.g. from a previous run), it opens the existing
    repo and fetches + checks out the target commit.

    Returns:
        A ``git.Repo`` object pointing at the local clone.
    """
    # Inject GitHub token into the URL for auth
    auth_url = _inject_token(repo_url, github_token)

    repo_name = _repo_name_from_url(repo_url)
    local_path = Path(clone_base_dir) / repo_name
    local_path.parent.mkdir(parents=True, exist_ok=True)

    if local_path.exists():
        logger.info(f"Repo already cloned at {local_path}, reusing")
        repo = git.Repo(local_path)
        repo.remotes.origin.fetch()
    else:
        logger.info(f"Cloning {repo_url} → {local_path}")
        repo = git.Repo.clone_from(auth_url, local_path)

    logger.info(f"Checking out commit {commit_sha}")
    repo.git.checkout(commit_sha)

    return repo


def create_fix_branch(repo: git.Repo, rule_key: str, issue_key: str) -> str:
    """
    Create and checkout a new fix branch.  Returns the branch name.
    Branch name format: fix/sonar-{rule_short}-{issue_key[:8]}
    """
    # Sanitise rule key: "java:S2259" → "S2259"
    rule_short = rule_key.split(":")[-1] if ":" in rule_key else rule_key
    branch_name = f"fix/sonar-{rule_short}-{issue_key[:8]}"

    try:
        repo.git.checkout("-b", branch_name)
        logger.info(f"Created fix branch: {branch_name}")
    except git.GitCommandError as exc:
        if "already exists" in str(exc):
            repo.git.checkout(branch_name)
            logger.info(f"Switched to existing fix branch: {branch_name}")
        else:
            raise

    return branch_name


# ── File resolution ───────────────────────────────────────────────────────────

def resolve_java_file(repo_local_path: str, component: str) -> Optional[str]:
    """
    Resolve a Sonar ``component`` path to an absolute .java file path.

    Sonar component format: ``project-key:src/main/java/com/example/Foo.java``

    Strategy:
    1. Strip the project-key prefix and join with repo root.
    2. If not found, fall back to rglob search by filename.

    Returns:
        Absolute path string, or None if the file cannot be located.
    """
    repo_root = Path(repo_local_path)

    # Strip "project-key:" prefix
    if ":" in component:
        relative_path = component.split(":", 1)[1]
    else:
        relative_path = component

    # Strategy 1: direct path join
    candidate = repo_root / relative_path
    if candidate.exists():
        logger.debug(f"Resolved component → {candidate}")
        return str(candidate)

    # Strategy 2: rglob by filename
    filename = Path(relative_path).name
    matches = list(repo_root.rglob(filename))
    if matches:
        # Prefer path that contains the most segments matching the component path
        best = _best_match(matches, relative_path)
        logger.debug(f"rglob fallback resolved {filename} → {best}")
        return str(best)

    logger.error(f"Cannot resolve component path: {component}")
    return None


def _best_match(candidates: list[Path], relative_path: str) -> Path:
    """Score candidates by how many path segments they share with relative_path."""
    target_parts = set(Path(relative_path).parts)

    def score(p: Path) -> int:
        return len(set(p.parts) & target_parts)

    return max(candidates, key=score)


# ── Method extraction via AST ─────────────────────────────────────────────────

def extract_method_context(file_path: str, flagged_line: int) -> str:
    """
    Extract the source of the method that contains ``flagged_line``.

    Uses javalang AST traversal when available; falls back to a ±50-line raw slice.

    Returns:
        A string with the method source (or raw slice) ready for LLM consumption.
    """
    source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()

    if _JAVALANG_AVAILABLE:
        context = _extract_via_ast(source, lines, flagged_line, file_path)
        if context:
            return context

    return _extract_raw_slice(lines, flagged_line, file_path)


def _extract_via_ast(
    source: str, lines: list[str], flagged_line: int, file_path: str
) -> Optional[str]:
    """
    Parse the Java file with javalang and find the MethodDeclaration whose body
    contains ``flagged_line``.  Returns formatted source or None on failure.
    """
    try:
        tree = javalang.parse.parse(source)
    except Exception as exc:
        logger.warning(
            f"javalang parse failed for {Path(file_path).name}: {exc}. "
            "Falling back to raw slice."
        )
        return None

    best_method = None
    best_start = 0

    for _, node in tree.filter(javalang.tree.MethodDeclaration):
        if node.position is None:
            continue
        method_start = node.position.line  # 1-based
        method_end = _estimate_method_end(lines, method_start)

        if method_start <= flagged_line <= method_end:
            # Prefer the innermost (latest-starting) enclosing method
            if method_start > best_start:
                best_start = method_start
                best_method = (method_start, method_end, node.name)

    if best_method is None:
        logger.debug(
            f"No MethodDeclaration enclosing line {flagged_line} in {Path(file_path).name}"
        )
        return None

    start, end, name = best_method
    # Convert to 0-based index
    method_lines = lines[start - 1 : end]
    logger.info(
        f"Extracted method '{name}' (lines {start}–{end}) from {Path(file_path).name}"
    )
    return _annotated_block(method_lines, start, file_path)


def _estimate_method_end(lines: list[str], method_start: int) -> int:
    """
    Estimate where a method ends by counting braces from ``method_start``.
    Returns the line number (1-based) of the closing brace.
    """
    depth = 0
    found_open = False
    for i, line in enumerate(lines[method_start - 1 :], start=method_start):
        opens = line.count("{")
        closes = line.count("}")
        if opens > 0:
            found_open = True
        depth += opens - closes
        if found_open and depth <= 0:
            return i
    return len(lines)  # fallback: end of file


def _extract_raw_slice(lines: list[str], flagged_line: int, file_path: str) -> str:
    """Return ±50 lines around the flagged line as a numbered block."""
    total = len(lines)
    start = max(0, flagged_line - 51)  # 0-based, 50 lines before
    end = min(total, flagged_line + 50)  # 50 lines after
    slice_lines = lines[start:end]
    logger.info(
        f"Raw slice fallback: lines {start + 1}–{end} of {Path(file_path).name}"
    )
    return _annotated_block(slice_lines, start + 1, file_path)


def _annotated_block(lines: list[str], first_line_num: int, file_path: str) -> str:
    """Return numbered source lines as a formatted code block string."""
    header = f"// File: {Path(file_path).name}\n"
    numbered = "\n".join(
        f"{first_line_num + i:4d}  {line}" for i, line in enumerate(lines)
    )
    return header + numbered


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject_token(url: str, token: str) -> str:
    """Inject a GitHub token into an HTTPS clone URL."""
    if not token or "x-access-token" in url:
        return url
    if url.startswith("https://github.com"):
        return url.replace("https://", f"https://x-access-token:{token}@")
    return url


def _repo_name_from_url(url: str) -> str:
    """Extract repo name from URL, strip .git suffix."""
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name
