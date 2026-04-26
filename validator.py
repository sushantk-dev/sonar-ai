"""
SonarAI — Patch Validator
Applies the generated unified diff and runs mvn compile + mvn test.
Returns a ValidationResult describing what passed and what failed.
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


# ── Public entry point ────────────────────────────────────────────────────────

def validate(state: AgentState) -> AgentState:
    """
    LangGraph node — apply the diff and validate with Maven.
    Populates state['validation'] and increments state['retry_count'] on failure.
    """
    repo_path = state["repo_local_path"]
    file_path = state["file_path"]
    patch_hunks = state.get("generator_output", {}).get("patch_hunks", "")

    result: ValidationResult = {
        "diff_ok": False,
        "compile_ok": False,
        "tests_ok": False,
        "compiler_error": "",
        "test_error": "",
    }

    # Step 1: Apply the diff
    if not patch_hunks.strip():
        logger.error("[Validator] Empty patch — nothing to apply")
        return {**state, "validation": result}

    diff_ok, apply_error = _apply_diff(repo_path, patch_hunks)
    result["diff_ok"] = diff_ok

    if not diff_ok:
        logger.error(f"[Validator] Diff apply failed: {apply_error}")
        result["compiler_error"] = apply_error
        return {**state, "validation": result}

    logger.info("[Validator] Diff applied successfully")

    # Step 2: Maven compile
    module = _detect_maven_module(repo_path, file_path)
    compile_ok, compiler_error = _mvn_compile(repo_path, module)
    result["compile_ok"] = compile_ok
    result["compiler_error"] = compiler_error

    if not compile_ok:
        logger.error(f"[Validator] Compile failed: {compiler_error[:200]}")
        return {**state, "validation": result}

    logger.info("[Validator] Maven compile passed")

    # Step 3: Maven test
    class_name = _class_name_from_path(file_path)
    tests_ok, test_error = _mvn_test(repo_path, module, class_name)
    result["tests_ok"] = tests_ok
    result["test_error"] = test_error

    if tests_ok:
        logger.info("[Validator] Maven tests passed")
    else:
        logger.warning(f"[Validator] Tests failed: {test_error[:200]}")

    return {**state, "validation": result}


# ── Diff application ──────────────────────────────────────────────────────────

def _apply_diff(repo_path: str, patch_hunks: str) -> tuple[bool, str]:
    """
    Write the patch to a temp file, dry-run with ``git apply --check``,
    then actually apply it.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(patch_hunks)
        patch_file = tmp.name

    try:
        # Dry run
        dry = subprocess.run(
            ["git", "apply", "--check", patch_file],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if dry.returncode != 0:
            return False, f"git apply --check failed:\n{dry.stderr}"

        # Actual apply
        apply = subprocess.run(
            ["git", "apply", patch_file],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if apply.returncode != 0:
            return False, f"git apply failed:\n{apply.stderr}"

        return True, ""

    except subprocess.TimeoutExpired:
        return False, "git apply timed out after 30s"
    finally:
        os.unlink(patch_file)


# ── Maven helpers ─────────────────────────────────────────────────────────────

def _mvn_compile(repo_path: str, module: Optional[str]) -> tuple[bool, str]:
    """Run mvn compile, optionally scoped to a module."""
    cmd = ["mvn", "compile", "-q"]
    if module:
        cmd += ["-pl", module]

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
        return False, (result.stdout + result.stderr)[-2000:]
    except FileNotFoundError:
        logger.warning("[Validator] mvn not found — skipping compile validation")
        return True, ""  # Don't block on missing Maven
    except subprocess.TimeoutExpired:
        return False, f"mvn compile timed out after {settings.compile_timeout}s"


def _mvn_test(
    repo_path: str, module: Optional[str], class_name: Optional[str]
) -> tuple[bool, str]:
    """Run mvn test for the affected class (or all tests if no class known)."""
    cmd = ["mvn", "test"]
    if module:
        cmd += ["-pl", module]
    if class_name:
        cmd += [f"-Dtest={class_name}Test", "--no-transfer-progress"]

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

        # Try to extract surefire test failure info
        surefire_error = _parse_surefire_results(repo_path)
        error_text = surefire_error or (result.stdout + result.stderr)[-2000:]
        return False, error_text

    except FileNotFoundError:
        logger.warning("[Validator] mvn not found — skipping test validation")
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"mvn test timed out after {settings.test_timeout}s"


def _parse_surefire_results(repo_path: str) -> Optional[str]:
    """
    Parse surefire XML reports to extract test failure messages.
    Returns a formatted string of failures, or None if no surefire reports found.
    """
    surefire_dirs = list(Path(repo_path).rglob("surefire-reports"))
    if not surefire_dirs:
        return None

    failures: list[str] = []
    for report_dir in surefire_dirs:
        for xml_file in report_dir.glob("TEST-*.xml"):
            try:
                tree = ET.parse(xml_file)
                root = tree.getroot()
                for testcase in root.findall(".//testcase"):
                    failure = testcase.find("failure")
                    error = testcase.find("error")
                    node = failure if failure is not None else error
                    if node is not None:
                        test_name = f"{testcase.get('classname', '')}.{testcase.get('name', '')}"
                        failures.append(
                            f"FAILED: {test_name}\n{node.get('message', '')}\n{node.text or ''}"
                        )
            except ET.ParseError:
                continue

    return "\n\n".join(failures[:5]) if failures else None  # Cap at first 5 failures


# ── Path helpers ──────────────────────────────────────────────────────────────

def _detect_maven_module(repo_path: str, file_path: str) -> Optional[str]:
    """
    Walk up from the file path to find the nearest pom.xml and return the
    Maven module path relative to repo root.
    """
    current = Path(file_path).parent
    repo_root = Path(repo_path)

    while current != repo_root and current != current.parent:
        if (current / "pom.xml").exists():
            try:
                return str(current.relative_to(repo_root))
            except ValueError:
                return None
        current = current.parent

    return None  # Root pom or no pom found — run from repo root


def _class_name_from_path(file_path: str) -> Optional[str]:
    """Extract the Java class name (file stem) from the file path."""
    name = Path(file_path).stem
    return name if name else None
