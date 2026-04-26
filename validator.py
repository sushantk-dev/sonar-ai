"""
SonarAI — Patch Validator  (Phase 04 — hardened)
Applies the generated unified diff and runs mvn compile + mvn test.
Compile/test failures are fed back into the LLM retry prompt via ValidationResult.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from loguru import logger

from sonar_ai.config import settings
from sonar_ai.state import AgentState, ValidationResult
from sonar_ai.diff_repair import repair_diff, normalise_diff_paths

# Maximum characters of error output forwarded to the LLM retry prompt
_MAX_ERROR_CHARS = 2000


# ── Public entry point ────────────────────────────────────────────────────────

def validate(state: AgentState) -> AgentState:
    """
    LangGraph node — apply the diff and validate with Maven.

    All failures populate validation error fields so the Generator retry
    prompt can include them verbatim. Never raises — all exceptions are
    caught and surfaced as validation failures.
    """
    repo_path = state.get("repo_local_path", "")
    file_path = state.get("file_path", "")
    patch_hunks = state.get("generator_output", {}).get("patch_hunks", "")

    result: ValidationResult = {
        "diff_ok": False,
        "compile_ok": False,
        "tests_ok": False,
        "compiler_error": "",
        "test_error": "",
    }

    # ── Guard: empty patch ────────────────────────────────────────────────────
    if not patch_hunks or not patch_hunks.strip():
        msg = "Generator produced an empty patch — nothing to apply."
        logger.error(f"[Validator] {msg}")
        result["compiler_error"] = msg
        return {**state, "validation": result}

    # ── Guard: patch lacks diff markers ──────────────────────────────────────
    if "@@" not in patch_hunks or ("---" not in patch_hunks and "+++" not in patch_hunks):
        msg = (
            "Patch does not look like a valid unified diff "
            "(missing --- / +++ / @@ markers). "
            "Regenerate with proper unified diff format."
        )
        logger.error(f"[Validator] {msg}")
        result["compiler_error"] = msg
        return {**state, "validation": result}

    # ── Step 1: Repair & normalise the diff ──────────────────────────────────
    # Fix Windows backslashes in --- / +++ headers and wrong @@ offsets
    patch_hunks = normalise_diff_paths(patch_hunks, repo_path, file_path)
    patch_hunks = repair_diff(patch_hunks, file_path)

    # ── Step 2: Apply diff ────────────────────────────────────────────────────
    diff_ok, apply_error = _apply_diff(repo_path, patch_hunks)
    result["diff_ok"] = diff_ok

    if not diff_ok:
        logger.error(f"[Validator] Diff apply failed: {apply_error[:300]}")
        result["compiler_error"] = apply_error
        return {**state, "validation": result}

    logger.info("[Validator] Diff applied successfully")

    # ── Step 3: Maven compile ─────────────────────────────────────────────────
    module = _detect_maven_module(repo_path, file_path)
    if module:
        logger.info(f"[Validator] Maven module scoped to: {module}")
    compile_ok, compiler_error = _mvn_compile(repo_path, module)
    result["compile_ok"] = compile_ok
    result["compiler_error"] = _trim(compiler_error)

    if not compile_ok:
        logger.error(f"[Validator] Compile FAILED:\n{compiler_error[:400]}")
        # Revert the patch so the repo stays clean for a retry
        _revert_patch(repo_path, patch_hunks)
        return {**state, "validation": result}

    logger.info("[Validator] Maven compile ✅")

    # ── Step 4: Maven test ────────────────────────────────────────────────────
    class_name = _class_name_from_path(file_path)
    tests_ok, test_error = _mvn_test(repo_path, module, class_name)
    result["tests_ok"] = tests_ok
    result["test_error"] = _trim(test_error)

    if tests_ok:
        logger.info("[Validator] Maven tests ✅")
    else:
        logger.warning(f"[Validator] Tests FAILED:\n{test_error[:400]}")
        # Revert so retry starts from clean state
        _revert_patch(repo_path, patch_hunks)

    return {**state, "validation": result}


# ── Diff application ──────────────────────────────────────────────────────────

def _apply_diff(repo_path: str, patch_hunks: str) -> tuple[bool, str]:
    """
    Write patch to a temp file.
    1. Dry-run: git apply --check  (validates offsets without touching files)
    2. Real apply: git apply
    Falls back to --ignore-whitespace on the dry-run to give a clearer error.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(patch_hunks)
        patch_file = tmp.name

    try:
        # Dry run
        dry = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", patch_file],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if dry.returncode != 0:
            hint = _diff_apply_hint(dry.stderr)
            return False, f"git apply --check failed:\n{dry.stderr.strip()}\n{hint}"

        # Real apply
        apply = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", patch_file],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if apply.returncode != 0:
            return False, f"git apply failed:\n{apply.stderr.strip()}"

        return True, ""

    except subprocess.TimeoutExpired:
        return False, "git apply timed out after 30s"
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass


def _revert_patch(repo_path: str, patch_hunks: str) -> None:
    """Attempt to revert an applied patch via git apply -R (reverse)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(patch_hunks)
        patch_file = tmp.name
    try:
        result = subprocess.run(
            ["git", "apply", "-R", "--whitespace=nowarn", patch_file],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("[Validator] Patch reverted (repo restored for retry)")
        else:
            logger.warning(
                f"[Validator] Could not auto-revert patch: {result.stderr.strip()[:200]}. "
                "Run 'git checkout .' manually if needed."
            )
    except Exception as exc:
        logger.warning(f"[Validator] Revert exception: {exc}")
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass


def _diff_apply_hint(stderr: str) -> str:
    """Return a human-readable hint for common git apply failure patterns."""
    s = stderr.lower()
    if "does not exist in index" in s:
        return "HINT: The file path in the diff header does not match the repo. Check --- / +++ paths."
    if "already exists in index" in s:
        return "HINT: The file already contains these changes. The diff may be a duplicate."
    if "patch does not apply" in s or "hunk" in s:
        return (
            "HINT: Hunk offset mismatch — the @@ line numbers don't match the current file. "
            "Re-read the method context line numbers and regenerate the diff."
        )
    return ""


# ── Maven helpers ─────────────────────────────────────────────────────────────

def _mvn_compile(repo_path: str, module: Optional[str]) -> tuple[bool, str]:
    """Run mvn compile -q, scoped to module if found. Skips gracefully if mvn absent."""
    cmd = ["mvn", "compile", "-q", "--no-transfer-progress"]
    if module:
        cmd += ["-pl", module, "--also-make"]

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=settings.compile_timeout,
        )
        if result.returncode == 0:
            return True, ""
        # Extract only ERROR lines for a tighter LLM prompt
        error_lines = _extract_error_lines(result.stdout + result.stderr)
        return False, error_lines or (result.stdout + result.stderr)[-_MAX_ERROR_CHARS:]
    except FileNotFoundError:
        logger.warning("[Validator] mvn not found — compile step skipped (mark as passed)")
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"mvn compile timed out after {settings.compile_timeout}s"


def _mvn_test(
    repo_path: str, module: Optional[str], class_name: Optional[str]
) -> tuple[bool, str]:
    """Run mvn test, scoped to the affected class when possible."""
    cmd = ["mvn", "test", "--no-transfer-progress"]
    if module:
        cmd += ["-pl", module, "--also-make"]
    if class_name:
        # Try both <ClassName>Test and <ClassName>Tests naming conventions
        cmd += [f"-Dtest={class_name}Test,{class_name}Tests"]

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=settings.test_timeout,
        )
        if result.returncode == 0:
            return True, ""

        surefire_error = _parse_surefire_results(repo_path)
        error_text = surefire_error or _extract_error_lines(result.stdout + result.stderr)
        return False, error_text or (result.stdout + result.stderr)[-_MAX_ERROR_CHARS:]

    except FileNotFoundError:
        logger.warning("[Validator] mvn not found — test step skipped (mark as passed)")
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"mvn test timed out after {settings.test_timeout}s"


def _parse_surefire_results(repo_path: str) -> Optional[str]:
    """
    Parse surefire XML reports. Returns formatted failure summary or None.
    Handles both target/surefire-reports and nested module paths.
    """
    surefire_dirs = list(Path(repo_path).rglob("surefire-reports"))
    if not surefire_dirs:
        return None

    failures: list[str] = []
    for report_dir in surefire_dirs:
        for xml_file in sorted(report_dir.glob("TEST-*.xml")):
            try:
                tree = ET.parse(xml_file)
                root = tree.getroot()
                for testcase in root.findall(".//testcase"):
                    failure = testcase.find("failure")
                    error = testcase.find("error")
                    node = failure if failure is not None else error
                    if node is not None:
                        class_name = testcase.get("classname", "")
                        method_name = testcase.get("name", "")
                        msg = node.get("message", "")
                        # Trim stack trace to first 15 lines
                        stack = "\n".join((node.text or "").splitlines()[:15])
                        failures.append(
                            f"FAILED: {class_name}.{method_name}\n"
                            f"Message: {msg}\n"
                            f"{stack}"
                        )
            except ET.ParseError as exc:
                logger.debug(f"[Validator] surefire XML parse error in {xml_file}: {exc}")
                continue

    if not failures:
        return None
    return "\n\n".join(failures[:5])  # Cap at first 5 failures


def _extract_error_lines(output: str) -> str:
    """
    Extract [ERROR] lines from Maven output — these are what matter for LLM feedback.
    Returns up to _MAX_ERROR_CHARS characters.
    """
    error_lines = [
        line for line in output.splitlines()
        if line.startswith("[ERROR]") or "error:" in line.lower()
    ]
    return _trim("\n".join(error_lines))


# ── Path helpers ──────────────────────────────────────────────────────────────

def _detect_maven_module(repo_path: str, file_path: str) -> Optional[str]:
    """
    Walk up from the .java file to the nearest pom.xml.
    Returns the module path relative to repo root, or None for root pom / no pom.
    """
    if not file_path:
        return None
    current = Path(file_path).parent
    repo_root = Path(repo_path)

    while current != repo_root and current != current.parent:
        if (current / "pom.xml").exists():
            try:
                rel = current.relative_to(repo_root)
                return str(rel) if str(rel) != "." else None
            except ValueError:
                return None
        current = current.parent

    return None


def _class_name_from_path(file_path: str) -> Optional[str]:
    """Extract Java class name (file stem) from path."""
    if not file_path:
        return None
    name = Path(file_path).stem
    return name or None


def _trim(text: str) -> str:
    """Trim error text to _MAX_ERROR_CHARS from the end (most relevant part)."""
    if len(text) > _MAX_ERROR_CHARS:
        return "...(trimmed)...\n" + text[-_MAX_ERROR_CHARS:]
    return text
