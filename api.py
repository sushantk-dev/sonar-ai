"""
SonarAI — FastAPI Bridge Server
Exposes the pipeline as an HTTP API for the Angular UI.

Run:
    uvicorn api:app --reload --port 8000

Endpoints:
    POST /api/pipeline/run        — start a pipeline run
    GET  /api/pipeline/status/{id} — poll run status + live step events
    GET  /api/issues              — list issues from the last loaded report
    POST /api/report/upload       — upload a sonar-report.json
    GET  /api/config              — read current settings
    POST /api/config              — update settings (writes .env)
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger

app = FastAPI(title="SonarAI API", version="2.0.0")

# Allow Angular dev server (ng serve defaults to 4200)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://localhost:4201"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory run store ───────────────────────────────────────────────────────
# run_id → { status, steps, results, error }
_runs: dict[str, dict[str, Any]] = {}
_last_report_issues: list[dict] = []
_cancelled: set[str] = set()  # run IDs cancelled by the user


# ── Request / Response models ─────────────────────────────────────────────────

class PipelineRunRequest(BaseModel):
    repo_url:   str
    commit_sha: str
    max_issues: int  = 0
    parallel:   bool = False
    rescan:     bool = False
    no_rag:     bool = False
    dry_run:    bool = False


class ConfigUpdateRequest(BaseModel):
    gcp_project:              Optional[str]   = None
    vertex_model:             Optional[str]   = None
    max_issues:               Optional[int]   = None
    max_tokens:               Optional[int]   = None
    confidence_high_threshold: Optional[float] = None
    confidence_medium_threshold: Optional[float] = None
    github_token:             Optional[str]   = None
    github_repo:              Optional[str]   = None
    sonar_token:              Optional[str]   = None
    sonar_org:                Optional[str]   = None
    planner_temp:             Optional[float] = None
    generator_temp:           Optional[float] = None
    max_critic_retries:       Optional[int]   = None
    chroma_persist_dir:       Optional[str]   = None
    embedding_model:          Optional[str]   = None
    rag_top_k:                Optional[int]   = None
    langsmith_api_key:        Optional[str]   = None
    langsmith_project:        Optional[str]   = None
    langchain_tracing:        Optional[bool]  = None


# ── Step event helper ─────────────────────────────────────────────────────────

def _push_step(run_id: str, label: str, status: str,
               detail: str = "", ms: int = 0) -> None:
    """Append a step event to the in-memory run store."""
    if run_id not in _runs:
        return
    steps = _runs[run_id].setdefault("steps", [])
    # Update existing step if label matches, else append
    for s in steps:
        if s["label"] == label:
            s["status"] = status
            if detail:
                s["detail"] = detail
            if ms:
                s["ms"] = ms
            return
    steps.append({"label": label, "status": status, "detail": detail, "ms": ms})


# ── Background pipeline runner ────────────────────────────────────────────────

def _run_pipeline_background(run_id: str, req: PipelineRunRequest,
                              report_path: str) -> None:
    """Executes the real pipeline and updates _runs[run_id] in place."""
    import time

    run = _runs[run_id]
    run["status"] = "running"

    step_labels = [
        "Ingest", "Load Repo", "RAG Fetch",
        "Planner", "Generator", "Critic",
        "Validate", "Deliver",
    ]
    for label in step_labels:
        _push_step(run_id, label, "pending")

    try:
        # Check early if already cancelled before starting
        if run_id in _cancelled:
            run["status"] = "cancelled"
            return

        # Apply env overrides from request flags
        if req.dry_run:
            os.environ["SONAR_AI_DRY_RUN"] = "1"
        if req.parallel:
            os.environ["PARALLEL_ISSUES"] = "true"
        if req.rescan:
            os.environ["ENABLE_SONAR_RESCAN"] = "true"
        if req.no_rag:
            os.environ["ENABLE_RAG"] = "false"

        from graph import run_pipeline

        # Wrap each graph node to emit step events.
        # We monkey-patch loguru so INFO messages drive step state.
        import loguru
        _orig_info = logger.info

        def _intercepting_info(msg: str, *a, **kw):
            _orig_info(msg, *a, **kw)
            m = str(msg)
            if   "[Ingest]"    in m: _push_step(run_id, "Ingest",     "running", m)
            elif "[Repo]"      in m: _push_step(run_id, "Load Repo",  "running", m)
            elif "[RAG]"       in m: _push_step(run_id, "RAG Fetch",  "running", m)
            elif "[Planner]"   in m: _push_step(run_id, "Planner",    "running", m)
            elif "[Generator]" in m: _push_step(run_id, "Generator",  "running", m)
            elif "[Critic]"    in m: _push_step(run_id, "Critic",     "running", m)
            elif "[Validate]"  in m: _push_step(run_id, "Validate",   "running", m)
            elif "[Deliver]"   in m: _push_step(run_id, "Deliver",    "running", m)

        logger.info = _intercepting_info  # type: ignore[method-assign]

        def _check_cancelled():
            if run_id in _cancelled:
                raise InterruptedError(f"Run {run_id} cancelled by user")

        t0 = time.time()
        final_state = run_pipeline(
            sonar_report_path=report_path,
            repo_url=req.repo_url,
            commit_sha=req.commit_sha,
            max_issues=req.max_issues,
        )
        elapsed_ms = int((time.time() - t0) * 1000)

        logger.info = _orig_info  # restore

        results: list[dict] = final_state.get("pipeline_results", [])
        run["results"]  = results
        run["status"]   = "done"
        run["elapsed_ms"] = elapsed_ms

        # Mark all still-running steps as done
        for s in run.get("steps", []):
            if s["status"] == "running":
                s["status"] = "done"

    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[API] Pipeline run {run_id} failed: {exc}")
        run["status"] = "error"
        run["error"]  = str(exc)
        for s in run.get("steps", []):
            if s["status"] == "running":
                s["status"] = "error"
                s["detail"] = str(exc)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/api/pipeline/cancel/{run_id}")
def cancel_run(run_id: str) -> dict:
    """Cancel a running pipeline run. Best-effort — stops polling and marks as cancelled."""
    _cancelled.add(run_id)
    if run_id in _runs:
        run = _runs[run_id]
        if run["status"] == "running":
            run["status"] = "cancelled"
            for s in run.get("steps", []):
                if s["status"] == "running":
                    s["status"] = "error"
                    s["detail"] = "Cancelled by user"
    return {"message": f"Run {run_id} cancellation requested"}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": "2.0.0"}


@app.post("/api/report/upload")
async def upload_report(file: UploadFile = File(...)) -> dict:
    """Accept a sonar-report.json upload, parse and return issues."""
    global _last_report_issues

    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(400, "Only .json files are accepted")

    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Invalid JSON: {exc}") from exc

    # Save to a known temp path so /run can reference it
    tmp = Path(tempfile.gettempdir()) / "sonar-ai-last-report.json"
    tmp.write_bytes(content)

    # Parse issues the same way the pipeline does
    from parser import parse_sonar_report
    issues = parse_sonar_report(str(tmp))
    _last_report_issues = [dict(i) for i in issues]

    return {
        "message":    f"Uploaded {file.filename}",
        "issue_count": len(issues),
        "path":        str(tmp),
    }


@app.get("/api/issues")
def get_issues() -> dict:
    """Return issues from the most recently uploaded report."""
    return {"issues": _last_report_issues, "total": len(_last_report_issues)}


@app.post("/api/pipeline/run")
def start_run(req: PipelineRunRequest,
              background_tasks: BackgroundTasks) -> dict:
    """Start a pipeline run and return a run_id to poll."""
    report_path = str(Path(tempfile.gettempdir()) / "sonar-ai-last-report.json")
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

    background_tasks.add_task(_run_pipeline_background, run_id, req, report_path)
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/pipeline/status/{run_id}")
def get_run_status(run_id: str) -> dict:
    """Poll a run's current state — steps, results, status."""
    if run_id not in _runs:
        raise HTTPException(404, f"Run {run_id} not found")
    return _runs[run_id]


@app.get("/api/pipeline/runs")
def list_runs() -> dict:
    """Return a summary list of all runs."""
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


@app.get("/api/config")
def get_config() -> dict:
    """Return current settings (secrets masked)."""
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
        "github_repo":                "",  # not in settings, set per-run
        "sonar_token":                mask(s.sonar_token),
        "sonar_host_url":             s.sonar_host_url,
        "max_critic_retries":         s.max_critic_retries,
        "chroma_persist_dir":         s.chroma_persist_dir,
        "embedding_model":            s.embedding_model,
        "rag_top_k":                  s.rag_top_k,
        "enable_rag":                 s.enable_rag,
        "langsmith_project":          s.langsmith_project,
        "langsmith_api_key":          mask(s.langsmith_api_key),
        "langchain_tracing":          bool(s.langsmith_api_key),
        "parallel_issues":            s.parallel_issues,
        "enable_sonar_rescan":        s.enable_sonar_rescan,
    }


@app.post("/api/config")
def update_config(req: ConfigUpdateRequest) -> dict:
    """
    Persist config changes to .env.
    Only non-None fields in the request body are written.
    """
    env_path = Path(".env")
    lines: list[str] = env_path.read_text().splitlines() if env_path.exists() else []

    mapping: dict[str, Any] = {
        k: v for k, v in req.model_dump().items() if v is not None
    }

    env_key_map = {
        "gcp_project":                "GCP_PROJECT",
        "vertex_model":               "VERTEX_MODEL",
        "max_issues":                 "MAX_ISSUES",
        "max_tokens":                 "MAX_TOKENS",
        "confidence_high_threshold":  "CONFIDENCE_HIGH_THRESHOLD",
        "confidence_medium_threshold": "CONFIDENCE_MEDIUM_THRESHOLD",
        "github_token":               "GITHUB_TOKEN",
        "sonar_token":                "SONAR_TOKEN",
        "max_critic_retries":         "MAX_CRITIC_RETRIES",
        "chroma_persist_dir":         "CHROMA_PERSIST_DIR",
        "embedding_model":            "EMBEDDING_MODEL",
        "rag_top_k":                  "RAG_TOP_K",
        "langsmith_api_key":          "LANGSMITH_API_KEY",
        "langsmith_project":          "LANGSMITH_PROJECT",
        "langchain_tracing":          "LANGCHAIN_TRACING_V2",
    }

    updated: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        written = False
        for field, env_key in env_key_map.items():
            if field in mapping and line.startswith(f"{env_key}="):
                val = "true" if mapping[field] is True else \
                      "false" if mapping[field] is False else str(mapping[field])
                new_lines.append(f"{env_key}={val}")
                updated.add(field)
                written = True
                break
        if not written:
            new_lines.append(line)

    # Append keys not yet present
    for field, env_key in env_key_map.items():
        if field in mapping and field not in updated:
            val = "true" if mapping[field] is True else \
                  "false" if mapping[field] is False else str(mapping[field])
            new_lines.append(f"{env_key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n")
    return {"message": "Config saved", "updated_fields": list(mapping.keys())}
