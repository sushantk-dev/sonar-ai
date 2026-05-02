// src/app/features/pipeline/pipeline.component.ts
import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { PipelineStateService, UiRun } from '../../core/pipeline-state.service';
import { SevClassPipe }    from '../../shared/sev-class.pipe';
import { OutcomeClassPipe } from '../../shared/outcome-class.pipe';
import { OutcomeLabelPipe } from '../../shared/outcome-label.pipe';

@Component({
  selector: 'app-pipeline',
  standalone: true,
  imports: [CommonModule, FormsModule, SevClassPipe, OutcomeClassPipe, OutcomeLabelPipe],
  templateUrl: './pipeline.component.html',
  styleUrl:    './pipeline.component.scss',
})
export class PipelineComponent {
  // Singleton — state persists across tab navigation
  state = inject(PipelineStateService);

  // ── Local form state ──────────────────────────────────────────────────────
  repoUrl   = signal('https://github.com/org/repo.git');
  commitSha = signal('');
  maxIssues = signal(0);
  parallel  = signal(false);
  rescan    = signal(false);
  noRag     = signal(false);
  dryRun    = signal(false);
  showForm  = signal(false);

  // ── Template-callable accessors (return values, not signals) ──────────────
  // Template calls these as running(), error(), selected() — they return the
  // current signal value so *ngIf / binding works without double ()
  running()  { return this.state.running(); }
  error()    { return this.state.error(); }
  selected() { return this.state.selected(); }

  get allRuns()  { return this.state.allRuns; }
  get canCancel(){ return this.state.canCancel; }

  // ── Delegate to service ───────────────────────────────────────────────────
  select(run: UiRun)       { this.state.select(run); }
  doneCnt(run: UiRun)      { return this.state.doneCnt(run); }
  confClass(c: any)        { return this.state.confClass(c); }
  outcomeIcon(o?: string)  { return this.state.outcomeIcon(o); }
  outcomeTitle(o?: string) { return this.state.outcomeTitle(o); }

  startRun() {
    this.showForm.set(false);
    this.state.startRun({
      repo_url:   this.repoUrl(),
      commit_sha: this.commitSha(),
      max_issues: this.maxIssues(),
      parallel:   this.parallel(),
      rescan:     this.rescan(),
      no_rag:     this.noRag(),
      dry_run:    this.dryRun(),
    });
  }

  cancelRun() { this.state.cancelRun(); }
}
