// src/app/features/pipeline/pipeline.component.ts
import { Component, inject, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { DataService } from '../../core/data.service';
import { PipelineRun } from '../../core/models';
import { SevClassPipe }    from '../../shared/sev-class.pipe';
import { OutcomeClassPipe } from '../../shared/outcome-class.pipe';
import { OutcomeLabelPipe } from '../../shared/outcome-label.pipe';

@Component({
  selector: 'app-pipeline',
  standalone: true,
  imports: [CommonModule, SevClassPipe, OutcomeClassPipe, OutcomeLabelPipe],
  templateUrl: './pipeline.component.html',
  styleUrl:    './pipeline.component.scss',
})
export class PipelineComponent implements OnDestroy {
  private svc = inject(DataService);

  runs: PipelineRun[] = [...this.svc.runs];
  simRun: PipelineRun | null = null;
  selected: PipelineRun | null = null;
  simulating = false;
  private timer: ReturnType<typeof setInterval> | null = null;

  get allRuns() {
    return this.simRun ? [this.simRun, ...this.runs] : this.runs;
  }

  select(run: PipelineRun) { this.selected = run; }

  doneCnt(run: PipelineRun) {
    return run.steps.filter(s => s.status === 'done').length;
  }

  outcomeIcon(outcome: string): string {
    return { pr_opened: '✓', draft_pr: '~', escalated: '!', error: '✕' }[outcome] ?? '?';
  }

  outcomeTitle(outcome: string): string {
    return {
      pr_opened: 'Pull request opened',
      draft_pr:  'Draft PR — review required',
      escalated: 'Escalated — manual fix needed',
      error:     'Pipeline error',
    }[outcome] ?? outcome;
  }

  simulate() {
    if (this.simulating) return;
    this.simulating = true;

    const stepLabels  = ['Ingest','Load Repo','RAG Fetch','Planner','Generator','Critic','Validate','Deliver'];
    const details     = [
      'Parsed S106 System.out.println — MAJOR severity',
      'Cloned @ def5678, resolved OrderController.java:55',
      '2 similar fixes retrieved from vector store',
      'Strategy: replace System.out with SLF4J logger (0.95)',
      'Generated unified diff — 3 lines changed',
      'Approved — standard logger replacement pattern',
      'git apply ✓  mvn compile ✓  mvn test ✓',
      'PR #145 opened, fix stored in vector DB',
    ];
    const durations   = [100, 2200, 310, 1700, 2100, 1400, 11000, 720];

    this.simRun = {
      id: 'sim', ruleKey: 'java:S106', severity: 'MAJOR',
      component: 'com.example.api:OrderController.java',
      ragHits: 2, retries: 0,
      steps: stepLabels.map(label => ({ label, status: 'pending' as const })),
    };
    this.selected = this.simRun;

    let step = 0;
    this.timer = setInterval(() => {
      if (!this.simRun) return;
      if (step < this.simRun.steps.length) {
        if (step > 0) {
          this.simRun.steps[step - 1].status = 'done';
          this.simRun.steps[step - 1].detail = details[step - 1];
          this.simRun.steps[step - 1].ms     = durations[step - 1];
        }
        this.simRun.steps[step].status = 'running';
        step++;
      } else {
        const last = this.simRun.steps.length - 1;
        this.simRun.steps[last].status = 'done';
        this.simRun.steps[last].detail = details[last];
        this.simRun.steps[last].ms     = durations[last];
        this.simRun.outcome    = 'pr_opened';
        this.simRun.confidence = 'HIGH';
        this.simRun.prUrl      = 'https://github.com/org/repo/pull/145';
        this.simulating = false;
        clearInterval(this.timer!);
      }
    }, 700);
  }

  ngOnDestroy() {
    if (this.timer) clearInterval(this.timer);
  }
}