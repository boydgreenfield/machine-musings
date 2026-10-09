import { describe, expect, it } from 'vitest';
import { buildGraph, neighbours, subtree, validateInputs } from '../src/data/graph';
import { RAW_NODES } from '../src/data/nodes';
import { DOMAINS, SYSTEMS } from '../src/data/domains';

const graph = buildGraph();

describe('specialty dataset', () => {
  it('passes structural validation', () => {
    expect(validateInputs(RAW_NODES)).toEqual([]);
  });

  it('has a root, nine domains and a substantial tree', () => {
    expect(graph.root.id).toBe('medicine');
    expect(graph.stats.domains).toBe(DOMAINS.length);
    expect(graph.stats.specialties).toBeGreaterThanOrEqual(30);
    expect(graph.stats.subspecialties).toBeGreaterThanOrEqual(130);
  });

  it('assigns every node a depth, path and cumulative years', () => {
    for (const n of graph.nodes) {
      expect(n.depth, n.id).toBeGreaterThanOrEqual(0);
      expect(n.path[0], n.id).toBe('medicine');
      expect(n.path[n.path.length - 1], n.id).toBe(n.id);
      expect(n.totalYears, n.id).toBeGreaterThanOrEqual(n.years);
    }
  });

  it('uses only known organ systems', () => {
    const ids = new Set(SYSTEMS.map((s) => s.id));
    for (const n of graph.nodes) expect(ids.has(n.system), `${n.id} -> ${n.system}`).toBe(true);
  });

  it('places every domain node directly under the root', () => {
    for (const n of graph.nodes.filter((n) => n.level === 'domain')) {
      expect(n.parents).toEqual(['medicine']);
      expect(n.depth).toBe(1);
    }
  });

  it('keeps specialties under a domain or another specialty', () => {
    for (const n of graph.nodes.filter((n) => n.level === 'specialty')) {
      const home = graph.byId.get(n.parents[0]!)!;
      expect(['domain', 'specialty'].includes(home.level), `${n.id} under ${home.id}`).toBe(true);
    }
  });

  it('keeps subspecialties under specialties or subspecialties', () => {
    for (const n of graph.nodes.filter((n) => n.level === 'subspecialty')) {
      const home = graph.byId.get(n.parents[0]!)!;
      expect(['specialty', 'subspecialty'].includes(home.level), `${n.id} under ${home.id}`).toBe(true);
    }
  });

  it('records the multi-board subspecialties as cross-board pathways', () => {
    const hpm = graph.byId.get('hospice-palliative-medicine')!;
    expect(hpm.parents.length).toBeGreaterThanOrEqual(10);
    const sleep = graph.byId.get('sleep-medicine')!;
    expect(sleep.parents).toContain('otolaryngology');
    expect(graph.crossEdges.length).toBeGreaterThan(40);
  });

  it('computes sensible training totals', () => {
    expect(graph.byId.get('internal-medicine')!.totalYears).toBe(3);
    expect(graph.byId.get('interventional-cardiology')!.totalYears).toBe(7);
    expect(graph.byId.get('neurological-surgery')!.totalYears).toBe(7);
    expect(graph.byId.get('colon-rectal-surgery')!.totalYears).toBe(6);
    expect(graph.stats.longestPathYears).toBeGreaterThanOrEqual(9);
  });

  it('exposes subtree and neighbour helpers', () => {
    const cardio = subtree(graph, 'cardiovascular-disease');
    expect(cardio.has('interventional-cardiology')).toBe(true);
    expect(cardio.has('nephrology')).toBe(false);
    const n = neighbours(graph, 'sleep-medicine');
    expect(n.has('internal-medicine')).toBe(true);
    expect(n.has('neurology')).toBe(true);
  });

  it('has unique, human-readable names', () => {
    const names = new Map<string, string>();
    for (const n of graph.nodes) {
      if (n.level === 'domain') continue; // domain nodes may share a name with their specialty (Pediatrics)
      const key = n.name.toLowerCase();
      expect(names.has(key), `${n.id} duplicates ${names.get(key)}`).toBe(false);
      names.set(key, n.id);
    }
  });
});
