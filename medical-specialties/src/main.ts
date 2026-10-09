import './style.css';
import { buildGraph } from './data/graph';
import { SceneApp } from './scene/app';
import { Store } from './state';
import { LAYOUTS, type LayoutId } from './graph/layouts';
import { setupHelp } from './ui/help';
import { setupHud } from './ui/hud';
import { setupKeyboard } from './ui/keyboard';
import { setupLegend } from './ui/legend';
import { setupPanel } from './ui/panel';
import { setupSearch } from './ui/search';
import { setupTour } from './ui/tour';
import { setupTooltip } from './ui/tooltip';
import { renderFallback } from './ui/fallback';

const graph = buildGraph();
const store = new Store(graph);

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
let app: SceneApp;
try {
  app = new SceneApp(graph, store, {
    container: document.getElementById('gl-container')!,
    labelContainer: document.getElementById('labels')!,
    reducedMotion,
  });
} catch (err) {
  renderFallback(graph, err instanceof Error ? err.message : String(err));
  throw err;
}

setupPanel(graph, store);
setupTooltip(graph, store, document.getElementById('gl-container')!);
setupLegend(graph, store);
const search = setupSearch(graph, store);
setupHud(graph, store, app);
const help = setupHelp();
const tour = setupTour(graph, store, app);
setupKeyboard({ graph, store, app, focusSearch: search.focus, toggleHelp: help.toggle, helpOpen: help.isOpen, tour });

// Announce selections for assistive technology.
const announce = document.getElementById('announce')!;
store.on('select', (s) => {
  const n = s.selected ? graph.byId.get(s.selected) : null;
  announce.textContent = n ? `${n.name}. ${n.description}` : 'Selection cleared.';
});

// Deep links: #/<node-id>?layout=<id> selects a node and layout on load and
// keeps the hash in sync afterwards.
const LAYOUT_IDS = new Set(LAYOUTS.map((l) => l.id));
const readHash = () => {
  const raw = location.hash.replace(/^#\/?/, '');
  const [idPart, query = ''] = raw.split('?');
  const params = new URLSearchParams(query);
  const layout = params.get('layout');
  return { id: decodeURIComponent(idPart ?? ''), layout: layout && LAYOUT_IDS.has(layout as LayoutId) ? (layout as LayoutId) : null };
};
const writeHash = () => {
  const { selected, layout } = store.state;
  const parts: string[] = [];
  if (selected) parts.push(selected);
  const query = layout === 'constellation' ? '' : `?layout=${layout}`;
  const next = parts.length || query ? `#/${parts.join('')}${query}` : '';
  if (location.hash !== next) history.replaceState(null, '', next || location.pathname + location.search);
};
const apply = ({ id, layout }: ReturnType<typeof readHash>) => {
  if (layout) store.setLayout(layout);
  if (id && graph.byId.has(id)) store.select(id);
};
// Read the initial hash once: the layout applies immediately, the selection
// after the intro, and only then does the hash start tracking state.
const initial = readHash();
if (initial.layout) store.setLayout(initial.layout);
window.setTimeout(() => {
  apply(initial);
  store.on('select', writeHash);
  store.on('layout', writeHash);
  window.addEventListener('hashchange', () => apply(readHash()));
}, reducedMotion ? 0 : 1800);

// Expose for debugging and tests in the console.
declare global {
  interface Window {
    atlas: { graph: typeof graph; store: Store; app: SceneApp };
  }
}
window.atlas = { graph, store, app };
