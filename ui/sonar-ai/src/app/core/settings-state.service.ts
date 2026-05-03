// src/app/core/settings-state.service.ts
import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from './api.service';

export interface AppConfig {
  gcpProject:     string;
  gcpLocation:    string;
  model:          string;
  maxIssues:      number;
  maxTokens:      number;
  highThresh:     number;
  medThresh:      number;
  githubToken:    string;
  sonarToken:     string;
  sonarOrg:       string;
  plannerTemp:    number;
  generatorTemp:  number;
  maxRetries:     number;
  chromaPath:     string;
  embeddingModel: string;
  ragTopK:        number;
}

export const VERTEX_MODELS = [
  { value: 'gemini-2.5-flash',     label: 'Gemini 2.5 Flash  (default)' },
  { value: 'gemini-2.5-pro',       label: 'Gemini 2.5 Pro' },
  { value: 'gemini-2.0-flash',     label: 'Gemini 2.0 Flash' },
  { value: 'gemini-1.5-pro-002',   label: 'Gemini 1.5 Pro 002' },
  { value: 'gemini-1.5-flash-002', label: 'Gemini 1.5 Flash 002' },
  { value: 'gemini-1.5-pro',       label: 'Gemini 1.5 Pro' },
  { value: 'gemini-1.5-flash',     label: 'Gemini 1.5 Flash' },
];

export const EMBEDDING_MODELS = [
  { value: 'text-embedding-005',  label: 'text-embedding-005  (default)' },
  { value: 'text-embedding-004',  label: 'text-embedding-004' },
  { value: 'textembedding-gecko', label: 'textembedding-gecko' },
  { value: 'all-MiniLM-L6-v2',   label: 'all-MiniLM-L6-v2 (local)' },
];

// Which tokens are set on the backend (masked '***') vs empty
export interface TokenStatus {
  githubToken:  boolean;   // true = set in .env
  sonarToken:   boolean;
}

@Injectable({ providedIn: 'root' })
export class SettingsStateService {
  private apiSvc = inject(ApiService);

  cfg = signal<AppConfig>({
    gcpProject:     '',
    gcpLocation:    'us-central1',
    model:          'gemini-2.5-flash',
    maxIssues:      0,
    maxTokens:      8192,
    highThresh:     0.80,
    medThresh:      0.50,
    githubToken:    '',
    sonarToken:     '',
    sonarOrg:       'https://sonarcloud.io',
    plannerTemp:    0.1,
    generatorTemp:  0.3,
    maxRetries:     1,
    chromaPath:     './chroma_db',
    embeddingModel: 'text-embedding-005',
    ragTopK:        3,
  });

  // Which tokens are already set on the backend
  tokenStatus = signal<TokenStatus>({
    githubToken:  false,
    sonarToken:   false,
  });

  // Track which token fields the user is actively editing
  // (so we show the input instead of the masked placeholder)
  editingTokens = signal<Set<string>>(new Set());

  loaded  = signal(false);
  saving  = signal(false);
  saved   = signal(false);
  saveErr = signal('');
  loadErr = signal('');

  patch(partial: Partial<AppConfig>) {
    this.cfg.update(c => ({ ...c, ...partial }));
  }

  // ── Token edit helpers ────────────────────────────────────────────────────

  isEditing(field: string): boolean {
    return this.editingTokens().has(field);
  }

  startEditing(field: string) {
    this.editingTokens.update(s => new Set([...s, field]));
    // Clear the field so user types a fresh value
    this.patch({ [field]: '' } as any);
  }

  cancelEditing(field: string) {
    this.editingTokens.update(s => { const n = new Set(s); n.delete(field); return n; });
    // Restore blank (the masked value stays on backend)
    this.patch({ [field]: '' } as any);
  }

  // ── Load from backend ─────────────────────────────────────────────────────

  load() {
    if (this.loaded()) return;

    this.apiSvc.getConfig().subscribe({
      next: (remote) => {
        // Detect which tokens are set (backend sends '***' for set tokens)
        this.tokenStatus.set({
          githubToken:  remote.github_token    === '***',
          sonarToken:   remote.sonar_token     === '***',
        });

        this.cfg.update(c => ({
          ...c,
          gcpProject:     remote.gcp_project                 || c.gcpProject,
          model:          remote.vertex_model                || c.model,
          maxIssues:      remote.max_issues                  ?? c.maxIssues,
          maxTokens:      remote.max_tokens                  ?? c.maxTokens,
          highThresh:     remote.confidence_high_threshold   ?? c.highThresh,
          medThresh:      remote.confidence_medium_threshold ?? c.medThresh,
          sonarOrg:       remote.sonar_host_url              || c.sonarOrg,
          maxRetries:     remote.max_critic_retries          ?? c.maxRetries,
          chromaPath:     remote.chroma_persist_dir          || c.chromaPath,
          embeddingModel: remote.embedding_model             || c.embeddingModel,
          ragTopK:        remote.rag_top_k                   ?? c.ragTopK,
          // Tokens: keep empty — we show the masked placeholder UI instead
          githubToken:    '',
          sonarToken:     '',
                }));

        this.loaded.set(true);
        this.loadErr.set('');
      },
      error: () => {
        this.loadErr.set('Backend offline — showing defaults. Changes will not persist until API is reachable.');
        this.loaded.set(true);
      },
    });
  }

  // ── Save to backend ───────────────────────────────────────────────────────

  save() {
    if (this.saving()) return;
    this.saving.set(true);
    this.saveErr.set('');
    this.saved.set(false);

    const c = this.cfg();

    // Build payload — always include token fields so the backend can:
    // - Write a new value if the user typed one
    // - Leave unchanged if we send null (not editing)
    // - Clear if we send "" (user explicitly blanked it)
    const payload: any = {
      gcp_project:                 c.gcpProject     || undefined,
      vertex_model:                c.model,
      max_issues:                  c.maxIssues,
      max_tokens:                  c.maxTokens,
      confidence_high_threshold:   c.highThresh,
      confidence_medium_threshold: c.medThresh,
      max_critic_retries:          c.maxRetries,
      chroma_persist_dir:          c.chromaPath,
      embedding_model:             c.embeddingModel,
      rag_top_k:                   c.ragTopK,
    };

    // Include token fields if:
    //   - user was actively editing (Change button flow), OR
    //   - token field has a value typed directly (first-time entry flow, no editingTokens set)
    const editing = this.editingTokens();

    if (editing.has('githubToken') || (!this.tokenStatus().githubToken && c.githubToken)) {
      payload['github_token'] = c.githubToken;
    }
    if (editing.has('sonarToken') || (!this.tokenStatus().sonarToken && c.sonarToken)) {
      payload['sonar_token'] = c.sonarToken;
    }

    // Always send sonar_host_url when it has a value
    if (c.sonarOrg) {
      payload['sonar_host_url'] = c.sonarOrg;
    }

    this.apiSvc.saveConfig(payload).subscribe({
      next: () => {
        this.saving.set(false);
        this.saved.set(true);

        // Update tokenStatus based on what was saved
        this.tokenStatus.update(ts => ({
          ...ts,
          ...(payload['github_token'] !== undefined ? { githubToken: !!c.githubToken } : {}),
          ...(payload['sonar_token']  !== undefined ? { sonarToken:  !!c.sonarToken  } : {}),
        }));

        // Clear editing state and token values after save
        this.editingTokens.set(new Set());
        this.cfg.update(cc => ({ ...cc, githubToken: '', sonarToken: '' }));

        setTimeout(() => this.saved.set(false), 2500);
      },
      error: (err: Error) => {
        this.saving.set(false);
        this.saveErr.set(err.message || 'Failed to save — is the backend running?');
      },
    });
  }

}