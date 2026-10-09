import { NODES, BOARDS } from './data/specialties';
import { CATEGORY_META, DOMAIN_META, type Category, type Domain, type NodeDef, type Tier } from './data/schema';

export interface GraphNode extends NodeDef {
  index: number;
  depth: number;
  parentNodes: GraphNode[];
  children: GraphNode[];
  /** True when the node builds on more than one specialty (a multi-board "bridge"). */
  isBridge: boolean;
  /** Primary ancestor specialty (first parent chain up to depth 1). */
  rootSpecialty: GraphNode | null;
  /** Total years of post-graduate training along the shortest path from medical school. */
  totalYears: number;
  /** Visual radius in world units. */
  radius: number;
  /** All ancestors (transitive parents). */
  ancestors: Set<GraphNode>;
}

export interface GraphEdge {
  source: GraphNode;
  target: GraphNode;
  /** 'tree' = training lineage, 'via' = secondary training route. */
  kind: 'tree' | 'via';
  index: number;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  byId: Map<string, GraphNode>;
  specialties: GraphNode[];
  root: GraphNode;
  categories: Category[];
  domains: Domain[];
}

const CATEGORY_ORDER: Category[] = ['primary', 'medical', 'surgical', 'acute', 'diagnostic', 'mind', 'population'];
const DOMAIN_ORDER = Object.keys(DOMAIN_META) as Domain[];

function radiusFor(def: NodeDef): number {
  if (def.tier === 'root') return 3.2;
  const wf = def.workforce ?? 300;
  // log scale – 300 physicians -> ~0.55, 120k -> ~1.9
  const r = 0.42 + 0.66 * Math.log10(wf / 100);
  return Math.max(0.5, Math.min(2.6, r));
}

export function buildGraph(defs: NodeDef[] = NODES): Graph {
  const byId = new Map<string, GraphNode>();
  const nodes: GraphNode[] = defs.map((def, index) => ({
    ...def,
    index,
    depth: 0,
    parentNodes: [],
    children: [],
    isBridge: false,
    rootSpecialty: null,
    totalYears: 0,
    radius: radiusFor(def),
    ancestors: new Set(),
  }));
  for (const n of nodes) {
    if (byId.has(n.id)) throw new Error(`Duplicate node id: ${n.id}`);
    byId.set(n.id, n);
  }
  for (const n of nodes) {
    for (const pid of n.parents) {
      const p = byId.get(pid);
      if (!p) throw new Error(`Unknown parent "${pid}" for node "${n.id}"`);
      n.parentNodes.push(p);
      p.children.push(n);
    }
  }
  // Depth, total years, ancestors — nodes are declared parents-first, but be
  // safe and iterate until stable.
  const root = byId.get('medicine');
  if (!root) throw new Error('Root node "medicine" missing');
  let changed = true;
  let guard = 0;
  while (changed && guard++ < 50) {
    changed = false;
    for (const n of nodes) {
      if (n.parentNodes.length === 0) continue;
      const depth = Math.min(...n.parentNodes.map((p) => p.depth)) + 1;
      const years = Math.min(...n.parentNodes.map((p) => p.totalYears)) + (n.tier === 'specialty' && n.residency ? n.residency : (n.fellowship ?? 0));
      if (depth !== n.depth || years !== n.totalYears) {
        n.depth = depth;
        n.totalYears = years;
        changed = true;
      }
    }
  }
  for (const n of nodes) {
    const stack = [...n.parentNodes];
    while (stack.length) {
      const p = stack.pop()!;
      if (n.ancestors.has(p)) continue;
      n.ancestors.add(p);
      stack.push(...p.parentNodes);
    }
    n.isBridge = n.parentNodes.filter((p) => p.tier !== 'root').length > 1;
    let r: GraphNode | null = n;
    while (r && r.depth > 1) r = r.parentNodes[0] ?? null;
    n.rootSpecialty = r && r.depth === 1 ? r : null;
  }

  const edges: GraphEdge[] = [];
  for (const n of nodes) {
    for (const p of n.parentNodes) edges.push({ source: p, target: n, kind: 'tree', index: edges.length });
    for (const vid of n.via ?? []) {
      const v = byId.get(vid);
      if (!v) throw new Error(`Unknown via "${vid}" for node "${n.id}"`);
      edges.push({ source: v, target: n, kind: 'via', index: edges.length });
    }
  }

  const specialties = nodes.filter((n) => n.depth === 1);
  return { nodes, edges, byId, specialties, root, categories: CATEGORY_ORDER, domains: DOMAIN_ORDER };
}

export function categoryColor(c: Category): string {
  return CATEGORY_META[c].color;
}
export function domainColor(d: Domain): string {
  return DOMAIN_META[d].color;
}

export function tierLabel(t: Tier): string {
  switch (t) {
    case 'root': return 'Root';
    case 'specialty': return 'Specialty';
    case 'subspecialty': return 'Subspecialty';
    case 'fellowship': return 'Fellowship';
  }
}

export function boardName(id: string): string {
  return BOARDS.find((b) => b.id === id)?.name ?? id;
}

/** Summary statistics for the header. */
export function graphStats(g: Graph) {
  const specialties = g.nodes.filter((n) => n.tier === 'specialty').length;
  const subspecialties = g.nodes.filter((n) => n.tier === 'subspecialty').length;
  const fellowships = g.nodes.filter((n) => n.tier === 'fellowship').length;
  const bridges = g.nodes.filter((n) => n.isBridge).length;
  return { specialties, subspecialties, fellowships, bridges, boards: BOARDS.length, total: g.nodes.length - 1 };
}
