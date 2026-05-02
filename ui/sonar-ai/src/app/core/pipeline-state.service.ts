// src/app/core/pipeline-state.service.ts
import { Injectable, inject, signal, computed } from '@angular/core';
import { Subscription } from 'rxjs';
import { ApiService, RunStatus, PipelineStep } from './api.service';
import { DataService } from './data.service';

export type ConfLabel = 'HIGH' | 'MEDIUM' | 'LOW' | null;

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
  status?:     'queued' | 'running' | 'done' | 'error' | 'cancelled';
}

@Injectable({ providedIn: 'root' })
export class PipelineStateService {
  private api  = inject(ApiService);
  private data = inject(DataService);

  // ── Persisted across navigation (singleton) ───────────────────────────────
  runs     = signal<UiRun[]>(this._seedRuns());
  selected = signal<UiRun | null>(null);
  running  = signal(false);
  error    = signal<string | null>(null);

  // Active run tracking for cancel
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
    }));
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  select(run: UiRun)   { this.selected.set(run); }
  doneCnt(run: UiRun)  { return run.steps.filter(s => s.status === 'done').length; }
  confClass(c: ConfLabel | string | undefined) { return (c ?? '').toLowerCase(); }

  outcomeIcon(o?: string) {
    return { pr_opened: '✓', draft_pr: '~', escalated: '!', error: '✕', cancelled: '◼' }[o ?? ''] ?? '?';
  }

  outcomeTitle(o?: string) {
    return {
      pr_opened: 'Pull request opened',
      draft_pr:  'Draft PR — review required',
      escalated: 'Escalated — manual fix needed',
      error:     'Pipeline error',
      cancelled: 'Run cancelled',
    }[o ?? ''] ?? o ?? '';
  }

  confLabel(score: number): ConfLabel {
    if (score >= 0.8) return 'HIGH';
    if (score >= 0.5) return 'MEDIUM';
    return 'LOW';
  }

  // ── Start ─────────────────────────────────────────────────────────────────
  startRun(req: {
    repo_url: string; commit_sha: string; max_issues: number;
    parallel: boolean; rescan: boolean; no_rag: boolean; dry_run: boolean;
  }) {
    if (this.running()) return;
    this.running.set(true);
    this.error.set(null);

    this.api.startRun(req).subscribe({
      next: ({ run_id }) => this._pollRun(run_id),
      error: (err: Error) => {
        this.error.set(err.message);
        this.running.set(false);
      },
    });
  }

  private _pollRun(runId: string) {
    this._activeRunId = runId;

    const liveRun: UiRun = {
      id:        runId,
      ruleKey:   'Starting…',
      severity:  'MAJOR',
      component: '',
      steps: ['Ingest','Load Repo','RAG Fetch','Planner','Generator','Critic','Validate','Deliver']
        .map(label => ({ label, status: 'pending' as const, detail: '', ms: 0 })),
      live:   true,
      status: 'running',
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
      const updated: UiRun = {
        ...r,
        steps:      status.steps ?? r.steps,
        outcome:    first?.outcome,
        confidence: first ? this.confLabel(first.confidence) : undefined,
        prUrl:      first?.pr_url ?? undefined,
        status:     status.status,
      };
      if (first) {
        updated.ruleKey   = first.rule_key;
        updated.severity  = first.severity;
        updated.component = first.file_path;
      }
      return updated;
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
      if ((status.results?.length ?? 0) > 1) {
        this._explodeResults(runId, status);
      }
    }
  }

  private _explodeResults(runId: string, status: RunStatus) {
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
    }));

    this.runs.update(rs => [...newCards, ...rs.filter(r => r.id !== runId)]);
    if (newCards[0]) this.selected.set(newCards[0]);
  }

  // ── Cancel / Stop ─────────────────────────────────────────────────────────
  cancelRun() {
    const runId = this._activeRunId;
    if (!runId) return;

    // Stop polling immediately
    this._poll?.unsubscribe();
    this._poll = undefined;

    // Call backend cancel endpoint
    this.api.cancelRun(runId).subscribe({
      next: () => {},
      error: () => {}, // best-effort
    });

    // Mark run as cancelled in UI instantly
    this.runs.update(rs => rs.map(r => {
      if (r.id !== runId) return r;
      return {
        ...r,
        status:  'cancelled',
        outcome: 'cancelled',
        steps: r.steps.map(s =>
          s.status === 'running' ? { ...s, status: 'error' as const, detail: 'Cancelled by user' } : s
        ),
      };
    }));

    const updated = this.runs().find(r => r.id === runId);
    if (updated && this.selected()?.id === runId) {
      this.selected.set(updated);
    }

    this.running.set(false);
    this._activeRunId = null;
  }

  get canCancel() { return this.running() && !!this._activeRunId; }
}
