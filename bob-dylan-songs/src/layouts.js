import * as THREE from 'three';
import { ERA_BY_KEY, cardTint } from './eras.js';
import { CARD_ASPECT, TYPE_FONT, WOOD_FONT } from './scene.js';

function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const UP = new THREE.Vector3(0, 1, 0);
const _q = new THREE.Quaternion(), _e = new THREE.Euler();

/** Card width for a song: a 5x3 index card that grows with the song's length. */
export const cardW = (song) => 1.35 + 0.72 * Math.sqrt(song.minutes);

function setCard(arrs, song, x, y, z, w, euler, post = 0) {
  const i = song.index;
  arrs.targets[i * 3] = x; arrs.targets[i * 3 + 1] = y; arrs.targets[i * 3 + 2] = z;
  arrs.scales[i * 3] = w; arrs.scales[i * 3 + 1] = w; arrs.scales[i * 3 + 2] = 1;
  _q.setFromEuler(euler);
  arrs.quats[i * 4] = _q.x; arrs.quats[i * 4 + 1] = _q.y; arrs.quats[i * 4 + 2] = _q.z; arrs.quats[i * 4 + 3] = _q.w;
  arrs.posts[i] = post;
}
function arrays(n) {
  return { targets: new Float32Array(n * 3), scales: new Float32Array(n * 3), quats: new Float32Array(n * 4), posts: new Float32Array(n) };
}

/**
 * Each layout returns: targets, scales, quats, posts (per song), labels, camera, guide,
 * focus(song), albumFocus(album), and optionally pickables (meshes that select an album).
 */

// ---------------------------------------------------------------- HIGHWAY
export function highwayLayout(catalog) {
  const { songs, albums } = catalog;
  const A = arrays(songs.length);
  const studio = albums.filter((a) => a.kind === 'album').slice().sort((a, b) => a.timelineYear - b.timelineYear);
  const TRACK_STEP = 2.5;
  const slots = [];
  let s = 14;
  let lastYear = null;
  studio.forEach((a) => {
    const gap = lastYear == null ? 0 : 8 + 10 * Math.sqrt(Math.max(0, a.timelineYear - lastYear));
    s += gap;
    const width = a.tracks.length * TRACK_STEP;
    slots.push({ album: a, start: s, width, center: s + width / 2 });
    s += width;
    lastYear = a.timelineYear;
  });
  const L = s + 30;

  const pts = [];
  const STEPS = 200;
  for (let i = 0; i <= STEPS; i++) {
    const d = (i / STEPS) * L;
    pts.push(new THREE.Vector3(d - L / 2, 0, 44 * Math.sin(d * 0.0075) + 16 * Math.sin(d * 0.0031 + 2)));
  }
  const curve = new THREE.CatmullRomCurve3(pts, false, 'centripetal', 0.5);
  const at = (dist) => curve.getPointAt(THREE.MathUtils.clamp(dist / L, 0, 1));
  const tangentAt = (dist) => curve.getTangentAt(THREE.MathUtils.clamp(dist / L, 0, 1));
  const frame = (dist) => {
    const p = at(dist);
    const t = tangentAt(dist);
    const n = new THREE.Vector3().crossVectors(t, UP).normalize();
    return { p, t, n };
  };
  const knots = slots.map((sl) => [sl.album.timelineYear, sl.center]);
  knots.unshift([1961.0, 2]);
  knots.push([2021.0, L - 8]);
  const distForYear = (y) => {
    for (let i = 0; i < knots.length - 1; i++) {
      const [y0, d0] = knots[i], [y1, d1] = knots[i + 1];
      if (y <= y1) return d0 + ((y - y0) / Math.max(1e-6, y1 - y0)) * (d1 - d0);
    }
    return L - 8;
  };
  const postFor = (song) => 0.6 + 3.6 * Math.sqrt(song.minutes);
  const yawFacing = (dir) => Math.atan2(dir.x, dir.z); // plane normal (+z) toward dir

  const labels = [];
  slots.forEach((sl) => {
    sl.album.tracks.forEach((song, k) => {
      const d = sl.start + (k + 0.5) * TRACK_STEP;
      const { p, t, n } = frame(d);
      const w = cardW(song);
      const post = postFor(song);
      const face = n.clone().negate().lerp(t.clone().negate(), 0.35).normalize(); // toward the road, turned a little to the oncoming driver
      setCard(A, song, p.x + n.x * 4.2, post + w * CARD_ASPECT / 2, p.z + n.z * 4.2, w, _e.set(0, yawFacing(face), 0), post);
    });
    const { p, n } = frame(sl.center);
    labels.push({
      text: sl.album.title,
      sub: sl.album.recorded ? `recorded ${sl.album.recorded} · released ${sl.album.year}` : String(sl.album.year),
      pos: new THREE.Vector3(p.x + n.x * 11, p.y + 0.4, p.z + n.z * 11),
      color: ERA_BY_KEY[sl.album.era].color, kind: 'album', albumIndex: sl.album.index, anchor: 'bottom',
    });
  });
  const beyond = albums.find((a) => a.kind === 'group');
  const byYear = new Map();
  beyond.tracks.forEach((song) => { const arr = byYear.get(song.year) || []; arr.push(song); byYear.set(song.year, arr); });
  byYear.forEach((arr, year) => {
    const base = distForYear(year + 0.5);
    arr.forEach((song, k) => {
      const d = base + (k - (arr.length - 1) / 2) * 2.2;
      const { p, t, n } = frame(d);
      const w = cardW(song) * 0.78;
      const post = postFor(song) * 0.7;
      const face = n.clone().lerp(t.clone().negate(), 0.35).normalize();
      setCard(A, song, p.x - n.x * 4.2, post + w * CARD_ASPECT / 2, p.z - n.z * 4.2, w, _e.set(0, yawFacing(face), 0), post);
    });
  });
  for (let y = 1965; y <= 2020; y += 5) {
    const { p, n } = frame(distForYear(y));
    labels.push({ text: String(y), sub: y % 10 === 0 ? 'HIGHWAY 61' : '', pos: new THREE.Vector3(p.x - n.x * 9, p.y + 0.3, p.z - n.z * 9), color: '#fff', kind: 'year', small: true });
  }

  // Guide: asphalt ribbon, yellow centre dashes, white edge lines, gravel shoulders.
  const guide = new THREE.Group();
  const ribbon = (halfW, y, color, roughness = 0.95, step = 1.2) => {
    const pos = [], idx = [];
    const n = Math.ceil(L / step);
    for (let i = 0; i <= n; i++) {
      const { p, n: nn } = frame((i / n) * L);
      pos.push(p.x + nn.x * halfW, y, p.z + nn.z * halfW, p.x - nn.x * halfW, y, p.z - nn.z * halfW);
      if (i < n) { const b = i * 2; idx.push(b, b + 1, b + 2, b + 1, b + 3, b + 2); }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setIndex(idx); g.computeVertexNormals();
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color, roughness, metalness: 0, transparent: true, side: THREE.DoubleSide }));
    m.receiveShadow = true;
    m.userData.baseOpacity = 1;
    return m;
  };
  const shoulder = ribbon(7.2, 0.0, 0xc9b58f); guide.add(shoulder);
  const asphalt = ribbon(5.0, 0.03, 0x3b3733); guide.add(asphalt);
  [-4.7, 4.7].forEach((side) => {
    const edge = ribbon(0.14, 0.05, 0xe9e2d2, 0.9, 1.2);
    edge.geometry.translate(0, 0, 0); // edge line built on centre; offset below
    // rebuild at offset: cheaper to just translate attribute along normals
    const arr = edge.geometry.attributes.position.array;
    const n = arr.length / 6;
    for (let i = 0; i < n; i++) {
      const d = (i / (n - 1)) * L; const { n: nn } = frame(d);
      for (let k = 0; k < 2; k++) { arr[(i * 2 + k) * 3] += nn.x * side; arr[(i * 2 + k) * 3 + 2] += nn.z * side; }
    }
    edge.geometry.attributes.position.needsUpdate = true;
    guide.add(edge);
  });
  {
    // dashed yellow centre line
    const pos = [], idx = [];
    let v = 0;
    for (let d = 0; d < L; d += 7) {
      for (const dd of [d, d + 3.2]) {
        const { p, n } = frame(Math.min(L, dd));
        pos.push(p.x + n.x * 0.16, 0.06, p.z + n.z * 0.16, p.x - n.x * 0.16, 0.06, p.z - n.z * 0.16);
      }
      idx.push(v, v + 1, v + 2, v + 1, v + 3, v + 2); v += 4;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); g.setIndex(idx); g.computeVertexNormals();
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: 0xe0b32a, roughness: 0.8, transparent: true, side: THREE.DoubleSide }));
    m.userData.baseOpacity = 1;
    guide.add(m);
  }

  const start = frame(0);
  const camera = { pos: new THREE.Vector3(start.p.x - 26, 14, start.p.z + 46), target: new THREE.Vector3(start.p.x + 62, 2, start.p.z - 4) };
  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(A.targets[i], A.targets[i + 1], A.targets[i + 2]);
    const d = song.nonAlbum ? distForYear(song.year + 0.5) : slots.find((sl) => sl.album === song.album).center;
    const { t, n } = frame(d);
    const side = song.nonAlbum ? 1 : -1;
    const pos = target.clone().add(n.clone().multiplyScalar(side * 11)).add(t.clone().multiplyScalar(-6)).add(new THREE.Vector3(0, 5, 0));
    return { pos, target };
  };
  const albumFocus = (album) => {
    const sl = slots.find((x) => x.album === album);
    if (!sl) return { pos: camera.pos, target: camera.target };
    const { p, t, n } = frame(sl.center);
    const target = p.clone().add(n.clone().multiplyScalar(4)).setY(p.y + 5);
    const dist = 16 + sl.width * 0.55;
    const pos = target.clone().add(n.clone().multiplyScalar(-dist * 0.9)).add(t.clone().multiplyScalar(-dist * 0.5)).add(new THREE.Vector3(0, dist * 0.45, 0));
    return { pos, target };
  };
  return { name: 'highway', ...A, labels, camera, guide, focus, albumFocus, road: { frame, length: L, slots, distForYear } };
}

// ---------------------------------------------------------------- YEARS
export function yearsLayout(catalog) {
  const { songs, albums } = catalog;
  const A = arrays(songs.length);
  const rnd = mulberry32(1941);
  const COLW = 2.35, ROWH = 0.82;
  const xFor = (y) => (y - 1990.5) * COLW;
  const byYear = new Map();
  const ordered = songs.slice().sort((a, b) => a.year - b.year || a.albumIndex - b.albumIndex || a.n - b.n);
  ordered.forEach((s) => { const arr = byYear.get(s.year) || []; arr.push(s); byYear.set(s.year, arr); });
  const albumCentres = new Map();
  byYear.forEach((arr, year) => {
    arr.forEach((song, k) => {
      const w = Math.min(2.25, 1.55 + 0.35 * Math.sqrt(song.minutes));
      const x = xFor(year) + (rnd() - 0.5) * 0.25;
      const y = 0.5 + k * ROWH + w * CARD_ASPECT / 2;
      setCard(A, song, x, y, (rnd() - 0.5) * 0.3 - (k % 2) * 0.12, w, _e.set(0, 0, (rnd() - 0.5) * 0.12));
      const c = albumCentres.get(song.album) || { x: 0, y: 0, n: 0 };
      c.x += x; c.y += y; c.n += 1; albumCentres.set(song.album, c);
    });
  });
  const labels = [];
  for (let y = 1965; y <= 2020; y += 5) {
    labels.push({ text: String(y), sub: `${(byYear.get(y) || []).length} songs`, pos: new THREE.Vector3(xFor(y), -1.2, 0), color: '#fff', kind: 'year', small: true, anchor: 'top' });
  }
  const guide = new THREE.Group();
  const rail = new THREE.Mesh(new THREE.BoxGeometry(60 * COLW + 6, 0.35, 1.2), new THREE.MeshStandardMaterial({ color: 0x4a3a2c, roughness: 0.9, transparent: true }));
  rail.position.set(0, 0.15, -0.2); rail.receiveShadow = true; rail.castShadow = true; rail.userData.baseOpacity = 1;
  guide.add(rail);
  for (let y = 1961; y <= 2020; y++) {
    const tick = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.6, 0.08), new THREE.MeshStandardMaterial({ color: 0xe9e2d2, roughness: 0.9, transparent: true }));
    tick.position.set(xFor(y), 0.33, 0.5); tick.userData.baseOpacity = 1;
    guide.add(tick);
  }
  const camera = { pos: new THREE.Vector3(0, 26, 132), target: new THREE.Vector3(0, 12, 0) };
  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(A.targets[i], A.targets[i + 1], A.targets[i + 2]);
    return { pos: target.clone().add(new THREE.Vector3(0, 1.5, 15)), target };
  };
  const albumFocus = (album) => {
    const c = albumCentres.get(album);
    if (!c) return { pos: camera.pos, target: camera.target };
    const target = new THREE.Vector3(c.x / c.n, c.y / c.n, 0);
    return { pos: target.clone().add(new THREE.Vector3(0, 4, 34)), target };
  };
  return { name: 'years', ...A, labels, camera, guide, focus, albumFocus };
}

// ---------------------------------------------------------------- RECORDS
function labelTexture(album) {
  const c = document.createElement('canvas'); c.width = c.height = 512;
  const ctx = c.getContext('2d');
  const color = album.era ? ERA_BY_KEY[album.era].color : '#8a7f70';
  ctx.fillStyle = cardTint(color, '#f1e7cf', 0.55);
  ctx.beginPath(); ctx.arc(256, 256, 256, 0, Math.PI * 2); ctx.fill();
  ctx.strokeStyle = 'rgba(30, 24, 18, 0.35)'; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.arc(256, 256, 246, 0, Math.PI * 2); ctx.stroke();
  ctx.beginPath(); ctx.arc(256, 256, 232, 0, Math.PI * 2); ctx.stroke();
  // spindle hole
  ctx.fillStyle = '#e9dec4'; ctx.beginPath(); ctx.arc(256, 256, 24, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#1e1915'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.font = `500 30px ${WOOD_FONT}`;
  ctx.fillText('COLUMBIA', 256, 112);
  ctx.font = `400 22px ${TYPE_FONT}`;
  ctx.fillText(album.kind === 'album' ? `${album.year} · ${album.tracks.length} tracks` : `${album.tracks.length} songs`, 256, 150);
  // title, wrapped, in the lower half
  let size = 40;
  let lines;
  for (;;) {
    ctx.font = `700 ${size}px ${WOOD_FONT}`;
    const words = album.title.split(' '); lines = []; let cur = '';
    for (const w of words) { const t = cur ? `${cur} ${w}` : w; if (ctx.measureText(t).width > 300 && cur) { lines.push(cur); cur = w; } else cur = t; }
    lines.push(cur);
    if (lines.length <= 3 || size <= 24) break; size -= 4;
  }
  lines.forEach((l, i) => ctx.fillText(l, 256, 330 + (i - (lines.length - 1) / 2) * (size * 1.08)));
  ctx.font = `400 18px ${TYPE_FONT}`; ctx.fillStyle = 'rgba(30,24,18,0.7)';
  ctx.fillText('33⅓ RPM · STEREO', 256, 430);
  const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 8;
  return tex;
}

export function recordsLayout(catalog) {
  const { songs, albums } = catalog;
  const A = arrays(songs.length);
  const rnd = mulberry32(1966);
  const COLS = 8, CW = 22, CH = 23, R = 6.6;
  const labels = [];
  const guide = new THREE.Group();
  const pickables = [];
  const rows = Math.ceil(albums.length / COLS);
  const centres = [];
  const vinylMat = new THREE.MeshStandardMaterial({ color: 0x17120f, roughness: 0.42, metalness: 0.15, transparent: true });
  const grooveMat = new THREE.MeshStandardMaterial({ color: 0x2a221c, roughness: 0.6, transparent: true });

  albums.forEach((album, j) => {
    const col = j % COLS, row = Math.floor(j / COLS);
    const cx = (col - (COLS - 1) / 2) * CW;
    const cy = (rows - 1 - row) * CH + 7;
    const c = new THREE.Vector3(cx, cy, 0);
    centres.push(c);
    if (album.kind === 'album') {
      let cum = 0;
      album.tracks.forEach((song) => {
        const frac = (cum + song.sec / 2) / album.duration;
        cum += song.sec;
        const th = Math.PI / 2 - frac * Math.PI * 2;
        const w = Math.min(1.9, 0.9 + 0.3 * Math.sqrt(song.minutes));
        const rr = R + 0.25 + w * CARD_ASPECT / 2;
        // card tangent to the rim, its top edge pointing outward
        setCard(A, song, cx + rr * Math.cos(th), cy + rr * Math.sin(th), 0.35, w, _e.set(0, 0, th - Math.PI / 2));
      });
      const disc = new THREE.Mesh(new THREE.CircleGeometry(R, 96), vinylMat);
      disc.position.copy(c); disc.receiveShadow = true; disc.userData.baseOpacity = 1;
      guide.add(disc);
      for (const f of [0.92, 0.8, 0.68, 0.56, 0.45]) {
        const g = new THREE.Mesh(new THREE.RingGeometry(R * f - 0.03, R * f + 0.03, 96), grooveMat);
        g.position.copy(c).setZ(0.02); g.userData.baseOpacity = 1;
        guide.add(g);
      }
      const label = new THREE.Mesh(new THREE.CircleGeometry(R * 0.36, 64), new THREE.MeshStandardMaterial({ map: labelTexture(album), roughness: 0.95, transparent: true }));
      label.position.copy(c).setZ(0.04); label.userData.baseOpacity = 1; label.userData.albumIndex = album.index;
      guide.add(label); pickables.push(label, disc);
      disc.userData.albumIndex = album.index;
    } else {
      // outtakes: a loose pile of cards
      album.tracks.forEach((song, k) => {
        const rr = 1.15 * Math.sqrt(k + 0.5);
        const th = k * 2.399963;
        const w = Math.min(1.9, 0.9 + 0.3 * Math.sqrt(song.minutes));
        setCard(A, song, cx + rr * Math.cos(th), cy + rr * Math.sin(th), 0.2 + (k % 5) * 0.03, w, _e.set(0, 0, (rnd() - 0.5) * 0.9));
      });
      labels.push({ text: album.title, sub: `${album.tracks.length} singles, B-sides & outtakes`, pos: new THREE.Vector3(cx, cy + R + 1.2, 0), color: '#1e1915', kind: 'album', albumIndex: album.index, anchor: 'bottom' });
    }
  });

  const camera = { pos: new THREE.Vector3(0, 50, 150), target: new THREE.Vector3(0, 51, 0) };
  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(A.targets[i], A.targets[i + 1], 0);
    return { pos: target.clone().add(new THREE.Vector3(0, -1.2, 13)), target };
  };
  const albumFocus = (album) => {
    const c = centres[album.index];
    return { pos: c.clone().add(new THREE.Vector3(0, -2, 30)), target: c.clone().add(new THREE.Vector3(0, -1, 0)) };
  };
  return { name: 'records', ...A, labels, camera, guide, focus, albumFocus, pickables, flat: true };
}

// ---------------------------------------------------------------- LEXICON
export function lexiconLayout(catalog) {
  const { songs, topWords } = catalog;
  const A = arrays(songs.length);
  const rnd = mulberry32(1975);
  const K = topWords.length;
  const RING = 54;
  const labels = [];
  const guide = new THREE.Group();
  const hubs = topWords.map((w, i) => {
    const th = i * (Math.PI * 2) / K - Math.PI / 2;
    return new THREE.Vector3(RING * Math.cos(th), 0, RING * Math.sin(th));
  });
  const counts = new Array(K).fill(0);
  const rest = [];
  const flat = (yaw) => _e.set(-Math.PI / 2, 0, yaw, 'ZYX');
  songs.forEach((song) => {
    const w = cardW(song) * 0.78;
    if (song.wordHub >= 0) {
      const k = counts[song.wordHub]++;
      const h = hubs[song.wordHub];
      const rr = 1.75 * Math.sqrt(k + 0.6);
      const th = k * 2.399963;
      setCard(A, song, h.x + rr * Math.cos(th), 0.05 + (k % 4) * 0.02, h.z + rr * Math.sin(th), w, flat((rnd() - 0.5) * 0.5));
    } else rest.push(song);
  });
  rest.forEach((song, k) => {
    const w = cardW(song) * 0.78;
    const rr = 1.6 * Math.sqrt(k + 0.6);
    const th = k * 2.399963;
    setCard(A, song, rr * Math.cos(th), 0.05 + (k % 4) * 0.02, rr * Math.sin(th), w, flat((rnd() - 0.5) * 0.5));
  });
  const inkRing = (radius, pos, opacity) => {
    const ring = new THREE.Mesh(new THREE.RingGeometry(radius, radius + 0.14, 128), new THREE.MeshBasicMaterial({ color: 0x3a2e24, transparent: true, opacity, side: THREE.DoubleSide, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2; ring.position.copy(pos).setY(0.01); ring.userData.baseOpacity = opacity;
    return ring;
  };
  topWords.forEach((w, i) => {
    const h = hubs[i];
    const radius = 1.75 * Math.sqrt(counts[i] + 1) + 1.4;
    labels.push({ text: `“${w.word}”`, sub: `${counts[i]} titles`, pos: h.clone().setY(0.6).add(new THREE.Vector3(0, 0, -(radius + 1.6))), color: '#1e1915', kind: 'word', anchor: 'bottom' });
    guide.add(inkRing(radius, h, 0.5));
  });
  const restR = 1.6 * Math.sqrt(rest.length + 1) + 1.4;
  labels.push({ text: 'everything else', sub: `${rest.length} titles`, pos: new THREE.Vector3(0, 0.6, -(restR + 2)), color: '#1e1915', kind: 'word', anchor: 'bottom' });
  guide.add(inkRing(restR, new THREE.Vector3(0, 0, 0), 0.35));

  const camera = { pos: new THREE.Vector3(0, 118, 92), target: new THREE.Vector3(0, 0, 0) };
  const focus = (song) => {
    const i = song.index * 3;
    const target = new THREE.Vector3(A.targets[i], A.targets[i + 1], A.targets[i + 2]);
    return { pos: target.clone().add(new THREE.Vector3(0, 18, 12)), target };
  };
  const albumFocus = () => ({ pos: camera.pos, target: camera.target });
  return { name: 'lexicon', ...A, labels, camera, guide, focus, albumFocus };
}

export function buildLayouts(catalog) {
  return {
    highway: highwayLayout(catalog),
    years: yearsLayout(catalog),
    records: recordsLayout(catalog),
    lexicon: lexiconLayout(catalog),
  };
}
