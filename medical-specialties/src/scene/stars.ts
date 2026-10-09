import { AdditiveBlending, BufferGeometry, Float32BufferAttribute, Points, ShaderMaterial } from 'three';
import { mulberry32 } from '../util/prng';

const vert = /* glsl */ `
  attribute float aSize;
  attribute float aPhase;
  uniform float uTime;
  uniform float uScale;
  varying float vAlpha;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float tw = 0.65 + 0.35 * sin(uTime * (0.6 + aPhase * 1.5) + aPhase * 40.0);
    gl_PointSize = clamp(aSize * tw * (uScale / max(1.0, -mv.z)), 0.0, 6.0);
    gl_Position = projectionMatrix * mv;
    vAlpha = tw;
  }
`;
const frag = /* glsl */ `
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0) discard;
    float a = (1.0 - d) * (1.0 - d) * vAlpha * 0.75;
    gl_FragColor = vec4(vec3(0.78, 0.84, 1.0) * a, a);
  }
`;

/** A distant, faintly twinkling star field. */
export class Stars {
  readonly points: Points;
  private readonly mat: ShaderMaterial;
  constructor(count = 2600, radius = 700) {
    const rnd = mulberry32(7);
    const pos = new Float32Array(count * 3);
    const size = new Float32Array(count);
    const phase = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      const u = rnd() * 2 - 1, th = rnd() * Math.PI * 2;
      const r = radius * (0.75 + rnd() * 0.5);
      const s = Math.sqrt(1 - u * u);
      pos[i * 3] = r * s * Math.cos(th);
      pos[i * 3 + 1] = r * u;
      pos[i * 3 + 2] = r * s * Math.sin(th);
      size[i] = 0.9 + Math.pow(rnd(), 3) * 3.2;
      phase[i] = rnd();
    }
    const geo = new BufferGeometry();
    geo.setAttribute('position', new Float32BufferAttribute(pos, 3));
    geo.setAttribute('aSize', new Float32BufferAttribute(size, 1));
    geo.setAttribute('aPhase', new Float32BufferAttribute(phase, 1));
    this.mat = new ShaderMaterial({ vertexShader: vert, fragmentShader: frag, uniforms: { uTime: { value: 0 }, uScale: { value: 400 } }, transparent: true, depthWrite: false, blending: AdditiveBlending });
    this.points = new Points(geo, this.mat);
    this.points.frustumCulled = false;
    this.points.renderOrder = -1;
  }
  setScale(viewportHeight: number, fovDeg: number) {
    this.mat.uniforms.uScale.value = viewportHeight / (2 * Math.tan((fovDeg * Math.PI) / 360));
  }
  update(time: number) { this.mat.uniforms.uTime.value = time; }
}
