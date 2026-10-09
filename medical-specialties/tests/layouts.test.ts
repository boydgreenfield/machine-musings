import { describe, expect, it } from 'vitest';
import { buildGraph } from '../src/data/graph';
import { computeLayout, LAYOUTS } from '../src/graph/layouts';

const graph = buildGraph();

describe('layouts', () => {
  for (const layout of LAYOUTS) {
    it(`${layout.id} positions every node with finite coordinates`, () => {
      const pos = computeLayout(graph, layout.id);
      for (const n of graph.nodes) {
        const p = pos.get(n.id);
        expect(p, n.id).toBeDefined();
        expect(Number.isFinite(p!.x) && Number.isFinite(p!.y) && Number.isFinite(p!.z), n.id).toBe(true);
        expect(p!.length(), `${layout.id}:${n.id}`).toBeLessThan(120);
      }
    });

    it(`${layout.id} keeps visible nodes apart`, () => {
      const pos = computeLayout(graph, layout.id);
      const visible = graph.nodes.filter((n) => layout.showDomains || (n.level !== 'domain'));
      let tooClose = 0;
      for (let i = 0; i < visible.length; i++) {
        for (let j = i + 1; j < visible.length; j++) {
          const d = pos.get(visible[i]!.id)!.distanceTo(pos.get(visible[j]!.id)!);
          if (d < 1.2) tooClose++;
        }
      }
      expect(tooClose, 'pairs closer than 1.2 units').toBeLessThanOrEqual(2);
    });
  }

  it('orders the timeline by cumulative years', () => {
    const pos = computeLayout(graph, 'timeline');
    const im = pos.get('internal-medicine')!;
    const cardio = pos.get('cardiovascular-disease')!;
    const ic = pos.get('interventional-cardiology')!;
    expect(im.x).toBeLessThan(cardio.x);
    expect(cardio.x).toBeLessThan(ic.x);
  });
});
