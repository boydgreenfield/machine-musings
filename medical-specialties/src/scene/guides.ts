import { BufferGeometry, Color, Float32BufferAttribute, LineBasicMaterial, LineSegments, Object3D } from 'three';
import { SUNBURST_RADII } from '../graph/layouts';

/** Faint concentric rings marking each depth in the sunburst layout. */
export class SunburstGuides extends Object3D {
  constructor() {
    super();
    const positions: number[] = [];
    const steps = 180;
    for (const r of SUNBURST_RADII.slice(1, 5)) {
      for (let i = 0; i < steps; i++) {
        const a0 = (i / steps) * Math.PI * 2;
        const a1 = ((i + 1) / steps) * Math.PI * 2;
        positions.push(r * Math.sin(a0), -0.4, r * Math.cos(a0), r * Math.sin(a1), -0.4, r * Math.cos(a1));
      }
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
    const material = new LineBasicMaterial({ color: new Color('#2b3652'), transparent: true, opacity: 0.6, toneMapped: false });
    const lines = new LineSegments(geometry, material);
    lines.frustumCulled = false;
    this.add(lines);
    this.visible = false;
  }
}
