import { BufferGeometry, Float32BufferAttribute, Group, LineBasicMaterial, LineSegments } from 'three';
import { Text } from 'troika-three-text';

/** Faint horizontal rings with year labels, shown only in the training tower. */
export class Guides {
  readonly group = new Group();
  private readonly mat: LineBasicMaterial;
  private readonly labels: Text[] = [];
  private lines: LineSegments | null = null;
  private opacity = 0;
  private target = 0;
  private readonly markers: Text[] = [];
  private markerOpacity = 0;
  private markerTarget = 0;

  constructor(private readonly fontUrl: string) {
    this.mat = new LineBasicMaterial({ color: 0x8fa3c8, transparent: true, opacity: 0, depthWrite: false });
    this.group.visible = false;
  }

  setRings(rings: { y: number; label: string }[] | undefined) {
    if (this.lines) { this.group.remove(this.lines); this.lines.geometry.dispose(); this.lines = null; }
    for (const l of this.labels) { this.group.remove(l); l.dispose(); }
    this.labels.length = 0;
    if (!rings || !rings.length) { this.target = 0; return; }
    const seg = 96, R = 66;
    const arr: number[] = [];
    for (const r of rings) {
      for (let i = 0; i < seg; i++) {
        const a0 = (i / seg) * Math.PI * 2, a1 = ((i + 1) / seg) * Math.PI * 2;
        arr.push(R * Math.cos(a0), r.y, R * Math.sin(a0), R * Math.cos(a1), r.y, R * Math.sin(a1));
      }
      const t = new Text();
      t.text = r.label;
      t.font = this.fontUrl;
      t.fontSize = 2.6;
      t.color = '#b9c6e3';
      t.anchorX = 'left';
      t.anchorY = 'middle';
      t.position.set(R + 3, r.y, 0);
      t.fillOpacity = 0;
      t.sync();
      this.labels.push(t);
      this.group.add(t);
    }
    const geo = new BufferGeometry();
    geo.setAttribute('position', new Float32BufferAttribute(arr, 3));
    this.lines = new LineSegments(geo, this.mat);
    this.group.add(this.lines);
    this.target = 1;
  }

  setMarkers(markers: { position: [number, number, number]; label: string; color: string }[] | undefined) {
    for (const m of this.markers) { this.group.remove(m); m.dispose(); }
    this.markers.length = 0;
    if (!markers || !markers.length) { this.markerTarget = 0; return; }
    for (const m of markers) {
      const t = new Text();
      t.text = m.label.toUpperCase();
      t.font = this.fontUrl;
      t.fontSize = 2.4;
      t.letterSpacing = 0.12;
      t.color = m.color;
      t.anchorX = 'center';
      t.anchorY = 'middle';
      t.outlineWidth = '10%';
      t.outlineColor = '#05070f';
      t.outlineOpacity = 0.9;
      t.position.set(...m.position);
      t.fillOpacity = 0;
      t.renderOrder = 11;
      (t.material as unknown as { depthWrite: boolean }).depthWrite = false;
      t.sync();
      this.markers.push(t);
      this.group.add(t);
    }
    this.markerTarget = 1;
  }

  update(dt: number, cameraQuaternion: { x: number; y: number; z: number; w: number }) {
    const k = 1 - Math.exp(-dt * 4);
    this.opacity += (this.target - this.opacity) * k;
    this.markerOpacity += (this.markerTarget - this.markerOpacity) * k;
    this.group.visible = this.opacity > 0.01 || this.markerOpacity > 0.01;
    this.mat.opacity = 0.18 * this.opacity;
    for (const l of this.labels) {
      l.fillOpacity = 0.85 * this.opacity;
      l.quaternion.set(cameraQuaternion.x, cameraQuaternion.y, cameraQuaternion.z, cameraQuaternion.w);
    }
    for (const m of this.markers) {
      m.fillOpacity = 0.9 * this.markerOpacity;
      m.outlineOpacity = 0.9 * this.markerOpacity;
      m.visible = this.markerOpacity > 0.02;
      m.quaternion.set(cameraQuaternion.x, cameraQuaternion.y, cameraQuaternion.z, cameraQuaternion.w);
    }
  }
}
