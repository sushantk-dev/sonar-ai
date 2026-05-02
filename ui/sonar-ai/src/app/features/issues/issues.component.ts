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
  svc            = inject(DataService);
  private apiSvc = inject(ApiService);

  // ── ALL reactive state as signals so computed() tracks them ───────────────
  private _issues    = signal<SonarIssue[]>([...this.svc.issues]);
  private _search    = signal('');
  private _sevFilter = signal('ALL');
  private _outFilter = signal('ALL');
  private _page      = signal(0);

  // Expose for template two-way binding
  get search()    { return this._search(); }
  set search(v: string) { this._search.set(v); this._page.set(0); }

  get sevFilter() { return this._sevFilter(); }
  get outFilter() { return this._outFilter(); }

  uploading        = false;
  uploadMsg        = '';
  uploadError      = '';
  deleteConfirmKey: string | null = null;

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

  // ── computed() reads signals — reruns automatically when any changes ───────
  filtered = computed(() => {
    const q   = this._search().toLowerCase();
    const sev = this._sevFilter();
    const out = this._outFilter();

    return this._issues()
      .filter(i => {
        if (sev !== 'ALL' && i.severity !== sev) return false;
        if (out !== 'ALL' && (i.outcome ?? 'pending') !== out) return false;
        if (q && !i.ruleKey.toLowerCase().includes(q) &&
                 !i.component.toLowerCase().includes(q) &&
                 !i.message.toLowerCase().includes(q)) return false;
        return true;
      })
      .sort((a, b) => SEV_ORDER.indexOf(a.severity) - SEV_ORDER.indexOf(b.severity));
  });

  totalIssues = computed(() => this._issues().length);
  totalPages  = computed(() => Math.ceil(this.filtered().length / this.PAGE_SIZE) || 1);
  currentPage = computed(() => Math.min(this._page(), Math.max(0, this.totalPages() - 1)));

  page = computed(() => {
    const start = this.currentPage() * this.PAGE_SIZE;
    return this.filtered().slice(start, start + this.PAGE_SIZE);
  });

  // ── Filter setters — update signals, reset to page 0 ─────────────────────
  setSev(s: string) { this._sevFilter.set(s); this._page.set(0); }
  setOut(o: string) { this._outFilter.set(o); this._page.set(0); }
  goPage(n: number) { this._page.set(n); }

  // ── Drawer ────────────────────────────────────────────────────────────────
  openDrawer(issue: SonarIssue) {
    if (this.deleteConfirmKey) { this.deleteConfirmKey = null; return; }
    this.drawer = this.drawer?.key === issue.key ? null : issue;
  }
  closeDrawer() { this.drawer = null; }

  // ── Delete ────────────────────────────────────────────────────────────────
  requestDelete(event: Event, key: string) {
    event.stopPropagation();
    this.deleteConfirmKey = this.deleteConfirmKey === key ? null : key;
  }

  confirmDelete(event: Event, key: string) {
    event.stopPropagation();
    this._issues.update(list => list.filter(i => i.key !== key));
    this.deleteConfirmKey = null;
    if (this.drawer?.key === key) this.drawer = null;
  }

  cancelDelete(event: Event) {
    event.stopPropagation();
    this.deleteConfirmKey = null;
  }

  pagerPages(): number[] {
    return Array.from({ length: Math.min(this.totalPages(), 7) }, (_, i) => i);
  }

  // ── Import ────────────────────────────────────────────────────────────────
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
        this.apiSvc.getIssues().subscribe({
          next: (data) => {
            const mapped: SonarIssue[] = data.issues.map((i: any) => ({
              key:       i.key,
              ruleKey:   i.rule_key,
              severity:  i.severity as Severity,
              component: i.component,
              line:      i.line,
              message:   i.message,
              effort:    i.effort,
              status:    i.status,
              outcome:   'pending' as const,
            }));
            this._issues.set(mapped);
            this._page.set(0);
          },
          error: () => this._parseFileLocally(file),
        });
      },
      error: () => {
        this.uploading = false;
        this._parseFileLocally(file);
      },
    });
    input.value = '';
  }

  private _parseFileLocally(file: File) {
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const json = JSON.parse(e.target?.result as string);
        const raw: any[] = Array.isArray(json) ? json : (json.issues ?? []);
        const mapped: SonarIssue[] = raw.map((i: any) => ({
          key:       i.key       ?? i.id       ?? crypto.randomUUID(),
          ruleKey:   i.rule      ?? i.ruleKey  ?? i.rule_key ?? '',
          severity:  (i.severity ?? 'INFO') as Severity,
          component: i.component ?? i.file     ?? '',
          line:      i.line      ?? i.textRange?.startLine ?? 0,
          message:   i.message   ?? i.msg      ?? '',
          effort:    i.effort    ?? i.remFn    ?? '',
          status:    i.status    ?? 'OPEN',
          outcome:   'pending' as const,
        }));
        this._issues.set(mapped);
        this._page.set(0);
        this.uploadMsg = `Loaded ${mapped.length} issues from ${file.name} (local parse)`;
      } catch {
        this.uploadError = `Could not parse ${file.name} — make sure it is valid JSON`;
      }
    };
    reader.readAsText(file);
  }
}
