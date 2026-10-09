import { Camera, Object3D, Vector3 } from 'three';
import { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import type { Graph } from '../data/graph';
import type { Cluster } from '../graph/layouts';
import { DOMAIN_BY_ID } from '../data/domains';
import type { SpecialtyNode } from '../data/types';
import type { LabelMode, NodeVisual } from '../state';
import { baseRadius } from './nodes';
import type { NodeCloud } from './nodes';

type Placement = 'below' | 'above' | 'right' | 'left';
type Kind = SpecialtyNode['level'] | 'cluster';

const PLACEMENTS: Record<Kind, Placement[]> = {
  root: ['below', 'above'],
  domain: ['below', 'above', 'right', 'left'],
  specialty: ['below', 'above', 'right', 'left'],
  subspecialty: ['right', 'left', 'below', 'above'],
  cluster: ['above', 'below'],
};

interface LabelEntry {
  kind: Kind;
  /** Graph node, or null for a cluster caption. */
  node: SpecialtyNode | null;
  /** Fixed world position for cluster captions. */
  fixed: Vector3 | null;
  object: CSS2DObject;
  anchor: HTMLDivElement;
  label: HTMLDivElement;
  index: number;
  width: number;
  height: number;
  /** Smoothed opacity so collisions fade rather than flicker. */
  opacity: number;
  placement: Placement;
}

interface Candidate {
  entry: LabelEntry;
  dist: number;
  pinned: boolean;
  emphasis: number;
  priority: number;
  sx: number;
  sy: number;
}

interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/**
 * HTML labels anchored to nodes. Visibility follows a level-of-detail rule and
 * a greedy screen-space collision pass: higher-priority labels claim a
 * rectangle first; lower-priority labels try several placements around their
 * node and fade out if none is free.
 */
export class NodeLabels extends Object3D {
  private readonly entries: LabelEntry[] = [];
  private readonly tmp = new Vector3();
  private visuals: Map<string, NodeVisual> | null = null;
  private mode: LabelMode = 'auto';
  private showDomains = true;
  private viewport = { width: 1, height: 1 };

  constructor(graph: Graph, private readonly cloud: NodeCloud) {
    super();
    const measure = document.createElement('div');
    measure.style.cssText = 'position:absolute;left:-9999px;top:-9999px;visibility:hidden;pointer-events:none;white-space:nowrap';
    document.body.appendChild(measure);
    for (const node of graph.nodes) {
      const anchor = document.createElement('div');
      anchor.className = 'label-anchor';
      const label = document.createElement('div');
      label.className = `label level-${node.level}`;
      label.dataset.place = 'below';
      const dot = document.createElement('span');
      dot.className = 'label-dot';
      dot.style.background = node.level === 'root' ? '#ffffff' : DOMAIN_BY_ID[node.domain]!.color;
      const text = document.createElement('span');
      text.className = 'label-text';
      const str = node.level === 'subspecialty' && node.short && node.short.length >= 4 && node.name.length > 30 ? node.short : node.name;
      text.textContent = str;
      label.append(dot, text);
      anchor.appendChild(label);
      anchor.dataset.id = node.id;
      anchor.style.opacity = '0';
      const object = new CSS2DObject(anchor);
      this.add(object);

      const probe = label.cloneNode(true) as HTMLDivElement;
      probe.style.position = 'static';
      probe.style.transform = 'none';
      measure.appendChild(probe);
      const rect = probe.getBoundingClientRect();
      measure.removeChild(probe);
      this.entries.push({
        kind: node.level,
        node,
        fixed: null,
        object,
        anchor,
        label,
        index: this.cloud.index.get(node.id)!,
        width: rect.width || str.length * 7,
        height: rect.height || 16,
        opacity: 0,
        placement: 'below',
      });
    }
    document.body.removeChild(measure);
  }

  /** Captions for the organ-system clusters; shown only when `showClusters` is set. */
  showClusters = false;

  setClusters(clusters: Cluster[]): void {
    for (const c of clusters) {
      const anchor = document.createElement('div');
      anchor.className = 'label-anchor';
      const label = document.createElement('div');
      label.className = 'label level-cluster';
      label.dataset.place = 'above';
      label.innerHTML = `<span class="label-text">${c.name}</span><span class="cluster-count">${c.count}</span>`;
      anchor.appendChild(label);
      anchor.style.opacity = '0';
      const object = new CSS2DObject(anchor);
      const fixed = c.centre.clone().setY(c.centre.y + c.radius + 1);
      object.position.copy(fixed);
      this.add(object);
      this.entries.push({
        kind: 'cluster',
        node: null,
        fixed,
        object,
        anchor,
        label,
        index: -1,
        width: c.name.length * 7.6 + 30,
        height: 18,
        opacity: 0,
        placement: 'above',
      });
    }
  }

  setViewport(width: number, height: number): void {
    this.viewport = { width, height };
  }

  applyVisuals(visuals: Map<string, NodeVisual>, mode: LabelMode, showDomains: boolean): void {
    this.visuals = visuals;
    this.mode = mode;
    this.showDomains = showDomains;
  }

  private rectFor(entry: LabelEntry, placement: Placement, sx: number, sy: number, offset: number): Rect {
    const { width: w, height: h } = entry;
    switch (placement) {
      case 'below':
        return { x0: sx - w / 2, y0: sy + offset, x1: sx + w / 2, y1: sy + offset + h };
      case 'above':
        return { x0: sx - w / 2, y0: sy - offset - h, x1: sx + w / 2, y1: sy - offset };
      case 'right':
        return { x0: sx + offset, y0: sy - h / 2, x1: sx + offset + w, y1: sy + h / 2 };
      case 'left':
        return { x0: sx - offset - w, y0: sy - h / 2, x1: sx - offset, y1: sy + h / 2 };
    }
  }

  /** Called every frame to position labels and apply LOD plus collision avoidance. */
  update(camera: Camera, cameraDistance: number, introProgress: number, dt: number): void {
    if (!this.visuals) return;
    const camPos = camera.position;
    const { width: vw, height: vh } = this.viewport;
    const candidates: Candidate[] = [];
    const nearThreshold = Math.max(40, cameraDistance * 0.6);

    for (const entry of this.entries) {
      let eligible: boolean;
      let pos: Vector3;
      let pinned = false;
      let emphasis = 1;
      const level = entry.kind;
      if (entry.node) {
        const v = this.visuals.get(entry.node.id)!;
        pos = this.cloud.positions[entry.index]!;
        entry.object.position.copy(pos);
        const scale = this.cloud.scales[entry.index]!;
        eligible = v.visible && this.mode !== 'none' && scale >= 0.3 && introProgress >= 0.35;
        if (level === 'domain' && !this.showDomains) eligible = false;
        pinned = v.labelPinned;
        emphasis = v.emphasis;
      } else {
        pos = entry.fixed!;
        eligible = this.showClusters;
      }
      if (!eligible) {
        this.fade(entry, 0, dt);
        continue;
      }
      this.tmp.copy(pos).project(camera);
      if (this.tmp.z > 1) {
        this.fade(entry, 0, dt);
        continue;
      }
      const sx = ((this.tmp.x + 1) / 2) * vw;
      const sy = ((1 - this.tmp.y) / 2) * vh;
      if (sx < -200 || sx > vw + 200 || sy < -100 || sy > vh + 100) {
        this.fade(entry, 0, dt);
        continue;
      }
      const dist = pos.distanceTo(camPos);
      let wanted = false;
      let priority = 0;
      if (level === 'cluster') {
        wanted = true;
        priority = 95;
      } else if (level === 'root') {
        wanted = emphasis > 0.2 || pinned;
        priority = 100;
      } else if (level === 'domain') {
        wanted = emphasis > 0.2 || pinned;
        priority = 90;
      } else if (level === 'specialty') {
        wanted = emphasis > 0.2 || pinned;
        priority = 70;
      } else if (pinned) {
        wanted = true;
        priority = 80;
      } else if (this.mode === 'all') {
        wanted = emphasis > 0.2;
        priority = 40;
      } else if (emphasis >= 1 && dist < nearThreshold) {
        wanted = true;
        priority = 40;
      }
      if (pinned) priority += 15;
      if (emphasis > 1.2) priority += 10;
      if (!wanted) {
        this.fade(entry, 0, dt);
        continue;
      }
      candidates.push({ entry, dist, pinned, emphasis, priority, sx, sy });
    }

    // Greedy collision pass: best priority first, then nearest to the camera.
    candidates.sort((a, b) => b.priority - a.priority || a.dist - b.dist);
    const taken: Rect[] = [];
    let budget = this.mode === 'all' ? Infinity : 64;
    for (const c of candidates) {
      const { entry, sx, sy, pinned, emphasis, dist } = c;
      const level = entry.kind;
      const r = entry.node ? baseRadius(entry.node) * (0.78 + 0.22 * Math.min(1.5, emphasis)) : 0;
      // Screen radius of the node grows as the camera approaches.
      const screenR = (r / Math.max(1, dist)) * (vh / (2 * Math.tan((48 / 2) * (Math.PI / 180))));
      const offset = Math.round(screenR + 5);

      // Prefer the placement used last frame to avoid jitter, then the others.
      const order = [entry.placement, ...PLACEMENTS[level].filter((p) => p !== entry.placement)];
      let chosen: Placement | null = null;
      let chosenRect: Rect | null = null;
      for (const p of order) {
        const rect = this.rectFor(entry, p, sx, sy, offset);
        const pad = { x0: rect.x0 - 3, y0: rect.y0 - 1, x1: rect.x1 + 3, y1: rect.y1 + 1 };
        const collides = taken.some((t) => pad.x0 < t.x1 && pad.x1 > t.x0 && pad.y0 < t.y1 && pad.y1 > t.y0);
        if (!collides) {
          chosen = p;
          chosenRect = pad;
          break;
        }
      }
      if (!chosen || !chosenRect || (level === 'subspecialty' && !pinned && budget <= 0)) {
        this.fade(entry, 0, dt);
        continue;
      }
      if (level === 'subspecialty' && !pinned) budget--;
      taken.push(chosenRect);
      if (entry.placement !== chosen) {
        entry.placement = chosen;
        entry.label.dataset.place = chosen;
      }
      const dim = emphasis < 1 ? 0.35 + 0.65 * emphasis : 1;
      const fade = level === 'subspecialty' && !pinned ? Math.min(1, Math.max(0.3, 1.5 - dist / nearThreshold)) : 1;
      this.fade(entry, dim * fade, dt);
      entry.label.classList.toggle('pinned', pinned);
      entry.label.classList.toggle('emphasis', emphasis > 1.2);
      entry.label.style.setProperty('--offset', `${offset}px`);
    }
  }

  private fade(entry: LabelEntry, target: number, dt: number): void {
    const k = target > entry.opacity ? 10 : 7;
    const next = entry.opacity + (target - entry.opacity) * Math.min(1, k * dt);
    entry.opacity = Math.abs(next - target) < 0.01 ? target : next;
    const visible = entry.opacity > 0.015;
    if (entry.object.visible !== visible) entry.object.visible = visible;
    if (visible) entry.anchor.style.opacity = entry.opacity.toFixed(3);
  }
}
