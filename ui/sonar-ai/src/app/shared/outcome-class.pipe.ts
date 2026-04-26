import { Pipe, PipeTransform } from '@angular/core';
import { Outcome } from '../core/models';

@Pipe({ name: 'outcomeClass', standalone: true })
export class OutcomeClassPipe implements PipeTransform {
  transform(value: Outcome | undefined): string {
    const map: Record<string, string> = {
      pr_opened: 'pr-opened',
      draft_pr:  'draft-pr',
      escalated: 'escalated',
      error:     'error',
      pending:   'pending',
    };
    return value ? (map[value] ?? 'pending') : 'pending';
  }
}
