import {
  AdditiveBlending,
  BufferGeometry,
  CanvasTexture,
  Color,
  Float32BufferAttribute,
  Points,
  PointsMaterial,
} from 'three';

function softDotTexture(): CanvasTexture {
  const size = 64;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.3, 'rgba(255,255,255,0.6)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  return new CanvasTexture(canvas);
}

/** A sparse, slowly drifting field of distant stars for depth. */
export function createStarfield(count = 2200): Points {
  const positions = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  const tints = [new Color('#9fb4ff'), new Color('#ffffff'), new Color('#ffd9b0'), new Color('#b8f2ff')];
  let seed = 7;
  const rand = () => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  for (let i = 0; i < count; i++) {
    const r = 260 + rand() * 420;
    const u = rand() * 2 - 1;
    const phi = rand() * Math.PI * 2;
    const s = Math.sqrt(1 - u * u);
    positions[i * 3] = r * s * Math.cos(phi);
    positions[i * 3 + 1] = r * u;
    positions[i * 3 + 2] = r * s * Math.sin(phi);
    const tint = tints[Math.floor(rand() * tints.length)]!;
    const b = 0.35 + rand() * 0.65;
    colors[i * 3] = tint.r * b;
    colors[i * 3 + 1] = tint.g * b;
    colors[i * 3 + 2] = tint.b * b;
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
  geometry.setAttribute('color', new Float32BufferAttribute(colors, 3));
  const material = new PointsMaterial({
    size: 2.2,
    sizeAttenuation: true,
    map: softDotTexture(),
    vertexColors: true,
    transparent: true,
    opacity: 0.85,
    depthWrite: false,
    blending: AdditiveBlending,
  });
  const points = new Points(geometry, material);
  points.name = 'starfield';
  points.frustumCulled = false;
  return points;
}
