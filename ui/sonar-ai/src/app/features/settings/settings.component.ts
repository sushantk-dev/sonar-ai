// src/app/features/settings/settings.component.ts
import { Component, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

interface Tab { id: string; label: string; icon: string; }

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.component.html',
  styleUrl:    './settings.component.scss',
})
export class SettingsComponent {
  active = signal('pipeline');
  saved  = false;

  tabs: Tab[] = [
    { id: 'pipeline', label: 'Pipeline', icon: 'M3 6h18M3 12h18M3 18h18' },
    { id: 'github',   label: 'GitHub',   icon: 'M12 2C6.48 2 2 6.48 2 12c0 4.42 2.87 8.17 6.84 9.49.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.45-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.61.07-.61 1 .07 1.53 1.03 1.53 1.03.89 1.52 2.34 1.08 2.91.83.09-.65.35-1.08.63-1.33-2.22-.25-4.55-1.11-4.55-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.64 0 0 .84-.27 2.75 1.02A9.56 9.56 0 0112 6.8c.85 0 1.71.11 2.5.33 1.91-1.29 2.75-1.02 2.75-1.02.55 1.37.2 2.39.1 2.64.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85v2.74c0 .27.18.58.69.48A10.01 10.01 0 0022 12c0-5.52-4.48-10-10-10z' },
    { id: 'agents',   label: 'Agents',   icon: 'M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z' },
    { id: 'tracing',  label: 'Tracing',  icon: 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z' },
  ];

  cfg = {
    gcpProject:       'my-sonar-ai-project',
    model:            'gemini-1.5-pro',
    maxIssues:        20,
    maxTokens:        4096,
    highThresh:       0.85,
    medThresh:        0.60,
    githubToken:      '',
    githubRepo:       'org/repo',
    commitSha:        '',
    sonarToken:       '',
    sonarOrg:         'my-org',
    plannerTemp:      0.2,
    generatorTemp:    0.1,
    maxRetries:       1,
    chromaPath:       './chroma_db',
    embeddingModel:   'all-MiniLM-L6-v2',
    ragTopK:          3,
    tracingEnabled:   true,
    langsmithKey:     '',
    langsmithProject: 'sonar-ai',
  };

  get envSnippet(): string {
    const key     = this.cfg.langsmithKey || '<your-key>';
    const proj    = this.cfg.langsmithProject || 'sonar-ai';
    const enabled = this.cfg.tracingEnabled ? 'true' : 'false';
    return [
      `LANGCHAIN_TRACING_V2=${enabled}`,
      `LANGCHAIN_ENDPOINT=https://api.smith.langchain.com`,
      `LANGCHAIN_API_KEY=${key}`,
      `LANGCHAIN_PROJECT=${proj}`,
    ].join('\n');
  }

  save() {
    this.saved = true;
    setTimeout(() => this.saved = false, 2500);
  }
}