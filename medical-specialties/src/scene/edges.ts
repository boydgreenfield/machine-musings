import { Color, InterleavedBufferAttribute, Object3D, Vector2, Vector3 } from 'three';
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js';
import { LineSegments2 } from 'three/examples/jsm/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/examples/jsm/lines/LineSegmentsGeometry.js';
import type { Edge, Graph } from '../data/graph';
import { DOMAIN_BY_ID } from '../data/domains';
import type { LayoutId } from '../graph/layouts';
import { LAYOUT_BY_ID } from '../graph/layouts';
import type { NodeVisual } from '../state';
import type { NodeCloud } from './nodes';

const SEGMENTS = 10;

interface DrawEdge extends Edge {
  /** Root → specialty shortcut used while domain nodes are hidden. */
  bypass: boolean;
}

interface EdgeSet {
  edges: DrawEdge[];
  mesh: LineSegments2;
  geometry: LineSegmentsGeometry;
  material: LineMaterial;
  positions: Float32Array;
  colors: Float32Array;
  /** Per-edge brightness after visuals are applied. */
  brightness: Float32Array;
  /** Per-edge curve bulge (fraction of length) and direction (0 = away from origin, 1 = up). */
  bulge: Float32Array;
  bulgeUp: Uint8Array;
  /** Per-edge multiplier set by the layout (e.g. dim cross-cluster arches). */
  layoutDim: Float32Array;
}

/**
 * Curved tree edges and dashed cross-board edges as fat lines. Positions are
 * rewritten in place every frame the layout is animating.
 */
export class EdgeLines extends Object3D {
  readonly tree: EdgeSet;
  readonly cross: EdgeSet;
  private readonly a = new Vector3();
  private readonly b = new Vector3();
  private readonly ctrl = new Vector3();
  private readonly dir = new Vector3();
  private readonly pt = new Vector3();
  private readonly cA = new Color();
  private readonly cB = new Color();
  private readonly cMix = new Color();
  private crossVisible = false;
  private layout: LayoutId = 'constellation';

  constructor(
    private readonly graph: Graph,
    private readonly cloud: NodeCloud,
  ) {
    super();
    const treeEdges: DrawEdge[] = graph.treeEdges.map((e) => ({ ...e, bypass: false }));
    for (const n of graph.nodes) {
      const home = n.parents[0] ? graph.byId.get(n.parents[0]) : undefined;
      if (home && home.level === 'domain') treeEdges.push({ source: graph.root.id, target: n.id, primary: true, bypass: true });
    }
    this.tree = this.build(treeEdges, false);
    this.cross = this.build(
      graph.crossEdges.map((e) => ({ ...e, bypass: false })),
      true,
    );
    this.add(this.tree.mesh, this.cross.mesh);
    this.setLayout('constellation');
  }

  private build(edges: DrawEdge[], dashed: boolean): EdgeSet {
    const segCount = edges.length * SEGMENTS;
    const positions = new Float32Array(segCount * 6);
    const colors = new Float32Array(segCount * 6);
    const geometry = new LineSegmentsGeometry();
    geometry.setPositions(positions);
    geometry.setColors(colors);
    const material = new LineMaterial({
      vertexColors: true,
      linewidth: dashed ? 1.1 : 1.5,
      worldUnits: false,
      transparent: true,
      opacity: dashed ? 0.9 : 0.95,
      depthWrite: false,
      dashed,
      dashSize: 1.4,
      gapSize: 1.0,
      toneMapped: false,
    });
    material.resolution = new Vector2(1, 1);
    const mesh = new LineSegments2(geometry, material);
    mesh.frustumCulled = false;
    mesh.renderOrder = 1;
    mesh.visible = !dashed;
    const n = edges.length;
    return {
      edges,
      mesh,
      geometry,
      material,
      positions,
      colors,
      brightness: new Float32Array(n).fill(1),
      bulge: new Float32Array(n).fill(0.08),
      bulgeUp: new Uint8Array(n),
      layoutDim: new Float32Array(n).fill(1),
    };
  }

  setResolution(width: number, height: number): void {
    this.tree.material.resolution.set(width, height);
    this.cross.material.resolution.set(width, height);
  }

  /** Choose curve shapes and per-edge dimming appropriate to the layout. */
  setLayout(layout: LayoutId): void {
    this.layout = layout;
    const g = this.graph;
    this.tree.edges.forEach((e, i) => {
      const a = g.byId.get(e.source)!;
      const b = g.byId.get(e.target)!;
      let bulge = 0.08;
      let up = 0;
      let dim = 1;
      switch (layout) {
        case 'systems':
          if (a.level === 'root') {
            bulge = 0.0;
            dim = 0;
          } else if (a.system !== b.system) {
            bulge = 0.3;
            up = 1;
            dim = 0.4;
          } else bulge = 0.04;
          break;
        case 'timeline':
          bulge = 0.0;
          if (a.level === 'root') dim = 0.5;
          break;
        case 'sunburst':
          bulge = 0;
          break;
        default:
          bulge = a.level === 'root' ? 0 : 0.08;
      }
      this.tree.bulge[i] = bulge;
      this.tree.bulgeUp[i] = up;
      this.tree.layoutDim[i] = dim;
    });
    this.cross.edges.forEach((_e, i) => {
      this.cross.bulge[i] = layout === 'sunburst' || layout === 'timeline' || layout === 'systems' ? 0.4 : 0.35;
      this.cross.bulgeUp[i] = layout === 'constellation' ? 0 : 1;
      this.cross.layoutDim[i] = 1;
    });
  }

  applyVisuals(visuals: Map<string, NodeVisual>, showCross: boolean, selected: string | null, hovered: string | null): void {
    const showDomains = LAYOUT_BY_ID[this.layout].showDomains;
    const apply = (set: EdgeSet, isCross: boolean) => {
      set.edges.forEach((e, i) => {
        const va = visuals.get(e.source)!;
        const vb = visuals.get(e.target)!;
        if (!va.visible || !vb.visible || (e.bypass && showDomains)) {
          set.brightness[i] = 0;
          return;
        }
        const minE = Math.min(va.emphasis, vb.emphasis);
        const touches = e.source === selected || e.target === selected || e.source === hovered || e.target === hovered;
        let b = minE < 1 ? 0.04 + 0.5 * minE * minE : Math.min(1.6, 0.5 + 0.5 * minE);
        if (isCross) {
          if (!showCross && !touches) b = 0;
          else if (touches) b = 1.3;
          else b *= 0.7;
        } else if (!touches) b *= set.layoutDim[i]!;
        set.brightness[i] = b;
      });
    };
    apply(this.tree, false);
    apply(this.cross, true);
    this.crossVisible = showCross || selected !== null || hovered !== null;
    this.cross.mesh.visible = this.crossVisible;
    this.writeColors(this.tree);
    this.writeColors(this.cross);
    // Brightness changes can collapse or expand segments, so refresh positions too.
    this.updatePositions(true);
  }

  private writeColors(set: EdgeSet): void {
    const { edges, colors } = set;
    let k = 0;
    const scale = set === this.cross ? 0.55 : 0.62;
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i]!;
      const na = this.graph.byId.get(e.source)!;
      const nb = this.graph.byId.get(e.target)!;
      const bright = set.brightness[i]!;
      this.cA.set(na.level === 'root' ? '#ffffff' : DOMAIN_BY_ID[na.domain]!.color);
      this.cB.set(DOMAIN_BY_ID[nb.domain]!.color);
      for (let s = 0; s < SEGMENTS; s++) {
        const t0 = s / SEGMENTS;
        const t1 = (s + 1) / SEGMENTS;
        this.cMix.copy(this.cA).lerp(this.cB, t0).multiplyScalar(bright * scale);
        colors[k++] = this.cMix.r;
        colors[k++] = this.cMix.g;
        colors[k++] = this.cMix.b;
        this.cMix.copy(this.cA).lerp(this.cB, t1).multiplyScalar(bright * scale);
        colors[k++] = this.cMix.r;
        colors[k++] = this.cMix.g;
        colors[k++] = this.cMix.b;
      }
    }
    const attr = set.geometry.getAttribute('instanceColorStart') as InterleavedBufferAttribute;
    (attr.data.array as Float32Array).set(colors);
    attr.data.needsUpdate = true;
  }

  /** Rewrite curve positions from the current node positions. */
  updatePositions(force = false): void {
    this.writePositions(this.tree);
    if (this.crossVisible || force) this.writePositions(this.cross);
  }

  private writePositions(set: EdgeSet): void {
    const { edges, positions } = set;
    let k = 0;
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i]!;
      const pa = this.cloud.positionOf(e.source)!;
      const pb = this.cloud.positionOf(e.target)!;
      // Invisible edges collapse to a point so they cannot leave dark streaks.
      if (set.brightness[i]! <= 0) {
        for (let s = 0; s < SEGMENTS; s++) {
          positions[k++] = pa.x;
          positions[k++] = pa.y;
          positions[k++] = pa.z;
          positions[k++] = pa.x;
          positions[k++] = pa.y;
          positions[k++] = pa.z;
        }
        continue;
      }
      this.a.copy(pa);
      this.b.copy(pb);
      this.ctrl.addVectors(this.a, this.b).multiplyScalar(0.5);
      const len = this.a.distanceTo(this.b);
      const bulge = set.bulge[i]!;
      if (bulge > 0) {
        if (set.bulgeUp[i]) this.dir.set(0, 1, 0);
        else if (this.ctrl.lengthSq() > 1e-4) this.dir.copy(this.ctrl).normalize();
        else this.dir.set(0, 1, 0);
        this.ctrl.addScaledVector(this.dir, len * bulge);
      }
      let prevX = this.a.x;
      let prevY = this.a.y;
      let prevZ = this.a.z;
      for (let s = 1; s <= SEGMENTS; s++) {
        const t = s / SEGMENTS;
        const u = 1 - t;
        this.pt.set(
          u * u * this.a.x + 2 * u * t * this.ctrl.x + t * t * this.b.x,
          u * u * this.a.y + 2 * u * t * this.ctrl.y + t * t * this.b.y,
          u * u * this.a.z + 2 * u * t * this.ctrl.z + t * t * this.b.z,
        );
        positions[k++] = prevX;
        positions[k++] = prevY;
        positions[k++] = prevZ;
        positions[k++] = this.pt.x;
        positions[k++] = this.pt.y;
        positions[k++] = this.pt.z;
        prevX = this.pt.x;
        prevY = this.pt.y;
        prevZ = this.pt.z;
      }
    }
    const attr = set.geometry.getAttribute('instanceStart') as InterleavedBufferAttribute;
    (attr.data.array as Float32Array).set(positions);
    attr.data.needsUpdate = true;
    if (set.material.dashed) set.mesh.computeLineDistances();
  }
}
