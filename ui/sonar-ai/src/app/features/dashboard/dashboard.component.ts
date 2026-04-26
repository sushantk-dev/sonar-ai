// src/app/features/dashboard/dashboard.component.ts
import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { DataService } from '../../core/data.service';
import { SevClassPipe }    from '../../shared/sev-class.pipe';
import { OutcomeLabelPipe } from '../../shared/outcome-label.pipe';
import { OutcomeClassPipe } from '../../shared/outcome-class.pipe';
import { ShortCompPipe }    from '../../shared/short-comp.pipe';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, RouterLink, SevClassPipe, OutcomeLabelPipe, OutcomeClassPipe, ShortCompPipe],
  templateUrl: './dashboard.component.html',
  styleUrl: './dashboard.component.scss',
})
export class DashboardComponent {
  svc = inject(DataService);

  pipelineSteps = [
    { name: 'Ingest',     desc: 'Parse + sort'    },
    { name: 'Load Repo',  desc: 'Clone + AST'     },
    { name: 'RAG Fetch',  desc: 'Vector store'    },
    { name: 'Planner',    desc: 'Chain-of-thought' },
    { name: 'Generator',  desc: 'Unified diff'    },
    { name: 'Critic',     desc: 'Review patch'    },
    { name: 'Validate',   desc: 'git + mvn'       },
    { name: 'Deliver',    desc: 'PR / Escalate'   },
  ];

  get breakdown() {
    const sevs = ['BLOCKER', 'CRITICAL', 'MAJOR', 'MINOR', 'INFO'] as const;
    return sevs.map(s => ({
      label: s,
      cls:   s.toLowerCase(),
      count: this.svc.issues.filter(i => i.severity === s).length,
    })).filter(b => b.count > 0);
  }
}