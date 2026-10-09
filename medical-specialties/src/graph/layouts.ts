import { Vector3 } from 'three';
import { DOMAINS, SYSTEMS } from '../data/domains';
import type { Graph } from '../data/graph';
import type { SpecialtyNode } from '../data/types';

export type LayoutId = 'constellation' | 'systems' | 'timeline' | 'sunburst';

export interface LayoutMeta {
  id: LayoutId;
  name: string;
  hint: string;
  /** Preferred camera position for this layout. */
  camera: [number, number, number];
  /** Whether organisational domain nodes are shown. */
  showDomains: boolean;
}

export const LAYOUTS: LayoutMeta[] = [
  {
    id: 'constellation',
    name: 'Constellation',
    hint: 'A three-dimensional tree: medicine at the centre, domains, specialties and fellowships radiating outward.',
    camera: [0, 100, 160],
    showDomains: true,
  },
  {
    id: 'systems',
    name: 'Organ systems',
    hint: 'A ring of clusters by the part of the body or kind of work each discipline serves; arches link fellowships to their home specialty.',
    camera: [0, 178, 92],
    showDomains: false,
  },
  {
    id: 'timeline',
    name: 'Training years',
    hint: 'Left to right by cumulative years of residency and fellowship after medical school.',
    camera: [6, 26, 132],
    showDomains: false,
  },
  {
    id: 'sunburst',
    name: 'Sunburst',
    hint: 'A flat radial tree, viewed from above.',
    camera: [0, 172, 22],
    showDomains: true,
  },
];

export const LAYOUT_BY_ID: Record<LayoutId, LayoutMeta> = Object.fromEntries(LAYOUTS.map((l) => [l.id, l])) as Record<
  LayoutId,
  LayoutMeta
>;

export type Positions = Map<string, Vector3>;

/** Ring radius per depth in the sunburst layout (exported for the ring guides). */
export const SUNBURST_RADII = [0, 17, 34, 49, 61, 70];

const DEG = Math.PI / 180;
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/** Leaf count of each node's home subtree (leaves count 1, internal nodes count their leaves). */
function leafWeights(graph: Graph): Map<string, number> {
  const w = new Map<string, number>();
  const visit = (id: string): number => {
    const n = graph.byId.get(id)!;
    if (!n.children.length) {
      w.set(id, 1);
      return 1;
    }
    let sum = 0;
    for (const c of n.children) sum += visit(c);
    // Internal nodes get a little extra so they keep room of their own.
    sum += 0.6;
    w.set(id, sum);
    return sum;
  };
  visit(graph.root.id);
  return w;
}

function spherical(r: number, lon: number, lat: number): Vector3 {
  const c = Math.cos(lat);
  return new Vector3(r * c * Math.sin(lon), r * Math.sin(lat), r * c * Math.cos(lon));
}

/** Deterministic pseudo-random in [0,1) from a string. */
function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 10007) / 10007;
}

interface AngularRect {
  lon0: number;
  lon1: number;
  lat0: number;
  lat1: number;
}

/**
 * Constellation: a "spherical treemap". Every node owns an angular rectangle of
 * the sky; children split it in proportion to their subtree size, alternating
 * between longitude and latitude so regions stay roughly square. Nodes sit at
 * the centre of their rectangle, at a radius determined by depth.
 */
export function constellationLayout(graph: Graph): Positions {
  const pos: Positions = new Map();
  const weights = leafWeights(graph);
  const radiusByDepth = [0, 33, 49, 61, 71, 79];
  const LAT_LIMIT = 42 * DEG;

  const place = (id: string, rect: AngularRect, depth: number) => {
    const node = graph.byId.get(id)!;
    const lon = (rect.lon0 + rect.lon1) / 2;
    const lat = (rect.lat0 + rect.lat1) / 2;
    const r = radiusByDepth[Math.min(depth, radiusByDepth.length - 1)]!;
    // Tiny deterministic radial jitter keeps rings from looking machine-stamped.
    const jitter = depth >= 2 ? (hash01(id) - 0.5) * 3.5 : 0;
    pos.set(id, depth === 0 ? new Vector3() : spherical(r + jitter, lon, lat));

    if (!node.children.length) return;
    const children = node.children;
    const total = children.reduce((s, c) => s + (weights.get(c) ?? 1), 0);
    const width = (rect.lon1 - rect.lon0) * Math.cos(lat);
    const height = rect.lat1 - rect.lat0;
    // Split along the longer side; for many children use a two-row grid so the
    // region fills evenly instead of forming a thin arc.
    const n = children.length;
    if (n >= 6 && height > 0.35) {
      const rows = n >= 14 ? 3 : 2;
      const perRow = Math.ceil(n / rows);
      const rowHeight = height / rows;
      for (let r2 = 0; r2 < rows; r2++) {
        const slice = children.slice(r2 * perRow, (r2 + 1) * perRow);
        const sliceTotal = slice.reduce((s, c) => s + (weights.get(c) ?? 1), 0);
        let cursor = rect.lon0;
        const lat0 = rect.lat1 - (r2 + 1) * rowHeight;
        const lat1 = rect.lat1 - r2 * rowHeight;
        for (const c of slice) {
          const frac = (weights.get(c) ?? 1) / sliceTotal;
          const span = (rect.lon1 - rect.lon0) * frac;
          place(c, { lon0: cursor, lon1: cursor + span, lat0, lat1 }, depth + 1);
          cursor += span;
        }
      }
      return;
    }
    const splitLon = width >= height;
    let cursor = splitLon ? rect.lon0 : rect.lat0;
    for (const c of children) {
      const frac = (weights.get(c) ?? 1) / total;
      if (splitLon) {
        const span = (rect.lon1 - rect.lon0) * frac;
        place(c, { lon0: cursor, lon1: cursor + span, lat0: rect.lat0, lat1: rect.lat1 }, depth + 1);
        cursor += span;
      } else {
        const span = (rect.lat1 - rect.lat0) * frac;
        place(c, { lon0: rect.lon0, lon1: rect.lon1, lat0: cursor, lat1: cursor + span }, depth + 1);
        cursor += span;
      }
    }
  };

  // Domains split the full longitude range; order them so that large families
  // alternate with small ones around the sphere.
  const root = graph.root;
  const domains = [...root.children];
  const total = domains.reduce((s, c) => s + (weights.get(c) ?? 1), 0);
  let cursor = -Math.PI;
  pos.set(root.id, new Vector3());
  for (const d of domains) {
    const span = (2 * Math.PI * (weights.get(d) ?? 1)) / total;
    place(d, { lon0: cursor, lon1: cursor + span, lat0: -LAT_LIMIT, lat1: LAT_LIMIT }, 1);
    cursor += span;
  }
  return pos;
}

/**
 * Sunburst: the same angular allocation flattened into the XZ plane, with the
 * depth mapped to ring radius and a gentle lift by depth for relief.
 */
export function sunburstLayout(graph: Graph): Positions {
  const pos: Positions = new Map();
  const weights = leafWeights(graph);
  const radiusByDepth = SUNBURST_RADII;
  const place = (id: string, a0: number, a1: number, depth: number) => {
    const node = graph.byId.get(id)!;
    const a = (a0 + a1) / 2;
    const r = radiusByDepth[Math.min(depth, radiusByDepth.length - 1)]!;
    pos.set(id, depth === 0 ? new Vector3() : new Vector3(r * Math.sin(a), 0, r * Math.cos(a)));
    const total = node.children.reduce((s, c) => s + (weights.get(c) ?? 1), 0);
    let cursor = a0;
    for (const c of node.children) {
      const span = ((a1 - a0) * (weights.get(c) ?? 1)) / total;
      place(c, cursor, cursor + span, depth + 1);
      cursor += span;
    }
  };
  place(graph.root.id, -Math.PI, Math.PI, 0);
  return pos;
}

export interface Cluster {
  id: string;
  name: string;
  centre: Vector3;
  radius: number;
  count: number;
}

const RING_RADIUS = 54;

/** Cluster centres for the organ-systems ring, in SYSTEMS order. */
export function systemClusters(graph: Graph): Cluster[] {
  const n = SYSTEMS.length;
  return SYSTEMS.map((s, i) => {
    const count = graph.nodes.filter((m) => m.system === s.id && m.level !== 'domain' && m.level !== 'root').length;
    const a = -Math.PI / 2 + (i / n) * Math.PI * 2;
    const y = i % 2 === 0 ? 6 : -6;
    return {
      id: s.id,
      name: s.name,
      centre: new Vector3(RING_RADIUS * Math.cos(a), y, RING_RADIUS * Math.sin(a)),
      radius: 1.8 + 2.1 * Math.sqrt(Math.max(1, count)),
      count,
    };
  });
}

/**
 * Organ systems: twenty clusters arranged on a ring in a meaningful order
 * (neighbours are related systems). Each cluster is a small ball of nodes with
 * specialties near its centre. Domain nodes collapse to a small inner ring and
 * are hidden; the root stays at the origin.
 */
export function systemsLayout(graph: Graph): Positions {
  const pos: Positions = new Map();
  pos.set(graph.root.id, new Vector3());
  const domainNodes = graph.nodes.filter((d) => d.level === 'domain');
  domainNodes.forEach((d, i) => {
    const a = (i / domainNodes.length) * Math.PI * 2;
    pos.set(d.id, new Vector3(Math.sin(a) * 6, 0, Math.cos(a) * 6));
  });

  for (const cluster of systemClusters(graph)) {
    const members = graph.nodes.filter((m) => m.system === cluster.id && m.level !== 'domain' && m.level !== 'root');
    members.sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name));
    const k = members.length;
    members.forEach((m, i) => {
      if (k === 1) {
        pos.set(m.id, cluster.centre.clone());
        return;
      }
      // Fibonacci ball: radius grows with the cube root so density is even.
      const r = (cluster.radius - 1) * Math.cbrt((i + 0.5) / k);
      const z = 1 - (2 * (i + 0.5)) / k;
      const rr = Math.sqrt(1 - z * z);
      const phi = i * GOLDEN_ANGLE;
      pos.set(m.id, new Vector3(rr * Math.cos(phi) * r, z * r * 0.8, rr * Math.sin(phi) * r).add(cluster.centre));
    });
  }
  return pos;
}

/**
 * Training years: x is cumulative years after medical school, y is the domain
 * lane, z spreads nodes that share a lane and a year.
 */
export function timelineLayout(graph: Graph): Positions {
  const pos: Positions = new Map();
  const maxYears = Math.max(...graph.nodes.map((n) => n.totalYears));
  const X0 = TIMELINE_X0;
  const X1 = TIMELINE_X1;
  const xOf = (years: number) => X0 + ((X1 - X0) * years) / maxYears;
  const laneY = new Map(timelineLanes().map((l) => [l.id, l.y]));
  const yOf = (domain: string) => laneY.get(domain) ?? 0;

  pos.set(graph.root.id, new Vector3(X0, 0, 0));
  for (const d of graph.nodes.filter((n) => n.level === 'domain')) {
    pos.set(d.id, new Vector3(X0 + 4, yOf(d.domain), 0));
  }

  // Bucket by (domain, totalYears) and spread each bucket along z.
  const buckets = new Map<string, SpecialtyNode[]>();
  for (const n of graph.nodes) {
    if (n.level === 'root' || n.level === 'domain') continue;
    const key = `${n.domain}|${n.totalYears}`;
    const list = buckets.get(key) ?? [];
    list.push(n);
    buckets.set(key, list);
  }
  for (const [key, list] of buckets) {
    list.sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name));
    const [domain, yearsStr] = key.split('|') as [string, string];
    const years = Number(yearsStr);
    const count = list.length;
    const cols = Math.min(count, 4);
    const rows = Math.ceil(count / cols);
    list.forEach((n, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const z = (col - (cols - 1) / 2) * 6;
      const y = yOf(domain) + (row - (rows - 1) / 2) * 2.6;
      const x = xOf(years) + (hash01(n.id) - 0.5) * 1.2;
      pos.set(n.id, new Vector3(x, y, z));
    });
  }
  return pos;
}

export function computeLayout(graph: Graph, id: LayoutId): Positions {
  switch (id) {
    case 'constellation':
      return constellationLayout(graph);
    case 'systems':
      return systemsLayout(graph);
    case 'timeline':
      return timelineLayout(graph);
    case 'sunburst':
      return sunburstLayout(graph);
  }
}

export const TIMELINE_X0 = -56;
export const TIMELINE_X1 = 58;

export interface Lane {
  id: string;
  name: string;
  color: string;
  y: number;
}

/** Domain lanes for the timeline axis. */
export function timelineLanes(): Lane[] {
  const laneGap = 8.4;
  return DOMAINS.map((d, i) => ({ id: d.id, name: d.name, color: d.color, y: ((DOMAINS.length - 1) / 2 - i) * laneGap }));
}

/** Year ticks for the timeline axis, in world coordinates. */
export function timelineTicks(graph: Graph): { years: number; x: number }[] {
  const maxYears = Math.max(...graph.nodes.map((n) => n.totalYears));
  const X0 = TIMELINE_X0;
  const X1 = TIMELINE_X1;
  const out: { years: number; x: number }[] = [];
  for (let y = 0; y <= maxYears; y++) out.push({ years: y, x: X0 + ((X1 - X0) * y) / maxYears });
  return out;
}
