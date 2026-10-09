import { AdditiveBlending, CanvasTexture, Sprite, SpriteMaterial } from 'three';

function ringTexture(): CanvasTexture {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  ctx.strokeStyle = 'rgba(255,255,255,0.95)';
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 6, 0, Math.PI * 2);
  ctx.stroke();
  ctx.strokeStyle = 'rgba(255,255,255,0.25)';
  ctx.lineWidth = 9;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 6, 0, Math.PI * 2);
  ctx.stroke();
  return new CanvasTexture(canvas);
}

/** A pulsing ring that marks the selected node. */
export class SelectionRing extends Sprite {
  private baseSize = 1;

  constructor() {
    super(
      new SpriteMaterial({
        map: ringTexture(),
        transparent: true,
        depthWrite: false,
        depthTest: false,
        blending: AdditiveBlending,
        toneMapped: false,
        opacity: 0.9,
      }),
    );
    this.visible = false;
    this.renderOrder = 3;
  }

  show(radius: number): void {
    this.baseSize = radius * 3.2;
    this.visible = true;
  }

  hide(): void {
    this.visible = false;
  }

  update(time: number): void {
    if (!this.visible) return;
    const pulse = 1 + 0.06 * Math.sin(time * 3);
    this.scale.setScalar(this.baseSize * pulse);
    (this.material as SpriteMaterial).opacity = 0.75 + 0.2 * Math.sin(time * 3);
  }
}
