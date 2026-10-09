import './styles.css';
import { preloadFont } from 'troika-three-text';
import { buildGraph, graphStats, type GraphNode } from './graph';
import { CATEGORY_META } from './data/schema';
import { LAYOUTS, type LayoutId } from './layouts/index';
import { createStore, type LensId } from './state';
import { Atlas } from './scene/atlas';
import { DetailPanel } from './ui/panel';
import { Search } from './ui/search';
import { Legend } from './ui/legend';
import { Tour } from './ui/tour';

const LOCAL_FONT_URL = `${import.meta.env.BASE_URL}fonts/Inter-SemiBold.ttf`;
// Static Inter 600 TTF served by Google Fonts, used only if the bundled font is absent.
const FALLBACK_FONT_URL = 'https://fonts.gstatic.com/s/inter/v20/UcCO3FwrK3iLTeHuS_nVMrMxCp50SjIw2boKoduKmMEVuGKYMZg.ttf';
async function resolveFontUrl(): Promise<string> {
  try {
    const r = await fetch(LOCAL_FONT_URL, { method: 'HEAD' });
    if (r.ok && !(r.headers.get('content-type') ?? '').includes('text/html')) return LOCAL_FONT_URL;
  } catch { /* fall through */ }
  return FALLBACK_FONT_URL;
}
const LENSES: LensId[] = ['category', 'domain', 'training', 'workforce', 'credential'];

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

const graph = buildGraph();
const store = createStore(graph.categories);
const canvas = $<HTMLCanvasElement>('scene');

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if (reducedMotion) store.set({ autoRotate: false, flow: false });

function webglAvailable(): boolean {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch { return false; }
}
if (!webglAvailable()) {
  const card = document.querySelector('#intro .overlay-card') as HTMLElement;
  card.innerHTML = `<p class="eyebrow">Unavailable</p><h2>WebGL is required</h2><p>This atlas is drawn with three.js and needs a browser with WebGL enabled. Try a current version of Chrome, Firefox, Safari or Edge, or enable hardware acceleration in your browser settings.</p>`;
  throw new Error('WebGL unavailable');
}

const FONT_URL = await resolveFontUrl();

// ── Tooltip ──────────────────────────────────────────────────────────────────
const tooltip = $('tooltip');
let tipNode: GraphNode | null = null;
function showTooltip(n: GraphNode | null, x: number, y: number) {
  if (!n || store.state.selected === n) { tooltip.hidden = true; tipNode = null; return; }
  if (tipNode !== n) {
    tipNode = n;
    const c = CATEGORY_META[n.category].color;
    const meta = n.tier === 'root' ? 'The starting point' : n.tier === 'specialty' ? `Specialty · ${n.residency ?? ''}-year residency` : `${n.isBridge ? `Bridges ${n.parentNodes.length} specialties` : `${n.tier === 'fellowship' ? 'Fellowship' : 'Subspecialty'} of ${n.parentNodes[0]?.short ?? n.parentNodes[0]?.name}`}${n.fellowship ? ` · +${n.fellowship} yr` : ''}`;
    tooltip.innerHTML = `<div class="t-name"><span class="swatch" style="color:${c};background:${c}"></span>${n.name}</div><div class="t-meta">${meta}${n.workforce ? ` · ≈${n.workforce.toLocaleString('en-US')} physicians` : ''}</div><div class="t-hint">Click to explore</div>`;
  }
  const w = tooltip.offsetWidth || 240, h = tooltip.offsetHeight || 60;
  const px = x + 14 + w > window.innerWidth - 8 ? x - w - 14 : x;
  const py = y + 14 + h > window.innerHeight - 8 ? y - h - 14 : y;
  tooltip.style.transform = `translate(${px + (px === x ? 14 : 0)}px, ${py + (py === y ? 14 : 0)}px)`;
  tooltip.style.left = '0'; tooltip.style.top = '0';
  tooltip.hidden = false;
}

// ── Scene ────────────────────────────────────────────────────────────────────
const atlas = new Atlas(canvas, graph, store, FONT_URL, {
  onHover: (n, x, y) => showTooltip(n, x, y),
  onSelect: (n) => select(n, true),
});

function select(n: GraphNode | null, fly: boolean) {
  if (n && n.tier === 'root') { store.set({ selected: null }); atlas.resetView(); return; }
  store.set({ selected: n });
  if (n && fly) atlas.flyToNode(n);
  if (n) tooltip.hidden = true;
}

// ── UI ───────────────────────────────────────────────────────────────────────
const panel = new DetailPanel($('panel'), graph, store, (n) => select(n, true));
const search = new Search($('search'), graph, store, (n) => { search.reset(); select(n, true); });
new Legend($('legend'), graph, store);
const tour = new Tour(document, graph, store, select);

const stats = graphStats(graph);
$('stats').innerHTML = `<b>${stats.boards}</b> boards · <b>${stats.specialties}</b> specialties · <b>${stats.subspecialties}</b> subspecialties · <b>${stats.fellowships}</b> fellowships · <b>${stats.bridges}</b> bridges`;

const layoutButtons = $('layout-buttons');
layoutButtons.innerHTML = LAYOUTS.map((l, i) => `<button class="seg" role="radio" data-layout="${l.id}" aria-checked="${l.id === store.state.layout}" title="${l.blurb}"><kbd>${i + 1}</kbd>${l.label}</button>`).join('');
layoutButtons.addEventListener('click', (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>('[data-layout]');
  if (b) store.set({ layout: b.dataset.layout as LayoutId });
});
function renderLayoutButtons() {
  layoutButtons.querySelectorAll<HTMLElement>('[data-layout]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.layout === store.state.layout)));
  $('layout-blurb').textContent = LAYOUTS.find((l) => l.id === store.state.layout)!.blurb;
}
renderLayoutButtons();

const lensSelect = $<HTMLSelectElement>('lens-select');
lensSelect.addEventListener('change', () => store.set({ lens: lensSelect.value as LensId }));

const toggles: Record<string, () => void> = {
  'toggle-fellowships': () => store.set({ showFellowships: !store.state.showFellowships }),
  'toggle-labels': () => store.set({ labelDensity: store.state.labelDensity >= 0.9 ? 0 : store.state.labelDensity >= 0.66 ? 1 : store.state.labelDensity >= 0.33 ? 0.75 : store.state.labelDensity > 0 ? 0.5 : 0.25 }),
  'toggle-rotate': () => store.set({ autoRotate: !store.state.autoRotate }),
  'toggle-glow': () => store.set({ bloom: !store.state.bloom, flow: !store.state.bloom }),
};
for (const id of Object.keys(toggles)) $(id).addEventListener('click', toggles[id]);
function renderToggles() {
  const s = store.state;
  $('toggle-fellowships').setAttribute('aria-pressed', String(s.showFellowships));
  const lbl = $('toggle-labels');
  lbl.setAttribute('aria-pressed', String(s.labelDensity > 0));
  lbl.textContent = s.labelDensity === 0 ? 'Labels off' : s.labelDensity < 0.33 ? 'Labels: specialties' : s.labelDensity < 0.66 ? 'Labels: nearby' : s.labelDensity < 0.9 ? 'Labels: subspecialties' : 'Labels: all';
  $('toggle-rotate').setAttribute('aria-pressed', String(s.autoRotate));
  $('toggle-glow').setAttribute('aria-pressed', String(s.bloom));
  lensSelect.value = s.lens;
  $('tour-button').textContent = s.tourActive ? '■ Stop tour' : '▶ Tour';
}
renderToggles();

$('tour-button').addEventListener('click', () => tour.toggle());
$('reset-button').addEventListener('click', () => { store.set({ selected: null }); atlas.resetView(); });
$('help-button').addEventListener('click', () => { $('help').hidden = false; });
$('help-close').addEventListener('click', () => { $('help').hidden = true; });
$('help').addEventListener('click', (e) => { if (e.target === $('help')) $('help').hidden = true; });

// ── Intro ────────────────────────────────────────────────────────────────────
const intro = $('intro');
function dismissIntro() { intro.hidden = true; }
$('intro-explore').addEventListener('click', dismissIntro);
$('intro-tour').addEventListener('click', () => { dismissIntro(); tour.start(); });
preloadFont({ font: FONT_URL, characters: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 &-–/()' }, () => {
  $('intro-loading').textContent = 'Fonts ready';
  setTimeout(() => { $('intro-loading').textContent = ''; }, 600);
});

// ── Store → UI ───────────────────────────────────────────────────────────────
store.subscribe((s, changed) => {
  if (changed.has('selected')) { panel.render(s.selected); if (s.selected) tooltip.hidden = true; }
  if (changed.has('layout')) renderLayoutButtons();
  renderToggles();
});

// ── Keyboard ─────────────────────────────────────────────────────────────────
window.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const inInput = (e.target as HTMLElement).tagName === 'INPUT' || (e.target as HTMLElement).tagName === 'SELECT';
  if (inInput) return;
  switch (e.key) {
    case '/': e.preventDefault(); search.focus(); break;
    case 'Escape':
      if (!$('help').hidden) $('help').hidden = true;
      else if (!intro.hidden) dismissIntro();
      else if (tour.active) tour.stop();
      else if (store.state.searchHits) search.reset();
      else if (store.state.selected) store.set({ selected: null });
      break;
    case '1': case '2': case '3': case '4': store.set({ layout: LAYOUTS[Number(e.key) - 1].id }); break;
    case 'c': case 'C': store.set({ lens: LENSES[(LENSES.indexOf(store.state.lens) + 1) % LENSES.length] }); break;
    case 'f': case 'F': toggles['toggle-fellowships'](); break;
    case 'l': case 'L': toggles['toggle-labels'](); break;
    case 'g': case 'G': toggles['toggle-glow'](); break;
    case ' ': e.preventDefault(); toggles['toggle-rotate'](); break;
    case 't': case 'T': if (!intro.hidden) dismissIntro(); tour.toggle(); break;
    case 'r': case 'R': store.set({ selected: null }); atlas.resetView(); break;
    case '?': $('help').hidden = !$('help').hidden; break;
    case 'ArrowLeft': case 'ArrowRight': {
      const sel = store.state.selected;
      if (!sel) break;
      const parent = sel.parentNodes[0];
      const sibs = parent ? parent.children.filter((c) => store.isVisible(c)) : graph.specialties;
      const i = sibs.indexOf(sel);
      if (i < 0) break;
      const next = sibs[(i + (e.key === 'ArrowRight' ? 1 : sibs.length - 1)) % sibs.length];
      select(next, true);
      break;
    }
  }
});

// ── URL state (shareable deep links) ─────────────────────────────────────────
function readHash() {
  const h = new URLSearchParams(location.hash.replace(/^#/, ''));
  const layout = h.get('layout') as LayoutId | null;
  const lens = h.get('lens') as LensId | null;
  const node = h.get('node');
  const patch: Partial<typeof store.state> = {};
  if (layout && LAYOUTS.some((l) => l.id === layout)) patch.layout = layout;
  if (lens && LENSES.includes(lens)) patch.lens = lens;
  store.set(patch);
  if (node && graph.byId.has(node)) {
    dismissIntro();
    select(graph.byId.get(node)!, true);
  }
}
let writingHash = false;
function writeHash() {
  const s = store.state;
  const h = new URLSearchParams();
  if (s.layout !== 'orbits') h.set('layout', s.layout);
  const defaultLens = LAYOUTS.find((l) => l.id === s.layout)!;
  if (s.lens !== ({ orbits: 'category', constellation: 'category', tower: 'training', systems: 'domain' } as Record<string, LensId>)[defaultLens.id]) h.set('lens', s.lens);
  if (s.selected) h.set('node', s.selected.id);
  const str = h.toString();
  writingHash = true;
  history.replaceState(null, '', str ? `#${str}` : location.pathname + location.search);
  writingHash = false;
}
store.subscribe((_, changed) => { if (changed.has('layout') || changed.has('lens') || changed.has('selected')) writeHash(); });
window.addEventListener('hashchange', () => { if (!writingHash) readHash(); });
readHash();

atlas.setFrameHook((dt) => tour.tick(dt));

// Expose for debugging & screenshots.
(window as unknown as { atlas: Atlas; store: typeof store; graph: typeof graph }).atlas = atlas;
(window as unknown as { store: typeof store }).store = store;
(window as unknown as { graph: typeof graph }).graph = graph;
