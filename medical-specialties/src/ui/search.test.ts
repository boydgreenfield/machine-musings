import { describe, expect, it } from 'vitest';
import { fuzzy } from './search';

describe('fuzzy search', () => {
  it('prefers exact substrings at word starts', () => {
    const a = fuzzy('onco', 'Medical Oncology')!;
    const b = fuzzy('onco', 'Gynecologic Oncology')!;
    expect(a.score).toBeGreaterThan(50);
    expect(b.score).toBeGreaterThan(50);
    expect(a.ranges).toEqual([[8, 12]]);
  });
  it('matches initials and prefix runs but not scattered letters', () => {
    expect(fuzzy('pem', 'Pediatric Emergency Medicine')).not.toBeNull();
    expect(fuzzy('card', 'Cardiovascular Disease')).not.toBeNull();
    expect(fuzzy('onco', 'Urogynecology')).toBeNull();
  });
  it('returns null when a character cannot be found', () => {
    expect(fuzzy('xyz', 'Dermatology')).toBeNull();
  });
});
