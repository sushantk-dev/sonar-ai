import { Pipe, PipeTransform } from '@angular/core';
@Pipe({ name: 'outcomeClass', standalone: true })
export class OutcomeClassPipe implements PipeTransform {
  transform(value: string | undefined): string {
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
