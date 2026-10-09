import { Group, type PerspectiveCamera, Vector3 } from 'three';
import { Text } from 'troika-three-text';
import type { Graph, GraphNode } from '../graph';

interface LabelState {
  text: Text;
  node: GraphNode;
  /** Current opacity (animated). */
  opacity: number;
  /** Desired opacity before occlusion culling. */
  want: number;
  /** Set by the screen-space culler. */
  culled: boolean;
  width: number; // approx world width
  priority: number;
}

const FONT_SIZE: Record<string, number> = { root: 4.6, specialty: 2.7, subspecialty: 1.55, fellowship: 1.35 };

/**
 * Billboarded SDF text labels. Visibility blends tier, label density, camera
 * distance, emphasis, and a cheap screen-space overlap cull so dense clusters
 * stay legible.
 */
export class Labels {
  readonly group = new Group();
  readonly states: LabelState[] = [];
  private readonly v = new Vector3();
  private cullClock = 0;

  constructor(graph: Graph, fontUrl: string) {
    for (const node of graph.nodes) {
      const t = new Text();
      t.text = node.short ?? node.name;
      t.font = fontUrl;
      t.fontSize = FONT_SIZE[node.tier];
      t.color = node.tier === 'root' ? '#f6e9c8' : '#e3e8f6';
      t.anchorX = 'center';
      t.anchorY = 'bottom';
      t.outlineWidth = '9%';
      t.outlineColor = '#05070f';
      t.outlineOpacity = 0.85;
      t.outlineBlur = '12%';
      t.fillOpacity = 0;
      t.depthOffset = -2;
      t.renderOrder = 10;
      t.visible = false;
      const mat = t.material as unknown as { depthTest: boolean; depthWrite: boolean };
      mat.depthWrite = false;
      t.sync();
      this.group.add(t);
      const priority = (node.tier === 'root' ? 1e9 : node.tier === 'specialty' ? 1e6 : node.tier === 'subspecialty' ? 1e3 : 0) + (node.workforce ?? 0);
      this.states.push({ text: t, node, opacity: 0, want: 0, culled: false, width: (node.short ?? node.name).length * FONT_SIZE[node.tier] * 0.56, priority });
    }
  }

  /**
   * @param pos display positions
   * @param vis node visibility 0..1
   * @param emph node emphasis 0..1
   * @param focus set of node indices that should always be labelled (neighbourhood of selection / hover / search)
   * @param hasFocus whether something is selected (dims everything else)
   */
  update(
    pos: Float32Array, vis: Float32Array, emph: Float32Array, focus: Set<number>, hasFocus: boolean,
    density: number, camera: PerspectiveCamera, viewportHeight: number, dt: number,
  ) {
    const k = 1 - Math.exp(-dt * 6);
    const camPos = camera.position;
    for (const s of this.states) {
      const i = s.node.index;
      const x = pos[i * 3], y = pos[i * 3 + 1], z = pos[i * 3 + 2];
      const d = Math.hypot(camPos.x - x, camPos.y - y, camPos.z - z);
      const near = 1 - smooth(55, 120, d);
      let base: number;
      switch (s.node.tier) {
        case 'root': base = 1; break;
        case 'specialty': base = 1; break;
        case 'subspecialty': base = density >= 0.66 ? 1 : density >= 0.33 ? near : 0; break;
        default: base = density >= 0.9 ? 1 : density >= 0.66 ? near : 0;
      }
      if (focus.has(i)) base = 1;
      else if (hasFocus) base *= 0.12;
      s.want = base * vis[i] * (0.25 + 0.75 * emph[i]);
      s.text.position.set(x, y + s.node.radius * (vis[i] + 0.0001) + 0.5, z);
      s.text.quaternion.copy(camera.quaternion);
    }

    // Screen-space overlap culling, refreshed a few times a second.
    this.cullClock += dt;
    if (this.cullClock > 0.12) {
      this.cullClock = 0;
      this.cull(camera, viewportHeight, focus);
    }

    for (const s of this.states) {
      const target = s.culled ? 0 : s.want;
      s.opacity += (target - s.opacity) * k;
      const o = s.opacity;
      s.text.visible = o > 0.015;
      s.text.fillOpacity = o;
      s.text.outlineOpacity = 0.85 * o;
    }
  }

  private cull(camera: PerspectiveCamera, viewportHeight: number, focus: Set<number>) {
    const placed: { x0: number; x1: number; y0: number; y1: number }[] = [];
    const sorted = this.states.filter((s) => s.want > 0.02).sort((a, b) => (focus.has(b.node.index) ? 1 : 0) - (focus.has(a.node.index) ? 1 : 0) || b.priority - a.priority);
    const f = viewportHeight / (2 * Math.tan((camera.fov * Math.PI) / 360));
    const aspect = camera.aspect;
    for (const s of sorted) {
      this.v.copy(s.text.position).applyMatrix4(camera.matrixWorldInverse);
      const depth = -this.v.z;
      if (depth <= 1) { s.culled = true; continue; }
      const scale = f / depth; // px per world unit
      const cx = (this.v.x * scale) / (viewportHeight * aspect) * 2; // normalised -1..1
      const cy = (this.v.y * scale) / viewportHeight * 2;
      const w = (s.width * scale) / (viewportHeight * aspect) * 2 * 0.5;
      const h = (s.text.fontSize * 1.25 * scale) / viewportHeight * 2 * 0.5;
      const box = { x0: cx - w, x1: cx + w, y0: cy, y1: cy + h * 2 };
      const hit = placed.some((p) => box.x0 < p.x1 && box.x1 > p.x0 && box.y0 < p.y1 && box.y1 > p.y0);
      s.culled = hit;
      if (!hit) placed.push(box);
    }
    for (const s of this.states) if (s.want <= 0.02) s.culled = false;
  }

  dispose() { for (const s of this.states) s.text.dispose(); }
}

function smooth(a: number, b: number, x: number) {
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}
