import * as THREE from 'three';
import { buildCatalog } from './data.js';
import { buildLayouts } from './layouts.js';
import { Stage, SongMarks, LabelSet, setGuideAlpha } from './scene.js';
import { setupUI } from './ui.js';

const catalog = buildCatalog();
const layouts = buildLayouts(catalog);
const { songs, albums } = catalog;
const studioOrder = albums.filter((a) => a.kind === 'album').slice().sort((a, b) => a.timelineYear - b.timelineYear);

const canvas = document.getElementById('stage');
const stage = new Stage(canvas);
const marks = new SongMarks(songs);
stage.scene.add(marks.mesh);
const labels = new LabelSet();
stage.scene.add(labels.group);
Object.values(layouts).forEach((l) => { l.alpha = 0; l.alphaTarget = 0; setGuideAlpha(l.guide, 0); stage.scene.add(l.guide); });

const state = {
  mode: null, selected: null, selectedAlbum: null, hovered: null, hoverEra: null,
  eras: new Set(), query: '', tour: null, interacted: false, started: false,
};
const songById = new Map(songs.map((s) => [s.id, s]));
function writeHash() {
  const parts = [state.mode];
  if (state.selected) parts.push(state.selected.id);
  else if (state.selectedAlbum) parts.push(`album:${state.selectedAlbum.id}`);
  history.replaceState(null, '', `#${parts.join('/')}`);
}
const nowS = () => performance.now() / 1000;

// ------------------------------------------------------------ filtering & highlight
function applyState() {
  const q = state.query;
  for (const s of songs) {
    let vis = 1;
    if (state.eras.size && !state.eras.has(s.era)) vis = 0;
    if (q && !(s.title.toLowerCase().includes(q) || s.album.title.toLowerCase().includes(q))) vis = Math.min(vis, 0.12);
    if (state.selectedAlbum && s.album !== state.selectedAlbum) vis = Math.min(vis, 0.3);
    marks.visTarget[s.index] = vis;
    let hi = 0;
    if (state.selected === s) hi = 1;
    else if (state.hovered === s) hi = 0.7;
    else if (state.selectedAlbum && s.album === state.selectedAlbum) hi = 0.25;
    else if (state.hoverEra && s.era === state.hoverEra) hi = 0.3;
    marks.hiTarget[s.index] = hi;
  }
  ui.setEraFilter(state.eras);
}

// ------------------------------------------------------------ modes
function setMode(name, animate = true) {
  if (state.mode === name) return;
  state.mode = name;
  const layout = layouts[name];
  marks.setTargets(layout.targets, layout.scales, nowS(), animate);
  labels.set(layout.labels);
  labels.alphaTarget = 1;
  Object.values(layouts).forEach((l) => (l.alphaTarget = l === layout ? 1 : 0));
  ui.setMode(name);
  stage.controls.autoRotate = name === 'galaxy' && !state.interacted;
  if (state.selected) {
    const f = layout.focus(state.selected);
    stage.flyTo(f.pos, f.target, animate ? 2.2 : 0.01, nowS());
  } else if (state.selectedAlbum) {
    const f = layout.albumFocus(state.selectedAlbum);
    stage.flyTo(f.pos, f.target, animate ? 2.2 : 0.01, nowS());
  } else {
    stage.flyTo(layout.camera.pos, layout.camera.target, animate ? 2.2 : 0.01, nowS());
  }
  writeHash();
}

// ------------------------------------------------------------ selection
function selectSong(song, fly = true) {
  state.selected = song;
  state.selectedAlbum = null;
  labels.emphasis = song.album.index;
  ui.showSong(song);
  if (fly) { const f = layouts[state.mode].focus(song); stage.flyTo(f.pos, f.target, 1.6, nowS()); }
  stage.controls.autoRotate = false;
  applyState();
  writeHash();
}
function selectAlbum(album, fly = true) {
  state.selectedAlbum = album;
  state.selected = null;
  labels.emphasis = album.index;
  ui.showAlbum(album);
  if (fly) { const f = layouts[state.mode].albumFocus(album); stage.flyTo(f.pos, f.target, 2.0, nowS()); }
  stage.controls.autoRotate = false;
  applyState();
  writeHash();
}
function clearSelection() {
  state.selected = null; state.selectedAlbum = null;
  labels.emphasis = null;
  ui.hidePanel();
  applyState();
  writeHash();
}
function randomSong() {
  const pool = songs.filter((s) => marks.visTarget[s.index] > 0.5 && s !== state.selected);
  selectSong(pool[Math.floor(Math.random() * pool.length)] || songs[0]);
}
function step(dir) {
  const cur = state.selected;
  if (!cur) { selectSong(songs[0]); return; }
  const i = (cur.index + dir + songs.length) % songs.length;
  selectSong(songs[i]);
}
function stepAlbum(dir) {
  const cur = state.selectedAlbum || (state.selected && state.selected.album) || null;
  let i = cur ? studioOrder.indexOf(cur) : -1;
  if (i < 0 && cur && cur.kind === 'group') i = studioOrder.length;
  i = (i + dir + studioOrder.length + 1) % (studioOrder.length + 1);
  selectAlbum(i === studioOrder.length ? albums[albums.length - 1] : studioOrder[i]);
}

// ------------------------------------------------------------ tour
function toggleTour() {
  if (state.tour) { clearInterval(state.tour.timer); state.tour = null; ui.setTour(false); return; }
  let i = -1;
  const next = () => {
    i += 1;
    if (i >= studioOrder.length) { toggleTour(); return; }
    selectAlbum(studioOrder[i]);
  };
  state.tour = { timer: setInterval(next, 6500) };
  ui.setTour(true);
  next();
}

// ------------------------------------------------------------ UI
const ui = setupUI(catalog, {
  mode: (m) => setMode(m),
  era: (key, additive) => {
    if (state.eras.has(key) && (state.eras.size === 1 || additive)) state.eras.delete(key);
    else if (additive) state.eras.add(key);
    else { state.eras.clear(); state.eras.add(key); }
    applyState();
  },
  search: (q) => { state.query = q; applyState(); },
  select: (s) => selectSong(s),
  album: (a) => selectAlbum(a),
  hoverEra: (key) => { state.hoverEra = key; applyState(); },
  random: () => randomSong(),
  clear: () => clearSelection(),
  step: (d) => step(d),
  tour: () => toggleTour(),
});

// ------------------------------------------------------------ pointer picking
const ray = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let pointerDirty = false, px = 0, py = 0, downAt = null;
canvas.addEventListener('pointermove', (e) => { px = e.clientX; py = e.clientY; pointer.set((px / innerWidth) * 2 - 1, -(py / innerHeight) * 2 + 1); pointerDirty = true; });
canvas.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; state.interacted = true; stage.controls.autoRotate = false; stage.camTween = null; });
canvas.addEventListener('pointerup', (e) => {
  if (!downAt) return;
  const moved = Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]);
  downAt = null;
  if (moved > 5) return;
  pointer.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  const hit = pick(true);
  if (hit && hit.song) selectSong(hit.song);
  else if (hit && hit.album) selectAlbum(hit.album);
  else if (state.selected || state.selectedAlbum) clearSelection();
});
canvas.addEventListener('wheel', () => { state.interacted = true; stage.controls.autoRotate = false; }, { passive: true });

function pick(includeLabels = false) {
  ray.setFromCamera(pointer, stage.camera);
  ray.params.Sprite = { threshold: 0 };
  const hits = ray.intersectObject(marks.mesh, false);
  for (const h of hits) {
    const s = songs[h.instanceId];
    if (marks.state[s.index * 2] > 0.2) return { song: s };
  }
  if (includeLabels && labels.alpha > 0.5) {
    const lh = ray.intersectObjects(labels.sprites, false);
    for (const h of lh) {
      const l = h.object.userData.label;
      if (l.albumIndex !== undefined) return { album: albums[l.albumIndex] };
    }
  }
  return null;
}

// ------------------------------------------------------------ keyboard
addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '1') setMode('galaxy');
  else if (e.key === '2') setMode('highway');
  else if (e.key === '3') setMode('records');
  else if (e.key === '4') setMode('lexicon');
  else if (e.key === '/') { e.preventDefault(); ui.focusSearch(); }
  else if (e.key === 'ArrowRight') step(1);
  else if (e.key === 'ArrowLeft') step(-1);
  else if (e.key === 'ArrowDown') stepAlbum(1);
  else if (e.key === 'ArrowUp') stepAlbum(-1);
  else if (e.key === 't' || e.key === 'T') toggleTour();
  else if (e.key === 'r' || e.key === 'R') randomSong();
  else if (e.key === '?') ui.toggleHelp();
  else if (e.key === 'Escape') {
    if (!document.getElementById('help').classList.contains('hidden')) { ui.toggleHelp(); return; }
    if (state.tour) toggleTour();
    state.eras.clear(); state.query = ''; ui.clearSearch(); clearSelection();
  }
});

// ------------------------------------------------------------ intro
const intro = document.getElementById('intro');
function enter() {
  if (state.started) return;
  state.started = true;
  intro.classList.add('gone');
  document.body.classList.add('started');
  const l = layouts[state.mode];
  if (state.selected) { const f = l.focus(state.selected); stage.flyTo(f.pos, f.target, 3.2, nowS()); }
  else if (state.selectedAlbum) { const f = l.albumFocus(state.selectedAlbum); stage.flyTo(f.pos, f.target, 3.2, nowS()); }
  else stage.flyTo(l.camera.pos, l.camera.target, 3.2, nowS());
}
document.getElementById('enter').addEventListener('click', enter);
addEventListener('keydown', (e) => { if (!state.started && (e.key === 'Enter' || e.key === ' ')) enter(); }, { once: false });

// Initial state from the URL hash (#mode/song-id), camera pulled far back for the entrance dolly.
const hashParts = location.hash.slice(1).split('/');
const initial = ['galaxy', 'highway', 'records', 'lexicon'].includes(hashParts[0]) ? hashParts[0] : 'galaxy';
setMode(initial, false);
{
  const l = layouts[initial];
  const far = l.camera.pos.clone().sub(l.camera.target).multiplyScalar(1.9).add(l.camera.target);
  stage.camera.position.copy(far);
  stage.controls.target.copy(l.camera.target);
  stage.camTween = null;
  if (hashParts[1]) {
    if (hashParts[1].startsWith('album:')) { const a = albums.find((x) => x.id === hashParts[1].slice(6)); if (a) selectAlbum(a, false); }
    else { const sg = songById.get(hashParts[1]); if (sg) selectSong(sg, false); }
  }
}
applyState();

// ------------------------------------------------------------ loop
let last = nowS();
let frames = 0;
function loop() {
  requestAnimationFrame(loop);
  const now = nowS();
  const dt = Math.min(0.05, now - last); last = now;
  marks.update(now, dt);
  labels.update(dt, stage.camera, innerHeight, innerWidth);
  for (const l of Object.values(layouts)) {
    if (l.alpha !== l.alphaTarget) {
      l.alpha += (l.alphaTarget - l.alpha) * (1 - Math.exp(-dt * 4));
      if (Math.abs(l.alpha - l.alphaTarget) < 0.002) l.alpha = l.alphaTarget;
      setGuideAlpha(l.guide, l.alpha);
      if (l.guide.children[0] && l.guide.children[0].material.uniforms && l.guide.children[0].material.uniforms.uPixelRatio) l.guide.children[0].material.uniforms.uPixelRatio.value = stage.renderer.getPixelRatio();
    }
  }
  if (pointerDirty && state.started) {
    pointerDirty = false;
    const hit = pick(false);
    const s = hit ? hit.song : null;
    if (s !== state.hovered) { state.hovered = s; applyState(); canvas.style.cursor = s ? 'pointer' : ''; }
    ui.tooltip(s, px, py);
  }
  stage.update(now, dt);
  stage.render();
  if (++frames === 2) window.__ready = true;
}
loop();

// For scripted screenshots and curious consoles.
window.__app = { setMode, selectSong, selectAlbum, clearSelection, enter, toggleTour, randomSong, catalog, layouts, stage, state,
  findSong: (t) => songs.find((s) => s.title.toLowerCase() === t.toLowerCase()),
  // Finish every animation immediately (used by the screenshot script).
  settle() {
    marks.t0 = -1e9; marks.cur.set(marks.pos1); marks.curS.set(marks.scl1);
    for (let i = 0; i < marks.n; i++) { marks.state[i * 2] = marks.visTarget[i]; marks.state[i * 2 + 1] = marks.hiTarget[i]; }
    for (const l of Object.values(layouts)) { l.alpha = l.alphaTarget; setGuideAlpha(l.guide, l.alpha); }
    labels.alpha = labels.alphaTarget;
    if (stage.camTween) { stage.camera.position.copy(stage.camTween.p1); stage.controls.target.copy(stage.camTween.t1); stage.camTween = null; }
    stage.controls.update();
  } };
