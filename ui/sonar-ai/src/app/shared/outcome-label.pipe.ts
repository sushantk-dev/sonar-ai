import { Pipe, PipeTransform } from '@angular/core';
import { Outcome } from '../core/models';

@Pipe({ name: 'outcomeLabel', standalone: true })
export class OutcomeLabelPipe implements PipeTransform {
  transform(value: Outcome | undefined): string {
    const map: Record<string, string> = {
      pr_opened: 'PR Opened',
      draft_pr:  'Draft PR',
      escalated: 'Escalated',
      error:     'Error',
      pending:   'Pending',
    };
    return value ? (map[value] ?? value) : '—';
  }
}
