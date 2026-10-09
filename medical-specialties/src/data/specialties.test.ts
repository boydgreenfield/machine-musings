import { describe, expect, it } from 'vitest';
import { NODES, BOARDS } from './specialties';
import { CATEGORY_META, DOMAIN_META } from './schema';
import { buildGraph, graphStats } from '../graph';
import { computeLayout, LAYOUTS } from '../layouts/index';

describe('specialty dataset', () => {
  const ids = new Set(NODES.map((n) => n.id));

  it('has unique ids', () => {
    expect(ids.size).toBe(NODES.length);
  });

  it('references only known parents, via routes, boards, categories and domains', () => {
    const boardIds = new Set(BOARDS.map((b) => b.id));
    for (const n of NODES) {
      for (const p of n.parents) expect(ids.has(p), `${n.id} → parent ${p}`).toBe(true);
      for (const v of n.via ?? []) expect(ids.has(v), `${n.id} → via ${v}`).toBe(true);
      for (const b of n.boards) expect(boardIds.has(b), `${n.id} → board ${b}`).toBe(true);
      expect(CATEGORY_META[n.category], `${n.id} category`).toBeDefined();
      expect(DOMAIN_META[n.domain], `${n.id} domain`).toBeDefined();
      expect(n.description.length, `${n.id} description`).toBeGreaterThan(20);
    }
  });

  it('has exactly one root and all specialties attach to it', () => {
    const roots = NODES.filter((n) => n.tier === 'root');
    expect(roots).toHaveLength(1);
    for (const n of NODES) {
      if (n.tier === 'root') expect(n.parents).toHaveLength(0);
      else expect(n.parents.length).toBeGreaterThan(0);
    }
  });

  it('gives every specialty a residency length and every sub/fellowship a fellowship length', () => {
    for (const n of NODES) {
      if (n.tier === 'specialty' && n.parents.includes('medicine')) expect(n.residency, n.id).toBeGreaterThan(0);
      if (n.tier === 'subspecialty' || n.tier === 'fellowship') expect(typeof n.fellowship, n.id).toBe('number');
    }
  });

  it('marks fellowships as non-certified and subspecialties as certified', () => {
    for (const n of NODES) {
      if (n.tier === 'fellowship') { expect(n.credential).toBe('fellowship'); expect(n.boards).toHaveLength(0); }
      if (n.tier === 'subspecialty') { expect(n.credential).not.toBe('fellowship'); expect(n.boards.length).toBeGreaterThan(0); }
    }
  });

  it('lists all 24 ABMS boards and uses each at least once', () => {
    expect(BOARDS).toHaveLength(24);
    const used = new Set(NODES.flatMap((n) => n.boards));
    for (const b of BOARDS) expect(used.has(b.id), b.id).toBe(true);
  });
});

describe('graph', () => {
  const g = buildGraph();
  it('computes depth, bridges and training years', () => {
    expect(g.root.depth).toBe(0);
    expect(g.byId.get('cardiology')!.depth).toBe(2);
    expect(g.byId.get('interventional-cardiology')!.totalYears).toBe(7);
    expect(g.byId.get('neurological-surgery')!.totalYears).toBe(7);
    expect(g.byId.get('hospice-palliative')!.isBridge).toBe(true);
    expect(g.byId.get('hospice-palliative')!.parentNodes.length).toBeGreaterThanOrEqual(10);
    expect(g.byId.get('cardiology')!.isBridge).toBe(false);
    expect(g.byId.get('interventional-cardiology')!.rootSpecialty!.id).toBe('internal-medicine');
  });
  it('reports sensible stats', () => {
    const s = graphStats(g);
    expect(s.boards).toBe(24);
    expect(s.total).toBeGreaterThan(150);
    expect(s.bridges).toBeGreaterThan(20);
  });
  it('produces finite, bounded positions in every layout', () => {
    for (const l of LAYOUTS) {
      const { positions } = computeLayout(l.id, g);
      expect(positions.length).toBe(g.nodes.length * 3);
      let maxR = 0;
      for (let i = 0; i < positions.length; i += 3) {
        const [x, y, z] = [positions[i], positions[i + 1], positions[i + 2]];
        expect(Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(z), `${l.id} node ${i / 3}`).toBe(true);
        maxR = Math.max(maxR, Math.hypot(x, y, z));
      }
      expect(maxR, l.id).toBeLessThan(160);
    }
  });
  it('keeps nodes from overlapping too closely in every layout', () => {
    for (const l of LAYOUTS) {
      const { positions } = computeLayout(l.id, g);
      let worst = Infinity;
      for (let i = 0; i < g.nodes.length; i++) for (let j = i + 1; j < g.nodes.length; j++) {
        const d = Math.hypot(positions[i * 3] - positions[j * 3], positions[i * 3 + 1] - positions[j * 3 + 1], positions[i * 3 + 2] - positions[j * 3 + 2]);
        worst = Math.min(worst, d / (g.nodes[i].radius + g.nodes[j].radius));
      }
      expect(worst, `${l.id} closest pair (ratio of distance to summed radii)`).toBeGreaterThan(0.9);
    }
  });
});
