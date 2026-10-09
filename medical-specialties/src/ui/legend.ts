import { CATEGORY_META, CREDENTIAL_META, DOMAIN_META, type Category, type Credential, type Domain } from '../data/schema';
import type { Graph } from '../graph';
import type { Store } from '../state';
import { CREDENTIAL_COLORS } from '../scene/atlas';
import { RAMP_AMBER, RAMP_TEAL } from '../util/color';

function esc(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}

export class Legend {
  constructor(private readonly el: HTMLElement, private readonly graph: Graph, private readonly store: Store) {
    el.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const item = t.closest<HTMLElement>('[data-category]');
      if (item) {
        const c = item.dataset.category as Category;
        const set = new Set(store.state.categories);
        if (e.altKey || e.metaKey || e.shiftKey) {
          // Solo this category
          set.clear(); set.add(c);
        } else if (set.has(c)) {
          if (set.size === 1) for (const k of graph.categories) set.add(k); // restoring all when the last one is clicked
          else set.delete(c);
        } else set.add(c);
        store.set({ categories: set });
        return;
      }
      if (t.closest('[data-reset]')) store.set({ categories: new Set(graph.categories) });
    });
    store.subscribe((_, changed) => { if (changed.has('lens') || changed.has('categories') || changed.has('showFellowships')) this.render(); });
    this.render();
  }

  render() {
    const s = this.store.state;
    const count = (pred: (n: Graph['nodes'][number]) => boolean) => this.graph.nodes.filter((n) => n.tier !== 'root' && pred(n) && (s.showFellowships || n.tier !== 'fellowship')).length;
    let body = '';
    let title = '';
    let note = '';
    switch (s.lens) {
      case 'category': {
        title = 'Family of practice';
        const allOn = s.categories.size === this.graph.categories.length;
        body = `<div class="legend-items">${this.graph.categories.map((c) => `<button class="legend-item clickable ${s.categories.has(c) ? '' : 'off'}" data-category="${c}" title="${esc(CATEGORY_META[c].blurb)}\n\nClick to toggle · shift-click to solo"><span class="swatch" style="color:${CATEGORY_META[c].color};background:${CATEGORY_META[c].color}"></span>${esc(CATEGORY_META[c].label)}<span class="count">${count((n) => n.category === c)}</span></button>`).join('')}</div>`;
        note = allOn ? 'Click a family to hide it; shift-click to show it alone.' : `<button data-reset style="border:0;background:none;color:var(--accent-2);padding:0;cursor:pointer">Show all families</button>`;
        break;
      }
      case 'domain': {
        title = 'Body system';
        const doms = Object.keys(DOMAIN_META) as Domain[];
        body = `<div class="legend-items" style="grid-template-columns:1fr 1fr;gap:2px 10px">${doms.map((d) => `<span class="legend-item" style="font-size:11.5px"><span class="swatch" style="color:${DOMAIN_META[d].color};background:${DOMAIN_META[d].color}"></span>${esc(DOMAIN_META[d].label)}</span>`).join('')}</div>`;
        break;
      }
      case 'training': {
        title = 'Years of training after medical school';
        const max = Math.max(...this.graph.nodes.map((n) => n.totalYears));
        body = `<div class="legend-ramp" style="background:linear-gradient(90deg,${RAMP_AMBER.join(',')})"></div><div class="legend-ramp-labels"><span>3 years</span><span>${max} years</span></div>`;
        note = 'Shortest typical path: residency plus any fellowship.';
        break;
      }
      case 'workforce': {
        title = 'Active US physicians (log scale)';
        body = `<div class="legend-ramp" style="background:linear-gradient(90deg,${RAMP_TEAL.join(',')})"></div><div class="legend-ramp-labels"><span>~200</span><span>~120,000</span></div>`;
        note = 'Node size also follows workforce in every lens.';
        break;
      }
      case 'credential': {
        title = 'Credential type';
        const creds = Object.keys(CREDENTIAL_META) as Credential[];
        body = `<div class="legend-items">${creds.map((c) => `<span class="legend-item"><span class="swatch" style="color:${CREDENTIAL_COLORS[c]};background:${CREDENTIAL_COLORS[c]}"></span>${esc(CREDENTIAL_META[c].label)}<span class="count">${count((n) => n.credential === c)}</span></span>`).join('')}</div>`;
        break;
      }
    }
    this.el.innerHTML = `<div class="legend-card"><p class="legend-title"><span>${esc(title)}</span></p>${body}${note ? `<p class="legend-note">${note}</p>` : ''}</div>`;
  }
}
