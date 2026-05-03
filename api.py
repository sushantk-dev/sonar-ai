"""
SonarAI — FastAPI Bridge Server
Exposes the pipeline as an HTTP API for the Angular UI.

Run:
    uvicorn api:app --reload --port 8000

Endpoints:
    POST /api/pipeline/run          — start a pipeline run
    POST /api/pipeline/cancel/{id}  — hard-kill a running run
    GET  /api/pipeline/status/{id}  — poll run status + live step events
    GET  /api/issues                — list issues from the last loaded report
    POST /api/report/upload         — upload a sonar-report.json
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

    step_labels = ["Ingest","Load Repo","RAG Fetch",
                   "Planner","Generator","Critic","Validate","Deliver"]
    for label in step_labels:
        push(label, "pending")

    try:
        # Apply env overrides
        if req_dict.get("dry_run"):
            os.environ["SONAR_AI_DRY_RUN"] = "1"
        if req_dict.get("parallel"):
            os.environ["PARALLEL_ISSUES"] = "true"
        if req_dict.get("rescan"):
            os.environ["ENABLE_SONAR_RESCAN"] = "true"
        if req_dict.get("no_rag"):
            os.environ["ENABLE_RAG"] = "false"

        from graph import run_pipeline
        from loguru import logger as _log

        # Intercept loguru INFO to drive step state
        _orig_info = _log.info

        def _intercepting_info(msg: str, *a, **kw):  # type: ignore[misc]
            _orig_info(msg, *a, **kw)
            m = str(msg)
            if   "[Ingest]"    in m: push("Ingest",     "running", m)
            elif "[LoadRepo]"  in m: push("Load Repo",  "running", m)
            elif "[RAG]"       in m: push("RAG Fetch",  "running", m)
            elif "[Planner]"   in m: push("Planner",    "running", m)
            elif "[Generator]" in m: push("Generator",  "running", m)
            elif "[Critic]"    in m: push("Critic",     "running", m)
            elif "[Validate]"  in m: push("Validate",   "running", m)
            elif "[Deliver]"   in m: push("Deliver",    "running", m)

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
                    # Not cloned yet — use ls-remote to get HEAD SHA without cloning
                    result = subprocess.run(
                        ["git", "ls-remote", _auth_url, "HEAD"],
                        capture_output=True, text=True, timeout=30
                    )
                    if result.returncode == 0 and result.stdout:
                        commit_sha = result.stdout.split()[0]
                    else:
                        commit_sha = "HEAD"  # fallback — let git handle it
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
                # Clean up process handle
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": "2.0.0"}


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

    # Save permanently next to api.py
    uploads_dir = Path(__file__).parent / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    report_path = uploads_dir / "sonar-ai-last-report.json"
    report_path.write_bytes(content)

    from parser import parse_sonar_report
    issues = parse_sonar_report(str(report_path))
    _last_report_issues = [dict(i) for i in issues]

    return {
        "message":    f"Uploaded {file.filename}",
        "issue_count": len(issues),
        "path":        str(report_path),
    }


@app.get("/api/issues")
def get_issues() -> dict:
    return {"issues": _last_report_issues, "total": len(_last_report_issues)}


@app.delete("/api/issues/{key}")
def delete_issue(key: str) -> dict:
    """
    Remove an issue from memory and rewrite the saved report JSON file
    so the deletion persists across restarts.
    """
    global _last_report_issues

    before = len(_last_report_issues)
    _last_report_issues = [i for i in _last_report_issues if i.get("key") != key]
    after  = len(_last_report_issues)

    if before == after:
        raise HTTPException(404, f"Issue {key} not found")

    # Rewrite the saved report file so the deletion survives a restart
    report_path = Path(__file__).parent / "uploads" / "sonar-ai-last-report.json"
    if report_path.exists():
        try:
            existing = json.loads(report_path.read_text())

            # Support both { "issues": [...] } and flat array shapes
            if isinstance(existing, dict) and "issues" in existing:
                existing["issues"] = [
                    i for i in existing["issues"] if i.get("key") != key
                ]
                report_path.write_text(json.dumps(existing, indent=2))
            elif isinstance(existing, list):
                filtered = [i for i in existing if i.get("key") != key]
                report_path.write_text(json.dumps(filtered, indent=2))

            logger.info(f"[Delete] Removed issue {key} — {after} issues remain in file")
        except Exception as exc:
            logger.warning(f"[Delete] Could not rewrite report file: {exc}")

    return {"message": f"Issue {key} deleted", "remaining": after}


@app.post("/api/pipeline/run")
def start_run(req: PipelineRunRequest) -> dict:
    report_path = str(Path(__file__).parent / "uploads" / "sonar-ai-last-report.json")
    if not Path(report_path).exists():
        raise HTTPException(400, "No report uploaded yet. POST /api/report/upload first.")

    run_id = str(uuid.uuid4())
    _runs[run_id] = {
        "id":      run_id,
        "status":  "queued",
        "steps":   [],
        "results": [],
        "error":   None,
        "request": req.model_dump(),
    }

    # Create a Queue for the child to send events back to the parent
    q: Queue = multiprocessing.Queue()  # type: ignore[type-arg]

    proc = Process(
        target=_pipeline_worker,
        args=(run_id, req.model_dump(), report_path, q),
        daemon=True,   # dies automatically if uvicorn exits
    )
    proc.start()

    # Store both the process and its queue so we can kill and drain
    _processes[run_id] = proc
    _runs[run_id]["_queue"] = q          # internal — not serialised to JSON
    _runs[run_id]["status"] = "running"

    logger.info(f"[API] Started pipeline worker PID={proc.pid} run_id={run_id}")
    return {"run_id": run_id, "status": "running"}


@app.get("/api/pipeline/status/{run_id}")
def get_run_status(run_id: str) -> dict:
    if run_id not in _runs:
        raise HTTPException(404, f"Run {run_id} not found")

    # Drain any events the child sent since last poll
    q = _runs[run_id].get("_queue")
    if q:
        _drain_queue(run_id, q)

    # Check if the process died unexpectedly
    proc = _processes.get(run_id)
    if proc and not proc.is_alive() and _runs[run_id]["status"] == "running":
        exit_code = proc.exitcode
        if exit_code and exit_code < 0:  # killed by signal (e.g. SIGTERM)
            _runs[run_id]["status"] = "cancelled"
        else:
            _runs[run_id]["status"] = "error"
            _runs[run_id]["error"]  = f"Worker exited unexpectedly (code {exit_code})"
        _processes.pop(run_id, None)

    # Return everything except the internal _queue object
    return {k: v for k, v in _runs[run_id].items() if k != "_queue"}


@app.post("/api/pipeline/cancel/{run_id}")
def cancel_run(run_id: str) -> dict:
    """
    Hard-kill the pipeline worker process immediately.
    SIGTERM → process.terminate() → OS kills the entire child process tree,
    stopping LLM calls, git clone, mvn — everything — instantly.
    """
    if run_id not in _runs:
        raise HTTPException(404, f"Run {run_id} not found")

    proc = _processes.get(run_id)

    if proc and proc.is_alive():
        logger.warning(f"[API] Terminating pipeline PID={proc.pid} run_id={run_id}")
        proc.terminate()          # sends SIGTERM — graceful
        proc.join(timeout=3)      # wait up to 3 s
        if proc.is_alive():
            proc.kill()           # sends SIGKILL — hard kill, no escape
            proc.join(timeout=2)
        _processes.pop(run_id, None)
        logger.warning(f"[API] Pipeline PID={proc.pid} terminated")
    else:
        logger.info(f"[API] Cancel called but run {run_id} is not running")

    # Update run state
    if run_id in _runs:
        run = _runs[run_id]
        run["status"] = "cancelled"
        run["error"]  = "Cancelled by user"
        for s in run.get("steps", []):
            if s["status"] in ("running", "pending"):
                s["status"] = "error"
                s["detail"] = "Cancelled by user"

    return {"message": f"Run {run_id} cancelled", "run_id": run_id}


@app.get("/api/pipeline/runs")
def list_runs() -> dict:
    summaries = [
        {
            "id":      r["id"],
            "status":  r["status"],
            "results": len(r.get("results", [])),
            "error":   r.get("error"),
        }
        for r in _runs.values()
    ]
    return {"runs": summaries}


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
        # Parse key fields from filename: {safe_key}_{rule_short}.md
        name  = md_file.stem          # e.g. "AYx1_S2259"
        parts = name.split("_", 1)
        issue_key = parts[0] if parts else name
        rule_short = parts[1] if len(parts) > 1 else ""

        # Peek first few lines to extract severity and file
        content = md_file.read_text(encoding="utf-8", errors="replace")
        severity = "UNKNOWN"
        file_name = ""
        rule_key  = ""
        for line in content.splitlines():
            if "| Severity |" in line:
                severity = line.split("|")[2].strip().strip("`")
            if "| File |" in line:
                file_name = line.split("|")[2].strip().strip("`")
            if "| Rule |" in line:
                rule_key = line.split("|")[2].strip().strip("`")
            if severity != "UNKNOWN" and file_name and rule_key:
                break

        items.append({
            "filename":  md_file.name,
            "issue_key": issue_key,
            "rule_key":  rule_key or rule_short,
            "severity":  severity,
            "file_name": file_name,
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        })

    return {"escalations": items, "total": len(items)}


@app.get("/api/escalations/{filename}")
def get_escalation(filename: str) -> dict:
    """Return the full markdown content of one escalation file."""
    from config import settings as s
    # Sanitise — only allow .md files, no path traversal
    if not filename.endswith(".md") or "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")

    esc_path = Path(s.escalation_dir) / filename
    if not esc_path.exists():
        raise HTTPException(404, f"Escalation {filename} not found")

    return {
        "filename": filename,
        "content":  esc_path.read_text(encoding="utf-8", errors="replace"),
        "modified_at": esc_path.stat().st_mtime,
    }


@app.delete("/api/escalations/{filename}")
def delete_escalation(filename: str) -> dict:
    """Delete an escalation file."""
    from config import settings as s
    if not filename.endswith(".md") or "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")

    esc_path = Path(s.escalation_dir) / filename
    if not esc_path.exists():
        raise HTTPException(404, f"Escalation {filename} not found")

    esc_path.unlink()
    logger.info(f"[API] Deleted escalation: {filename}")
    return {"message": f"Deleted {filename}"}


@app.get("/api/config")
def get_config() -> dict:
    from config import settings as s

    def mask(v: str) -> str:
        return "***" if v else ""

    return {
        "gcp_project":                s.gcp_project,
        "vertex_model":               s.vertex_model,
        "max_issues":                 s.max_issues,
        "max_tokens":                 s.max_tokens,
        "confidence_high_threshold":  s.confidence_high_threshold,
        "confidence_medium_threshold": s.confidence_medium_threshold,
        "github_token":               mask(s.github_token),
        "github_repo":                "",
        "sonar_token":                mask(s.sonar_token),
        "sonar_host_url":             s.sonar_host_url,
        "max_critic_retries":         s.max_critic_retries,
        "chroma_persist_dir":         s.chroma_persist_dir,
        "embedding_model":            s.embedding_model,
        "rag_top_k":                  s.rag_top_k,
        "enable_rag":                 s.enable_rag,
        "parallel_issues":            s.parallel_issues,
        "enable_sonar_rescan":        s.enable_sonar_rescan,
    }


@app.post("/api/config")
def update_config(req: ConfigUpdateRequest) -> dict:
    env_path = Path(".env")
    lines: list[str] = env_path.read_text().splitlines() if env_path.exists() else []

    # For token fields, include empty string (explicit clear); skip None (not provided)
    token_fields = {"github_token", "sonar_token"}
    # Re-filter: include non-None values AND token fields even if empty string
    mapping = {
        k: v for k, v in req.model_dump().items()
        if v is not None or (k in token_fields and v == "")
    }

    env_key_map = {
        "gcp_project":                "GCP_PROJECT",
        "vertex_model":               "VERTEX_MODEL",
        "max_issues":                 "MAX_ISSUES",
        "max_tokens":                 "MAX_TOKENS",
        "confidence_high_threshold":  "CONFIDENCE_HIGH_THRESHOLD",
        "confidence_medium_threshold":"CONFIDENCE_MEDIUM_THRESHOLD",
        "github_token":               "GITHUB_TOKEN",
        "sonar_token":                "SONAR_TOKEN",
        "sonar_host_url":             "SONAR_HOST_URL",
        "max_critic_retries":         "MAX_CRITIC_RETRIES",
        "chroma_persist_dir":         "CHROMA_PERSIST_DIR",
        "embedding_model":            "EMBEDDING_MODEL",
        "rag_top_k":                  "RAG_TOP_K",
    }

    updated: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        written = False
        for field, env_key in env_key_map.items():
            if field in mapping and line.startswith(f"{env_key}="):
                val = ("true"  if mapping[field] is True  else
                       "false" if mapping[field] is False else
                       str(mapping[field]))
                new_lines.append(f"{env_key}={val}")
                updated.add(field)
                written = True
                break
        if not written:
            new_lines.append(line)

    for field, env_key in env_key_map.items():
        if field in mapping and field not in updated:
            val = ("true"  if mapping[field] is True  else
                   "false" if mapping[field] is False else
                   str(mapping[field]))
            new_lines.append(f"{env_key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n")
    return {"message": "Config saved", "updated_fields": list(mapping.keys())}


# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
def _startup() -> None:
    global _last_report_issues
    # Required on macOS/Windows for multiprocessing to work correctly with uvicorn
    multiprocessing.set_start_method("spawn", force=True)

    # Pre-load any previously uploaded report so GET /api/issues works after restart
    report_path = Path(__file__).parent / "uploads" / "sonar-ai-last-report.json"
    if report_path.exists():
        try:
            from parser import parse_sonar_report
            issues = parse_sonar_report(str(report_path))
            _last_report_issues = [dict(i) for i in issues]
            logger.info(f"[Startup] Loaded {len(_last_report_issues)} issues from {report_path}")
        except Exception as exc:
            logger.warning(f"[Startup] Could not load saved report: {exc}")


@app.on_event("shutdown")
def _shutdown() -> None:
    """Kill all child processes when uvicorn stops."""
    for run_id, proc in list(_processes.items()):
        if proc.is_alive():
            logger.warning(f"[API] Shutdown: terminating PID={proc.pid}")
            proc.terminate()
            proc.join(timeout=2)