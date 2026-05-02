// src/app/core/issues-state.service.ts
import { Injectable, inject, signal, computed } from '@angular/core';
import { DataService } from './data.service';
import { ApiService } from './api.service';
import { SonarIssue, Severity } from './models';

const SEV_ORDER: Severity[] = ['BLOCKER','CRITICAL','MAJOR','MINOR','INFO'];

@Injectable({ providedIn: 'root' })
export class IssuesStateService {
  private data   = inject(DataService);
  private apiSvc = inject(ApiService);

  // ── All state as signals — persists for full app lifetime ─────────────────
  private _issues    = signal<SonarIssue[]>([...this.data.issues]);
  private _search    = signal('');
  private _sevFilter = signal('ALL');
  private _outFilter = signal('ALL');
  private _page      = signal(0);

  uploading        = signal(false);
  uploadMsg        = signal('');
  uploadError      = signal('');
  deleteConfirmKey = signal<string | null>(null);

  readonly PAGE_SIZE = 10;

  // ── Public getters / setters ──────────────────────────────────────────────
  get search()    { return this._search(); }
  set search(v: string) { this._search.set(v); this._page.set(0); }

  get sevFilter() { return this._sevFilter(); }
  get outFilter() { return this._outFilter(); }

  setSev(s: string) { this._sevFilter.set(s); this._page.set(0); }
  setOut(o: string) { this._outFilter.set(o); this._page.set(0); }
  goPage(n: number) { this._page.set(n); }

  // ── Computed views ────────────────────────────────────────────────────────
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

  pagerPages = computed(() =>
    Array.from({ length: Math.min(this.totalPages(), 7) }, (_, i) => i)
  );

  // ── Delete ────────────────────────────────────────────────────────────────
  requestDelete(key: string) {
    this.deleteConfirmKey.set(this.deleteConfirmKey() === key ? null : key);
  }

  confirmDelete(key: string) {
    this._issues.update(list => list.filter(i => i.key !== key));
    this.deleteConfirmKey.set(null);
  }

  cancelDelete() {
    this.deleteConfirmKey.set(null);
  }

  // ── Import ────────────────────────────────────────────────────────────────
  onImport(file: File) {
    this.uploading.set(true);
    this.uploadMsg.set('');
    this.uploadError.set('');

    this.apiSvc.uploadReport(file).subscribe({
      next: (res) => {
        this.uploading.set(false);
        this.uploadMsg.set(`Loaded ${res.issue_count} issues from ${file.name}`);
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
          error: () => this._parseLocally(file),
        });
      },
      error: () => {
        this.uploading.set(false);
        this._parseLocally(file);
      },
    });
  }

  private _parseLocally(file: File) {
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
        this.uploadMsg.set(`Loaded ${mapped.length} issues from ${file.name} (local parse)`);
      } catch {
        this.uploadError.set(`Could not parse ${file.name} — make sure it is valid JSON`);
      }
    };
    reader.readAsText(file);
  }
}
