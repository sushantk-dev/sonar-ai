// src/app/core/pipeline-state.service.ts
import { Injectable, inject, signal } from '@angular/core';
import { Subscription } from 'rxjs';
import { ApiService, RunStatus, PipelineStep } from './api.service';
import { DataService } from './data.service';

export type ConfLabel = 'HIGH' | 'MEDIUM' | 'LOW' | null;

export interface RunRequest {
  repo_url:   string;
  commit_sha: string;
  max_issues: number;
  parallel:   boolean;
  rescan:     boolean;
  no_rag:     boolean;
  dry_run:    boolean;
}

export interface UiRun {
  id:          string;
  ruleKey:     string;
  severity:    string;
  component:   string;
  steps:       PipelineStep[];
  outcome?:    string;
  confidence?: ConfLabel;
  prUrl?:      string;
  ragHits?:    number;
  retries?:    number;
  live:        boolean;
  status?:     'queued' | 'running' | 'done' | 'error' | 'cancelled' | 'empty';
  request?:    RunRequest;
}

@Injectable({ providedIn: 'root' })
export class PipelineStateService {
  private api  = inject(ApiService);
  private data = inject(DataService);

  runs     = signal<UiRun[]>(this._seedRuns());
  selected = signal<UiRun | null>(null);
  running  = signal(false);
  error    = signal<string | null>(null);

  private _activeRunId: string | null = null;
  private _poll?: Subscription;

  get allRuns() { return this.runs(); }

  private _seedRuns(): UiRun[] {
    return this.data.runs.map(r => ({
      id:         r.id,
      ruleKey:    r.ruleKey,
      severity:   r.severity,
      component:  r.component,
      steps:      r.steps.map(s => ({
        label:  s.label,
        status: s.status as any,
        detail: s.detail ?? '',
        ms:     s.ms     ?? 0,
      })),
      outcome:    r.outcome,
      confidence: r.confidence as ConfLabel,
      prUrl:      r.prUrl,
      ragHits:    r.ragHits,
      retries:    r.retries,
      live:       false,
      status:     'done',
      request:    undefined,
    }));
  }

  select(run: UiRun)   { this.selected.set(run); }
  doneCnt(run: UiRun)  { return run.steps.filter(s => s.status === 'done').length; }
  confClass(c: ConfLabel | string | undefined) { return (c ?? '').toLowerCase(); }

  outcomeIcon(o?: string) {
    return { pr_opened:'✓', draft_pr:'~', escalated:'!', error:'✕', cancelled:'◼', empty:'—' }[o ?? ''] ?? '?';
  }

  outcomeTitle(o?: string) {
    return {
      pr_opened: 'Pull request opened',
      draft_pr:  'Draft PR — review required',
      escalated: 'Escalated — manual fix needed',
      error:     'Pipeline error',
      cancelled: 'Run cancelled',
      empty:     'No issues found in report',
    }[o ?? ''] ?? o ?? '';
  }

  confLabel(score: number): ConfLabel {
    if (score >= 0.8) return 'HIGH';
    if (score >= 0.5) return 'MEDIUM';
    return 'LOW';
  }

  // ── Start ─────────────────────────────────────────────────────────────────
  startRun(req: RunRequest) {
    if (this.running()) return;
    this.running.set(true);
    this.error.set(null);

    this.api.startRun(req).subscribe({
      next: ({ run_id }) => this._pollRun(run_id, req),
      error: (err: Error) => {
        this.error.set(err.message);
        this.running.set(false);
      },
    });
  }

  private _pollRun(runId: string, req: RunRequest) {
    this._activeRunId = runId;

    const liveRun: UiRun = {
      id:        runId,
      ruleKey:   '—',           // blank until first result comes in
      severity:  'INFO',
      component: '',
      steps: ['Ingest','Load Repo','RAG Fetch','Planner','Generator','Critic','Validate','Deliver']
        .map(label => ({ label, status: 'pending' as const, detail: '', ms: 0 })),
      live:    true,
      status:  'running',
      request: req,
    };

    this.runs.update(rs => [liveRun, ...rs]);
    this.selected.set(liveRun);

    this._poll = this.api.pollRun(runId).subscribe({
      next:  (s: RunStatus) => this._applyStatus(runId, s),
      error: (err: Error) => {
        this.error.set(err.message);
        this.running.set(false);
        this._activeRunId = null;
      },
    });
  }

  private _applyStatus(runId: string, status: RunStatus) {
    this.runs.update(rs => rs.map(r => {
      if (r.id !== runId) return r;

      const first = status.results?.[0];

      // Handle no-issues case: pipeline finished but nothing was processed
      const noResults = (status.status === 'done') && (!status.results || status.results.length === 0);

      return {
        ...r,
        steps:      status.steps?.length ? status.steps : r.steps,
        outcome:    noResults ? 'empty' : (first?.outcome ?? r.outcome),
        confidence: first ? this.confLabel(first.confidence) : r.confidence,
        prUrl:      first?.pr_url ?? r.prUrl,
        status:     noResults ? 'empty' as any : status.status,
        // Only overwrite header fields if we have a real result
        ruleKey:    first?.rule_key  ? first.rule_key  : r.ruleKey,
        severity:   first?.severity  ? first.severity  : r.severity,
        component:  first?.file_path ? first.file_path : r.component,
      };
    }));

    if (this.selected()?.id === runId) {
      const updated = this.runs().find(r => r.id === runId);
      if (updated) this.selected.set(updated);
    }

    if (status.status === 'done' || status.status === 'error') {
      this.running.set(false);
      this._activeRunId = null;

      if (status.status === 'error' && status.error) {
        this.error.set(status.error);
      }

      // No issues in the report — auto-remove placeholder after 4 s
      const noResults = !status.results || status.results.length === 0;
      if (noResults && status.status === 'done') {
        this.error.set('No issues found in the report — nothing to process.');
        setTimeout(() => this.deleteRun(runId), 4000);
        return;
      }

      if ((status.results?.length ?? 0) > 1) {
        this._explodeResults(runId, status);
      }
    }
  }

  private _explodeResults(runId: string, status: RunStatus) {
    const parentReq = this.runs().find(r => r.id === runId)?.request;

    const newCards: UiRun[] = status.results.map((r, i) => ({
      id:         `${runId}-${i}`,
      ruleKey:    r.rule_key,
      severity:   r.severity,
      component:  r.file_path,
      outcome:    r.outcome,
      confidence: this.confLabel(r.confidence),
      prUrl:      r.pr_url ?? undefined,
      steps:      status.steps ?? [],
      live:       true,
      status:     'done' as const,
      request:    parentReq,
    }));

    this.runs.update(rs => [...newCards, ...rs.filter(r => r.id !== runId)]);
    if (newCards[0]) this.selected.set(newCards[0]);
  }

  // ── Delete a finished run card ────────────────────────────────────────────
  deleteRun(id: string) {
    // Don't delete actively running run
    if (id === this._activeRunId) return;

    this.runs.update(rs => rs.filter(r => r.id !== id));

    // Clear selected if it was this run
    if (this.selected()?.id === id) {
      this.selected.set(this.runs()[0] ?? null);
    }
  }

  // ── Cancel ────────────────────────────────────────────────────────────────
  cancelRun() {
    const runId = this._activeRunId;
    if (!runId) return;

    this._poll?.unsubscribe();
    this._poll = undefined;

    this.api.cancelRun(runId).subscribe({ error: () => {} });

    this.runs.update(rs => rs.map(r => {
      if (r.id !== runId) return r;
      return {
        ...r,
        status:  'cancelled',
        outcome: 'cancelled',
        steps: r.steps.map(s =>
          s.status === 'running'
            ? { ...s, status: 'error' as const, detail: 'Cancelled by user' }
            : s
        ),
      };
    }));

    const updated = this.runs().find(r => r.id === runId);
    if (updated && this.selected()?.id === runId) this.selected.set(updated);

    this.running.set(false);
    this._activeRunId = null;
  }

  get canCancel() { return this.running() && !!this._activeRunId; }
}
