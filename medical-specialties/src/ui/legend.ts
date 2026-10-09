import type { Graph } from '../data/graph';
import type { DomainId } from '../data/types';
import type { Store } from '../state';

export function setupLegend(graph: Graph, store: Store): void {
  const list = document.getElementById('legend-list')!;
  const all = document.getElementById('legend-all')!;
  const counts = new Map<DomainId, number>();
  for (const n of graph.nodes) {
    if (n.level === 'root' || n.level === 'domain') continue;
    counts.set(n.domain, (counts.get(n.domain) ?? 0) + 1);
  }
  for (const d of graph.domains) {
    const li = document.createElement('li');
    li.dataset.domain = d.id;
    li.title = d.blurb;
    li.innerHTML = `<span class="swatch" style="color:${d.color};background:${d.color}"></span><span>${d.name}</span><span class="count">${counts.get(d.id) ?? 0}</span>`;
    li.addEventListener('click', (ev) => {
      if (ev.shiftKey || ev.metaKey || ev.ctrlKey) store.toggleDomain(d.id);
      else store.soloDomain(d.id);
    });
    li.addEventListener('mouseenter', () => store.setHoveredDomain(d.id));
    li.addEventListener('mouseleave', () => store.setHoveredDomain(null));
    li.addEventListener('dblclick', () => {
      const domainNode = graph.nodes.find((n) => n.level === 'domain' && n.domain === d.id);
      if (domainNode) store.select(domainNode.id);
    });
    list.appendChild(li);
  }
  all.addEventListener('click', () => store.showAllDomains());

  const sync = () => {
    for (const li of list.querySelectorAll<HTMLLIElement>('li')) {
      li.classList.toggle('off', store.state.hiddenDomains.has(li.dataset.domain as DomainId));
    }
    all.toggleAttribute('hidden', store.state.hiddenDomains.size === 0);
  };
  store.on('change', sync);
  sync();
}
