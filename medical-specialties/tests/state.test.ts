import { describe, expect, it } from 'vitest';
import { buildGraph } from '../src/data/graph';
import { Store } from '../src/state';
import { fuzzyScore } from '../src/util/fuzzy';

describe('fuzzy search', () => {
  it('ranks exact, prefix, word and subsequence matches in that order', () => {
    const exact = fuzzyScore('neurology', 'Neurology');
    const prefix = fuzzyScore('neuro', 'Neurology');
    const word = fuzzyScore('neuro', 'Child Neurology');
    const sub = fuzzyScore('nrlgy', 'Neurology');
    expect(exact).toBeGreaterThan(prefix);
    expect(prefix).toBeGreaterThan(word);
    expect(word).toBeGreaterThan(sub);
    expect(sub).toBeGreaterThan(0);
    expect(fuzzyScore('xyz', 'Neurology')).toBe(-1);
  });
});

describe('store', () => {
  const graph = buildGraph();

  it('finds nodes by name, abbreviation and alias', () => {
    const store = new Store(graph);
    store.setQuery('ENT');
    expect(store.state.matchList[0]).toBe('otolaryngology');
    store.setQuery('cardio');
    expect(store.state.matchList).toContain('cardiovascular-disease');
    store.setQuery('physiatry');
    expect(store.state.matchList[0]).toBe('physical-medicine-rehabilitation');
    store.setQuery('');
    expect(store.state.matches).toBeNull();
  });

  it('dims everything outside the selected subtree, path and neighbours', () => {
    const store = new Store(graph);
    store.select('cardiovascular-disease');
    const v = store.computeVisuals();
    expect(v.get('cardiovascular-disease')!.emphasis).toBeGreaterThan(1);
    expect(v.get('interventional-cardiology')!.emphasis).toBeGreaterThanOrEqual(1);
    expect(v.get('internal-medicine')!.emphasis).toBeGreaterThanOrEqual(1);
    expect(v.get('pediatric-cardiology')!.emphasis).toBeLessThan(0.3); // two hops away via ACHD
    expect(v.get('dermatology')!.emphasis).toBeLessThan(0.3);
  });

  it('hides domains that are filtered out and domain nodes in flat layouts', () => {
    const store = new Store(graph);
    store.soloDomain('surgery');
    let v = store.computeVisuals();
    expect(v.get('general-surgery')!.visible).toBe(true);
    expect(v.get('internal-medicine')!.visible).toBe(false);
    expect(v.get('medicine')!.visible).toBe(true);
    store.soloDomain('surgery'); // toggles back to all
    expect(store.state.hiddenDomains.size).toBe(0);
    store.setLayout('timeline');
    v = store.computeVisuals();
    expect(v.get('surgery')!.visible).toBe(false);
    expect(v.get('general-surgery')!.visible).toBe(true);
  });

  it('pins labels of search matches and emits events', () => {
    const store = new Store(graph);
    let events = 0;
    store.on('change', () => events++);
    store.setQuery('sleep');
    const v = store.computeVisuals();
    expect(v.get('sleep-medicine')!.labelPinned).toBe(true);
    expect(v.get('urology')!.emphasis).toBeLessThan(0.3);
    expect(events).toBe(1);
  });
});
