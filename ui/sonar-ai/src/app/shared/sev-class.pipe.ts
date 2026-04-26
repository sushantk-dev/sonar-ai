import { Pipe, PipeTransform } from '@angular/core';
import { Severity } from '../core/models';

@Pipe({ name: 'sevClass', standalone: true })
export class SevClassPipe implements PipeTransform {
  transform(value: Severity): string {
    return value.toLowerCase();
  }
}
