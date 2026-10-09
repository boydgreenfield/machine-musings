import './style.css';
import { buildGraph } from './data/graph';
import { SceneApp } from './scene/app';
import { Store } from './state';
import { setupHelp } from './ui/help';
import { setupHud } from './ui/hud';
import { setupKeyboard } from './ui/keyboard';
import { setupLegend } from './ui/legend';
import { setupPanel } from './ui/panel';
import { setupSearch } from './ui/search';

const graph = buildGraph();
const store = new Store(graph);

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const app = new SceneApp(graph, store, {
  container: document.getElementById('gl-container')!,
  labelContainer: document.getElementById('labels')!,
  reducedMotion,
});

setupPanel(graph, store);
setupLegend(graph, store);
const search = setupSearch(graph, store);
setupHud(graph, store, app);
const help = setupHelp();
setupKeyboard({ graph, store, app, focusSearch: search.focus, toggleHelp: help.toggle, helpOpen: help.isOpen });

// Deep links: #/<node-id> selects a node on load and keeps the hash in sync.
const applyHash = () => {
  const id = decodeURIComponent(location.hash.replace(/^#\/?/, ''));
  if (id && graph.byId.has(id)) store.select(id);
};
store.on('select', (s) => {
  const next = s.selected ? `#/${s.selected}` : '';
  if (location.hash !== next) history.replaceState(null, '', next || location.pathname + location.search);
});
window.addEventListener('hashchange', applyHash);
window.setTimeout(applyHash, reducedMotion ? 0 : 1800);

// Expose for debugging and tests in the console.
declare global {
  interface Window {
    atlas: { graph: typeof graph; store: Store; app: SceneApp };
  }
}
window.atlas = { graph, store, app };
