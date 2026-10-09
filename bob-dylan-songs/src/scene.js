import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { cardTint } from './eras.js';

export const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
export const easeOut = (t) => 1 - Math.pow(1 - t, 3);

export const PAPER = '#e9dec4';
export const INK = '#1e1915';
export const MARKER_FONT = '"Permanent Marker", "Marker Felt", "Comic Sans MS", Impact, sans-serif';
export const TYPE_FONT = '"Special Elite", "Courier Prime", "Courier New", Courier, monospace';
export const WOOD_FONT = '"Oswald", "Arial Narrow", "Helvetica Neue", Arial, sans-serif';

// Card proportions: a 5x3 index card.
export const CARD_ASPECT = 0.62;

/** Wait briefly for the web fonts so canvas textures use them; never block the page on it. */
export function fontsReady(timeoutMs = 2500) {
  if (!document.fonts) return Promise.resolve();
  const loads = [
    document.fonts.load(`700 40px ${MARKER_FONT}`),
    document.fonts.load(`400 24px ${TYPE_FONT}`),
    document.fonts.load(`500 40px ${WOOD_FONT}`),
  ];
  return Promise.race([Promise.all(loads), new Promise((r) => setTimeout(r, timeoutMs))]).catch(() => {});
}

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

// ------------------------------------------------------------ the card atlas
// Every song's title, hand-lettered on its own index card, packed into one texture.
export class CardAtlas {
  constructor(songs) {
    this.songs = songs;
    this.cols = 20; this.rows = 28;
    this.size = 4096;
    this.cellW = this.size / this.cols; // 204.8
    this.cellH = this.size / this.rows; // 146.3  (aspect ≈ 0.71, card plane is 0.62; a hair of bleed)
    this.canvas = document.createElement('canvas');
    this.canvas.width = this.canvas.height = this.size;
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.anisotropy = 8;
    this.texture.generateMipmaps = true;
    this.texture.minFilter = THREE.LinearMipmapLinearFilter;
    this.draw();
  }
  uvOffset(i) {
    const col = i % this.cols, row = Math.floor(i / this.cols);
    return [col / this.cols, 1 - (row + 1) / this.rows];
  }
  draw() {
    const ctx = this.canvas.getContext('2d');
    const { cellW: W, cellH: H } = this;
    ctx.clearRect(0, 0, this.size, this.size);
    this.songs.forEach((s, i) => {
      const col = i % this.cols, row = Math.floor(i / this.cols);
      const x = col * W, y = row * H;
      ctx.save();
      ctx.translate(x, y);
      // card stock, tinted by era
      ctx.fillStyle = cardTint(s.color, '#f1e7cf', s.cover ? 0.26 : 0.42);
      ctx.fillRect(0, 0, W, H);
      // a faint ruled line like an index card, red for the head rule
      ctx.strokeStyle = 'rgba(181, 38, 31, 0.55)'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(10, 26); ctx.lineTo(W - 10, 26); ctx.stroke();
      ctx.strokeStyle = 'rgba(43, 120, 194, 0.22)'; ctx.lineWidth = 1.5;
      for (let ly = 26 + 34; ly < H - 8; ly += 34) { ctx.beginPath(); ctx.moveTo(10, ly); ctx.lineTo(W - 10, ly); ctx.stroke(); }
      // title in marker; covers in pencil grey
      ctx.fillStyle = s.cover ? 'rgba(74, 66, 58, 0.9)' : 'rgba(26, 20, 16, 0.96)';
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      let size = 40, lines;
      for (;;) {
        ctx.font = `700 ${size}px ${MARKER_FONT}`;
        lines = wrapLines(ctx, s.title, W - 28);
        const fits = lines.length * size * 1.02 <= H - 40 && lines.every((l) => ctx.measureText(l).width <= W - 28);
        if (fits || size <= 17) break;
        size -= 2;
      }
      const lh = size * 1.02;
      const top = 30 + ((H - 36) - lines.length * lh) / 2;
      ctx.save();
      ctx.translate(14, top);
      ctx.rotate((((i * 7919) % 11) - 5) * 0.004); // a little hand wobble
      lines.forEach((l, k) => ctx.fillText(l, 0, lh * (k + 0.5)));
      ctx.restore();
      // small track stamp
      ctx.font = `400 14px ${TYPE_FONT}`;
      ctx.fillStyle = 'rgba(26, 20, 16, 0.55)'; ctx.textAlign = 'right';
      ctx.fillText(s.nonAlbum ? String(s.year) : `${s.year} · ${s.n}`, W - 12, 13);
      ctx.restore();
    });
    this.texture.needsUpdate = true;
  }
}

// ------------------------------------------------------------ song cards (instanced)
export class SongMarks {
  constructor(songs, atlas) {
    this.n = songs.length;
    this.atlas = atlas;
    this.geo = new THREE.PlaneGeometry(1, CARD_ASPECT);
    this.mat = new THREE.MeshStandardMaterial({ map: atlas.texture, roughness: 0.92, metalness: 0, side: THREE.DoubleSide });
    const cols = atlas.cols, rows = atlas.rows;
    this.mat.onBeforeCompile = (shader) => {
      shader.vertexShader = shader.vertexShader
        .replace('#include <common>', `#include <common>\nattribute vec2 aUv; attribute vec2 aState; attribute vec3 aTint; varying vec2 vState; varying vec3 vTint;`)
        .replace('#include <uv_vertex>', `#include <uv_vertex>\nvMapUv = vMapUv * vec2(${(1 / cols).toFixed(6)}, ${(1 / rows).toFixed(6)}) + aUv;\nvState = aState; vTint = aTint;`);
      shader.fragmentShader = shader.fragmentShader
        .replace('#include <common>', `#include <common>\nvarying vec2 vState; varying vec3 vTint;`)
        .replace('#include <map_fragment>', `#include <map_fragment>
          if (!gl_FrontFacing) diffuseColor.rgb = vTint * 0.9;
          float vis = mix(0.0, 1.0, vState.x);
          diffuseColor.rgb = mix(vec3(0.80, 0.76, 0.68), diffuseColor.rgb, mix(0.25, 1.0, vis));`)
        .replace('#include <emissivemap_fragment>', `#include <emissivemap_fragment>\ntotalEmissiveRadiance += vec3(0.62, 0.18, 0.08) * vState.y * 0.38;`);
    };
    this.mesh = new THREE.InstancedMesh(this.geo, this.mat, this.n);
    this.mesh.frustumCulled = false;
    this.mesh.castShadow = true;
    this.mesh.receiveShadow = true;

    this.uvs = new Float32Array(this.n * 2);
    this.state = new Float32Array(this.n * 2);
    this.tints = new Float32Array(this.n * 3);
    const c = new THREE.Color();
    songs.forEach((s, i) => {
      const [u, v] = atlas.uvOffset(i);
      this.uvs[i * 2] = u; this.uvs[i * 2 + 1] = v;
      this.state[i * 2] = 1; this.state[i * 2 + 1] = 0;
      c.set(cardTint(s.color, '#f1e7cf', 0.42));
      this.tints[i * 3] = c.r; this.tints[i * 3 + 1] = c.g; this.tints[i * 3 + 2] = c.b;
    });
    this.geo.setAttribute('aUv', new THREE.InstancedBufferAttribute(this.uvs, 2));
    this.stateAttr = new THREE.InstancedBufferAttribute(this.state, 2);
    this.stateAttr.setUsage(THREE.DynamicDrawUsage);
    this.geo.setAttribute('aState', this.stateAttr);
    this.geo.setAttribute('aTint', new THREE.InstancedBufferAttribute(this.tints, 3));

    // Posts for the highway: a card on a stake, height = length.
    this.postGeo = new THREE.BoxGeometry(0.09, 1, 0.09);
    this.postGeo.translate(0, 0.5, 0);
    this.posts = new THREE.InstancedMesh(this.postGeo, new THREE.MeshStandardMaterial({ color: 0x4a3a2c, roughness: 0.95 }), this.n);
    this.posts.frustumCulled = false;
    this.posts.castShadow = true;

    // Morph state: position, quaternion, scale, post height
    const N = this.n;
    this.pos0 = new Float32Array(N * 3); this.pos1 = new Float32Array(N * 3); this.cur = new Float32Array(N * 3);
    this.scl0 = new Float32Array(N * 3); this.scl1 = new Float32Array(N * 3); this.curS = new Float32Array(N * 3);
    this.q0 = new Float32Array(N * 4); this.q1 = new Float32Array(N * 4); this.curQ = new Float32Array(N * 4);
    this.ph0 = new Float32Array(N); this.ph1 = new Float32Array(N); this.curP = new Float32Array(N);
    for (let i = 0; i < N; i++) { this.q0[i * 4 + 3] = 1; this.q1[i * 4 + 3] = 1; this.curQ[i * 4 + 3] = 1; }
    this.stagger = new Float32Array(N);
    this.t0 = -1e9; this.dur = 1.5;
    this.visTarget = new Float32Array(N).fill(1);
    this.hiTarget = new Float32Array(N).fill(0);
    this._m = new THREE.Matrix4(); this._p = new THREE.Vector3(); this._q = new THREE.Quaternion(); this._qa = new THREE.Quaternion(); this._qb = new THREE.Quaternion(); this._s = new THREE.Vector3();
  }

  setTargets(layout, now, animate = true) {
    const { targets, scales, quats, posts } = layout;
    this.pos0.set(this.cur); this.scl0.set(this.curS); this.q0.set(this.curQ); this.ph0.set(this.curP);
    this.pos1.set(targets); this.scl1.set(scales);
    if (quats) this.q1.set(quats); else { this.q1.fill(0); for (let i = 0; i < this.n; i++) this.q1[i * 4 + 3] = 1; }
    if (posts) this.ph1.set(posts); else this.ph1.fill(0);
    let minX = Infinity, maxX = -Infinity;
    for (let i = 0; i < this.n; i++) { const x = targets[i * 3]; if (x < minX) minX = x; if (x > maxX) maxX = x; }
    const span = Math.max(1e-6, maxX - minX);
    for (let i = 0; i < this.n; i++) this.stagger[i] = 0.5 * ((targets[i * 3] - minX) / span) + Math.random() * 0.12;
    this.t0 = animate ? now : -1e9;
    if (!animate) { this.cur.set(this.pos1); this.curS.set(this.scl1); this.curQ.set(this.q1); this.curP.set(this.ph1); }
  }

  update(now, dt) {
    const elapsed = now - this.t0;
    const moving = elapsed < this.dur + 0.6;
    const st = this.state;
    const k = 1 - Math.exp(-dt * 9);
    for (let i = 0; i < this.n; i++) {
      st[i * 2] += (this.visTarget[i] - st[i * 2]) * k;
      st[i * 2 + 1] += (this.hiTarget[i] - st[i * 2 + 1]) * k;
      const j = i * 3, q = i * 4;
      if (moving) {
        const u = THREE.MathUtils.clamp((elapsed - this.stagger[i]) / this.dur, 0, 1);
        const e = easeInOut(u);
        for (let a = 0; a < 3; a++) {
          this.cur[j + a] = this.pos0[j + a] + (this.pos1[j + a] - this.pos0[j + a]) * e;
          this.curS[j + a] = this.scl0[j + a] + (this.scl1[j + a] - this.scl0[j + a]) * e;
        }
        // cards flip while they travel
        this._qa.set(this.q0[q], this.q0[q + 1], this.q0[q + 2], this.q0[q + 3]);
        this._qb.set(this.q1[q], this.q1[q + 1], this.q1[q + 2], this.q1[q + 3]);
        this._qa.slerp(this._qb, e);
        this.curQ[q] = this._qa.x; this.curQ[q + 1] = this._qa.y; this.curQ[q + 2] = this._qa.z; this.curQ[q + 3] = this._qa.w;
        this.curP[i] = this.ph0[i] + (this.ph1[i] - this.ph0[i]) * e;
      }
      const hi = st[i * 2 + 1];
      const lift = hi > 0.01 ? 1 + hi * 0.14 * (0.5 + 0.5 * Math.sin(now * 4 + i)) : 1;
      this._p.set(this.cur[j], this.cur[j + 1], this.cur[j + 2]);
      this._q.set(this.curQ[q], this.curQ[q + 1], this.curQ[q + 2], this.curQ[q + 3]);
      this._s.set(this.curS[j] * lift, this.curS[j + 1] * lift, 1);
      this._m.compose(this._p, this._q, this._s);
      this.mesh.setMatrixAt(i, this._m);
      // post from the ground up to the card's bottom edge
      const ph = this.curP[i];
      if (ph > 0.02) {
        this._p.set(this.cur[j], this.cur[j + 1] - this.curS[j + 1] * CARD_ASPECT * 0.5 - ph, this.cur[j + 2]);
        this._s.set(1, ph, 1);
        this._m.compose(this._p, this._q, this._s);
      } else {
        this._s.set(0, 0, 0);
        this._m.compose(this._p, this._q, this._s);
      }
      this.posts.setMatrixAt(i, this._m);
    }
    this.mesh.instanceMatrix.needsUpdate = true;
    this.posts.instanceMatrix.needsUpdate = true;
    this.stateAttr.needsUpdate = true;
  }

  positionOf(i, out = new THREE.Vector3()) {
    return out.set(this.cur[i * 3], this.cur[i * 3 + 1], this.cur[i * 3 + 2]);
  }
}

// ------------------------------------------------------------ signs (canvas sprites)
const labelCache = new Map();
function makeSignTexture(text, sub, color, kind, small) {
  const key = `${text}|${sub}|${color}|${kind}|${small}`;
  if (labelCache.has(key)) return labelCache.get(key);
  const dpr = 2;
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  const isYear = kind === 'year';
  const titleFont = isYear ? `500 36px ${WOOD_FONT}` : `700 ${small ? 34 : 40}px ${MARKER_FONT}`;
  const subFont = `400 21px ${TYPE_FONT}`;
  ctx.font = titleFont;
  const lines = wrapLines(ctx, text, 330);
  const LH = isYear ? 40 : small ? 38 : 46;
  const w1 = Math.max(...lines.map((l) => ctx.measureText(l).width));
  ctx.font = subFont; const w2 = sub ? ctx.measureText(sub).width : 0;
  const PAD = 22;
  const W = Math.ceil(Math.max(w1, w2) + PAD * 2 + 10);
  const H = Math.ceil(PAD + lines.length * LH + (sub ? 28 : 0) + PAD * 0.9);
  c.width = W * dpr; c.height = H * dpr;
  ctx.scale(dpr, dpr);
  // the board
  ctx.save();
  ctx.translate(W / 2, H / 2);
  ctx.rotate(((text.length * 37) % 7 - 3) * 0.006);
  if (isYear) {
    // a green highway sign with a white border
    ctx.fillStyle = '#1f6b3a';
    ctx.shadowColor = 'rgba(0,0,0,0.25)'; ctx.shadowBlur = 8; ctx.shadowOffsetY = 3;
    roundRect(ctx, -W / 2 + 4, -H / 2 + 4, W - 8, H - 8, 6); ctx.fill();
    ctx.shadowColor = 'transparent';
    ctx.strokeStyle = 'rgba(255,255,255,0.9)'; ctx.lineWidth = 2.5;
    roundRect(ctx, -W / 2 + 9, -H / 2 + 9, W - 18, H - 18, 4); ctx.stroke();
  } else {
    // kraft cardboard with a darker edge
    ctx.fillStyle = kind === 'word' ? '#e4d6b6' : '#c8a66e';
    ctx.shadowColor = 'rgba(0,0,0,0.28)'; ctx.shadowBlur = 10; ctx.shadowOffsetY = 4;
    ctx.fillRect(-W / 2 + 4, -H / 2 + 4, W - 8, H - 8);
    ctx.shadowColor = 'transparent';
    ctx.strokeStyle = 'rgba(70, 48, 22, 0.35)'; ctx.lineWidth = 2;
    ctx.strokeRect(-W / 2 + 4, -H / 2 + 4, W - 8, H - 8);
    // corrugation hint
    ctx.strokeStyle = 'rgba(70, 48, 22, 0.08)'; ctx.lineWidth = 1;
    for (let x = -W / 2 + 12; x < W / 2 - 8; x += 7) { ctx.beginPath(); ctx.moveTo(x, -H / 2 + 6); ctx.lineTo(x, H / 2 - 6); ctx.stroke(); }
  }
  ctx.restore();
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.font = titleFont;
  ctx.fillStyle = isYear ? '#ffffff' : '#1a1410';
  lines.forEach((l, i) => ctx.fillText(l, W / 2, PAD + LH * (i + 0.5) + 2));
  if (sub) { ctx.font = subFont; ctx.fillStyle = isYear ? 'rgba(255,255,255,0.85)' : 'rgba(40, 30, 20, 0.85)'; ctx.fillText(sub, W / 2, PAD + LH * lines.length + 14); }
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.minFilter = THREE.LinearMipmapLinearFilter;
  tex.anisotropy = 4;
  const out = { tex, w: W, h: H };
  labelCache.set(key, out);
  return out;
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}
export function clearLabelCache() { labelCache.forEach((v) => v.tex.dispose()); labelCache.clear(); }

const _v = new THREE.Vector3();

export class LabelSet {
  constructor() {
    this.group = new THREE.Group();
    this.sprites = [];
    this.alpha = 0; this.alphaTarget = 0;
    this.emphasis = null;
    this.labels = [];
  }
  set(labels) {
    this.labels = labels;
    this.sprites.forEach((s) => { this.group.remove(s); s.material.dispose(); });
    this.sprites = labels.map((l) => {
      const { tex, w, h } = makeSignTexture(l.text, l.sub, l.color, l.kind, l.small);
      const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, depthTest: false, opacity: 0, sizeAttenuation: false });
      const sp = new THREE.Sprite(mat);
      sp.position.copy(l.pos);
      sp.center.set(0.5, l.anchor === 'top' ? 1 : l.anchor === 'bottom' ? 0 : 0.5);
      sp.renderOrder = 10;
      sp.userData = { label: l, w, h, f: l.small ? 0.3 : 0.4, base: 1, show: 0, prio: l.kind === 'year' ? 2 : l.kind === 'word' ? 0 : 1 };
      this.group.add(sp);
      return sp;
    });
  }
  refresh() { this.set(this.labels); }
  update(dt, camera, viewH, viewW) {
    const k = 1 - Math.exp(-dt * 6);
    this.alpha += (this.alphaTarget - this.alpha) * k;
    const unit = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) / viewH;
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
        for (const q of kept) if (r[0] < q[2] && r[2] > q[0] && r[1] < q[3] && r[3] > q[1]) { show = false; break; }
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
      else { o.material.opacity = a * base; o.material.transparent = true; }
    }
  });
}

// ------------------------------------------------------------ final grade: Kodachrome, vignette, grain
const GradeShader = {
  uniforms: { tDiffuse: { value: null }, uTime: { value: 0 }, uAspect: { value: 1 } },
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
  fragmentShader: `varying vec2 vUv; uniform sampler2D tDiffuse; uniform float uTime; uniform float uAspect;
    float hash(vec2 p){ return fract(sin(dot(p, vec2(12.9898,78.233))) * 43758.5453); }
    void main(){
      vec4 c = texture2D(tDiffuse, vUv);
      // warm lift in the shadows, a touch less blue in the highlights
      float l = dot(c.rgb, vec3(0.299, 0.587, 0.114));
      c.rgb = mix(c.rgb, c.rgb * vec3(1.03, 0.995, 0.94), smoothstep(0.4, 1.0, l));
      c.rgb += vec3(0.035, 0.02, 0.0) * (1.0 - l) * 0.6;
      vec2 q = (vUv - 0.5) * vec2(uAspect, 1.0);
      float vig = smoothstep(1.35, 0.4, length(q));
      c.rgb *= mix(0.78, 1.0, vig);
      float g = hash(vUv * 1000.0 + fract(uTime)) - 0.5;
      c.rgb += g * 0.03;
      gl_FragColor = c;
    }`,
};

// ------------------------------------------------------------ the stage
export class Stage {
  constructor(canvas) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(PAPER);
    this.scene.fog = new THREE.FogExp2(PAPER, 0.0019);
    this.camera = new THREE.PerspectiveCamera(46, 1, 0.5, 2500);
    this.camera.position.set(0, 60, 180);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.06;
    this.controls.maxDistance = 700;
    this.controls.minDistance = 3;
    this.controls.maxPolarAngle = Math.PI * 0.52;
    this.controls.zoomSpeed = 0.8;

    // Light: a warm afternoon key and a soft sky fill, shadows following the view.
    this.hemi = new THREE.HemisphereLight(0xfff4e0, 0xcdb48c, 1.1);
    this.scene.add(this.hemi);
    this.sun = new THREE.DirectionalLight(0xffe6c4, 2.4);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(2048, 2048);
    this.sun.shadow.camera.near = 1; this.sun.shadow.camera.far = 520;
    this.sun.shadow.camera.left = -170; this.sun.shadow.camera.right = 170;
    this.sun.shadow.camera.top = 170; this.sun.shadow.camera.bottom = -170;
    this.sun.shadow.bias = -0.0006;
    this.sun.shadow.normalBias = 0.02;
    this.sun.shadow.radius = 3;
    this.scene.add(this.sun); this.scene.add(this.sun.target);

    // Ground: paper that only receives shadows, so every card and sign rests on something.
    this.ground = new THREE.Mesh(new THREE.PlaneGeometry(4000, 4000), new THREE.ShadowMaterial({ opacity: 0.22, color: 0x3a2a18 }));
    this.ground.rotation.x = -Math.PI / 2;
    this.ground.position.y = -0.02;
    this.ground.receiveShadow = true;
    this.scene.add(this.ground);

    this.composer = new EffectComposer(this.renderer);
    this.renderPass = new RenderPass(this.scene, this.camera);
    this.grade = new ShaderPass(GradeShader);
    this.composer.addPass(this.renderPass);
    this.composer.addPass(new OutputPass());
    this.composer.addPass(this.grade);

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
  }

  flyTo(pos, target, duration = 1.6, now = performance.now() / 1000) {
    this.camTween = { p0: this.camera.position.clone(), p1: pos.clone(), t0: this.controls.target.clone(), t1: target.clone(), start: now, dur: duration };
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
    // the sun follows the view so the shadow map stays sharp where you look
    const t = this.controls.target;
    this.sun.position.set(t.x + 90, t.y + 150, t.z + 70);
    this.sun.target.position.copy(t);
    this.sun.target.updateMatrixWorld();
    this.ground.position.x = t.x; this.ground.position.z = t.z;
    this.grade.uniforms.uTime.value = now;
  }

  render() { this.composer.render(); }
}
