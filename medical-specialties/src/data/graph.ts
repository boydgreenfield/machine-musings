import { DOMAIN_BY_ID, DOMAINS, SYSTEM_BY_ID } from './domains';
import { RAW_NODES } from './nodes';
import type { Domain, DomainId, SpecialtyInput, SpecialtyNode, SystemId } from './types';

export const ROOT_ID = 'medicine';

export interface Edge {
  source: string;
  target: string;
  /** True for the home (tree) edge, false for a secondary cross-board pathway. */
  primary: boolean;
}

export interface Graph {
  nodes: SpecialtyNode[];
  byId: Map<string, SpecialtyNode>;
  edges: Edge[];
  /** Tree edges only (one per non-root node). */
  treeEdges: Edge[];
  /** Secondary pathways only. */
  crossEdges: Edge[];
  root: SpecialtyNode;
  domains: Domain[];
  stats: GraphStats;
}

export interface GraphStats {
  domains: number;
  specialties: number;
  subspecialties: number;
  crossPathways: number;
  abmsBoards: number;
  longestPathYears: number;
  longestPathId: string;
}

const ROOT_INPUT: SpecialtyInput = {
  id: ROOT_ID,
  name: 'Medicine',
  level: 'specialty', // overridden to 'root' below
  domain: 'internal',
  system: 'general',
  years: 0,
  description:
    'Four years of medical school lead to a single degree, after which every physician chooses one of roughly two dozen residencies. From those trunks grow the fellowships and subspecialties mapped here.',
};

/** Assert data invariants and throw a readable error listing every problem found. */
export function validateInputs(inputs: SpecialtyInput[]): string[] {
  const problems: string[] = [];
  const ids = new Set<string>();
  for (const n of inputs) {
    if (ids.has(n.id)) problems.push(`duplicate id: ${n.id}`);
    ids.add(n.id);
    if (!/^[a-z0-9-]+$/.test(n.id)) problems.push(`id is not kebab-case: ${n.id}`);
    if (!DOMAIN_BY_ID[n.domain]) problems.push(`${n.id}: unknown domain ${n.domain}`);
    if (n.system && !SYSTEM_BY_ID[n.system]) problems.push(`${n.id}: unknown system ${n.system}`);
    if (n.level === 'domain') {
      if (n.parents && n.parents.length) problems.push(`${n.id}: domain nodes must not declare parents`);
    } else if (!n.parents || n.parents.length === 0) {
      problems.push(`${n.id}: missing parents`);
    }
    if (n.description.trim().length < 40) problems.push(`${n.id}: description too short`);
    if (n.level !== 'domain' && n.years === undefined) problems.push(`${n.id}: missing years`);
  }
  for (const n of inputs) {
    for (const p of n.parents ?? []) {
      if (!ids.has(p)) problems.push(`${n.id}: unknown parent ${p}`);
      if (p === n.id) problems.push(`${n.id}: node is its own parent`);
    }
    if (new Set(n.parents ?? []).size !== (n.parents ?? []).length) problems.push(`${n.id}: duplicate parents`);
  }
  // Exactly one domain node per domain id.
  for (const d of DOMAINS) {
    const count = inputs.filter((n) => n.level === 'domain' && n.domain === d.id).length;
    if (count !== 1) problems.push(`domain ${d.id} has ${count} domain nodes (expected 1)`);
  }
  return problems;
}

export function buildGraph(inputs: SpecialtyInput[] = RAW_NODES): Graph {
  const problems = validateInputs(inputs);
  if (problems.length) {
    throw new Error(`Specialty data is invalid:\n  ${problems.join('\n  ')}`);
  }

  const byId = new Map<string, SpecialtyNode>();
  const root: SpecialtyNode = {
    ...ROOT_INPUT,
    level: 'root',
    parents: [],
    certification: 'other',
    years: 0,
    system: 'general',
    depth: 0,
    children: [],
    path: [ROOT_ID],
    totalYears: 0,
  };
  byId.set(ROOT_ID, root);

  for (const input of inputs) {
    const parents = input.level === 'domain' ? [ROOT_ID] : [...(input.parents ?? [])];
    byId.set(input.id, {
      ...input,
      parents,
      certification: input.certification ?? (input.level === 'domain' ? 'other' : 'fellowship'),
      years: input.years ?? 0,
      system: input.system ?? 'general',
      depth: -1,
      children: [],
      path: [],
      totalYears: 0,
    });
  }

  // Children (home edges only) and all edges.
  const edges: Edge[] = [];
  for (const node of byId.values()) {
    node.parents.forEach((p, i) => {
      const parent = byId.get(p);
      if (!parent) throw new Error(`${node.id}: unknown parent ${p}`);
      if (i === 0) parent.children.push(node.id);
      edges.push({ source: p, target: node.id, primary: i === 0 });
    });
  }
  for (const node of byId.values()) {
    node.children.sort((a, b) => byId.get(a)!.name.localeCompare(byId.get(b)!.name));
  }

  // Depth, path, cumulative years along the home path (iterative DFS from the root).
  const stack: string[] = [ROOT_ID];
  const seen = new Set<string>();
  while (stack.length) {
    const id = stack.pop()!;
    if (seen.has(id)) throw new Error(`cycle detected at ${id}`);
    seen.add(id);
    const node = byId.get(id)!;
    const parent = node.parents[0] ? byId.get(node.parents[0]) : undefined;
    node.depth = parent ? parent.depth + 1 : 0;
    node.path = parent ? [...parent.path, id] : [id];
    node.totalYears = parent ? parent.totalYears + node.years : 0;
    for (const c of node.children) stack.push(c);
  }
  for (const node of byId.values()) {
    if (!seen.has(node.id)) throw new Error(`${node.id} is not reachable from the root`);
  }

  const nodes = [...byId.values()];
  const treeEdges = edges.filter((e) => e.primary);
  const crossEdges = edges.filter((e) => !e.primary);
  const boards = new Set<string>();
  for (const n of nodes) {
    if (n.certification === 'abms-primary' && n.board && !n.board.includes('/') && !n.board.includes('(')) boards.add(n.board);
  }
  let longest = root;
  for (const n of nodes) if (n.totalYears > longest.totalYears) longest = n;

  const stats: GraphStats = {
    domains: nodes.filter((n) => n.level === 'domain').length,
    specialties: nodes.filter((n) => n.level === 'specialty').length,
    subspecialties: nodes.filter((n) => n.level === 'subspecialty').length,
    crossPathways: crossEdges.length,
    abmsBoards: boards.size,
    longestPathYears: longest.totalYears,
    longestPathId: longest.id,
  };

  return { nodes, byId, edges, treeEdges, crossEdges, root, domains: DOMAINS, stats };
}

/** Descendants of a node along home edges, including itself. */
export function subtree(graph: Graph, id: string): Set<string> {
  const out = new Set<string>();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop()!;
    if (out.has(cur)) continue;
    out.add(cur);
    const n = graph.byId.get(cur);
    if (n) stack.push(...n.children);
  }
  return out;
}

/** Ids directly connected to `id` by any edge (parents, children, cross-board). */
export function neighbours(graph: Graph, id: string): Set<string> {
  const out = new Set<string>();
  for (const e of graph.edges) {
    if (e.source === id) out.add(e.target);
    if (e.target === id) out.add(e.source);
  }
  return out;
}

export function domainOf(node: SpecialtyNode): Domain {
  return DOMAIN_BY_ID[node.domain]!;
}

export const DOMAIN_IDS: DomainId[] = DOMAINS.map((d) => d.id);
export type { SystemId };
