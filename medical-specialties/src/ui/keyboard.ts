import type { Graph } from '../data/graph';
import type { SceneApp } from '../scene/app';
import type { Store } from '../state';
import { layoutByIndex } from './hud';

interface KeyboardDeps {
  graph: Graph;
  store: Store;
  app: SceneApp;
  focusSearch: () => void;
  toggleHelp: () => void;
  helpOpen: () => boolean;
}

export function setupKeyboard({ graph, store, app, focusSearch, toggleHelp, helpOpen }: KeyboardDeps): void {
  window.addEventListener('keydown', (ev) => {
    const target = ev.target as HTMLElement | null;
    if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

    if (helpOpen() && ev.key !== 'Escape' && ev.key !== '?') return;

    const sel = store.state.selected ? graph.byId.get(store.state.selected) : null;
    switch (ev.key) {
      case '/':
        ev.preventDefault();
        focusSearch();
        break;
      case '?':
        toggleHelp();
        break;
      case 'Escape':
        if (helpOpen()) toggleHelp();
        else if (store.state.selected) store.select(null);
        else store.showAllDomains();
        break;
      case '1':
      case '2':
      case '3':
      case '4': {
        const id = layoutByIndex(Number(ev.key) - 1);
        if (id) store.setLayout(id);
        break;
      }
      case 'c':
      case 'C':
        store.setShowCross(!store.state.showCross);
        break;
      case 'l':
      case 'L':
        store.setLabels(store.state.labels === 'all' ? 'auto' : 'all');
        break;
      case 'r':
      case 'R':
        store.select(null);
        app.resetView();
        break;
      case 'ArrowLeft':
      case 'ArrowRight': {
        if (!sel) {
          store.select(graph.root.children[0] ?? null);
          break;
        }
        const parent = sel.parents[0] ? graph.byId.get(sel.parents[0]) : null;
        if (!parent) break;
        const siblings = parent.children;
        const i = siblings.indexOf(sel.id);
        const next = siblings[(i + (ev.key === 'ArrowRight' ? 1 : -1) + siblings.length) % siblings.length];
        if (next) store.select(next);
        ev.preventDefault();
        break;
      }
      case 'ArrowUp': {
        if (sel?.parents[0]) store.select(sel.parents[0]);
        ev.preventDefault();
        break;
      }
      case 'ArrowDown': {
        const base = sel ?? graph.root;
        if (base.children[0]) store.select(base.children[0]);
        ev.preventDefault();
        break;
      }
    }
  });
}
