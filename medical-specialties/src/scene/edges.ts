import {
  AdditiveBlending, BufferGeometry, DynamicDrawUsage, Float32BufferAttribute, LineSegments, Points, ShaderMaterial,
} from 'three';
import type { Graph } from '../graph';
import { hash01 } from '../util/prng';

const lineVert = /* glsl */ `
  attribute vec3 aColor;
  attribute float aAlpha;
  attribute float aT;
  attribute float aKind;
  varying vec3 vColor;
  varying float vAlpha;
  varying float vT;
  varying float vKind;
  void main() {
    vColor = aColor;
    vAlpha = aAlpha;
    vT = aT;
    vKind = aKind;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const lineFrag = /* glsl */ `
  uniform float uGlobal;
  varying vec3 vColor;
  varying float vAlpha;
  varying float vT;
  varying float vKind;
  void main() {
    if (vKind > 0.5 && fract(vT * 18.0) > 0.45) discard;
    float base = vKind > 0.5 ? 0.22 : 0.34;
    // Fade slightly toward the child end so clusters read as radiating from their parent.
    float a = base * vAlpha * (1.0 - 0.35 * vT) * uGlobal;
    gl_FragColor = vec4(vColor * a, a);
  }
`;

const flowVert = /* glsl */ `
  attribute vec3 aStart;
  attribute vec3 aEnd;
  attribute float aPhase;
  attribute float aSpeed;
  attribute vec3 aColor;
  attribute float aAlpha;
  uniform float uTime;
  uniform float uScale;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float t = fract(aPhase + uTime * aSpeed);
    vec3 p = mix(aStart, aEnd, t);
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    float fade = smoothstep(0.0, 0.12, t) * (1.0 - smoothstep(0.85, 1.0, t));
    gl_PointSize = clamp(1.1 * (uScale / max(1.0, -mv.z)), 0.0, 14.0);
    gl_Position = projectionMatrix * mv;
    vColor = aColor;
    vAlpha = aAlpha * fade;
  }
`;

const flowFrag = /* glsl */ `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0) discard;
    float a = (1.0 - d * d) * vAlpha * 0.9;
    gl_FragColor = vec4(vColor * a + vec3(0.25) * a, a);
  }
`;

const PARTICLES_PER_EDGE = 2;

/** Training-lineage links plus a slow stream of particles along each. */
export class EdgeCloud {
  readonly lines: LineSegments;
  readonly flow: Points;
  readonly m: number;
  /** Emphasis per edge (target/current). */
  readonly emphTarget: Float32Array;
  readonly emphCurrent: Float32Array;

  private readonly pos: Float32BufferAttribute;
  private readonly col: Float32BufferAttribute;
  private readonly alpha: Float32BufferAttribute;
  private readonly fStart: Float32BufferAttribute;
  private readonly fEnd: Float32BufferAttribute;
  private readonly fColor: Float32BufferAttribute;
  private readonly fAlpha: Float32BufferAttribute;
  private readonly flowMat: ShaderMaterial;
  private readonly lineMat: ShaderMaterial;
  /** Layout-level brightness multiplier (target / current). */
  globalTarget = 1;
  private globalCurrent = 1;

  constructor(private readonly graph: Graph) {
    const m = (this.m = graph.edges.length);
    this.emphTarget = new Float32Array(m).fill(1);
    this.emphCurrent = new Float32Array(m).fill(1);

    const geo = new BufferGeometry();
    this.pos = new Float32BufferAttribute(new Float32Array(m * 6), 3).setUsage(DynamicDrawUsage);
    this.col = new Float32BufferAttribute(new Float32Array(m * 6), 3).setUsage(DynamicDrawUsage);
    this.alpha = new Float32BufferAttribute(new Float32Array(m * 2), 1).setUsage(DynamicDrawUsage);
    const t = new Float32Array(m * 2);
    const kind = new Float32Array(m * 2);
    graph.edges.forEach((e, i) => {
      t[i * 2] = 0; t[i * 2 + 1] = 1;
      kind[i * 2] = kind[i * 2 + 1] = e.kind === 'via' ? 1 : 0;
    });
    geo.setAttribute('position', this.pos);
    geo.setAttribute('aColor', this.col);
    geo.setAttribute('aAlpha', this.alpha);
    geo.setAttribute('aT', new Float32BufferAttribute(t, 1));
    geo.setAttribute('aKind', new Float32BufferAttribute(kind, 1));
    this.lineMat = new ShaderMaterial({ vertexShader: lineVert, fragmentShader: lineFrag, uniforms: { uGlobal: { value: 1 } }, transparent: true, depthWrite: false, blending: AdditiveBlending });
    this.lines = new LineSegments(geo, this.lineMat);
    this.lines.frustumCulled = false;
    this.lines.renderOrder = 1;

    const fn = m * PARTICLES_PER_EDGE;
    const fgeo = new BufferGeometry();
    fgeo.setAttribute('position', new Float32BufferAttribute(new Float32Array(fn * 3), 3)); // unused, required by three
    this.fStart = new Float32BufferAttribute(new Float32Array(fn * 3), 3).setUsage(DynamicDrawUsage);
    this.fEnd = new Float32BufferAttribute(new Float32Array(fn * 3), 3).setUsage(DynamicDrawUsage);
    this.fColor = new Float32BufferAttribute(new Float32Array(fn * 3), 3).setUsage(DynamicDrawUsage);
    this.fAlpha = new Float32BufferAttribute(new Float32Array(fn), 1).setUsage(DynamicDrawUsage);
    const phase = new Float32Array(fn);
    const speed = new Float32Array(fn);
    for (let i = 0; i < m; i++) for (let j = 0; j < PARTICLES_PER_EDGE; j++) {
      const k = i * PARTICLES_PER_EDGE + j;
      phase[k] = hash01(graph.edges[i].target.id + j);
      speed[k] = 0.05 + 0.06 * hash01(graph.edges[i].source.id + k);
    }
    fgeo.setAttribute('aStart', this.fStart);
    fgeo.setAttribute('aEnd', this.fEnd);
    fgeo.setAttribute('aColor', this.fColor);
    fgeo.setAttribute('aAlpha', this.fAlpha);
    fgeo.setAttribute('aPhase', new Float32BufferAttribute(phase, 1));
    fgeo.setAttribute('aSpeed', new Float32BufferAttribute(speed, 1));
    this.flowMat = new ShaderMaterial({
      vertexShader: flowVert, fragmentShader: flowFrag, uniforms: { uTime: { value: 0 }, uScale: { value: 400 } },
      transparent: true, depthWrite: false, blending: AdditiveBlending,
    });
    this.flow = new Points(fgeo, this.flowMat);
    this.flow.frustumCulled = false;
    this.flow.renderOrder = 3;
  }

  setScale(viewportHeight: number, fovDeg: number) {
    this.flowMat.uniforms.uScale.value = viewportHeight / (2 * Math.tan((fovDeg * Math.PI) / 360));
  }

  /**
   * @param pos display positions (n*3)
   * @param color current node colours (n*3)
   * @param vis current node visibility (n)
   */
  update(pos: Float32Array, color: Float32Array, vis: Float32Array, dt: number, time: number, flowOn: boolean) {
    const k = 1 - Math.exp(-dt * 7);
    this.globalCurrent += (this.globalTarget - this.globalCurrent) * (1 - Math.exp(-dt * 3));
    this.lineMat.uniforms.uGlobal.value = this.globalCurrent;
    const edges = this.graph.edges;
    for (let i = 0; i < this.m; i++) {
      this.emphCurrent[i] += (this.emphTarget[i] - this.emphCurrent[i]) * k;
      const s = edges[i].source.index, t = edges[i].target.index;
      const a = this.emphCurrent[i] * Math.min(vis[s], vis[t]);
      for (let c = 0; c < 3; c++) {
        this.pos.array[i * 6 + c] = pos[s * 3 + c];
        this.pos.array[i * 6 + 3 + c] = pos[t * 3 + c];
        this.col.array[i * 6 + c] = color[s * 3 + c];
        this.col.array[i * 6 + 3 + c] = color[t * 3 + c];
      }
      this.alpha.array[i * 2] = a;
      this.alpha.array[i * 2 + 1] = a;
      for (let j = 0; j < PARTICLES_PER_EDGE; j++) {
        const q = i * PARTICLES_PER_EDGE + j;
        for (let c = 0; c < 3; c++) {
          this.fStart.array[q * 3 + c] = pos[s * 3 + c];
          this.fEnd.array[q * 3 + c] = pos[t * 3 + c];
          this.fColor.array[q * 3 + c] = color[t * 3 + c];
        }
        this.fAlpha.array[q] = flowOn && edges[i].kind === 'tree' ? a * a * this.globalCurrent : 0;
      }
    }
    this.pos.needsUpdate = true;
    this.col.needsUpdate = true;
    this.alpha.needsUpdate = true;
    this.fStart.needsUpdate = true;
    this.fEnd.needsUpdate = true;
    this.fColor.needsUpdate = true;
    this.fAlpha.needsUpdate = true;
    this.flowMat.uniforms.uTime.value = time;
    this.flow.visible = flowOn;
  }
}
