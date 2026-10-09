import {
  AdditiveBlending,
  CanvasTexture,
  Color,
  DynamicDrawUsage,
  InstancedMesh,
  Matrix4,
  MeshBasicMaterial,
  Object3D,
  PlaneGeometry,
  Quaternion,
  SphereGeometry,
  Vector3,
} from 'three';
import type { Graph } from '../data/graph';
import { DOMAIN_BY_ID } from '../data/domains';
import type { SpecialtyNode } from '../data/types';
import type { NodeVisual } from '../state';

/** Base radius for each level. */
export function baseRadius(node: SpecialtyNode): number {
  switch (node.level) {
    case 'root':
      return 2.6;
    case 'domain':
      return 1.55;
    case 'specialty':
      return 1.0;
    default:
      return node.depth >= 4 ? 0.5 : 0.62;
  }
}

function haloTexture(): CanvasTexture {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, 'rgba(255,255,255,0.55)');
  g.addColorStop(0.25, 'rgba(255,255,255,0.18)');
  g.addColorStop(0.6, 'rgba(255,255,255,0.04)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  return new CanvasTexture(canvas);
}

/**
 * All nodes as two instanced meshes: a solid glowing core sphere and a soft
 * camera-facing halo. Per-instance colour carries both domain hue and emphasis.
 */
export class NodeCloud extends Object3D {
  readonly cores: InstancedMesh;
  readonly halos: InstancedMesh;
  readonly index = new Map<string, number>();
  readonly nodes: SpecialtyNode[];
  /** Current (animated) world positions, one per node, in node order. */
  readonly positions: Vector3[];
  /** Current scale multiplier per node (animated: intro, hover pulse). */
  readonly scales: Float32Array;
  readonly emphasis: Float32Array;
  private readonly baseColors: Color[];
  private readonly tmpM = new Matrix4();
  private readonly tmpQ = new Quaternion();
  private readonly tmpS = new Vector3();
  private readonly tmpC = new Color();
  private readonly white = new Color('#ffffff');
  /** Multiplier on halo size, lowered by the app as the camera approaches. */
  haloScale = 1;

  constructor(graph: Graph) {
    super();
    this.nodes = graph.nodes;
    const count = graph.nodes.length;
    this.positions = graph.nodes.map(() => new Vector3());
    this.scales = new Float32Array(count).fill(0);
    this.emphasis = new Float32Array(count).fill(1);
    this.baseColors = graph.nodes.map((n) => {
      if (n.level === 'root') return new Color('#ffffff');
      return new Color(DOMAIN_BY_ID[n.domain]!.color);
    });
    graph.nodes.forEach((n, i) => this.index.set(n.id, i));

    const coreGeo = new SphereGeometry(1, 24, 16);
    const coreMat = new MeshBasicMaterial({ toneMapped: false });
    this.cores = new InstancedMesh(coreGeo, coreMat, count);
    this.cores.instanceMatrix.setUsage(DynamicDrawUsage);
    this.cores.frustumCulled = false;
    this.cores.name = 'node-cores';

    const haloGeo = new PlaneGeometry(1, 1);
    const haloMat = new MeshBasicMaterial({
      map: haloTexture(),
      transparent: true,
      depthWrite: false,
      blending: AdditiveBlending,
      toneMapped: false,
    });
    this.halos = new InstancedMesh(haloGeo, haloMat, count);
    this.halos.instanceMatrix.setUsage(DynamicDrawUsage);
    this.halos.frustumCulled = false;
    this.halos.name = 'node-halos';
    this.halos.renderOrder = 2;

    for (let i = 0; i < count; i++) {
      this.cores.setColorAt(i, this.baseColors[i]!);
      this.halos.setColorAt(i, this.baseColors[i]!);
    }
    this.add(this.cores, this.halos);
  }

  applyVisuals(visuals: Map<string, NodeVisual>): void {
    this.nodes.forEach((n, i) => {
      const v = visuals.get(n.id)!;
      this.emphasis[i] = v.visible ? v.emphasis : 0;
    });
  }

  /** Rebuild instance matrices and colours. Call once per frame. */
  update(cameraQuaternion: Quaternion, time: number): void {
    const count = this.nodes.length;
    for (let i = 0; i < count; i++) {
      const node = this.nodes[i]!;
      const e = this.emphasis[i]!;
      const s = this.scales[i]!;
      const r = baseRadius(node) * s * (e <= 0 ? 0 : e < 1 ? 0.5 + 0.5 * e : 0.78 + 0.22 * Math.min(1.5, e));
      // Gentle breathing on emphasised nodes.
      const pulse = e > 1.2 ? 1 + 0.08 * Math.sin(time * 4 + i) : 1;
      const p = this.positions[i]!;

      this.tmpS.setScalar(Math.max(0.0001, r * pulse));
      this.tmpM.compose(p, this.tmpQ.identity(), this.tmpS);
      this.cores.setMatrixAt(i, this.tmpM);

      const haloSize = Math.max(
        0.0001,
        r * pulse * this.haloScale * (node.level === 'root' ? 6 : node.level === 'domain' ? 4.5 : node.level === 'specialty' ? 3.6 : 3.2),
      );
      this.tmpS.setScalar(haloSize);
      this.tmpM.compose(p, cameraQuaternion, this.tmpS);
      this.halos.setMatrixAt(i, this.tmpM);

      const base = this.baseColors[i]!;
      // Brightness: dimmed nodes fall toward the background; emphasised ones
      // lift toward white so the bloom pass catches them.
      const bright = e <= 0 ? 0 : e < 1 ? 0.12 + 0.88 * e * e : 1;
      const lift = e > 1 ? Math.min(0.4, (e - 1) * 0.7) : node.level === 'root' ? 0 : 0.12;
      this.tmpC.copy(base).lerp(this.white, lift).multiplyScalar(bright * (node.level === 'root' ? 1.3 : 1.05));
      this.cores.setColorAt(i, this.tmpC);
      this.tmpC.copy(base).multiplyScalar(e <= 0 ? 0 : 0.18 + 0.55 * Math.min(1, Math.max(0, e - 0.3)));
      this.halos.setColorAt(i, this.tmpC);
    }
    this.cores.instanceMatrix.needsUpdate = true;
    this.halos.instanceMatrix.needsUpdate = true;
    if (this.cores.instanceColor) this.cores.instanceColor.needsUpdate = true;
    if (this.halos.instanceColor) this.halos.instanceColor.needsUpdate = true;
  }

  positionOf(id: string): Vector3 | undefined {
    const i = this.index.get(id);
    return i === undefined ? undefined : this.positions[i];
  }
}
