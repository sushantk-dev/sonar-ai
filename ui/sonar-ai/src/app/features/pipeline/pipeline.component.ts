// src/app/features/pipeline/pipeline.component.ts
import { Component, inject, OnDestroy, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subscription } from 'rxjs';
import { ApiService, RunStatus, PipelineStep } from '../../core/api.service';
import { DataService } from '../../core/data.service';
import { SevClassPipe }    from '../../shared/sev-class.pipe';
import { OutcomeClassPipe } from '../../shared/outcome-class.pipe';
import { OutcomeLabelPipe } from '../../shared/outcome-label.pipe';

type ConfLabel = 'HIGH' | 'MEDIUM' | 'LOW' | null;

interface UiRun {
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
  live:        boolean;   // true = comes from real API
}

@Component({
  selector: 'app-pipeline',
  standalone: true,
  imports: [CommonModule, FormsModule, SevClassPipe, OutcomeClassPipe, OutcomeLabelPipe],
  templateUrl: './pipeline.component.html',
  styleUrl:    './pipeline.component.scss',
})
export class PipelineComponent implements OnDestroy {
  private api  = inject(ApiService);
  private data = inject(DataService);

  // ── Run form ──────────────────────────────────────────────────────────────
  repoUrl   = signal('https://github.com/org/repo.git');
  commitSha = signal('');
  maxIssues = signal(0);
  parallel  = signal(false);
  rescan    = signal(false);
  noRag     = signal(false);
  dryRun    = signal(false);

  showForm  = signal(false);

  // ── State ─────────────────────────────────────────────────────────────────
  runs     = signal<UiRun[]>(this._seedRuns());
  selected = signal<UiRun | null>(null);
  running  = signal(false);
  error    = signal<string | null>(null);

  private _poll?: Subscription;

  get allRuns() { return this.runs(); }

  // ── Seed with local mock data so the UI isn't empty on first open ─────────
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
    }));
  }

  // ── UI helpers ────────────────────────────────────────────────────────────
  select(run: UiRun)   { this.selected.set(run); }
  doneCnt(run: UiRun)  { return run.steps.filter(s => s.status === 'done').length; }
  confClass(c: ConfLabel | string | undefined) { return (c ?? '').toLowerCase(); }

  outcomeIcon(o?: string)  {
    return { pr_opened:'✓', draft_pr:'~', escalated:'!', error:'✕' }[o ?? ''] ?? '?';
  }
  outcomeTitle(o?: string) {
    return {
      pr_opened: 'Pull request opened',
      draft_pr:  'Draft PR — review required',
      escalated: 'Escalated — manual fix needed',
      error:     'Pipeline error',
    }[o ?? ''] ?? o ?? '';
  }

  confLabel(score: number): ConfLabel {
    if (score >= 0.8)  return 'HIGH';
    if (score >= 0.5)  return 'MEDIUM';
    return 'LOW';
  }

  // ── Start real pipeline run ───────────────────────────────────────────────
  startRun() {
    if (this.running()) return;

    this.running.set(true);
    this.error.set(null);
    this.showForm.set(false);

    this.api.startRun({
      repo_url:   this.repoUrl(),
      commit_sha: this.commitSha(),
      max_issues: this.maxIssues(),
      parallel:   this.parallel(),
      rescan:     this.rescan(),
      no_rag:     this.noRag(),
      dry_run:    this.dryRun(),
    }).subscribe({
      next: ({ run_id }) => this._pollRun(run_id),
      error: (err: Error) => {
        this.error.set(err.message);
        this.running.set(false);
      },
    });
  }

  private _pollRun(runId: string) {
    // Create a live placeholder run card immediately
    const liveRun: UiRun = {
      id:        runId,
      ruleKey:   'Starting…',
      severity:  'MAJOR',
      component: '',
      steps: [
        'Ingest','Load Repo','RAG Fetch',
        'Planner','Generator','Critic','Validate','Deliver',
      ].map(label => ({ label, status: 'pending' as const, detail: '', ms: 0 })),
      live: true,
    };

    this.runs.update(rs => [liveRun, ...rs]);
    this.selected.set(liveRun);

    this._poll = this.api.pollRun(runId).subscribe({
      next: (status: RunStatus) => this._applyStatus(runId, status),
      error: (err: Error) => {
        this.error.set(err.message);
        this.running.set(false);
      },
    });
  }

  private _applyStatus(runId: string, status: RunStatus) {
    this.runs.update(rs => rs.map(r => {
      if (r.id !== runId) return r;

      // Derive display fields from the first result if available
      const first = status.results?.[0];
      const updatedRun: UiRun = {
        ...r,
        steps:      status.steps ?? r.steps,
        outcome:    first?.outcome,
        confidence: first ? this.confLabel(first.confidence) : undefined,
        prUrl:      first?.pr_url ?? undefined,
      };

      // If first result has rule info, fill run header
      if (first) {
        updatedRun.ruleKey   = first.rule_key;
        updatedRun.severity  = first.severity;
        updatedRun.component = first.file_path;
      }

      return updatedRun;
    }));

    // Keep selected in sync
    if (this.selected()?.id === runId) {
      const updated = this.runs().find(r => r.id === runId);
      if (updated) this.selected.set(updated);
    }

    if (status.status === 'done' || status.status === 'error') {
      this.running.set(false);
      if (status.status === 'error' && status.error) {
        this.error.set(status.error);
      }
      // If multi-issue run, explode results into individual run cards
      if ((status.results?.length ?? 0) > 1) {
        this._explodeResults(runId, status);
      }
    }
  }

  /** For multi-issue runs, replace the placeholder card with one card per issue. */
  private _explodeResults(runId: string, status: RunStatus) {
    const newCards: UiRun[] = status.results.map((r, i) => ({
      id:         `${runId}-${i}`,
      ruleKey:    r.rule_key,
      severity:   r.severity,
      component:  r.file_path,
      outcome:    r.outcome,
      confidence: this.confLabel(r.confidence),
      prUrl:      r.pr_url ?? undefined,
      steps:      status.steps ?? [],   // shared steps for now
      live:       true,
    }));

    this.runs.update(rs => [...newCards, ...rs.filter(r => r.id !== runId)]);
    if (newCards[0]) this.selected.set(newCards[0]);
  }

  // ── Cleanup ───────────────────────────────────────────────────────────────
  ngOnDestroy() { this._poll?.unsubscribe(); }
}
