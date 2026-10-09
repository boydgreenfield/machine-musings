import { BufferGeometry, Color, Float32BufferAttribute, LineBasicMaterial, LineSegments, Object3D } from 'three';
import { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import type { Lane } from '../graph/layouts';

/** Year ticks, lane names and a baseline for the training-years layout. */
export class TimelineAxis extends Object3D {
  private readonly labels: CSS2DObject[] = [];

  constructor(ticks: { years: number; x: number }[], lanes: Lane[]) {
    super();
    const positions: number[] = [];
    const first = ticks[0]!;
    const last = ticks[ticks.length - 1]!;
    const bottom = Math.min(...lanes.map((l) => l.y)) - 6;
    const top = Math.max(...lanes.map((l) => l.y)) + 6;
    positions.push(first.x, bottom, 0, last.x, bottom, 0);
    for (const t of ticks) {
      positions.push(t.x, bottom - 1.2, 0, t.x, bottom + 1.2, 0);
      positions.push(t.x, bottom, 0, t.x, top, 0);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
    const material = new LineBasicMaterial({ color: new Color('#2f3a58'), transparent: true, opacity: 0.6, toneMapped: false });
    const lines = new LineSegments(geometry, material);
    lines.frustumCulled = false;
    this.add(lines);

    for (const t of ticks) {
      const el = document.createElement('div');
      el.className = 'axis-label';
      el.textContent = t.years === 0 ? 'Medical school' : `${t.years} yr${t.years === 1 ? '' : 's'}`;
      const obj = new CSS2DObject(el);
      obj.position.set(t.x, bottom - 2.5, 0);
      obj.center.set(0.5, 0);
      this.add(obj);
      this.labels.push(obj);
    }
    for (const lane of lanes) {
      const el = document.createElement('div');
      el.className = 'axis-label lane-label';
      el.innerHTML = `<span class="swatch" style="color:${lane.color};background:${lane.color}"></span>${lane.name}`;
      const obj = new CSS2DObject(el);
      obj.position.set(first.x - 3, lane.y, 0);
      obj.center.set(1, 0.5);
      this.add(obj);
      this.labels.push(obj);
    }
    const title = document.createElement('div');
    title.className = 'axis-label axis-title';
    title.textContent = 'Cumulative years of residency and fellowship after medical school';
    const titleObj = new CSS2DObject(title);
    titleObj.position.set((first.x + last.x) / 2, bottom - 7, 0);
    titleObj.center.set(0.5, 0);
    this.add(titleObj);
    this.labels.push(titleObj);
    this.visible = false;
  }

  update(): void {
    for (const l of this.labels) l.visible = this.visible;
  }
}
