import {
  AdditiveBlending, BufferGeometry, Color, DynamicDrawUsage, Float32BufferAttribute, InstancedBufferAttribute, InstancedMesh,
  Matrix4, Object3D, Points, Quaternion, RingGeometry, ShaderMaterial, SphereGeometry, Vector3,
} from 'three';
import type { Graph } from '../graph';

const sphereVert = /* glsl */ `
  attribute vec3 aColor;
  attribute float aEmph;
  attribute float aGlow;
  varying vec3 vColor;
  varying float vEmph;
  varying float vGlow;
  varying vec3 vNormalW;
  varying vec3 vViewDir;
  void main() {
    mat4 im = modelMatrix * instanceMatrix;
    vec4 wp = im * vec4(position, 1.0);
    vNormalW = normalize(mat3(im) * normal);
    vViewDir = normalize(cameraPosition - wp.xyz);
    vColor = aColor;
    vEmph = aEmph;
    vGlow = aGlow;
    gl_Position = projectionMatrix * viewMatrix * wp;
  }
`;

const sphereFrag = /* glsl */ `
  uniform float uTime;
  varying vec3 vColor;
  varying float vEmph;
  varying float vGlow;
  varying vec3 vNormalW;
  varying vec3 vViewDir;
  void main() {
    float ndv = max(dot(normalize(vNormalW), normalize(vViewDir)), 0.0);
    float fres = pow(1.0 - ndv, 2.4);
    // Soft "lit from the camera" core with a luminous rim.
    vec3 core = vColor * (0.22 + 0.33 * ndv);
    vec3 rim = (vColor * 1.25 + vec3(0.35)) * fres;
    vec3 col = core + rim;
    // Highlight: brighten everything and whiten the core.
    col += (vColor * 0.6 + vec3(0.22)) * vGlow * (0.8 + 0.2 * sin(uTime * 3.0));
    col *= mix(0.16, 1.0, vEmph);
    gl_FragColor = vec4(col, 1.0);
  }
`;

const ringVert = /* glsl */ `
  attribute vec3 aColor;
  attribute float aEmph;
  varying vec3 vColor;
  varying float vEmph;
  varying vec2 vUv;
  void main() {
    vColor = aColor;
    vEmph = aEmph;
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
  }
`;

const ringFrag = /* glsl */ `
  uniform float uTime;
  varying vec3 vColor;
  varying float vEmph;
  varying vec2 vUv;
  void main() {
    // Two faint arcs sweeping slowly around the node: the mark of a multi-board bridge.
    float ang = atan(vUv.y - 0.5, vUv.x - 0.5);
    float sweep = 0.55 + 0.45 * sin(ang * 2.0 + uTime * 0.9);
    float a = (0.35 + 0.4 * sweep) * vEmph;
    gl_FragColor = vec4((vColor * 0.8 + vec3(0.2)) * a, a);
  }
`;

const haloVert = /* glsl */ `
  attribute float aSize;
  attribute vec3 aColor;
  attribute float aEmph;
  attribute float aGlow;
  uniform float uScale;
  varying vec3 vColor;
  varying float vAlpha;
  varying float vGlow;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float px = aSize * (uScale / max(1.0, -mv.z));
    gl_PointSize = clamp(px, 0.0, 220.0);
    gl_Position = projectionMatrix * mv;
    vColor = aColor;
    vAlpha = aEmph * step(0.01, aSize);
    vGlow = aGlow;
  }
`;

const haloFrag = /* glsl */ `
  varying vec3 vColor;
  varying float vAlpha;
  varying float vGlow;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0) discard;
    float a = exp(-d * d * 5.0) * (1.0 - smoothstep(0.6, 1.0, d));
    a *= (0.28 + vGlow * 0.9) * vAlpha;
    gl_FragColor = vec4(vColor * a, a);
  }
`;

/**
 * All nodes as one instanced sphere mesh plus one additive point cloud of halos.
 * Per-node visual state lives in typed arrays that the Atlas writes each frame.
 */
export class NodeCloud {
  readonly mesh: InstancedMesh;
  readonly halos: Points;
  /** Billboarded rings drawn around multi-board "bridge" nodes. */
  readonly rings: InstancedMesh;
  readonly n: number;

  /** Target & current colour (rgb triplets). */
  readonly colorTarget: Float32Array;
  readonly colorCurrent: Float32Array;
  /** Emphasis 0..1 (dimmed → full), target & current. */
  readonly emphTarget: Float32Array;
  readonly emphCurrent: Float32Array;
  /** Highlight glow 0..1. */
  readonly glowTarget: Float32Array;
  readonly glowCurrent: Float32Array;
  /** Visibility scale 0..1. */
  readonly visTarget: Float32Array;
  readonly visCurrent: Float32Array;

  private readonly colorAttr: InstancedBufferAttribute;
  private readonly emphAttr: InstancedBufferAttribute;
  private readonly glowAttr: InstancedBufferAttribute;
  private readonly haloPos: Float32BufferAttribute;
  private readonly haloSize: Float32BufferAttribute;
  private readonly haloColor: Float32BufferAttribute;
  private readonly haloEmph: Float32BufferAttribute;
  private readonly haloGlow: Float32BufferAttribute;
  private readonly sphereMat: ShaderMaterial;
  private readonly haloMat: ShaderMaterial;
  private readonly ringMat: ShaderMaterial;
  private readonly ringColor: InstancedBufferAttribute;
  private readonly ringEmph: InstancedBufferAttribute;
  private readonly ringQuat = new Quaternion();
  private readonly dummy = new Object3D();
  private readonly m4 = new Matrix4();
  private readonly q = new Quaternion();
  private readonly v3 = new Vector3();
  private readonly s3 = new Vector3();

  constructor(private readonly graph: Graph) {
    const n = (this.n = graph.nodes.length);
    const geo = new SphereGeometry(1, 28, 20);
    this.colorTarget = new Float32Array(n * 3);
    this.colorCurrent = new Float32Array(n * 3);
    this.emphTarget = new Float32Array(n).fill(1);
    this.emphCurrent = new Float32Array(n).fill(1);
    this.glowTarget = new Float32Array(n);
    this.glowCurrent = new Float32Array(n);
    this.visTarget = new Float32Array(n).fill(1);
    this.visCurrent = new Float32Array(n).fill(0);

    this.colorAttr = new InstancedBufferAttribute(new Float32Array(n * 3), 3).setUsage(DynamicDrawUsage);
    this.emphAttr = new InstancedBufferAttribute(new Float32Array(n).fill(1), 1).setUsage(DynamicDrawUsage);
    this.glowAttr = new InstancedBufferAttribute(new Float32Array(n), 1).setUsage(DynamicDrawUsage);
    geo.setAttribute('aColor', this.colorAttr);
    geo.setAttribute('aEmph', this.emphAttr);
    geo.setAttribute('aGlow', this.glowAttr);

    this.sphereMat = new ShaderMaterial({ vertexShader: sphereVert, fragmentShader: sphereFrag, uniforms: { uTime: { value: 0 } } });
    this.mesh = new InstancedMesh(geo, this.sphereMat, n);
    this.mesh.instanceMatrix.setUsage(DynamicDrawUsage);
    this.mesh.frustumCulled = false;

    const hgeo = new BufferGeometry();
    this.haloPos = new Float32BufferAttribute(new Float32Array(n * 3), 3).setUsage(DynamicDrawUsage);
    this.haloSize = new Float32BufferAttribute(new Float32Array(n), 1).setUsage(DynamicDrawUsage);
    this.haloColor = new Float32BufferAttribute(new Float32Array(n * 3), 3).setUsage(DynamicDrawUsage);
    this.haloEmph = new Float32BufferAttribute(new Float32Array(n), 1).setUsage(DynamicDrawUsage);
    this.haloGlow = new Float32BufferAttribute(new Float32Array(n), 1).setUsage(DynamicDrawUsage);
    hgeo.setAttribute('position', this.haloPos);
    hgeo.setAttribute('aSize', this.haloSize);
    hgeo.setAttribute('aColor', this.haloColor);
    hgeo.setAttribute('aEmph', this.haloEmph);
    hgeo.setAttribute('aGlow', this.haloGlow);
    this.haloMat = new ShaderMaterial({
      vertexShader: haloVert, fragmentShader: haloFrag, uniforms: { uScale: { value: 400 } },
      transparent: true, depthWrite: false, blending: AdditiveBlending,
    });
    this.halos = new Points(hgeo, this.haloMat);
    this.halos.frustumCulled = false;
    this.halos.renderOrder = 2;

    const rgeo = new RingGeometry(1.55, 1.75, 48);
    this.ringColor = new InstancedBufferAttribute(new Float32Array(n * 3), 3).setUsage(DynamicDrawUsage);
    this.ringEmph = new InstancedBufferAttribute(new Float32Array(n), 1).setUsage(DynamicDrawUsage);
    rgeo.setAttribute('aColor', this.ringColor);
    rgeo.setAttribute('aEmph', this.ringEmph);
    this.ringMat = new ShaderMaterial({ vertexShader: ringVert, fragmentShader: ringFrag, uniforms: { uTime: { value: 0 } }, transparent: true, depthWrite: false, blending: AdditiveBlending, side: 2 });
    this.rings = new InstancedMesh(rgeo, this.ringMat, n);
    this.rings.instanceMatrix.setUsage(DynamicDrawUsage);
    this.rings.frustumCulled = false;
    this.rings.renderOrder = 2;
  }

  /** Rings face the camera. */
  setBillboard(q: Quaternion) { this.ringQuat.copy(q); }

  setHaloScale(viewportHeight: number, fovDeg: number) {
    this.haloMat.uniforms.uScale.value = viewportHeight / (2 * Math.tan((fovDeg * Math.PI) / 360));
  }

  setColor(i: number, hex: string, tmp = new Color()) {
    tmp.set(hex);
    this.colorTarget[i * 3] = tmp.r; this.colorTarget[i * 3 + 1] = tmp.g; this.colorTarget[i * 3 + 2] = tmp.b;
  }

  /** Snap current colours to their targets (used on first frame). */
  snapColors() { this.colorCurrent.set(this.colorTarget); }

  /**
   * Advance the animated state and write it to the GPU buffers.
   * @param pos display positions (n*3)
   * @param dt seconds since last frame
   */
  update(pos: Float32Array, dt: number, time: number) {
    const k = 1 - Math.exp(-dt * 7);
    const kc = 1 - Math.exp(-dt * 4);
    const nodes = this.graph.nodes;
    for (let i = 0; i < this.n; i++) {
      this.emphCurrent[i] += (this.emphTarget[i] - this.emphCurrent[i]) * k;
      this.glowCurrent[i] += (this.glowTarget[i] - this.glowCurrent[i]) * k;
      this.visCurrent[i] += (this.visTarget[i] - this.visCurrent[i]) * k;
      for (let c = 0; c < 3; c++) this.colorCurrent[i * 3 + c] += (this.colorTarget[i * 3 + c] - this.colorCurrent[i * 3 + c]) * kc;

      const vis = this.visCurrent[i];
      const r = nodes[i].radius * (0.0001 + vis) * (1 + this.glowCurrent[i] * 0.18) * (0.55 + 0.45 * this.emphCurrent[i]);
      this.v3.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]);
      this.s3.setScalar(Math.max(r, 0.0001));
      this.m4.compose(this.v3, this.q, this.s3);
      this.mesh.setMatrixAt(i, this.m4);

      this.colorAttr.array[i * 3] = this.colorCurrent[i * 3];
      this.colorAttr.array[i * 3 + 1] = this.colorCurrent[i * 3 + 1];
      this.colorAttr.array[i * 3 + 2] = this.colorCurrent[i * 3 + 2];
      this.emphAttr.array[i] = this.emphCurrent[i];
      this.glowAttr.array[i] = this.glowCurrent[i];

      this.haloPos.array[i * 3] = pos[i * 3];
      this.haloPos.array[i * 3 + 1] = pos[i * 3 + 1];
      this.haloPos.array[i * 3 + 2] = pos[i * 3 + 2];
      this.haloSize.array[i] = vis < 0.02 ? 0 : nodes[i].radius * (3.4 + this.glowCurrent[i] * 2.2) * vis;
      this.haloColor.array[i * 3] = this.colorCurrent[i * 3];
      this.haloColor.array[i * 3 + 1] = this.colorCurrent[i * 3 + 1];
      this.haloColor.array[i * 3 + 2] = this.colorCurrent[i * 3 + 2];
      this.haloEmph.array[i] = this.emphCurrent[i] * this.emphCurrent[i];
      this.haloGlow.array[i] = this.glowCurrent[i];

      const ringScale = nodes[i].isBridge ? Math.max(r, 0.0001) : 0.0001;
      this.s3.setScalar(ringScale);
      this.m4.compose(this.v3, this.ringQuat, this.s3);
      this.rings.setMatrixAt(i, this.m4);
      this.ringColor.array[i * 3] = this.colorCurrent[i * 3];
      this.ringColor.array[i * 3 + 1] = this.colorCurrent[i * 3 + 1];
      this.ringColor.array[i * 3 + 2] = this.colorCurrent[i * 3 + 2];
      this.ringEmph.array[i] = this.emphCurrent[i] * this.emphCurrent[i] * (0.6 + 0.4 * this.glowCurrent[i]);
    }
    this.rings.instanceMatrix.needsUpdate = true;
    this.ringColor.needsUpdate = true;
    this.ringEmph.needsUpdate = true;
    this.ringMat.uniforms.uTime.value = time;
    this.mesh.instanceMatrix.needsUpdate = true;
    this.colorAttr.needsUpdate = true;
    this.emphAttr.needsUpdate = true;
    this.glowAttr.needsUpdate = true;
    this.haloPos.needsUpdate = true;
    this.haloSize.needsUpdate = true;
    this.haloColor.needsUpdate = true;
    this.haloEmph.needsUpdate = true;
    this.haloGlow.needsUpdate = true;
    this.sphereMat.uniforms.uTime.value = time;
    void this.dummy;
  }
}
