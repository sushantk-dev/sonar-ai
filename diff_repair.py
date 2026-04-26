"""
SonarAI — Diff Repair  (v3)

Pre-processing steps applied before git apply, in order:

  0a. _inject_file_headers  — prepend --- / +++ if patch starts with @@
  0b. _fix_hunk_headers     — canonicalise malformed @@ lines
                              (spaces after commas, missing spaces, etc.)
  A.  _fix_offsets          — locate removed lines in file, rewrite @@ start
  B.  _rebuild_from_intent  — full difflib rebuild if A still fails

normalise_diff_paths() rewrites --- / +++ to the correct repo-relative path.
Call order in validator: repair_diff() then normalise_diff_paths().
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
    All steps are applied in order; early exit when a step produces a change.
    """
    if not file_path or not Path(file_path).exists():
        return patch

    try:
        file_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patch

    file_lines = file_text.splitlines()

    # -1 — strip markdown code fences that the LLM embeds inside the diff string
    patch = _strip_markdown_fences(patch)

    # 0a — inject missing file headers (patch starts with @@)
    patch = _inject_file_headers(patch, file_path)

    # 0b — fix malformed @@ syntax (spaces after commas, missing spaces, etc.)
    patch = _fix_hunk_headers(patch)

    # A — fix wrong line offsets
    try:
        fixed = _fix_offsets(patch, file_lines)
        if fixed != patch:
            logger.info("[DiffRepair] Strategy A: @@ offsets corrected")
            return fixed
    except Exception as exc:
        logger.debug(f"[DiffRepair] Strategy A failed: {exc}")

    # B — full rebuild from intended +/- lines
    try:
        rebuilt = _rebuild_from_intent(patch, file_lines, file_path)
        if rebuilt:
            logger.info("[DiffRepair] Strategy B: diff rebuilt from intent")
            return rebuilt
    except Exception as exc:
        logger.debug(f"[DiffRepair] Strategy B failed: {exc}")

    logger.warning("[DiffRepair] All strategies failed — using original patch")
    return patch


def normalise_diff_paths(patch: str, repo_root: str, file_path: str) -> str:
    """
    Rewrite --- / +++ lines to use the correct relative POSIX path.
    Fixes absolute paths, Windows backslashes, and wrong filenames.
    Call AFTER repair_diff() so injected headers get corrected too.
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


# ── Step -1: strip markdown code fences ──────────────────────────────────────

def _strip_markdown_fences(patch: str) -> str:
    """
    Remove markdown code fences that the LLM embeds inside the diff string.

    Handles all of:
      ```diff\\n...\\n```        (with language tag)
      ```\\n...\\n```            (no language tag)
      literal \\n in the string (double-escaped by JSON serialisation)
    """
    if not patch:
        return patch

    # Unescape literal \\n / \\r\\n sequences (LLM JSON artefact)
    patch = patch.replace("\\r\\n", "\n").replace("\\n", "\n")

    # Opening fence: ```diff, ```java, ```patch, ```text, ``` etc.
    patch = re.sub(r"^```[a-zA-Z]*\s*\n?", "", patch.lstrip(), flags=re.MULTILINE)

    # Closing fence: ``` on its own line
    patch = re.sub(r"\n?^```\s*$", "", patch.rstrip(), flags=re.MULTILINE)

    stripped = patch.strip()
    if stripped != patch:
        logger.info("[DiffRepair] Step -1: stripped markdown code fences from patch")
    return stripped


# ── Step 0a: inject missing file headers ─────────────────────────────────────

def _inject_file_headers(patch: str, file_path: str) -> str:
    """Prepend --- / +++ headers when the patch starts directly with @@."""
    stripped = patch.lstrip()
    if not stripped.startswith("@@"):
        return patch
    fname = Path(file_path).name
    header = f"--- a/{fname}\n+++ b/{fname}\n"
    logger.info(f"[DiffRepair] Injected missing file headers for {fname}")
    return header + stripped


# ── Step 0b: fix malformed @@ lines ──────────────────────────────────────────

# Permissive regex: captures digits even when whitespace surrounds the comma
_BAD_HUNK_RE = re.compile(
    r"^@@\s*"                           # opening @@
    r"-\s*(\d+)\s*(?:,\s*(\d+))?\s*"   # old range:  -start[,count]
    r"\+\s*(\d+)\s*(?:,\s*(\d+))?\s*"  # new range:  +start[,count]
    r"@@(.*)",                          # closing @@ + optional suffix
)


def _fix_hunk_headers(patch: str) -> str:
    """
    Rewrite every @@ line to the canonical form:
        @@ -<start>,<count> +<start>,<count> @@[ method_name]

    Fixes all of:
      @@ -36, 8 +37,8 @@        space after comma
      @@ -36,8 +37, 8 @@        space after comma (new range)
      @@ -36 ,8 +37,8 @@        space before comma
      @@ - 36,8 + 37,8 @@       space after sign
      @@-36,8 +37,8@@           no spaces around @@
      @@ -36 +37 @@             missing count (defaults to 1)
      @@ -39,6 +40,7 @@?        trailing ? or other punctuation after @@
    """
    out = []
    changed = False
    for line in patch.splitlines(keepends=True):
        if "@@" not in line or not line.lstrip().startswith("@@"):
            out.append(line)
            continue

        m = _BAD_HUNK_RE.match(line.strip())
        if not m:
            out.append(line)
            continue

        old_start = m.group(1)
        old_count = m.group(2) if m.group(2) is not None else "1"
        new_start = m.group(3)
        new_count = m.group(4) if m.group(4) is not None else "1"
        suffix    = m.group(5)

        # Strip trailing punctuation/garbage from the suffix.
        # Valid suffix is either empty or a method name starting with a space.
        # Anything else (?, !, trailing dots, etc.) is an LLM artefact.
        suffix = _clean_hunk_suffix(suffix)

        canonical = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{suffix}\n"
        if canonical.rstrip() != line.rstrip():
            logger.info(
                f"[DiffRepair] Fixed @@ syntax: {line.rstrip()!r} → {canonical.rstrip()!r}"
            )
            changed = True
        out.append(canonical)

    if changed:
        logger.info("[DiffRepair] Step 0b: malformed @@ headers corrected")
    return "".join(out)


def _clean_hunk_suffix(suffix: str) -> str:
    """
    The optional text after the closing @@ is normally a method name like
    ' processTemplateFromFile'.  Strip any trailing punctuation that isn't
    part of a valid Java identifier (?, !, ., trailing spaces, etc.).
    Keep a leading space if the suffix contains a word character.
    """
    if not suffix:
        return ""
    # Remove trailing non-identifier characters (anything after the last word char)
    cleaned = re.sub(r"[^\w\s]+$", "", suffix).rstrip()
    # Re-add leading space if there's content
    if cleaned.strip():
        return " " + cleaned.strip()
    return ""


# ── Strategy A: offset correction ────────────────────────────────────────────

_HUNK_RE   = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", re.MULTILINE)
_FILE_HDR  = re.compile(r"^(---|\+\+\+) ", re.MULTILINE)


def _fix_offsets(patch: str, file_lines: list[str]) -> str:
    """Rewrite each @@ header so old_start matches where the removed lines live."""
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
        while i < len(lines) and not _HUNK_RE.match(lines[i]) and not _FILE_HDR.match(lines[i]):
            hunk_body.append(lines[i])
            i += 1

        removed = [l[1:].rstrip() for l in hunk_body if l.startswith("-")]

        if not removed:
            ctx = [l[1:].rstrip() for l in hunk_body if l.startswith(" ") and l[1:].strip()]
            anchor = _find_sequence(ctx, file_stripped) if ctx else None
        else:
            anchor = _find_sequence(removed, file_stripped)
        if anchor is None:
            result.append(line)
            result.extend(hunk_body)
            continue

        old_count = sum(1 for l in hunk_body if not l.startswith("+"))
        new_count = sum(1 for l in hunk_body if not l.startswith("-"))
        new_plus  = anchor + (int(m.group(3)) - int(m.group(1)))
        suffix    = _clean_hunk_suffix(m.group(5) or "")
        result.append(f"@@ -{anchor},{old_count} +{new_plus},{new_count} @@{suffix}\n")
        result.extend(hunk_body)

    return "".join(result)


def _find_sequence(needles: list[str], haystack: list[str]) -> Optional[int]:
    """1-based index of the first contiguous match of needles in haystack."""
    if not needles:
        return None
    n = len(needles)
    for i in range(len(haystack) - n + 1):
        if haystack[i : i + n] == needles:
            return i + 1
    return None


# ── Strategy B: full rebuild ──────────────────────────────────────────────────

def _rebuild_from_intent(patch: str, file_lines: list[str], file_path: str) -> Optional[str]:
    """
    Apply each hunk's intended changes in memory and produce a fresh unified diff.

    Handles three hunk types:
      - Remove + add  : find removed lines, replace with added lines
      - Remove only   : find removed lines, delete them
      - Add only      : find the context lines before/after, insert after the
                        last pre-insertion context line (pure insertion hunk)
    """
    hunks = _parse_hunks(patch)
    if not hunks:
        return None

    file_stripped = [l.rstrip() for l in file_lines]
    new_lines = list(file_lines)
    offset = 0  # cumulative shift from previous hunks

    for removed, added, context_before, context_after in hunks:
        if removed:
            # Locate the lines to remove
            pos = _find_sequence(removed, file_stripped)
            if pos is None:
                logger.debug(f"[DiffRepair-B] Cannot locate removed: {removed[:2]}")
                return None
            idx = pos - 1 + offset
            new_lines[idx : idx + len(removed)] = added
            offset += len(added) - len(removed)

        elif added:
            # Pure insertion: anchor using context_before (lines just before insertion)
            insert_after = _find_insertion_point(context_before, context_after, file_stripped)
            if insert_after is None:
                logger.debug(f"[DiffRepair-B] Cannot anchor insertion for: {added[:2]}")
                # Skip this hunk — don't fail the whole rebuild for a missing import
                continue
            idx = insert_after + offset  # insert_after is 0-based (insert AFTER this index)
            new_lines[idx:idx] = added
            offset += len(added)

    rel_path = Path(file_path).name
    diff = list(difflib.unified_diff(
        [l + "\n" for l in file_lines],
        [l.rstrip("\n") + "\n" for l in new_lines],
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
    ))
    if not diff:
        return None
    return "\n".join(diff) + "\n"


def _find_insertion_point(
    context_before: list[str],
    context_after: list[str],
    file_stripped: list[str],
) -> Optional[int]:
    """
    Find the 0-based index after which to insert lines.
    Tries context_before first (insert after the last pre-insertion context line),
    then context_after (insert before the first post-insertion context line).
    Returns 0-based index to insert after, or None.
    """
    if context_before:
        # Find the last context_before line in the file and insert after it
        for anchor in reversed(context_before):
            if not anchor.strip():
                continue
            for i in range(len(file_stripped) - 1, -1, -1):
                if file_stripped[i] == anchor:
                    return i + 1  # insert after index i
    if context_after:
        # Insert before the first context_after line
        anchor = next((l for l in context_after if l.strip()), None)
        if anchor:
            for i, line in enumerate(file_stripped):
                if line == anchor:
                    return i  # insert before index i = insert after index i-1
    return None


def _parse_hunks(
    patch: str,
) -> list[tuple[list[str], list[str], list[str], list[str]]]:
    """
    Parse a unified diff into (removed, added, context_before, context_after) tuples.

    context_before: unchanged lines (starting with ' ') before the first +/- line
    context_after:  unchanged lines after the last +/- line
    """
    hunks: list[tuple[list[str], list[str], list[str], list[str]]] = []
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        if _HUNK_RE.match(lines[i]):
            removed: list[str] = []
            added: list[str] = []
            context_before: list[str] = []
            context_after: list[str] = []
            seen_change = False
            i += 1
            hunk_body: list[str] = []
            while i < len(lines) and not _HUNK_RE.match(lines[i]) and not _FILE_HDR.match(lines[i]):
                hunk_body.append(lines[i])
                i += 1

            for l in hunk_body:
                if l.startswith("-"):
                    removed.append(l[1:].rstrip())
                    seen_change = True
                elif l.startswith("+"):
                    added.append(l[1:])
                    seen_change = True
                elif l.startswith(" "):
                    ctx_line = l[1:].rstrip()
                    if not seen_change:
                        context_before.append(ctx_line)
                    else:
                        context_after.append(ctx_line)

            hunks.append((removed, added, context_before, context_after))
        else:
            i += 1
    return hunks
