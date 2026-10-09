import * as THREE from 'three';
import { ERA_BY_KEY } from './eras.js';

// Deterministic PRNG so layouts are stable between visits.
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function gauss(rnd) {
  // Box–Muller
  const u = 1 - rnd(), v = rnd();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

const UP = new THREE.Vector3(0, 1, 0);

const radialCache = new Map();
function radialTexture(color) {
  if (radialCache.has(color)) return radialCache.get(color);
  const c = document.createElement('canvas'); c.width = c.height = 256;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
  g.addColorStop(0, color); g.addColorStop(0.25, color + 'aa'); g.addColorStop(0.6, color + '22'); g.addColorStop(1, color + '00');
  ctx.fillStyle = g; ctx.fillRect(0, 0, 256, 256);
  const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
  radialCache.set(color, tex);
  return tex;
}

/**
 * Each layout returns:
 *   targets: Float32Array(N*3) positions, scales: Float32Array(N*3)
 *   labels: [{ text, sub, pos: Vector3, color, kind, albumIndex }]
 *   camera: { pos: Vector3, target: Vector3 }
 *   guide: THREE.Group (static geometry: road, rings, dust ...)
 *   focus(song) -> { pos, target } camera vantage for a song
 *   albumFocus(album) -> { pos, target }
 */

// ---------------------------------------------------------------- HIGHWAY
export function highwayLayout(catalog) {
  const { songs, albums } = catalog;
  const N = songs.length;
  const targets = new Float32Array(N * 3);
  const scales = new Float32Array(N * 3);

  const studio = albums.filter((a) => a.kind === 'album').slice().sort((a, b) => a.timelineYear - b.timelineYear);
  const TRACK_STEP = 1.25;
  const slots = [];
  let s = 12;
  let lastYear = null;
  studio.forEach((a) => {
    const gap = lastYear == null ? 0 : 6 + 9 * Math.sqrt(Math.max(0, a.timelineYear - lastYear));
    s += gap;
    const width = a.tracks.length * TRACK_STEP;
    slots.push({ album: a, start: s, width, center: s + width / 2 });
    s += width;
    lastYear = a.timelineYear;
  });
  const L = s + 24;

  // A meandering road, arc-length parametrised via CatmullRom sampling.
  const pts = [];
  const STEPS = 160;
  for (let i = 0; i <= STEPS; i++) {
    const d = (i / STEPS) * L;
    pts.push(new THREE.Vector3(
      d - L / 2,
      4 * Math.sin(d * 0.012) + 2 * Math.sin(d * 0.031 + 1),
      36 * Math.sin(d * 0.0135) + 14 * Math.sin(d * 0.0052 + 2),
    ));
  }
  const curve = new THREE.CatmullRomCurve3(pts, false, 'centripetal', 0.5);
  const curveLen = curve.getLength();
  const at = (dist) => curve.getPointAt(THREE.MathUtils.clamp(dist / L, 0, 1));
  const tangentAt = (dist) => curve.getTangentAt(THREE.MathUtils.clamp(dist / L, 0, 1));
  const frame = (dist) => {
    const p = at(dist);
    const t = tangentAt(dist);
    const n = new THREE.Vector3().crossVectors(t, UP).normalize();
    return { p, t, n };
  };

  // Year -> road distance, piecewise linear through album centres.
  const knots = slots.map((sl) => [sl.album.timelineYear, sl.center]);
  knots.unshift([1961.0, 2]);
  knots.push([2021.0, L - 6]);
  const distForYear = (y) => {
    for (let i = 0; i < knots.length - 1; i++) {
      const [y0, d0] = knots[i], [y1, d1] = knots[i + 1];
      if (y <= y1) return d0 + ((y - y0) / Math.max(1e-6, y1 - y0)) * (d1 - d0);
    }
    return L - 6;
  };

  const heightFor = (song) => 1.2 + 4.2 * Math.sqrt(song.minutes);

  const labels = [];
  slots.forEach((sl) => {
    sl.album.tracks.forEach((song, k) => {
      const d = sl.start + (k + 0.5) * TRACK_STEP;
      const { p, n } = frame(d);
      const h = heightFor(song);
      const i = song.index * 3;
      targets[i] = p.x + n.x * 2.6;
      targets[i + 1] = p.y + h / 2;
      targets[i + 2] = p.z + n.z * 2.6;
      scales[i] = 0.62; scales[i + 1] = h; scales[i + 2] = 0.62;
    });
    const { p, n } = frame(sl.center);
    labels.push({
      text: sl.album.title,
      sub: sl.album.recorded ? `recorded ${sl.album.recorded} · released ${sl.album.year}` : String(sl.album.year),
      pos: new THREE.Vector3(p.x + n.x * 8.5, p.y + 1.2, p.z + n.z * 8.5),
      color: ERA_BY_KEY[sl.album.era].color,
      kind: 'album',
      albumIndex: sl.album.index,
      anchor: 'bottom',
    });
  });

  // Non-album songs on the far shoulder, spread so same-year songs don't stack.
  const beyond = albums.find((a) => a.kind === 'group');
  const byYear = new Map();
  beyond.tracks.forEach((song) => {
    const arr = byYear.get(song.year) || [];
    arr.push(song); byYear.set(song.year, arr);
  });
  byYear.forEach((arr, year) => {
    const base = distForYear(year + 0.5);
    arr.forEach((song, k) => {
      const d = base + (k - (arr.length - 1) / 2) * 1.15;
      const { p, n } = frame(d);
      const h = heightFor(song) * 0.75;
      const i = song.index * 3;
      targets[i] = p.x - n.x * 2.6;
      targets[i + 1] = p.y + h / 2;
      targets[i + 2] = p.z - n.z * 2.6;
      scales[i] = 0.42; scales[i + 1] = h; scales[i + 2] = 0.42;
    });
  });

  // Decade milestones on the road itself.
  for (let y = 1965; y <= 2020; y += 5) {
    const { p, n } = frame(distForYear(y));
    labels.push({
      text: String(y), sub: '', pos: new THREE.Vector3(p.x - n.x * 7, p.y + 0.6, p.z - n.z * 7),
      color: '#8f8aa3', kind: 'year', small: true,
    });
  }

  // Guide geometry: the road, its centre line, a ground haze.
  const guide = new THREE.Group();
  const tube = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 700, 1.9, 6, false),
    new THREE.MeshBasicMaterial({ color: 0x1b1530, transparent: true, opacity: 0.95, depthWrite: false }),
  );
  tube.scale.y = 0.08;
  tube.userData.baseOpacity = 0.95;
  guide.add(tube);
  const linePts = curve.getSpacedPoints(900).map((p) => p.clone().setY(p.y + 0.18));
  const line = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(linePts),
    new THREE.LineBasicMaterial({ color: 0xffd9a0, transparent: true, opacity: 0.55 }),
  );
  line.userData.baseOpacity = 0.55;
  guide.add(line);
  // Shoulder lines
  [-2.6, 2.6].forEach((side) => {
    const sp = [];
    for (let i = 0; i <= 900; i++) {
      const d = (i / 900) * L; const { p, n } = frame(d);
      sp.push(new THREE.Vector3(p.x + n.x * side, p.y + 0.05, p.z + n.z * side));
    }
    const sl = new THREE.Line(new THREE.BufferGeometry().setFromPoints(sp),
      new THREE.LineBasicMaterial({ color: 0x5a4d7a, transparent: true, opacity: 0.35 }));
    sl.userData.baseOpacity = 0.35;
    guide.add(sl);
  });
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(L * 1.6, 420),
    new THREE.ShaderMaterial({
      transparent: true, depthWrite: false,
      uniforms: { uOpacity: { value: 1 } },
      vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
      fragmentShader: `varying vec2 vUv; uniform float uOpacity;
        void main(){ vec2 c = vUv - 0.5; float r = length(c * vec2(1.0, 1.6)); float a = smoothstep(0.55, 0.05, r);
        gl_FragColor = vec4(vec3(0.075, 0.06, 0.11), a * 0.9 * uOpacity); }`,
    }),
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -1.5;
  ground.userData.baseOpacity = 1;
  guide.add(ground);

  const start = frame(0);
  const camera = {
    pos: new THREE.Vector3(start.p.x - 70, 48, start.p.z + 95),
    target: new THREE.Vector3(start.p.x + 60, 2, start.p.z - 10),
  };

  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(targets[i], Math.min(targets[i + 1], 6), targets[i + 2]);
    const d = song.nonAlbum ? distForYear(song.year + 0.5) : slots.find((sl) => sl.album === song.album).center;
    const { t, n } = frame(d);
    const side = song.nonAlbum ? -1 : 1;
    const pos = target.clone().add(n.clone().multiplyScalar(side * 26)).add(t.clone().multiplyScalar(-14)).add(new THREE.Vector3(0, 16, 0));
    return { pos, target };
  };
  const albumFocus = (album) => {
    const sl = slots.find((x) => x.album === album);
    if (!sl) return { pos: camera.pos, target: camera.target };
    const { p, t, n } = frame(sl.center);
    const target = p.clone().add(n.clone().multiplyScalar(2.6)).setY(p.y + 4);
    const dist = 22 + sl.width * 0.9;
    const pos = target.clone().add(n.clone().multiplyScalar(dist)).add(t.clone().multiplyScalar(-dist * 0.45)).add(new THREE.Vector3(0, dist * 0.55, 0));
    return { pos, target };
  };

  return { name: 'highway', targets, scales, labels, camera, guide, focus, albumFocus, curveLen };
}

// ---------------------------------------------------------------- RECORDS
export function recordsLayout(catalog) {
  const { songs, albums } = catalog;
  const N = songs.length;
  const targets = new Float32Array(N * 3);
  const scales = new Float32Array(N * 3);
  const COLS = 8, CW = 21, CH = 23, R = 7;
  const labels = [];
  const guide = new THREE.Group();
  const rows = Math.ceil(albums.length / COLS);
  const centres = [];

  albums.forEach((album, j) => {
    const col = j % COLS, row = Math.floor(j / COLS);
    const cx = (col - (COLS - 1) / 2) * CW;
    const cy = ((rows - 1) / 2 - row) * CH + 2;
    const c = new THREE.Vector3(cx, cy, 0);
    centres.push(c);
    const color = album.era ? ERA_BY_KEY[album.era].color : '#8f8aa3';

    if (album.kind === 'album') {
      let cum = 0;
      album.tracks.forEach((song) => {
        const frac = (cum + song.sec / 2) / album.duration;
        cum += song.sec;
        const th = Math.PI / 2 - frac * Math.PI * 2;
        const r = 0.3 + 0.42 * Math.sqrt(song.minutes);
        const i = song.index * 3;
        targets[i] = cx + R * Math.cos(th);
        targets[i + 1] = cy + R * Math.sin(th);
        targets[i + 2] = 0;
        scales[i] = r; scales[i + 1] = r; scales[i + 2] = r;
      });
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(R, 0.06, 6, 160),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.45, depthWrite: false }),
      );
      ring.position.copy(c);
      ring.userData.baseOpacity = 0.45;
      guide.add(ring);
      const disc = new THREE.Mesh(
        new THREE.CircleGeometry(R - 0.4, 64),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.045, depthWrite: false }),
      );
      disc.position.copy(c).setZ(-0.5);
      disc.userData.baseOpacity = 0.045;
      guide.add(disc);
      [0.78, 0.56].forEach((f) => {
        const groove = new THREE.Mesh(
          new THREE.TorusGeometry(R * f, 0.035, 4, 120),
          new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.16, depthWrite: false }),
        );
        groove.position.copy(c); groove.userData.baseOpacity = 0.16; guide.add(groove);
      });
      const hole = new THREE.Mesh(
        new THREE.CircleGeometry(0.5, 24),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.6, depthWrite: false }),
      );
      hole.position.copy(c);
      hole.userData.baseOpacity = 0.6;
      guide.add(hole);
      labels.push({
        text: album.title, sub: `${album.year} · ${album.tracks.length} tracks`,
        pos: new THREE.Vector3(cx, cy - R - 1.6, 0), color, kind: 'album', albumIndex: album.index, anchor: 'top',
      });
    } else {
      // Non-album songs: a phyllotaxis pile, oldest in the centre.
      album.tracks.forEach((song, k) => {
        const rr = 0.95 * Math.sqrt(k + 0.5);
        const th = k * 2.399963;
        const r = 0.28 + 0.36 * Math.sqrt(song.minutes);
        const i = song.index * 3;
        targets[i] = cx + rr * Math.cos(th);
        targets[i + 1] = cy + rr * Math.sin(th);
        targets[i + 2] = 0;
        scales[i] = r; scales[i + 1] = r; scales[i + 2] = r;
      });
      labels.push({
        text: album.title, sub: `${album.tracks.length} singles, B-sides & outtakes`,
        pos: new THREE.Vector3(cx, cy - R - 1.6, 0), color, kind: 'album', albumIndex: album.index, anchor: 'top',
      });
    }
  });

  const camera = { pos: new THREE.Vector3(0, 4, 150), target: new THREE.Vector3(0, 2, 0) };
  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(targets[i], targets[i + 1], 0);
    return { pos: target.clone().add(new THREE.Vector3(0, -1.5, 22)), target };
  };
  const albumFocus = (album) => {
    const c = centres[album.index];
    return { pos: c.clone().add(new THREE.Vector3(0, -2, 34)), target: c.clone().add(new THREE.Vector3(0, -1.5, 0)) };
  };
  return { name: 'records', targets, scales, labels, camera, guide, focus, albumFocus };
}

// ---------------------------------------------------------------- GALAXY
export function galaxyLayout(catalog) {
  const { songs, albums } = catalog;
  const N = songs.length;
  const targets = new Float32Array(N * 3);
  const scales = new Float32Array(N * 3);
  const rnd = mulberry32(1941);

  const studio = albums.filter((a) => a.kind === 'album').slice().sort((a, b) => a.timelineYear - b.timelineYear);
  // Spiral parameter: blend of time and rank so the crowded sixties get room.
  const rankOf = new Map(studio.map((a, i) => [a, i / (studio.length - 1)]));
  const tauForYear = (y) => {
    const yf = THREE.MathUtils.clamp((y - 1961) / 60, 0, 1);
    // rank interpolation through album years
    let rf = yf;
    for (let i = 0; i < studio.length - 1; i++) {
      const a = studio[i], b = studio[i + 1];
      if (y >= a.timelineYear && y <= b.timelineYear) {
        rf = THREE.MathUtils.lerp(rankOf.get(a), rankOf.get(b), (y - a.timelineYear) / (b.timelineYear - a.timelineYear));
        break;
      }
    }
    if (y < studio[0].timelineYear) rf = 0; else if (y > studio[studio.length - 1].timelineYear) rf = 1;
    return 0.35 * yf + 0.65 * rf;
  };
  const TURNS = 2.35;
  const armPoint = (tau, out = new THREE.Vector3()) => {
    const phi = tau * Math.PI * 2 * TURNS;
    const rho = 30 + 76 * tau;
    return out.set(rho * Math.cos(phi), 0, rho * Math.sin(phi));
  };

  const labels = [];
  studio.forEach((album) => {
    const tau = tauForYear(album.timelineYear);
    const c = armPoint(tau);
    const spread = 2.4 + 1.6 * Math.sqrt(album.tracks.length / 10);
    album.tracks.forEach((song) => {
      const i = song.index * 3;
      const r = 0.3 + 0.4 * Math.sqrt(song.minutes);
      targets[i] = c.x + gauss(rnd) * spread;
      targets[i + 1] = gauss(rnd) * 1.3;
      targets[i + 2] = c.z + gauss(rnd) * spread;
      scales[i] = r; scales[i + 1] = r; scales[i + 2] = r;
    });
    labels.push({
      text: album.title, sub: String(album.year), pos: c.clone().setY(4.5 + spread * 0.4),
      color: ERA_BY_KEY[album.era].color, kind: 'album', albumIndex: album.index, anchor: 'bottom',
    });
  });
  const beyond = albums.find((a) => a.kind === 'group');
  beyond.tracks.forEach((song) => {
    const tau = tauForYear(song.timelineYear + (rnd() - 0.5) * 0.6);
    const c = armPoint(tau);
    const i = song.index * 3;
    const r = 0.24 + 0.3 * Math.sqrt(song.minutes);
    // halo: pushed off the arm, above and below the plane
    const off = 6 + 7 * rnd();
    const ang = rnd() * Math.PI * 2;
    targets[i] = c.x + Math.cos(ang) * off;
    targets[i + 1] = gauss(rnd) * 3.2;
    targets[i + 2] = c.z + Math.sin(ang) * off;
    scales[i] = r; scales[i + 1] = r; scales[i + 2] = r;
  });

  // Dust: thousands of faint particles along the arm, coloured by era.
  const DUST = 16000;
  const dpos = new Float32Array(DUST * 3);
  const dcol = new Float32Array(DUST * 3);
  const dsize = new Float32Array(DUST);
  const tmp = new THREE.Color();
  const eraForTau = (tau) => {
    // nearest album era by tau
    let best = studio[0], bd = Infinity;
    studio.forEach((a) => { const d = Math.abs(tauForYear(a.timelineYear) - tau); if (d < bd) { bd = d; best = a; } });
    return best.era;
  };
  const eraCache = new Map();
  for (let k = 0; k < DUST; k++) {
    const tau = Math.pow(rnd(), 0.8);
    const c = armPoint(tau);
    const spread = 4.0 + 3.0 * tau;
    dpos[k * 3] = c.x + gauss(rnd) * spread;
    dpos[k * 3 + 1] = gauss(rnd) * 1.4;
    dpos[k * 3 + 2] = c.z + gauss(rnd) * spread;
    const q = Math.round(tau * 60);
    let era = eraCache.get(q); if (!era) { era = eraForTau(tau); eraCache.set(q, era); }
    tmp.set(ERA_BY_KEY[era].color);
    dcol[k * 3] = tmp.r; dcol[k * 3 + 1] = tmp.g; dcol[k * 3 + 2] = tmp.b;
    dsize[k] = 0.8 + Math.pow(rnd(), 2) * 3.2;
  }
  const dustGeo = new THREE.BufferGeometry();
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dpos, 3));
  dustGeo.setAttribute('color', new THREE.BufferAttribute(dcol, 3));
  dustGeo.setAttribute('aSize', new THREE.BufferAttribute(dsize, 1));
  const dustMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, vertexColors: true,
    uniforms: { uOpacity: { value: 1 }, uPixelRatio: { value: 1 } },
    vertexShader: `attribute float aSize; varying vec3 vColor; uniform float uPixelRatio;
      void main(){ vColor = color; vec4 mv = modelViewMatrix * vec4(position,1.0);
      gl_PointSize = aSize * uPixelRatio * 160.0 / -mv.z; gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `varying vec3 vColor; uniform float uOpacity;
      void main(){ vec2 c = gl_PointCoord - 0.5; float d = length(c); if (d > 0.5) discard;
      float a = smoothstep(0.5, 0.0, d); a *= a; gl_FragColor = vec4(vColor * 0.6, a * 0.3 * uOpacity); }`,
  });
  const dust = new THREE.Points(dustGeo, dustMat);
  dust.userData.baseOpacity = 1;
  const guide = new THREE.Group();
  guide.add(dust);
  // A warm core where the spiral begins: Hibbing, 1941.
  const core = new THREE.Sprite(new THREE.SpriteMaterial({ map: radialTexture('#f2d479'), transparent: true, opacity: 0.42, depthWrite: false, blending: THREE.AdditiveBlending }));
  core.scale.set(34, 34, 1);
  core.userData.baseOpacity = 0.42;
  guide.add(core);
  const haze = new THREE.Sprite(new THREE.SpriteMaterial({ map: radialTexture('#3a2a5a'), transparent: true, opacity: 0.5, depthWrite: false, blending: THREE.AdditiveBlending }));
  haze.scale.set(260, 260, 1);
  haze.userData.baseOpacity = 0.5;
  guide.add(haze);

  const camera = { pos: new THREE.Vector3(-34, 98, 142), target: new THREE.Vector3(6, 0, 4) };
  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(targets[i], targets[i + 1], targets[i + 2]);
    const dir = target.clone().setY(0).normalize();
    const pos = target.clone().add(dir.multiplyScalar(14)).add(new THREE.Vector3(0, 12, 0));
    return { pos, target };
  };
  const albumFocus = (album) => {
    if (album.kind !== 'album') return { pos: camera.pos, target: camera.target };
    const target = armPoint(tauForYear(album.timelineYear));
    const dir = target.clone().setY(0).normalize();
    const pos = target.clone().add(dir.multiplyScalar(26)).add(new THREE.Vector3(0, 22, 0));
    return { pos, target };
  };
  return { name: 'galaxy', targets, scales, labels, camera, guide, focus, albumFocus };
}

// ---------------------------------------------------------------- LEXICON
export function lexiconLayout(catalog) {
  const { songs, topWords } = catalog;
  const N = songs.length;
  const targets = new Float32Array(N * 3);
  const scales = new Float32Array(N * 3);
  const K = topWords.length;
  const RING = 52;
  const labels = [];
  const guide = new THREE.Group();
  const hubs = topWords.map((w, i) => {
    const th = i * (Math.PI * 2) / K - Math.PI / 2;
    return new THREE.Vector3(RING * Math.cos(th), 0, RING * Math.sin(th));
  });
  const counts = new Array(K).fill(0);
  const rest = [];
  songs.forEach((song) => {
    const r = 0.26 + 0.34 * Math.sqrt(song.minutes);
    const i = song.index * 3;
    scales[i] = r; scales[i + 1] = r; scales[i + 2] = r;
    if (song.wordHub >= 0) {
      const k = counts[song.wordHub]++;
      const h = hubs[song.wordHub];
      const rr = 1.5 * Math.sqrt(k + 0.6);
      const th = k * 2.399963;
      targets[i] = h.x + rr * Math.cos(th);
      targets[i + 1] = 0;
      targets[i + 2] = h.z + rr * Math.sin(th);
    } else rest.push(song);
  });
  rest.forEach((song, k) => {
    const rr = 1.35 * Math.sqrt(k + 0.6);
    const th = k * 2.399963;
    const i = song.index * 3;
    targets[i] = rr * Math.cos(th);
    targets[i + 1] = -0.5;
    targets[i + 2] = rr * Math.sin(th);
  });
  topWords.forEach((w, i) => {
    const h = hubs[i];
    const radius = 1.5 * Math.sqrt(counts[i] + 1) + 1.2;
    labels.push({
      text: `“${w.word}”`, sub: `${counts[i]} titles`, pos: h.clone().setY(1.5).add(new THREE.Vector3(0, 0, -(1.5 * Math.sqrt(counts[i] + 1) + 2.4))), color: '#f6efe0', kind: 'word', anchor: 'bottom',
    });
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(radius, radius + 0.12, 96),
      new THREE.MeshBasicMaterial({ color: 0xf6efe0, transparent: true, opacity: 0.18, side: THREE.DoubleSide, depthWrite: false }),
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.copy(h).setY(-0.6);
    ring.userData.baseOpacity = 0.18;
    guide.add(ring);
  });
  labels.push({ text: 'everything else', sub: `${rest.length} titles`, pos: new THREE.Vector3(0, 1.5, -(1.35 * Math.sqrt(rest.length + 1) + 3)), color: '#8f8aa3', kind: 'word', anchor: 'bottom' });
  const restR = 1.35 * Math.sqrt(rest.length + 1) + 1.2;
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(restR, restR + 0.12, 128),
    new THREE.MeshBasicMaterial({ color: 0x8f8aa3, transparent: true, opacity: 0.12, side: THREE.DoubleSide, depthWrite: false }),
  );
  ring.rotation.x = -Math.PI / 2; ring.position.y = -0.6; ring.userData.baseOpacity = 0.12;
  guide.add(ring);

  const camera = { pos: new THREE.Vector3(0, 125, 95), target: new THREE.Vector3(0, 0, 0) };
  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(targets[i], targets[i + 1], targets[i + 2]);
    return { pos: target.clone().add(new THREE.Vector3(0, 24, 19)), target };
  };
  const albumFocus = () => ({ pos: camera.pos, target: camera.target });
  return { name: 'lexicon', targets, scales, labels, camera, guide, focus, albumFocus };
}

export function buildLayouts(catalog) {
  return {
    highway: highwayLayout(catalog),
    records: recordsLayout(catalog),
    galaxy: galaxyLayout(catalog),
    lexicon: lexiconLayout(catalog),
  };
}
