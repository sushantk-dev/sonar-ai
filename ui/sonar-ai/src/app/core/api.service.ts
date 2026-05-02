// src/app/core/api.service.ts
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import {
  Observable, interval, switchMap, takeWhile,
  catchError, throwError, tap, share
} from 'rxjs';

export interface RunRequest {
  repo_url:   string;
  commit_sha: string;
  max_issues: number;
  parallel:   boolean;
  rescan:     boolean;
  no_rag:     boolean;
  dry_run:    boolean;
}

export interface PipelineStep {
  label:  string;
  status: 'pending' | 'running' | 'done' | 'error';
  detail: string;
  ms:     number;
}

export interface IssueResult {
  issue_key:       string;
  rule_key:        string;
  severity:        string;
  file_path:       string;
  line:            number;
  outcome:         string;
  pr_url:          string | null;
  escalation_path: string | null;
  confidence:      number;
  sonar_rescan_ok: boolean | null;
  error:           string | null;
}

export interface RunStatus {
  id:         string;
  status:     'queued' | 'running' | 'done' | 'error';
  steps:      PipelineStep[];
  results:    IssueResult[];
  error:      string | null;
  elapsed_ms?: number;
}

export interface ApiIssue {
  key:       string;
  rule_key:  string;
  severity:  string;
  component: string;
  line:      number;
  message:   string;
  status:    string;
  effort:    string;
}

export interface BackendConfig {
  gcp_project:                 string;
  vertex_model:                string;
  max_issues:                  number;
  max_tokens:                  number;
  confidence_high_threshold:   number;
  confidence_medium_threshold: number;
  github_token:                string;
  github_repo:                 string;
  sonar_token:                 string;
  sonar_host_url:              string;
  max_critic_retries:          number;
  chroma_persist_dir:          string;
  embedding_model:             string;
  rag_top_k:                   number;
  enable_rag:                  boolean;
  langsmith_project:           string;
  langsmith_api_key:           string;
  langchain_tracing:           boolean;
  parallel_issues:             boolean;
  enable_sonar_rescan:         boolean;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);
  readonly base = 'http://localhost:8000';

  // ── Health ────────────────────────────────────────────────────────────────

  health(): Observable<{ status: string }> {
    return this.http.get<{ status: string }>(`${this.base}/api/health`);
  }

  // ── Report ────────────────────────────────────────────────────────────────

  uploadReport(file: File): Observable<{ message: string; issue_count: number; path: string }> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<any>(`${this.base}/api/report/upload`, form);
  }

  getIssues(): Observable<{ issues: ApiIssue[]; total: number }> {
    return this.http.get<any>(`${this.base}/api/issues`);
  }

  // ── Pipeline ──────────────────────────────────────────────────────────────

  startRun(req: RunRequest): Observable<{ run_id: string; status: string }> {
    return this.http.post<any>(`${this.base}/api/pipeline/run`, req);
  }

  getRunStatus(runId: string): Observable<RunStatus> {
    return this.http.get<RunStatus>(`${this.base}/api/pipeline/status/${runId}`);
  }

  /**
   * Poll a run every 1.5 s until status is 'done' or 'error'.
   * Emits each intermediate RunStatus so the UI can show live steps.
   */
  pollRun(runId: string): Observable<RunStatus> {
    return interval(1500).pipe(
      switchMap(() => this.getRunStatus(runId)),
      takeWhile(s => s.status !== 'done' && s.status !== 'error', true),
      share(),
    );
  }

  cancelRun(runId: string): Observable<{ message: string }> {
    return this.http.post<any>(`${this.base}/api/pipeline/cancel/${runId}`, {});
  }

  listRuns(): Observable<{ runs: any[] }> {
    return this.http.get<any>(`${this.base}/api/pipeline/runs`);
  }

  // ── Config ────────────────────────────────────────────────────────────────

  getConfig(): Observable<BackendConfig> {
    return this.http.get<BackendConfig>(`${this.base}/api/config`);
  }

  saveConfig(cfg: Partial<BackendConfig>): Observable<{ message: string }> {
    return this.http.post<any>(`${this.base}/api/config`, cfg);
  }

  // ── Error helper ──────────────────────────────────────────────────────────

  handleError(err: HttpErrorResponse): Observable<never> {
    const msg = err.error?.detail ?? err.message ?? 'Unknown API error';
    return throwError(() => new Error(msg));
  }
}
