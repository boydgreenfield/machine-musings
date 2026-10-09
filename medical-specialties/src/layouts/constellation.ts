import { forceSimulation, forceLink, forceManyBody, forceCollide, forceRadial, forceCenter, type SimNode } from 'd3-force-3d';
import type { Graph, GraphNode } from '../graph';
import { mulberry32 } from '../util/prng';
import type { LayoutResult } from './index';

interface SNode extends SimNode { node: GraphNode }
interface SLink { source: SNode; target: SNode; kind: 'tree' | 'via' }

/**
 * Force-directed layout seeded from the orbit layout so it is stable and
 * deterministic. Runs synchronously (≈190 nodes, 300 ticks: a few ms).
 */
export function constellationLayout(g: Graph, seed: Float32Array): LayoutResult {
  const snodes: SNode[] = g.nodes.map((node) => ({
    node,
    x: seed[node.index * 3] * 0.9,
    y: seed[node.index * 3 + 1] * 0.9,
    z: seed[node.index * 3 + 2] * 0.9,
  }));
  const root = snodes[g.root.index];
  root.fx = 0; root.fy = 0; root.fz = 0;

  const links: SLink[] = g.edges.map((e) => ({ source: snodes[e.source.index], target: snodes[e.target.index], kind: e.kind }));

  const rand = mulberry32(42);
  const sim = forceSimulation<SNode>(snodes, 3)
    .randomSource(rand)
    .alphaDecay(0.02)
    .velocityDecay(0.35)
    .force('link', forceLink<SNode, SLink>(links)
      .distance((l) => {
        if (l.kind === 'via') return 40;
        const s = l.source.node, t = l.target.node;
        if (s.depth === 0) return 62;
        if (t.isBridge) return 24;
        if (s.depth === 1) return 11 + t.radius * 3;
        return 7 + t.radius * 2;
      })
      .strength((l) => (l.kind === 'via' ? 0.05 : l.target.node.isBridge ? 0.35 : 0.9)))
    .force('charge', forceManyBody<SNode>().strength((d) => (d.node.depth === 1 ? -140 : -14)).distanceMax(70))
    .force('collide', forceCollide<SNode>((d) => d.node.radius * 1.7 + 1.2).strength(0.9).iterations(2))
    .force('radial', forceRadial<SNode>((d) => (d.node.depth === 1 ? 60 : d.node.isBridge ? 42 : 74)).strength((d) => (d.node.depth === 1 ? 0.7 : 0.18)))
    .force('center', forceCenter<SNode>(0, 0, 0).strength(0.02))
    .stop();

  sim.tick(320);

  const p = new Float32Array(g.nodes.length * 3);
  for (const s of snodes) {
    p[s.node.index * 3] = s.x;
    p[s.node.index * 3 + 1] = s.y;
    p[s.node.index * 3 + 2] = s.z;
  }
  return { positions: p };
}
