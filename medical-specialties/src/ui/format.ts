import type { Certification, SpecialtyNode } from '../data/types';

export const CERTIFICATION_LABEL: Record<Certification, string> = {
  'abms-primary': 'ABMS primary certificate',
  'abms-sub': 'ABMS subspecialty certificate',
  ucns: 'UCNS certificate',
  fellowship: 'Fellowship (no separate certificate)',
  focused: 'Focused practice designation',
  other: 'Recognised outside ABMS',
};

export const LEVEL_LABEL: Record<SpecialtyNode['level'], string> = {
  root: 'Medicine',
  domain: 'Domain',
  specialty: 'Specialty',
  subspecialty: 'Subspecialty',
};

export function yearsLabel(years: number): string {
  if (years === 0) return '—';
  return `${years} yr${years === 1 ? '' : 's'}`;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}
