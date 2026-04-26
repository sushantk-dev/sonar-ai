"""
SonarAI — Diff Repair  (v2)

Two complementary strategies applied in sequence:

Strategy A — Offset correction (fast):
  Parse the patch hunks, locate the removed lines (-) in the actual file by
  exact text match, rewrite the @@ header with the correct 1-based offset.
  This handles the common case where the model's +/- lines are right but the
  @@ numbers are wrong.

Strategy B — Full rebuild (fallback):
  If Strategy A still can't be applied, extract what the model intended to
  delete and add, find the deletion target in the file, and synthesise a
  brand-new unified diff from scratch using difflib.  This is immune to any
  @@ header error.

normalise_diff_paths():
  Rewrites --- / +++ headers to use the correct relative POSIX path.
  Must be called before repair_diff().
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Optional

from loguru import logger


# ── Public API ────────────────────────────────────────────────────────────────

def repair_diff(patch: str, file_path: str) -> str:
    """
    Return a version of ``patch`` that git apply will accept against ``file_path``.
    Steps applied in order:
      0. Inject missing --- / +++ file headers if the patch starts with @@
      A. Fix wrong @@ offsets (locate removed lines in file, rewrite header)
      B. Full rebuild via difflib if A still doesn't apply
    Returns original patch unchanged if all steps fail.
    """
    if not file_path or not Path(file_path).exists():
        return patch

    try:
        file_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patch

    file_lines = file_text.splitlines()

    # ── Step 0: inject missing file headers ──────────────────────────────────
    patch = _inject_file_headers(patch, file_path)

    # ── Strategy A: fix offsets ───────────────────────────────────────────────
    try:
        fixed = _fix_offsets(patch, file_lines)
        if fixed != patch:
            logger.info("[DiffRepair] Strategy A: @@ offsets corrected")
            return fixed
    except Exception as exc:
        logger.debug(f"[DiffRepair] Strategy A failed: {exc}")

    # ── Strategy B: full rebuild from intended changes ────────────────────────
    try:
        rebuilt = _rebuild_from_intent(patch, file_lines, file_path)
        if rebuilt:
            logger.info("[DiffRepair] Strategy B: diff rebuilt from intent")
            return rebuilt
    except Exception as exc:
        logger.debug(f"[DiffRepair] Strategy B failed: {exc}")

    logger.warning("[DiffRepair] All strategies failed — using original patch")
    return patch


def _inject_file_headers(patch: str, file_path: str) -> str:
    """
    If the patch starts with @@ (missing --- / +++ headers), prepend them.
    git apply requires file headers before the first hunk.
    """
    stripped = patch.lstrip()
    if not stripped.startswith("@@"):
        return patch   # headers already present

    # Derive a sensible relative path from just the filename
    # (normalise_diff_paths will fix it to the real relative path afterwards
    #  if called in the right order — but we need *something* here so git
    #  doesn't reject the patch before we even get to apply it)
    fname = Path(file_path).name
    header = f"--- a/{fname}\n+++ b/{fname}\n"
    logger.info(f"[DiffRepair] Injected missing file headers for {fname}")
    return header + stripped


def normalise_diff_paths(patch: str, repo_root: str, file_path: str) -> str:
    """
    Rewrite --- / +++ lines to use the correct relative POSIX path.
    Fixes absolute paths, Windows backslashes, and wrong filenames.
    """
    if not patch:
        return patch
    try:
        rel_posix = Path(file_path).relative_to(repo_root).as_posix()
    except ValueError:
        rel_posix = Path(file_path).name

    out = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("--- "):
            out.append(f"--- a/{rel_posix}\n")
        elif line.startswith("+++ "):
            out.append(f"+++ b/{rel_posix}\n")
        else:
            out.append(line)
    return "".join(out)


# ── Strategy A: offset correction ────────────────────────────────────────────

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", re.MULTILINE)
_FILE_HDR_RE = re.compile(r"^(---|\+\+\+) ", re.MULTILINE)


def _fix_offsets(patch: str, file_lines: list[str]) -> str:
    """Rewrite each @@ header so old_start points to where the removed lines live."""
    file_stripped = [l.rstrip() for l in file_lines]
    result: list[str] = []
    lines = patch.splitlines(keepends=True)
    i = 0

    while i < len(lines):
        line = lines[i]
        m = _HUNK_RE.match(line)
        if not m:
            result.append(line)
            i += 1
            continue

        # Collect hunk body
        hunk_body: list[str] = []
        i += 1
        while i < len(lines) and not _HUNK_RE.match(lines[i]) and not _FILE_HDR_RE.match(lines[i]):
            hunk_body.append(lines[i])
            i += 1

        # Lines the model says to remove (stripped of leading '-')
        removed = [l[1:].rstrip() for l in hunk_body if l.startswith("-")]

        if not removed:
            # Addition-only hunk — use context to anchor
            ctx = [l[1:].rstrip() for l in hunk_body if l.startswith(" ") and l[1:].strip()]
            anchor_1based = _find_sequence(ctx, file_stripped) if ctx else None
        else:
            anchor_1based = _find_sequence(removed, file_stripped)

        if anchor_1based is None:
            # Can't locate — keep original header
            result.append(line)
            result.extend(hunk_body)
            continue

        old_count = sum(1 for l in hunk_body if not l.startswith("+"))
        new_count = sum(1 for l in hunk_body if not l.startswith("-"))
        suffix = m.group(5) or ""
        new_start = anchor_1based + (int(m.group(3)) - int(m.group(1)))
        new_hdr = f"@@ -{anchor_1based},{old_count} +{new_start},{new_count} @@{suffix}\n"
        result.append(new_hdr)
        result.extend(hunk_body)

    return "".join(result)


def _find_sequence(needles: list[str], haystack: list[str]) -> Optional[int]:
    """
    Find the 1-based index in haystack where needles appear as a contiguous
    subsequence (ignoring trailing whitespace).  Returns None if not found.
    """
    if not needles:
        return None
    n = len(needles)
    for i in range(len(haystack) - n + 1):
        if haystack[i : i + n] == needles:
            return i + 1
    return None


# ── Strategy B: rebuild from intent ──────────────────────────────────────────

def _rebuild_from_intent(patch: str, file_lines: list[str], file_path: str) -> Optional[str]:
    """
    Extract the model's intended deletions and additions from the patch,
    apply them to the file in memory, and produce a fresh unified diff.
    """
    hunks = _parse_hunks(patch)
    if not hunks:
        return None

    file_stripped = [l.rstrip() for l in file_lines]
    # Work on a mutable copy (preserve original indentation)
    new_lines = list(file_lines)
    offset = 0  # cumulative line shift from previous hunks

    for removed, added in hunks:
        if not removed:
            # Pure insertion — can't locate without context; skip this hunk
            continue

        pos = _find_sequence(removed, file_stripped)
        if pos is None:
            logger.debug(f"[DiffRepair-B] Could not locate removed lines: {removed[:2]}")
            return None

        # Apply: replace removed lines with added lines at pos (1-based → 0-based)
        idx = pos - 1 + offset
        new_lines[idx : idx + len(removed)] = added
        offset += len(added) - len(removed)

    rel_path = Path(file_path).name
    old_text = [l + "\n" for l in file_lines]
    new_text = [l.rstrip("\n") + "\n" for l in new_lines]

    diff = list(difflib.unified_diff(
        old_text, new_text,
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
    ))
    if not diff:
        return None
    return "\n".join(diff) + "\n"


def _parse_hunks(patch: str) -> list[tuple[list[str], list[str]]]:
    """
    Parse a unified diff into (removed_lines, added_lines) pairs per hunk.
    Strips the leading -/+ character and trailing whitespace.
    """
    hunks: list[tuple[list[str], list[str]]] = []
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        if _HUNK_RE.match(lines[i]):
            removed, added = [], []
            i += 1
            while i < len(lines) and not _HUNK_RE.match(lines[i]) and not _FILE_HDR_RE.match(lines[i]):
                l = lines[i]
                if l.startswith("-"):
                    removed.append(l[1:].rstrip())
                elif l.startswith("+"):
                    added.append(l[1:])   # preserve indentation on additions
                i += 1
            hunks.append((removed, added))
        else:
            i += 1
    return hunks
