"""
SonarAI — FastAPI Bridge Server
Exposes the pipeline as an HTTP API for the Angular UI.

Run:
    uvicorn api:app --reload --port 8000

Endpoints:
    POST /api/pipeline/run          — start a pipeline run
    POST /api/pipeline/cancel/{id}  — hard-kill a running run
    GET  /api/pipeline/status/{id}  — poll run status + live step events
    GET  /api/pipeline/runs         — list all runs with full data for UI rehydration
    GET  /api/issues                — list issues from the last loaded report
    DELETE /api/issues/{key}        — remove one issue
    POST /api/report/upload         — upload a sonar-report.json
    POST /api/sonar/fetch           — live-fetch issues from SonarQube API
    GET  /api/sonar/report          — structured summary report of loaded issues
    GET  /api/config                — read current settings
    POST /api/config                — update settings (writes .env)
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import signal
import tempfile
import time
import uuid
from multiprocessing import Process, Queue
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

app = FastAPI(title="SonarAI API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://localhost:4201"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory stores ──────────────────────────────────────────────────────────
_runs:                dict[str, dict[str, Any]] = {}
_processes:           dict[str, Process]        = {}   # run_id → live Process
_last_report_issues:  list[dict]                = []


# ── Pydantic models ───────────────────────────────────────────────────────────

class PipelineRunRequest(BaseModel):
    repo_url:   str
    commit_sha: str
    max_issues: int  = 0
    parallel:   bool = False
    rescan:     bool = False
    no_rag:     bool = False
    dry_run:    bool = False
    severities: str  = "BLOCKER,CRITICAL,MAJOR,MINOR,INFO"


class ConfigUpdateRequest(BaseModel):
    gcp_project:                 Optional[str]   = None
    vertex_model:                Optional[str]   = None
    max_issues:                  Optional[int]   = None
    max_tokens:                  Optional[int]   = None
    confidence_high_threshold:   Optional[float] = None
    confidence_medium_threshold: Optional[float] = None
    github_token:                Optional[str]   = None   # empty string = clear token
    github_repo:                 Optional[str]   = None
    sonar_token:                 Optional[str]   = None   # empty string = clear token
    sonar_host_url:              Optional[str]   = None
    sonar_org:                   Optional[str]   = None
    planner_temp:                Optional[float] = None
    generator_temp:              Optional[float] = None
    max_critic_retries:          Optional[int]   = None
    chroma_persist_dir:          Optional[str]   = None
    embedding_model:             Optional[str]   = None
    rag_top_k:                   Optional[int]   = None
    langsmith_api_key:           Optional[str]   = None
    langsmith_project:           Optional[str]   = None
    langchain_tracing:           Optional[bool]  = None


class SonarFetchRequest(BaseModel):
    component_keys: str
    severities:     str  = "BLOCKER,CRITICAL,MAJOR,MINOR,INFO"
    resolved:       bool = False
    ps:             int  = 500


# ── Worker process function ───────────────────────────────────────────────────
# Runs in a SEPARATE PROCESS — can be hard-killed via process.terminate()

def _pipeline_worker(
    run_id: str,
    req_dict: dict,
    report_path: str,
    event_queue: Queue,  # type: ignore[type-arg]
) -> None:
    """
    Runs inside a child process. Sends step events through a Queue so the
    parent process can update _runs without shared memory.
    """

    def push(label: str, status: str, detail: str = "", ms: int = 0) -> None:
        event_queue.put({"type": "step", "label": label,
                         "status": status, "detail": detail, "ms": ms})

    def set_status(status: str, error: str = "") -> None:
        event_queue.put({"type": "status", "status": status, "error": error})

    step_labels = ["Ingest","Load Repo","RAG Fetch","Rule Fetch",
                   "Planner","Generator","Critic","Validate","Deliver"]

    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    try:
        from loguru import logger as _log
        from graph import run_pipeline

        # Initialise all steps as pending so the UI shows the full list immediately
        for label in step_labels:
            event_queue.put({"type": "step", "label": label,
                             "status": "pending", "detail": "", "ms": 0})

        # Intercept loguru INFO so we can push detail strings per step
        _orig_info = _log.info

        def _push_detail(step_label: str, msg: str, prefix: str) -> None:
            detail = msg.replace(prefix, "").strip(" :]")
            event_queue.put({"type": "step", "label": step_label,
                             "status": "running", "detail": detail, "ms": 0})

        def _intercepting_info(msg: str, *args, **kwargs) -> None:
            _orig_info(msg, *args, **kwargs)
            if   "[Ingest]"    in msg: _push_detail("Ingest",    msg, "[Ingest]")
            elif "[RepoLoad]"  in msg: _push_detail("Load Repo", msg, "[RepoLoad]")
            elif "[RAG]"       in msg: _push_detail("RAG Fetch", msg, "[RAG]")
            elif "[RuleFetch]" in msg: _push_detail("Rule Fetch",msg, "[RuleFetch]")
            elif "[Planner]"   in msg: _push_detail("Planner",   msg, "[Planner]")
            elif "[Generator]" in msg: _push_detail("Generator", msg, "[Generator]")
            elif "[Critic]"    in msg: _push_detail("Critic",    msg, "[Critic]")
            elif "[Validator]" in msg: _push_detail("Validate",  msg, "[Validator]")
            elif "[Deliver]"   in msg: _push_detail("Deliver",   msg, "[Deliver]")

        _log.info = _intercepting_info  # type: ignore[method-assign]

        # Resolve HEAD / empty commit_sha to the actual SHA
        commit_sha = req_dict.get("commit_sha", "").strip()
        if not commit_sha or commit_sha.upper() in ("HEAD", "LATEST", ""):
            try:
                import subprocess, tempfile as _tmp
                from config import settings as _cfg
                from repo_loader import _inject_token, _repo_name_from_url
                _auth_url   = _inject_token(req_dict["repo_url"], _cfg.github_token)
                _repo_name  = _repo_name_from_url(req_dict["repo_url"])
                _local_path = Path(_cfg.clone_dir) / _repo_name
                if _local_path.exists():
                    import git as _git
                    _repo      = _git.Repo(_local_path)
                    commit_sha = _repo.head.commit.hexsha
                else:
                    result = subprocess.run(
                        ["git", "ls-remote", _auth_url, "HEAD"],
                        capture_output=True, text=True, timeout=30
                    )
                    if result.returncode == 0 and result.stdout:
                        commit_sha = result.stdout.split()[0]
                    else:
                        commit_sha = "HEAD"
                logger.info(f"[API] Resolved HEAD → {commit_sha[:12]}")
            except Exception as _exc:
                logger.warning(f"[API] Could not resolve HEAD SHA: {_exc} — using HEAD")
                commit_sha = "HEAD"

        t0 = time.time()
        final_state = run_pipeline(
            sonar_report_path=report_path,
            repo_url=req_dict["repo_url"],
            commit_sha=commit_sha,
            max_issues=req_dict.get("max_issues", 0),
            severities=req_dict.get("severities", "BLOCKER,CRITICAL,MAJOR,MINOR,INFO"),
        )
        elapsed_ms = int((time.time() - t0) * 1000)

        _log.info = _orig_info

        results: list[dict] = final_state.get("pipeline_results", [])
        event_queue.put({
            "type":       "done",
            "results":    results,
            "elapsed_ms": elapsed_ms,
        })

    except Exception as exc:  # noqa: BLE001
        event_queue.put({
            "type":   "error",
            "error":  str(exc),
        })


# ── Event queue drainer (runs in parent, called on each status poll) ──────────

def _drain_queue(run_id: str, q: Queue) -> None:  # type: ignore[type-arg]
    """Pull all pending events from the child queue and apply to _runs."""
    run = _runs.get(run_id)
    if not run:
        return

    steps: list[dict] = run.setdefault("steps", [])

    try:
        while True:
            event = q.get_nowait()

            if event["type"] == "step":
                label, status = event["label"], event["status"]
                detail, ms = event.get("detail", ""), event.get("ms", 0)
                matched = False
                for s in steps:
                    if s["label"] == label:
                        s["status"] = status
                        if detail: s["detail"] = detail
                        if ms:     s["ms"]     = ms
                        matched = True
                        break
                if not matched:
                    steps.append({"label": label, "status": status,
                                  "detail": detail, "ms": ms})

            elif event["type"] == "done":
                run["status"]     = "done"
                run["results"]    = event.get("results", [])
                run["elapsed_ms"] = event.get("elapsed_ms", 0)
                for s in steps:
                    if s["status"] == "running":
                        s["status"] = "done"
                _processes.pop(run_id, None)

            elif event["type"] == "error":
                run["status"] = "error"
                run["error"]  = event.get("error", "Unknown error")
                for s in steps:
                    if s["status"] == "running":
                        s["status"] = "error"
                        s["detail"] = run["error"]
                _processes.pop(run_id, None)

    except queue.Empty:
        pass


# ── SonarQube issue normaliser ────────────────────────────────────────────────

def _normalize_sonar_issue(raw: dict) -> dict:
    """Map a raw SonarQube API issue object to the internal schema."""
    text_range = raw.get("textRange", {})
    return {
        "key":       raw.get("key", ""),
        "rule_key":  raw.get("rule", ""),
        "severity":  raw.get("severity", "INFO"),
        "component": raw.get("component", ""),
        "project":   raw.get("project", ""),
        "line":      raw.get("line") or text_range.get("startLine", 0),
        "message":   raw.get("message", ""),
        "effort":    raw.get("effort", ""),
        "status":    raw.get("status", "OPEN"),
        "hash":      raw.get("hash", ""),
        "text_range": {
            "start_line":   text_range.get("startLine", 0),
            "end_line":     text_range.get("endLine", 0),
            "start_offset": text_range.get("startOffset", 0),
            "end_offset":   text_range.get("endOffset", 0),
        },
        "tags":  raw.get("tags", []),
        "type":  raw.get("type", ""),
        "debt":  raw.get("debt", ""),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": "2.0.0"}


# ── Report upload ─────────────────────────────────────────────────────────────

@app.post("/api/report/upload")
async def upload_report(file: UploadFile = File(...)) -> dict:
    global _last_report_issues

    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(400, "Only .json files are accepted")

    content = await file.read()
    try:
        json.loads(content)  # validate JSON
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Invalid JSON: {exc}") from exc

    uploads_dir = Path(__file__).parent / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    report_path = uploads_dir / "sonar-ai-last-report.json"
    report_path.write_bytes(content)

    from parser import parse_sonar_report
    issues = parse_sonar_report(str(report_path))
    _last_report_issues = [dict(i) for i in issues]

    return {
        "message":     f"Uploaded {file.filename}",
        "issue_count": len(issues),
        "path":        str(report_path),
    }


# ── Issues CRUD ───────────────────────────────────────────────────────────────

@app.get("/api/issues")
def get_issues() -> dict:
    return {"issues": _last_report_issues, "total": len(_last_report_issues)}


@app.delete("/api/issues/{key}")
def delete_issue(key: str) -> dict:
    """Remove an issue from memory and rewrite the saved report file."""
    global _last_report_issues

    before = len(_last_report_issues)
    _last_report_issues = [i for i in _last_report_issues if i.get("key") != key]
    after  = len(_last_report_issues)

    if before == after:
        raise HTTPException(404, f"Issue {key} not found")

    report_path = Path(__file__).parent / "uploads" / "sonar-ai-last-report.json"
    if report_path.exists():
        try:
            existing = json.loads(report_path.read_text())
            if isinstance(existing, dict) and "issues" in existing:
                existing["issues"] = [i for i in existing["issues"] if i.get("key") != key]
                report_path.write_text(json.dumps(existing, indent=2))
            elif isinstance(existing, list):
                filtered = [i for i in existing if i.get("key") != key]
                report_path.write_text(json.dumps(filtered, indent=2))
            logger.info(f"[Delete] Removed issue {key} — {after} issues remain in file")
        except Exception as exc:
            logger.warning(f"[Delete] Could not rewrite report file: {exc}")

    return {"message": f"Issue {key} deleted", "remaining": after}


# ── Live SonarQube fetch ──────────────────────────────────────────────────────

@app.get("/api/sonar/rule/{rule_key:path}")
def get_sonar_rule(rule_key: str) -> dict:
    """
    Proxy a GET /api/rules/show call to SonarQube for a single rule key.
    Returns structured rule metadata including name, description, fix guidance,
    remediation effort, type, severity, and tags.

    Example: GET /api/sonar/rule/java:S1128
    """
    import requests as _requests
    import html as _html
    import re as _re
    from config import settings as s

    if not s.sonar_token:
        raise HTTPException(400, "SONAR_TOKEN is not configured. Add it in Settings.")
    if not s.sonar_host_url:
        raise HTTPException(400, "SONAR_HOST_URL is not configured. Add it in Settings.")

    base_url = s.sonar_host_url.rstrip("/")
    try:
        resp = _requests.get(
            f"{base_url}/api/rules/show",
            auth=(s.sonar_token, ""),
            params={"key": rule_key},
            timeout=15,
        )
    except Exception as exc:
        raise HTTPException(502, f"Could not reach SonarQube: {exc}") from exc

    if resp.status_code == 401:
        raise HTTPException(401, "SonarQube authentication failed — check SONAR_TOKEN")
    if resp.status_code == 404:
        raise HTTPException(404, f"Rule '{rule_key}' not found in SonarQube")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"SonarQube error: {resp.text[:300]}")

    body = resp.json()
    rule = body.get("rule", {})

    # Build plain-text description by stripping HTML
    html_desc = rule.get("htmlDesc", "") or rule.get("mdDesc", "")
    plain_desc = _html.unescape(_re.sub(r"<[^>]+>", " ", html_desc))
    plain_desc = _re.sub(r"\s{2,}", " ", plain_desc).strip()

    # Try to extract compliant/fix section
    fix_summary = ""
    for pat in [
        r"(?:Compliant[^<]*Solution|How to[^<]*[Ff]ix|Recommended[^<]*Practice)(.*?)(?=<h\d|$)",
    ]:
        m = _re.search(pat, html_desc, _re.DOTALL | _re.IGNORECASE)
        if m:
            snippet = _html.unescape(_re.sub(r"<[^>]+>", " ", m.group(0)))
            snippet = _re.sub(r"\s{2,}", " ", snippet).strip()
            if len(snippet) > 30:
                fix_summary = snippet[:800]
                break
    if not fix_summary:
        fix_summary = plain_desc[:600]

    logger.info(f"[API] Served rule detail for {rule_key}: {rule.get('name', '')}")

    return {
        "rule_key":           rule_key,
        "name":               rule.get("name", ""),
        "html_desc":          html_desc,
        "plain_desc":         plain_desc[:2000],
        "fix_summary":        fix_summary,
        "severity":           rule.get("severity", ""),
        "type":               rule.get("type", ""),
        "tags":               rule.get("tags", []),
        "remediation_effort": rule.get("remFnBaseEffort", ""),
    }


@app.post("/api/sonar/fetch")
def fetch_sonar_issues(req: SonarFetchRequest) -> dict:
    """Live-pull issues from a SonarQube instance and store them in memory."""
    global _last_report_issues
    import requests as _requests
    from config import settings as s

    if not s.sonar_token:
        raise HTTPException(400, "SONAR_TOKEN is not configured. Add it in Settings.")
    if not s.sonar_host_url:
        raise HTTPException(400, "SONAR_HOST_URL is not configured. Add it in Settings.")

    base_url  = s.sonar_host_url.rstrip("/")
    all_issues: list[dict] = []
    page = 1

    while True:
        try:
            resp = _requests.get(
                f"{base_url}/api/issues/search",
                auth=(s.sonar_token, ""),
                params={
                    "componentKeys": req.component_keys,
                    "severities":    req.severities,
                    "resolved":      str(req.resolved).lower(),
                    "ps":            min(req.ps, 500),
                    "p":             page,
                },
                timeout=30,
            )
        except Exception as exc:
            raise HTTPException(502, f"Could not reach SonarQube: {exc}") from exc

        if resp.status_code == 401:
            raise HTTPException(401, "SonarQube authentication failed — check SONAR_TOKEN")
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"SonarQube error: {resp.text[:300]}")

        body   = resp.json()
        issues = body.get("issues", [])
        all_issues.extend(_normalize_sonar_issue(i) for i in issues)

        total   = body.get("total", 0)
        fetched = page * min(req.ps, 500)
        if fetched >= total or not issues:
            break
        page += 1

    _last_report_issues = all_issues

    effort_total = sum(
        int(i["effort"].replace("min", "").replace("h", "").strip() or 0)
        for i in all_issues if i.get("effort")
    )

    logger.info(
        f"[API] Fetched {len(all_issues)} issues from SonarQube "
        f"component={req.component_keys}"
    )

    return {
        "message":      f"Fetched {len(all_issues)} issues",
        "issue_count":  len(all_issues),
        "total":        len(all_issues),
        "effort_total": effort_total,
        "component":    req.component_keys,
    }


@app.get("/api/sonar/report")
def get_sonar_report() -> dict:
    """Return a structured summary of the currently loaded issues."""
    from datetime import datetime

    issues = _last_report_issues
    by_severity: dict[str, dict] = {}
    by_rule:     dict[str, dict] = {}

    for i in issues:
        sev  = i.get("severity", "INFO")
        rule = i.get("rule_key", "")
        file = i.get("component", "")

        if sev not in by_severity:
            by_severity[sev] = {"count": 0, "issues": []}
        by_severity[sev]["count"] += 1
        by_severity[sev]["issues"].append(i)

        if rule not in by_rule:
            by_rule[rule] = {"rule_key": rule, "severity": sev, "count": 0, "files": []}
        by_rule[rule]["count"] += 1
        if file not in by_rule[rule]["files"]:
            by_rule[rule]["files"].append(file)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total":        len(issues),
        "by_severity":  by_severity,
        "by_rule":      by_rule,
        "issues":       issues,
    }


# ── Pipeline ──────────────────────────────────────────────────────────────────

@app.post("/api/pipeline/run")
def start_pipeline(req: PipelineRunRequest) -> dict:
    """
    Start a pipeline run in a child process.
    Returns run_id immediately; client polls /api/pipeline/status/{run_id}.
    """
    report_path = str(Path(__file__).parent / "uploads" / "sonar-ai-last-report.json")
    if not Path(report_path).exists():
        raise HTTPException(400, "No sonar report uploaded yet.")

    run_id = str(uuid.uuid4())
    _runs[run_id] = {
        "id":      run_id,
        "status":  "queued",
        "steps":   [],
        "results": [],
        "error":   None,
        "request": req.model_dump(),
    }

    q: Queue = multiprocessing.Queue()  # type: ignore[type-arg]

    proc = Process(
        target=_pipeline_worker,
        args=(run_id, req.model_dump(), report_path, q),
        daemon=True,
    )
    proc.start()

    _processes[run_id]      = proc
    _runs[run_id]["_queue"] = q
    _runs[run_id]["status"] = "running"

    logger.info(
        f"[API] Started pipeline worker PID={proc.pid} run_id={run_id} "
        f"sev={req.severities}"
    )
    return {"run_id": run_id, "status": "running"}


@app.get("/api/pipeline/status/{run_id}")
def get_run_status(run_id: str) -> dict:
    if run_id not in _runs:
        raise HTTPException(404, f"Run {run_id} not found")

    q = _runs[run_id].get("_queue")
    if q:
        _drain_queue(run_id, q)

    proc = _processes.get(run_id)
    if proc and not proc.is_alive() and _runs[run_id]["status"] == "running":
        exit_code = proc.exitcode
        if exit_code and exit_code < 0:
            _runs[run_id]["status"] = "cancelled"
        else:
            _runs[run_id]["status"] = "error"
            _runs[run_id]["error"]  = f"Worker exited unexpectedly (code {exit_code})"
        _processes.pop(run_id, None)

    return {k: v for k, v in _runs[run_id].items() if k != "_queue"}


@app.post("/api/pipeline/cancel/{run_id}")
def cancel_run(run_id: str) -> dict:
    """Hard-kill the pipeline worker process immediately."""
    if run_id not in _runs:
        raise HTTPException(404, f"Run {run_id} not found")

    proc = _processes.get(run_id)

    if proc and proc.is_alive():
        logger.warning(f"[API] Terminating pipeline PID={proc.pid} run_id={run_id}")
        proc.terminate()
        proc.join(timeout=3)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)
        _processes.pop(run_id, None)
        logger.warning(f"[API] Pipeline PID={proc.pid} terminated")
    else:
        logger.info(f"[API] Cancel called but run {run_id} is not running")

    if run_id in _runs:
        run = _runs[run_id]
        run["status"] = "cancelled"
        run["error"]  = "Cancelled by user"
        for s in run.get("steps", []):
            if s["status"] in ("running", "pending"):
                was_running = s["status"] == "running"
                s["status"] = "cancelled"
                if was_running:
                    s["detail"] = "Cancelled by user"

    return {"message": f"Run {run_id} cancelled", "run_id": run_id}


@app.get("/api/pipeline/runs")
def list_runs() -> dict:
    """
    Return full run data for every run in memory so the Angular UI can
    rehydrate its pipeline history after a page reload.

    Each entry mirrors the shape of GET /api/pipeline/status/{run_id}
    (minus the internal _queue key) so the frontend can use the same
    _backendRunToUiRun() mapping for both endpoints.
    """
    runs_out = []
    for run_id, run in _runs.items():
        # Drain any pending queue events so the snapshot is as fresh as possible
        q = run.get("_queue")
        if q:
            _drain_queue(run_id, q)

        runs_out.append({k: v for k, v in run.items() if k != "_queue"})

    # Most-recent first (insertion order is preserved in Python 3.7+ dicts,
    # newest runs are appended last, so we reverse)
    runs_out.reverse()

    return {"runs": runs_out}


# ── Escalations ───────────────────────────────────────────────────────────────

@app.get("/api/escalations")
def list_escalations() -> dict:
    """List all escalation markdown files from the escalations/ directory."""
    from config import settings as s
    esc_dir = Path(s.escalation_dir)
    if not esc_dir.exists():
        return {"escalations": [], "total": 0}

    items = []
    for md_file in sorted(esc_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
        stat = md_file.stat()
        name  = md_file.stem
        parts = name.split("_", 1)
        issue_key  = parts[0] if parts else name
        rule_short = parts[1] if len(parts) > 1 else ""

        content   = md_file.read_text(encoding="utf-8", errors="replace")
        severity  = "UNKNOWN"
        file_name = ""
        rule_key  = ""
        for line in content.splitlines():
            if "| Severity |" in line:
                severity = line.split("|")[2].strip().strip("`")
            if "| File |" in line:
                file_name = line.split("|")[2].strip().strip("`")
            if "| Rule |" in line:
                rule_key = line.split("|")[2].strip().strip("`")

        items.append({
            "filename":    md_file.name,
            "issue_key":   issue_key,
            "rule_short":  rule_short,
            "rule_key":    rule_key,
            "severity":    severity,
            "file":        file_name,
            "modified_at": stat.st_mtime,
            "size_bytes":  stat.st_size,
        })

    return {"escalations": items, "total": len(items)}


@app.get("/api/escalations/{filename}")
def get_escalation(filename: str) -> dict:
    """Return the raw markdown content of a single escalation file."""
    from config import settings as s
    esc_dir  = Path(s.escalation_dir)
    esc_file = esc_dir / filename

    if not esc_file.exists() or not esc_file.suffix == ".md":
        raise HTTPException(404, f"Escalation '{filename}' not found")

    return {
        "filename":    filename,
        "content":     esc_file.read_text(encoding="utf-8", errors="replace"),
        "modified_at": esc_file.stat().st_mtime,
    }


@app.delete("/api/escalations/{filename}")
def delete_escalation(filename: str) -> dict:
    """Delete a single escalation file."""
    from config import settings as s
    esc_dir  = Path(s.escalation_dir)
    esc_file = esc_dir / filename

    if not esc_file.exists():
        raise HTTPException(404, f"Escalation '{filename}' not found")

    esc_file.unlink()
    logger.info(f"[API] Deleted escalation: {filename}")
    return {"message": f"Escalation '{filename}' deleted"}


# ── Config ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config() -> dict:
    from config import settings as s
    return {
        "gcp_project":                 s.gcp_project,
        "vertex_model":                s.vertex_model,
        "max_issues":                  s.max_issues,
        "max_tokens":                  s.max_tokens,
        "confidence_high_threshold":   s.confidence_high_threshold,
        "confidence_medium_threshold": s.confidence_medium_threshold,
        "github_token":                "***" if s.github_token else "",
        "github_repo":                 s.github_repo,
        "sonar_token":                 "***" if s.sonar_token  else "",
        "sonar_host_url":              s.sonar_host_url,
        "planner_temperature":         s.planner_temp,
        "generator_temperature":       s.generator_temp,
        "max_critic_retries":          s.max_critic_retries,
        "chroma_persist_dir":          s.chroma_persist_dir,
        "embedding_model":             s.embedding_model,
        "rag_top_k":                   s.rag_top_k,
        "enable_rag":                  not getattr(s, "no_rag", False),
        "langsmith_project":           s.langsmith_project,
        "langsmith_api_key":           "***" if s.langsmith_api_key else "",
        "langchain_tracing":           s.langchain_tracing,
        "parallel_issues":             s.parallel_issues,
        "enable_sonar_rescan":         s.enable_sonar_rescan,
    }


@app.post("/api/config")
def save_config(req: ConfigUpdateRequest) -> dict:
    """
    Persist changed settings to the .env file.
    Only fields explicitly included in the request body are updated.
    """
    env_path = Path(__file__).parent / ".env"
    env_lines: list[str] = []

    if env_path.exists():
        env_lines = env_path.read_text().splitlines()

    def _upsert(key: str, value: str) -> None:
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                env_lines[i] = f"{key}={value}"
                return
        env_lines.append(f"{key}={value}")

    payload = req.model_dump(exclude_none=True)

    field_map = {
        "gcp_project":                 "GCP_PROJECT",
        "vertex_model":                "VERTEX_MODEL",
        "max_issues":                  "MAX_ISSUES",
        "max_tokens":                  "MAX_TOKENS",
        "confidence_high_threshold":   "CONFIDENCE_HIGH_THRESHOLD",
        "confidence_medium_threshold": "CONFIDENCE_MEDIUM_THRESHOLD",
        "github_repo":                 "GITHUB_REPO",
        "sonar_host_url":              "SONAR_HOST_URL",
        "sonar_org":                   "SONAR_ORG",
        "planner_temp":                "PLANNER_TEMP",
        "generator_temp":              "GENERATOR_TEMP",
        "max_critic_retries":          "MAX_CRITIC_RETRIES",
        "chroma_persist_dir":          "CHROMA_PERSIST_DIR",
        "embedding_model":             "EMBEDDING_MODEL",
        "rag_top_k":                   "RAG_TOP_K",
        "langsmith_project":           "LANGSMITH_PROJECT",
        "langchain_tracing":           "LANGCHAIN_TRACING_V2",
    }

    for py_field, env_key in field_map.items():
        if py_field in payload:
            _upsert(env_key, str(payload[py_field]))

    # Tokens: only write if non-empty (empty string = user cleared the field — skip)
    if payload.get("github_token"):
        _upsert("GITHUB_TOKEN", payload["github_token"])
    if payload.get("sonar_token"):
        _upsert("SONAR_TOKEN", payload["sonar_token"])
    if payload.get("langsmith_api_key"):
        _upsert("LANGSMITH_API_KEY", payload["langsmith_api_key"])

    env_path.write_text("\n".join(env_lines) + "\n")
    logger.info(f"[API] Config saved — updated keys: {list(payload.keys())}")
    return {"message": "Config saved"}


@app.post("/api/reload")
def reload_config() -> dict:
    """
    Re-read the .env file and patch the live settings object so new values
    take effect immediately without restarting uvicorn.
    """
    from config import settings as s
    from dotenv import dotenv_values

    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return {"message": "No .env file found — nothing to reload",
                "sonar_token_set": bool(s.sonar_token),
                "sonar_host_url":  s.sonar_host_url}

    fresh = dotenv_values(str(env_path))

    # Patch the live singleton so in-process code picks up new values
    for attr, env_key in [
        ("github_token",  "GITHUB_TOKEN"),
        ("github_repo",   "GITHUB_REPO"),
        ("sonar_token",   "SONAR_TOKEN"),
        ("sonar_host_url","SONAR_HOST_URL"),
        ("sonar_org",     "SONAR_ORG"),
        ("gcp_project",   "GCP_PROJECT"),
        ("vertex_model",  "VERTEX_MODEL"),
    ]:
        if env_key in fresh:
            object.__setattr__(s, attr, fresh[env_key])

    logger.info("[API] Live config reloaded from .env")
    return {
        "message":         "Config reloaded",
        "sonar_token_set": bool(s.sonar_token),
        "sonar_host_url":  s.sonar_host_url,
    }