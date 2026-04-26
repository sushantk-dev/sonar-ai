"""
SonarAI — Diff Repair
Attempts to salvage an LLM-generated unified diff whose @@ hunk offsets are wrong.

The most common LLM failure: the model writes correct +/- lines but uses the wrong
line numbers in the @@ header, so git apply --check rejects it with "hunk does not apply".

Strategy:
1. Parse the patch into hunks
2. For each hunk, search the target file for the context lines (lines starting with ' ')
   to find where the hunk actually belongs
3. Rewrite the @@ header with the correct offsets
4. Return the repaired patch

If repair fails (e.g. context lines not found), return the original patch unchanged
and let git apply produce the real error.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from loguru import logger


def repair_diff(patch: str, file_path: str) -> str:
    """
    Attempt to fix wrong @@ line offsets in a unified diff.
    Returns the (possibly repaired) patch string.
    """
    if not file_path or not Path(file_path).exists():
        return patch

    try:
        file_lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return patch

    try:
        repaired = _repair(patch, file_lines)
        if repaired != patch:
            logger.info("[DiffRepair] Hunk offsets corrected — retrying git apply")
        return repaired
    except Exception as exc:
        logger.debug(f"[DiffRepair] Repair attempt failed ({exc}) — using original patch")
        return patch


# ── Internal ──────────────────────────────────────────────────────────────────

_HUNK_HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)', re.MULTILINE)
_FILE_HEADER = re.compile(r'^(---|\+\+\+) .+', re.MULTILINE)


def _repair(patch: str, file_lines: list[str]) -> str:
    """Rewrite each hunk header with offsets derived from actual file content."""
    # Split patch into: file headers + hunks
    # We preserve the --- / +++ lines and only rewrite @@ lines
    result_parts: list[str] = []
    pos = 0
    lines = patch.splitlines(keepends=True)

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HUNK_HEADER.match(line)
        if not m:
            result_parts.append(line)
            i += 1
            continue

        # Collect all lines of this hunk
        hunk_lines = [line]
        i += 1
        while i < len(lines) and not _HUNK_HEADER.match(lines[i]) and not _FILE_HEADER.match(lines[i]):
            hunk_lines.append(lines[i])
            i += 1

        # Extract context lines (lines starting with ' ') from the hunk body
        context_lines = [
            l[1:].rstrip()              # strip leading ' ' and trailing whitespace
            for l in hunk_lines[1:]     # skip the @@ header itself
            if l.startswith(' ') and l[1:].strip()  # skip blank context lines
        ]

        # Try to find where these context lines appear in the file
        new_old_start = _find_context_in_file(context_lines, file_lines)

        if new_old_start is None:
            # Can't locate — keep the original hunk unchanged
            logger.debug("[DiffRepair] Could not locate context lines in file — keeping original hunk")
            result_parts.extend(hunk_lines)
            continue

        # Recount lines in hunk
        old_count = sum(1 for l in hunk_lines[1:] if not l.startswith('+'))
        new_count = sum(1 for l in hunk_lines[1:] if not l.startswith('-'))

        # Compute new + start: same offset from old start
        orig_old_start = int(m.group(1))
        orig_new_start = int(m.group(3))
        offset = orig_new_start - orig_old_start
        new_new_start = new_old_start + offset

        suffix = m.group(5) or ""
        new_header = f"@@ -{new_old_start},{old_count} +{new_new_start},{new_count} @@{suffix}\n"
        result_parts.append(new_header)
        result_parts.extend(hunk_lines[1:])

    return "".join(result_parts)


def _find_context_in_file(context_lines: list[str], file_lines: list[str]) -> Optional[int]:
    """
    Find the 1-based line number in file_lines where context_lines appear.
    Uses the first context line as an anchor, then verifies the others exist
    nearby (within the hunk window). Returns None if not found.
    """
    if not context_lines:
        return None

    file_stripped = [l.rstrip() for l in file_lines]
    anchor = context_lines[0].rstrip()

    for i, fline in enumerate(file_stripped):
        if fline != anchor:
            continue
        # Verify at least half the remaining context lines appear in the
        # next 20 lines (generous window to handle deleted lines in between)
        window = file_stripped[i : i + 20]
        matched = sum(1 for c in context_lines[1:] if c.rstrip() in window)
        if matched >= max(1, len(context_lines[1:]) // 2):
            return i + 1  # 1-based

    return None


def normalise_diff_paths(patch: str, repo_root: str, file_path: str) -> str:
    """
    Ensure --- / +++ headers use forward slashes (git expects POSIX paths).
    Also fixes the common LLM mistake of using absolute paths in diff headers.
    """
    if not patch:
        return patch

    try:
        rel = Path(file_path).relative_to(repo_root)
        # Always forward slashes regardless of OS
        rel_posix = rel.as_posix()
    except ValueError:
        rel_posix = Path(file_path).name

    lines = patch.splitlines(keepends=True)
    out = []
    for line in lines:
        if line.startswith("--- "):
            out.append(f"--- a/{rel_posix}\n")
        elif line.startswith("+++ "):
            out.append(f"+++ b/{rel_posix}\n")
        else:
            out.append(line)
    return "".join(out)
