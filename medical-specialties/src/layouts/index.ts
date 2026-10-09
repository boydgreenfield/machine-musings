import type { Graph, GraphNode } from '../graph';
import { DOMAIN_META, type Category, type Domain } from '../data/schema';
import { hash01, mulberry32 } from '../util/prng';
import { constellationLayout } from './constellation';

export type LayoutId = 'orbits' | 'constellation' | 'tower' | 'systems';

export interface LayoutMeta {
  id: LayoutId;
  label: string;
  blurb: string;
  /** Default camera position & target for this layout. */
  camera: { position: [number, number, number]; target: [number, number, number] };
}

export const LAYOUTS: LayoutMeta[] = [
  { id: 'orbits', label: 'Orbits', blurb: 'Specialties circle the core of medicine; their subspecialties orbit them. Multi-board fields drift toward the centre, between the specialties they bridge.', camera: { position: [0, 118, 182], target: [0, -4, 0] } },
  { id: 'constellation', label: 'Constellation', blurb: 'A physics simulation pulls related fields together and pushes unrelated ones apart, revealing the natural neighbourhoods of medicine.', camera: { position: [0, 95, 215], target: [0, 0, 0] } },
  { id: 'tower', label: 'Training tower', blurb: 'Height is years of training after medical school. Residencies form the lower floors; each fellowship climbs one more.', camera: { position: [150, 42, 150], target: [0, 16, 0] } },
  { id: 'systems', label: 'Body systems', blurb: 'The same fields regrouped by the organ system or domain they serve, cutting across board lines.', camera: { position: [0, 215, 150], target: [0, 0, 0] } },
];

export interface LayoutResult {
  positions: Float32Array; // n * 3
  /** Optional guide rings for the training tower: y, label. */
  guides?: { y: number; label: string }[];
  /** Optional floating captions (e.g. cluster names in the body-systems view). */
  markers?: { position: [number, number, number]; label: string; color: string }[];
  /** Multiplier for lineage-edge brightness in this layout (default 1). */
  edgeOpacity?: number;
  /** Colour lens that makes this layout most legible. */
  lens?: 'category' | 'domain' | 'training' | 'workforce' | 'credential';
}

const TAU = Math.PI * 2;

/** Angular sector assignment for the ring of specialties, grouped by category. */
export function specialtyAngles(g: Graph): Map<GraphNode, number> {
  const groups = new Map<Category, GraphNode[]>();
  for (const c of g.categories) groups.set(c, []);
  for (const s of g.specialties) groups.get(s.category)!.push(s);
  for (const arr of groups.values()) arr.sort((a, b) => (b.workforce ?? 0) - (a.workforce ?? 0));
  // Interleave large/small so each sector reads as a smooth arc.
  const total = g.specialties.length;
  const gap = 0.16; // radians between categories
  const usable = TAU - gap * g.categories.length;
  const angles = new Map<GraphNode, number>();
  // Start so that the primary-care sector is centred at the front (+z faces the default camera).
  const firstWidth = (groups.get(g.categories[0])!.length / total) * usable;
  let theta = Math.PI / 2 - firstWidth / 2;
  for (const c of g.categories) {
    const arr = groups.get(c)!;
    const width = (arr.length / total) * usable;
    const step = width / arr.length;
    // Put the largest in the middle of the sector, then alternate outward.
    const ordered: GraphNode[] = [];
    arr.forEach((n, i) => (i % 2 === 0 ? ordered.push(n) : ordered.unshift(n)));
    ordered.forEach((n, i) => angles.set(n, theta + step * (i + 0.5)));
    theta += width + gap;
  }
  return angles;
}

export function categorySectors(g: Graph): Map<Category, { start: number; end: number }> {
  const angles = specialtyAngles(g);
  const out = new Map<Category, { start: number; end: number }>();
  for (const c of g.categories) {
    const a = g.specialties.filter((s) => s.category === c).map((s) => angles.get(s)!);
    out.set(c, { start: Math.min(...a), end: Math.max(...a) });
  }
  return out;
}

function set(p: Float32Array, i: number, x: number, y: number, z: number) {
  p[i * 3] = x; p[i * 3 + 1] = y; p[i * 3 + 2] = z;
}
function get(p: Float32Array, i: number): [number, number, number] {
  return [p[i * 3], p[i * 3 + 1], p[i * 3 + 2]];
}

/**
 * Simple iterative collision relaxation. `anchor[i]` in [0,1] is how strongly a
 * node resists being moved (1 = fixed). `lockY` keeps the vertical coordinate.
 */
export function relax(g: Graph, p: Float32Array, anchor: Float32Array, iterations = 80, lockY = false, pad = 1.6) {
  const n = g.nodes.length;
  for (let it = 0; it < iterations; it++) {
    for (let i = 0; i < n; i++) {
      const ri = g.nodes[i].radius;
      for (let j = i + 1; j < n; j++) {
        const rj = g.nodes[j].radius;
        const minD = (ri + rj) * pad + 1.2;
        let dx = p[j * 3] - p[i * 3];
        let dy = lockY ? 0 : p[j * 3 + 1] - p[i * 3 + 1];
        let dz = p[j * 3 + 2] - p[i * 3 + 2];
        let d = Math.hypot(dx, dy, dz);
        if (d >= minD) continue;
        if (d < 1e-4) { dx = hash01(g.nodes[i].id + j) - 0.5; dy = lockY ? 0 : hash01(g.nodes[j].id + i) - 0.5; dz = 0.37; d = Math.hypot(dx, dy, dz); }
        const push = (minD - d) / d * 0.5;
        const wi = 1 - anchor[i];
        const wj = 1 - anchor[j];
        const ws = wi + wj || 1;
        p[i * 3] -= dx * push * (wi / ws) * 2; p[i * 3 + 1] -= dy * push * (wi / ws) * 2; p[i * 3 + 2] -= dz * push * (wi / ws) * 2;
        p[j * 3] += dx * push * (wj / ws) * 2; p[j * 3 + 1] += dy * push * (wj / ws) * 2; p[j * 3 + 2] += dz * push * (wj / ws) * 2;
      }
    }
  }
}

/** Mean of parents' positions, pulled toward the origin for many-parent bridges. */
function bridgePosition(n: GraphNode, p: Float32Array, pull: number): [number, number, number] {
  const ps = n.parentNodes;
  let x = 0, y = 0, z = 0;
  for (const q of ps) { const [a, b, c] = get(p, q.index); x += a; y += b; z += c; }
  x /= ps.length; y /= ps.length; z /= ps.length;
  const k = Math.pow(pull, Math.max(0, ps.length - 2));
  const h = hash01(n.id);
  let bx = x * k, by = y * k + (h - 0.5) * 14, bz = z * k;
  // Keep many-parent bridges off the root itself: an inner ring around the core.
  const r = Math.hypot(bx, bz);
  const minR = 17 + h * 8;
  if (r < minR) {
    const a = r < 1e-3 ? h * Math.PI * 2 : Math.atan2(bz, bx);
    bx = minR * Math.cos(a); bz = minR * Math.sin(a);
  }
  return [bx, by, bz];
}

// ─────────────────────────────────────────────────────────────────────────────
// ORBITS
// ─────────────────────────────────────────────────────────────────────────────

export function orbitsLayout(g: Graph): LayoutResult {
  const n = g.nodes.length;
  const p = new Float32Array(n * 3);
  const anchor = new Float32Array(n).fill(0.1);
  const R1 = 78;
  const angles = specialtyAngles(g);

  set(p, g.root.index, 0, 0, 0);
  anchor[g.root.index] = 1;

  for (const s of g.specialties) {
    const th = angles.get(s)!;
    const y = 9 * Math.sin(3 * th + 0.7);
    set(p, s.index, R1 * Math.cos(th), y, R1 * Math.sin(th));
    anchor[s.index] = 1;
  }

  // Children of specialties (single-parent), on a tilted ring around the parent.
  const placeRing = (parent: GraphNode, kids: GraphNode[], baseRadius: number, outwardBias: boolean) => {
    const k = kids.length;
    if (!k) return;
    const [px, py, pz] = get(p, parent.index);
    const th = Math.atan2(pz, px);
    const rr = Math.max(baseRadius, (k * 4.6) / TAU);
    // Basis: tangent (around the big ring) and up, tilted about the tangent axis.
    const tx = -Math.sin(th), tz = Math.cos(th);
    const tilt = 0.45 + (hash01(parent.id) - 0.5) * 0.5;
    const ux = Math.cos(th) * Math.sin(tilt), uy = Math.cos(tilt), uz = Math.sin(th) * Math.sin(tilt);
    kids.sort((a, b) => (b.workforce ?? 0) - (a.workforce ?? 0));
    kids.forEach((c, i) => {
      // Alternate so the largest sit at the top and the rest fan down both sides.
      const idx = i % 2 === 0 ? i / 2 : k - (i + 1) / 2;
      let phi = (idx / k) * TAU + Math.PI / 2;
      if (outwardBias) phi = Math.PI / 2 + (idx / Math.max(1, k - 1) - 0.5) * Math.PI * 1.1;
      const cx = px + rr * (Math.cos(phi) * tx + Math.sin(phi) * ux);
      const cy = py + rr * (Math.sin(phi) * uy);
      const cz = pz + rr * (Math.cos(phi) * tz + Math.sin(phi) * uz);
      set(p, c.index, cx, cy, cz);
      anchor[c.index] = 0.6;
    });
  };

  for (const s of g.specialties) {
    const kids = s.children.filter((c) => !c.isBridge && c.parentNodes.length === 1);
    placeRing(s, kids, 8, false);
  }
  // Depth-3 (single parent subs of subs), small outward arc.
  for (const s of g.specialties) {
    for (const c of s.children) {
      const grand = c.children.filter((x) => !x.isBridge && x.parentNodes.length === 1);
      if (!grand.length) continue;
      const [cx, cy, cz] = get(p, c.index);
      const [sx, sy, sz] = get(p, s.index);
      const dx = cx - sx, dy = cy - sy, dz = cz - sz;
      const d = Math.hypot(dx, dy, dz) || 1;
      const k = grand.length;
      grand.forEach((x, i) => {
        const spread = (i - (k - 1) / 2) * 0.55;
        // Direction continuing outward from the specialty, rotated in the plane.
        const ox = dx / d, oy = dy / d, oz = dz / d;
        const px2 = -oz, pz2 = ox; // perpendicular in xz
        const rr = 4.2;
        set(p, x.index, cx + rr * (ox * Math.cos(spread) + px2 * Math.sin(spread)), cy + rr * oy * Math.cos(spread) + spread * 1.5, cz + rr * (oz * Math.cos(spread) + pz2 * Math.sin(spread)));
        anchor[x.index] = 0.3;
      });
    }
  }
  // Bridges: centroid of parents (parents may themselves be unplaced bridges – iterate by depth).
  const bridges = g.nodes.filter((x) => x.isBridge).sort((a, b) => a.depth - b.depth);
  for (const b of bridges) {
    const [x, y, z] = bridgePosition(b, p, 0.82);
    set(p, b.index, x, y, z);
    anchor[b.index] = 0.05;
  }
  relax(g, p, anchor, 90);
  return { positions: p };
}

// ─────────────────────────────────────────────────────────────────────────────
// TRAINING TOWER
// ─────────────────────────────────────────────────────────────────────────────

export function towerLayout(g: Graph): LayoutResult {
  const n = g.nodes.length;
  const p = new Float32Array(n * 3);
  const anchor = new Float32Array(n).fill(0.2);
  const angles = specialtyAngles(g);
  const Y0 = -40;
  const PER_YEAR = 17;
  const yFor = (years: number) => Y0 + (years - 3) * PER_YEAR;

  set(p, g.root.index, 0, Y0 - 24, 0);
  anchor[g.root.index] = 1;

  const thetaOf = new Map<GraphNode, number>();
  for (const s of g.specialties) thetaOf.set(s, angles.get(s)!);

  const circularMean = (ths: number[]) => Math.atan2(ths.reduce((a, t) => a + Math.sin(t), 0), ths.reduce((a, t) => a + Math.cos(t), 0));

  const ordered = [...g.nodes].filter((x) => x !== g.root).sort((a, b) => a.depth - b.depth);
  for (const node of ordered) {
    let th: number;
    if (node.depth === 1) th = thetaOf.get(node)!;
    else {
      const ths = node.parentNodes.map((q) => thetaOf.get(q) ?? 0);
      th = circularMean(ths) + (hash01(node.id) - 0.5) * (node.isBridge ? 0.08 : 0.22);
    }
    thetaOf.set(node, th);
    const r = node.depth === 1 ? 36 : node.tier === 'fellowship' ? 58 : 48 + (hash01(node.id + 'r') - 0.5) * 6;
    set(p, node.index, r * Math.cos(th), yFor(node.totalYears), r * Math.sin(th));
    anchor[node.index] = node.depth === 1 ? 0.9 : 0.15;
  }
  relax(g, p, anchor, 120, true, 1.35);
  const maxYears = Math.max(...g.nodes.map((x) => x.totalYears));
  const guides: { y: number; label: string }[] = [];
  for (let y = 3; y <= maxYears; y++) guides.push({ y: yFor(y), label: `${y} yrs` });
  return { positions: p, guides, lens: 'training' };
}

// ─────────────────────────────────────────────────────────────────────────────
// BODY SYSTEMS
// ─────────────────────────────────────────────────────────────────────────────

export function systemsLayout(g: Graph): LayoutResult {
  const n = g.nodes.length;
  const p = new Float32Array(n * 3);
  const anchor = new Float32Array(n).fill(0.4);
  const domains = Object.keys(DOMAIN_META) as Domain[];
  const groups = new Map<Domain, GraphNode[]>();
  for (const d of domains) groups.set(d, []);
  for (const node of g.nodes) if (node !== g.root) groups.get(node.domain)!.push(node);

  set(p, g.root.index, 0, 0, 0);
  anchor[g.root.index] = 1;

  // Cluster centres on one wide ring, alternating height so neighbours don't merge.
  const R = 92;
  const markers: NonNullable<LayoutResult['markers']> = [];
  domains.forEach((d, i) => {
    const members = groups.get(d)!;
    const th = (i / domains.length) * TAU + Math.PI / 2;
    const cy = (i % 2 === 0 ? 13 : -13) + 5 * Math.sin(3 * th);
    const cx = R * Math.cos(th), cz = R * Math.sin(th);
    const shellR = 3 + 2.3 * Math.sqrt(members.length);
    markers.push({ position: [cx * 1.04, cy + shellR * 1.1 + 4, cz * 1.04], label: DOMAIN_META[d].label, color: DOMAIN_META[d].color });
    members.sort((a, b) => (b.workforce ?? 0) - (a.workforce ?? 0));
    const k = members.length;
    const shell = 3 + 2.3 * Math.sqrt(k);
    const rnd = mulberry32(i * 7919 + 13);
    members.forEach((m, j) => {
      if (j === 0) { set(p, m.index, cx, cy, cz); anchor[m.index] = 0.95; return; }
      // Fibonacci sphere shell
      const t = (j - 0.5) / Math.max(1, k - 1);
      const phi = Math.acos(1 - 2 * t);
      const theta = Math.PI * (1 + Math.sqrt(5)) * j + rnd() * 0.1;
      const rr = shell * (0.75 + 0.5 * rnd());
      set(p, m.index, cx + rr * Math.sin(phi) * Math.cos(theta), cy + rr * Math.cos(phi), cz + rr * Math.sin(phi) * Math.sin(theta));
      anchor[m.index] = 0.3;
    });
  });
  relax(g, p, anchor, 70);
  return { positions: p, markers, edgeOpacity: 0.3, lens: 'domain' };
}

export function computeLayout(id: LayoutId, g: Graph): LayoutResult {
  switch (id) {
    case 'orbits': return { ...orbitsLayout(g), lens: 'category' };
    case 'constellation': return { ...constellationLayout(g, orbitsLayout(g).positions), lens: 'category' };
    case 'tower': return towerLayout(g);
    case 'systems': return systemsLayout(g);
  }
}
