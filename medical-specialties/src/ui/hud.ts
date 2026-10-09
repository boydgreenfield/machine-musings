import type { Graph } from '../data/graph';
import { LAYOUTS, type LayoutId } from '../graph/layouts';
import type { SceneApp } from '../scene/app';
import type { Store } from '../state';

export function setupHud(graph: Graph, store: Store, app: SceneApp): void {
  const switcher = document.getElementById('layout-switch')!;
  const hint = document.getElementById('layout-hint')!;
  const cross = document.getElementById('toggle-cross') as HTMLInputElement;
  const labels = document.getElementById('toggle-labels') as HTMLInputElement;
  const reset = document.getElementById('reset-view')!;
  const stats = document.getElementById('stats')!;

  LAYOUTS.forEach((l, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.role = 'tab';
    btn.dataset.layout = l.id;
    btn.innerHTML = `${l.name}<kbd>${i + 1}</kbd>`;
    btn.title = l.hint;
    btn.addEventListener('click', () => store.setLayout(l.id));
    switcher.appendChild(btn);
  });

  cross.addEventListener('change', () => store.setShowCross(cross.checked));
  labels.addEventListener('change', () => store.setLabels(labels.checked ? 'all' : 'auto'));
  reset.addEventListener('click', () => {
    store.select(null);
    app.resetView();
  });

  const s = graph.stats;
  stats.innerHTML = `<b>${s.specialties}</b> specialties · <b>${s.subspecialties}</b> subspecialties · <b>${s.crossPathways}</b> cross-board pathways`;

  const sync = () => {
    for (const btn of switcher.querySelectorAll<HTMLButtonElement>('button')) {
      btn.setAttribute('aria-selected', String(btn.dataset.layout === store.state.layout));
    }
    const meta = LAYOUTS.find((l) => l.id === store.state.layout)!;
    hint.textContent = meta.hint;
    cross.checked = store.state.showCross;
    labels.checked = store.state.labels === 'all';
  };
  store.on('change', sync);
  sync();
}

export function layoutByIndex(i: number): LayoutId | undefined {
  return LAYOUTS[i]?.id;
}
