import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';

export const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
export const easeOut = (t) => 1 - Math.pow(1 - t, 3);

// ------------------------------------------------------------ song marks
const songVert = /* glsl */ `
  attribute vec3 aColor;
  attribute vec2 aState; // x: visibility 0..1 (filter dim), y: highlight 0..1
  varying vec3 vColor; varying vec2 vState; varying vec3 vNormalW; varying vec3 vViewPos;
  #include <common>
  void main() {
    vColor = aColor; vState = aState;
    #include <beginnormal_vertex>
    #include <defaultnormal_vertex>
    #include <begin_vertex>
    #include <project_vertex>
    vNormalW = normalize(transformedNormal);
    vViewPos = -mvPosition.xyz;
  }`;
const songFrag = /* glsl */ `
  varying vec3 vColor; varying vec2 vState; varying vec3 vNormalW; varying vec3 vViewPos;
  uniform float uTime;
  void main() {
    vec3 n = normalize(vNormalW);
    vec3 v = normalize(vViewPos);
    vec3 l = normalize(vec3(0.4, 0.9, 0.6));
    float lam = 0.35 + 0.65 * max(dot(n, l), 0.0);
    float rim = pow(1.0 - max(dot(n, v), 0.0), 2.2);
    float vis = mix(0.09, 1.0, vState.x);
    float hi = vState.y;
    vec3 col = vColor * (0.42 * lam) + vColor * rim * 1.25;
    col += vColor * hi * 0.7 + vec3(1.0) * hi * 0.12;
    col *= vis;
    gl_FragColor = vec4(col, 1.0);
  }`;

export class SongMarks {
  constructor(songs) {
    this.n = songs.length;
    this.geo = new THREE.IcosahedronGeometry(0.5, 2);
    this.mat = new THREE.ShaderMaterial({
      vertexShader: songVert, fragmentShader: songFrag,
      uniforms: { uTime: { value: 0 } },
    });
    this.mesh = new THREE.InstancedMesh(this.geo, this.mat, this.n);
    this.mesh.frustumCulled = false;
    this.colors = new Float32Array(this.n * 3);
    this.state = new Float32Array(this.n * 2);
    const c = new THREE.Color();
    songs.forEach((s, i) => {
      c.set(s.color);
      // Covers and instrumentals are rendered muted: identity is still the era hue.
      const k = s.cover ? 0.55 : s.inst ? 0.7 : 1;
      this.colors[i * 3] = c.r * k; this.colors[i * 3 + 1] = c.g * k; this.colors[i * 3 + 2] = c.b * k;
      this.state[i * 2] = 1; this.state[i * 2 + 1] = 0;
    });
    this.colorAttr = new THREE.InstancedBufferAttribute(this.colors, 3);
    this.stateAttr = new THREE.InstancedBufferAttribute(this.state, 2);
    this.stateAttr.setUsage(THREE.DynamicDrawUsage);
    this.geo.setAttribute('aColor', this.colorAttr);
    this.geo.setAttribute('aState', this.stateAttr);

    // Morph state
    this.pos0 = new Float32Array(this.n * 3);
    this.pos1 = new Float32Array(this.n * 3);
    this.scl0 = new Float32Array(this.n * 3);
    this.scl1 = new Float32Array(this.n * 3);
    this.cur = new Float32Array(this.n * 3);
    this.curS = new Float32Array(this.n * 3);
    this.stagger = new Float32Array(this.n);
    this.t0 = -1e9; this.dur = 1.5;
    this.visTarget = new Float32Array(this.n).fill(1);
    this.hiTarget = new Float32Array(this.n).fill(0);
    this._m = new THREE.Matrix4();
    this._p = new THREE.Vector3();
    this._q = new THREE.Quaternion();
    this._s = new THREE.Vector3();
  }

  /** Snap or animate to a layout's targets. */
  setTargets(targets, scales, now, animate = true) {
    for (let i = 0; i < this.n * 3; i++) {
      this.pos0[i] = this.cur[i]; this.scl0[i] = this.curS[i];
      this.pos1[i] = targets[i]; this.scl1[i] = scales[i];
    }
    for (let i = 0; i < this.n; i++) this.stagger[i] = Math.random() * 0.45;
    this.t0 = animate ? now : -1e9;
    if (!animate) { this.cur.set(targets); this.curS.set(scales); }
  }

  update(now, dt) {
    const elapsed = now - this.t0;
    let moving = elapsed < this.dur + 0.5;
    const st = this.state;
    for (let i = 0; i < this.n; i++) {
      // filter visibility and highlight ease toward targets
      const k = 1 - Math.exp(-dt * 9);
      st[i * 2] += (this.visTarget[i] - st[i * 2]) * k;
      st[i * 2 + 1] += (this.hiTarget[i] - st[i * 2 + 1]) * k;
      if (moving) {
        const u = THREE.MathUtils.clamp((elapsed - this.stagger[i]) / this.dur, 0, 1);
        const e = easeInOut(u);
        const j = i * 3;
        for (let a = 0; a < 3; a++) {
          this.cur[j + a] = this.pos0[j + a] + (this.pos1[j + a] - this.pos0[j + a]) * e;
          this.curS[j + a] = this.scl0[j + a] + (this.scl1[j + a] - this.scl0[j + a]) * e;
        }
      }
      // breathing on highlighted songs
      const j = i * 3;
      const hi = st[i * 2 + 1];
      const pulse = hi > 0.01 ? 1 + hi * 0.18 * (0.5 + 0.5 * Math.sin(now * 5 + i)) : 1;
      this._p.set(this.cur[j], this.cur[j + 1], this.cur[j + 2]);
      this._s.set(this.curS[j] * pulse, this.curS[j + 1] * pulse, this.curS[j + 2] * pulse);
      this._m.compose(this._p, this._q, this._s);
      this.mesh.setMatrixAt(i, this._m);
    }
    this.mesh.instanceMatrix.needsUpdate = true;
    this.stateAttr.needsUpdate = true;
    this.mat.uniforms.uTime.value = now;
  }

  positionOf(i, out = new THREE.Vector3()) {
    return out.set(this.cur[i * 3], this.cur[i * 3 + 1], this.cur[i * 3 + 2]);
  }
}

// ------------------------------------------------------------ labels (canvas sprites)
const labelCache = new Map();
const TITLE_FONT = '600 38px "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif';
const TITLE_FONT_SMALL = '500 34px "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif';
const SUB_FONT = '400 24px "Helvetica Neue", Helvetica, Arial, sans-serif';

function wrapLines(ctx, text, maxW) {
  const words = text.split(' ');
  const lines = [];
  let cur = '';
  for (const w of words) {
    const t = cur ? `${cur} ${w}` : w;
    if (ctx.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t;
  }
  if (cur) lines.push(cur);
  return lines;
}

function makeLabelTexture(text, sub, color, small) {
  const key = `${text}|${sub}|${color}|${small}`;
  if (labelCache.has(key)) return labelCache.get(key);
  const dpr = 2;
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  ctx.font = small ? TITLE_FONT_SMALL : TITLE_FONT;
  const lines = wrapLines(ctx, text, 330);
  const LH = small ? 38 : 44;
  const w1 = Math.max(...lines.map((l) => ctx.measureText(l).width));
  ctx.font = SUB_FONT; const w2 = sub ? ctx.measureText(sub).width : 0;
  const PAD = 18;
  const W = Math.ceil(Math.max(w1, w2) + PAD * 2);
  const H = Math.ceil(PAD + lines.length * LH + (sub ? 30 : 0) + PAD * 0.6);
  c.width = W * dpr; c.height = H * dpr;
  ctx.scale(dpr, dpr);
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.shadowColor = 'rgba(0,0,0,0.95)'; ctx.shadowBlur = 10;
  ctx.font = small ? TITLE_FONT_SMALL : TITLE_FONT; ctx.fillStyle = color;
  lines.forEach((l, i) => ctx.fillText(l, W / 2, PAD + LH * (i + 0.5)));
  if (sub) { ctx.font = SUB_FONT; ctx.fillStyle = 'rgba(232,226,240,0.82)'; ctx.fillText(sub, W / 2, PAD + LH * lines.length + 14); }
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.minFilter = THREE.LinearMipmapLinearFilter;
  tex.generateMipmaps = true;
  tex.anisotropy = 4;
  const out = { tex, w: W, h: H };
  labelCache.set(key, out);
  return out;
}

const _v = new THREE.Vector3();

export class LabelSet {
  constructor() {
    this.group = new THREE.Group();
    this.sprites = [];
    this.alpha = 0; this.alphaTarget = 0;
    this.emphasis = null; // albumIndex to keep on top
  }
  set(labels) {
    this.sprites.forEach((s) => { this.group.remove(s); s.material.dispose(); });
    this.sprites = labels.map((l) => {
      const { tex, w, h } = makeLabelTexture(l.text, l.sub, l.color, l.small);
      const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, depthTest: false, opacity: 0, sizeAttenuation: false });
      const sp = new THREE.Sprite(mat);
      sp.position.copy(l.pos);
      sp.center.set(0.5, l.anchor === 'top' ? 1 : l.anchor === 'bottom' ? 0 : 0.5);
      sp.renderOrder = 10;
      sp.userData = { label: l, w, h, f: l.small ? 0.3 : 0.4, base: l.kind === 'year' ? 0.6 : 0.95, show: 0, prio: l.kind === 'year' ? 2 : l.kind === 'word' ? 0 : 1 };
      this.group.add(sp);
      return sp;
    });
  }
  /** Screen-constant sizing, distance fade, and greedy overlap culling. */
  update(dt, camera, viewH, viewW) {
    const k = 1 - Math.exp(-dt * 6);
    this.alpha += (this.alphaTarget - this.alpha) * k;
    const unit = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) / viewH; // scale per on-screen pixel
    const items = [];
    for (const sp of this.sprites) {
      const u = sp.userData;
      const scale = u.f * (u.label.albumIndex !== undefined && u.label.albumIndex === this.emphasis ? 1.2 : 1);
      sp.scale.set(u.w * scale * unit, u.h * scale * unit, 1);
      _v.copy(sp.position).project(camera);
      const behind = _v.z > 1 || _v.z < -1;
      const sx = (_v.x * 0.5 + 0.5) * viewW, sy = (1 - (_v.y * 0.5 + 0.5)) * viewH;
      const pw = u.w * scale, ph = u.h * scale;
      const top = sp.center.y === 1 ? sy : sp.center.y === 0 ? sy - ph : sy - ph / 2;
      u.rect = [sx - pw / 2, top, sx + pw / 2, top + ph];
      u.behind = behind;
      u.dist = camera.position.distanceTo(sp.position);
      items.push(sp);
    }
    // priority: emphasised album, then kind, then nearer first
    items.sort((a, b) => {
      const ea = a.userData.label.albumIndex === this.emphasis ? -1 : 0;
      const eb = b.userData.label.albumIndex === this.emphasis ? -1 : 0;
      return ea - eb || a.userData.prio - b.userData.prio || a.userData.dist - b.userData.dist;
    });
    const kept = [];
    for (const sp of items) {
      const u = sp.userData;
      let show = !u.behind;
      if (show) {
        const r = u.rect;
        for (const q of kept) {
          if (r[0] < q[2] && r[2] > q[0] && r[1] < q[3] && r[3] > q[1]) { show = false; break; }
        }
      }
      if (show) kept.push(u.rect);
      u.show += ((show ? 1 : 0) - u.show) * (1 - Math.exp(-dt * 8));
      const far = 1 - THREE.MathUtils.smoothstep(u.dist, 260, 520);
      sp.material.opacity = this.alpha * u.base * far * u.show;
      sp.visible = sp.material.opacity > 0.01;
    }
  }
}

// ------------------------------------------------------------ guides fade
export function setGuideAlpha(group, a) {
  group.visible = a > 0.002;
  group.traverse((o) => {
    if (o.material) {
      const base = o.userData.baseOpacity ?? 1;
      if (o.material.uniforms && o.material.uniforms.uOpacity) o.material.uniforms.uOpacity.value = a * base;
      else o.material.opacity = a * base;
    }
  });
}

// ------------------------------------------------------------ starfield
export function makeStars(count = 3000, radius = 900) {
  const pos = new Float32Array(count * 3);
  const size = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    const u = Math.random() * 2 - 1, th = Math.random() * Math.PI * 2;
    const r = radius * (0.7 + 0.3 * Math.random());
    const s = Math.sqrt(1 - u * u);
    pos[i * 3] = r * s * Math.cos(th); pos[i * 3 + 1] = r * u; pos[i * 3 + 2] = r * s * Math.sin(th);
    size[i] = 0.5 + Math.pow(Math.random(), 3) * 2.5;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('aSize', new THREE.BufferAttribute(size, 1));
  const mat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { uPixelRatio: { value: 1 }, uTime: { value: 0 } },
    vertexShader: `attribute float aSize; varying float vA; uniform float uPixelRatio; uniform float uTime;
      void main(){ vec4 mv = modelViewMatrix * vec4(position,1.0); gl_PointSize = aSize * uPixelRatio * 1.6;
      vA = 0.55 + 0.45 * sin(uTime * 0.7 + position.x * 0.05 + position.y * 0.03); gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `varying float vA; void main(){ vec2 c = gl_PointCoord - 0.5; float d = length(c); if (d > 0.5) discard;
      float a = smoothstep(0.5, 0.1, d); gl_FragColor = vec4(vec3(0.85, 0.82, 0.95), a * 0.5 * vA); }`,
  });
  return new THREE.Points(geo, mat);
}

// ------------------------------------------------------------ final grade pass
const GradeShader = {
  uniforms: { tDiffuse: { value: null }, uTime: { value: 0 }, uAspect: { value: 1 } },
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
  fragmentShader: `varying vec2 vUv; uniform sampler2D tDiffuse; uniform float uTime; uniform float uAspect;
    float hash(vec2 p){ return fract(sin(dot(p, vec2(12.9898,78.233))) * 43758.5453); }
    void main(){
      vec4 c = texture2D(tDiffuse, vUv);
      vec2 q = (vUv - 0.5) * vec2(uAspect, 1.0);
      float vig = smoothstep(1.25, 0.35, length(q));
      c.rgb *= mix(0.72, 1.0, vig);
      float g = hash(vUv * 1000.0 + fract(uTime)) - 0.5;
      c.rgb += g * 0.035;
      // gentle warm lift in the shadows
      c.rgb += vec3(0.012, 0.008, 0.02) * (1.0 - vig);
      gl_FragColor = c;
    }`,
};

// ------------------------------------------------------------ the stage
export class Stage {
  constructor(canvas) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x07060b);
    this.scene.fog = new THREE.FogExp2(0x07060b, 0.0022);
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.5, 2500);
    this.camera.position.set(0, 80, 200);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.06;
    this.controls.maxDistance = 900;
    this.controls.minDistance = 4;
    this.controls.zoomSpeed = 0.8;

    this.composer = new EffectComposer(this.renderer);
    this.renderPass = new RenderPass(this.scene, this.camera);
    this.bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.85, 0.55, 0.72);
    this.grade = new ShaderPass(GradeShader);
    this.composer.addPass(this.renderPass);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());
    this.composer.addPass(this.grade);

    this.stars = makeStars();
    this.scene.add(this.stars);

    this.camTween = null;
    this.resize();
    addEventListener('resize', () => this.resize());
  }

  resize() {
    const w = innerWidth, h = innerHeight;
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.grade.uniforms.uAspect.value = w / h;
    const pr = this.renderer.getPixelRatio();
    this.stars.material.uniforms.uPixelRatio.value = pr;
  }

  /** Smoothly fly the camera to a vantage. */
  flyTo(pos, target, duration = 1.6, now = performance.now() / 1000) {
    this.camTween = {
      p0: this.camera.position.clone(), p1: pos.clone(),
      t0: this.controls.target.clone(), t1: target.clone(),
      start: now, dur: duration,
    };
  }

  update(now, dt) {
    if (this.camTween) {
      const tw = this.camTween;
      const u = THREE.MathUtils.clamp((now - tw.start) / tw.dur, 0, 1);
      const e = easeInOut(u);
      this.camera.position.lerpVectors(tw.p0, tw.p1, e);
      this.controls.target.lerpVectors(tw.t0, tw.t1, e);
      if (u >= 1) this.camTween = null;
    }
    this.controls.update();
    this.stars.material.uniforms.uTime.value = now;
    this.grade.uniforms.uTime.value = now;
  }

  render() { this.composer.render(); }
}
