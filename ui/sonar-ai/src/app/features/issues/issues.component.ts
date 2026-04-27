// src/app/features/issues/issues.component.ts
import { Component, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DataService } from '../../core/data.service';
import { SonarIssue, Severity } from '../../core/models';
import { ApiService } from '../../core/api.service';
import { SevClassPipe }    from '../../shared/sev-class.pipe';
import { OutcomeLabelPipe } from '../../shared/outcome-label.pipe';
import { OutcomeClassPipe } from '../../shared/outcome-class.pipe';
import { ShortCompPipe }    from '../../shared/short-comp.pipe';

const SEV_ORDER: Severity[] = ['BLOCKER','CRITICAL','MAJOR','MINOR','INFO'];

@Component({
  selector: 'app-issues',
  standalone: true,
  imports: [CommonModule, FormsModule, SevClassPipe, OutcomeLabelPipe, OutcomeClassPipe, ShortCompPipe],
  templateUrl: './issues.component.html',
  styleUrl:    './issues.component.scss',
})
export class IssuesComponent {
  svc         = inject(DataService);
  private apiSvc = inject(ApiService);

  uploading   = false;
  uploadMsg   = '';
  uploadError = '';

  search    = '';
  sevFilter = 'ALL';
  outFilter = 'ALL';

  private _page  = signal(0);
  readonly PAGE_SIZE = 10;

  drawer: SonarIssue | null = null;
  get kb() { return this.drawer ? this.svc.getRuleKb(this.drawer.ruleKey) : null; }

  severities = ['ALL', 'BLOCKER', 'CRITICAL', 'MAJOR', 'MINOR', 'INFO'];

  outcomeFilters = [
    { label: 'All',       value: 'ALL'       },
    { label: 'PR',        value: 'pr_opened' },
    { label: 'Draft PR',  value: 'draft_pr'  },
    { label: 'Escalated', value: 'escalated' },
    { label: 'Pending',   value: 'pending'   },
  ];

  filtered = computed(() => {
    const q = this.search.toLowerCase();
    return this.svc.issues
      .filter(i => {
        if (this.sevFilter !== 'ALL' && i.severity !== this.sevFilter) return false;
        if (this.outFilter !== 'ALL' && (i.outcome ?? 'pending') !== this.outFilter) return false;
        if (q && !i.ruleKey.toLowerCase().includes(q) &&
                 !i.component.toLowerCase().includes(q) &&
                 !i.message.toLowerCase().includes(q)) return false;
        return true;
      })
      .sort((a, b) => SEV_ORDER.indexOf(a.severity) - SEV_ORDER.indexOf(b.severity));
  });

  totalPages  = computed(() => Math.ceil(this.filtered().length / this.PAGE_SIZE) || 1);
  currentPage = computed(() => Math.min(this._page(), Math.max(0, this.totalPages() - 1)));

  page = computed(() => {
    const start = this.currentPage() * this.PAGE_SIZE;
    return this.filtered().slice(start, start + this.PAGE_SIZE);
  });

  setSev(s: string) { this.sevFilter = s; this._page.set(0); }
  setOut(o: string) { this.outFilter = o; this._page.set(0); }
  goPage(n: number) { this._page.set(n); }

  openDrawer(issue: SonarIssue) {
    this.drawer = this.drawer?.key === issue.key ? null : issue;
  }
  closeDrawer() { this.drawer = null; }

  pagerPages(): number[] {
    return Array.from({ length: Math.min(this.totalPages(), 7) }, (_, i) => i);
  }

  onImport(event: Event) {
    const input = event.target as HTMLInputElement;
    const file  = input.files?.[0];
    if (!file) return;

    this.uploading   = true;
    this.uploadMsg   = '';
    this.uploadError = '';

    this.apiSvc.uploadReport(file).subscribe({
      next: (res) => {
        this.uploading = false;
        this.uploadMsg = `Loaded ${res.issue_count} issues from ${file.name}`;
        // Reload issues from API
        this.apiSvc.getIssues().subscribe(data => {
          // Patch local data service issues with real data
          (this.svc as any).issues = data.issues.map((i: any) => ({
            key:       i.key,
            ruleKey:   i.rule_key,
            severity:  i.severity,
            component: i.component,
            line:      i.line,
            message:   i.message,
            effort:    i.effort,
            status:    i.status,
            outcome:   'pending' as const,
          }));
        });
      },
      error: (err: Error) => {
        this.uploading   = false;
        this.uploadError = err.message;
      },
    });
    input.value = ''; // reset so same file can be re-uploaded
  }
}
