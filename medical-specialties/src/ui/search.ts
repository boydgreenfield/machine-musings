import { CATEGORY_META } from '../data/schema';
import type { Graph, GraphNode } from '../graph';
import type { Store } from '../state';

interface Hit { node: GraphNode; score: number; ranges: [number, number][] }

function esc(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}

/** Subsequence match with a preference for word starts and contiguous runs. */
export function fuzzy(query: string, text: string): { score: number; ranges: [number, number][] } | null {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (!q) return { score: 0, ranges: [] };
  const idx = t.indexOf(q);
  if (idx >= 0) {
    const wordStart = idx === 0 || /[\s\-–&/(]/.test(t[idx - 1]);
    return { score: 100 + (wordStart ? 40 : 0) - idx * 0.5 - (t.length - q.length) * 0.1, ranges: [[idx, idx + q.length]] };
  }
  // Subsequence
  let ti = 0, score = 0, run = 0;
  const ranges: [number, number][] = [];
  const sep = /[\s\-–&/(]/;
  for (let qi = 0; qi < q.length; qi++) {
    const c = q[qi];
    if (c === ' ') continue;
    // Each character must either continue the current run or start a new word,
    // so "pem" matches "Pediatric Emergency Medicine" but "onco" cannot match "urOgyNeCOlogy".
    let at = -1;
    if (t[ti] === c) at = ti;
    else for (let k = t.indexOf(c, ti); k >= 0; k = t.indexOf(c, k + 1)) { if (k === 0 || sep.test(t[k - 1])) { at = k; break; } }
    if (at < 0) return null;
    const wordStart = at === 0 || sep.test(t[at - 1]);
    run = at === ti ? run + 1 : 0;
    score += (wordStart ? 8 : 1) + run * 3 - (at - ti) * 0.3;
    const last = ranges[ranges.length - 1];
    if (last && last[1] === at) last[1] = at + 1; else ranges.push([at, at + 1]);
    ti = at + 1;
  }
  return { score: score - (t.length - q.length) * 0.05, ranges };
}

export class Search {
  private hits: Hit[] = [];
  private cursor = -1;
  private readonly input: HTMLInputElement;
  private readonly list: HTMLUListElement;
  private readonly clear: HTMLButtonElement;

  constructor(root: HTMLElement, private readonly graph: Graph, private readonly store: Store, private readonly onPick: (n: GraphNode) => void) {
    this.input = root.querySelector('#search-input')!;
    this.list = root.querySelector('#search-results')!;
    this.clear = root.querySelector('#search-clear')!;
    this.input.addEventListener('input', () => this.update());
    this.input.addEventListener('focus', () => { if (this.input.value) this.update(); });
    this.input.addEventListener('keydown', (e) => this.onKey(e));
    this.clear.addEventListener('click', () => this.reset(true));
    this.list.addEventListener('click', (e) => {
      const li = (e.target as HTMLElement).closest<HTMLElement>('li[data-id]');
      if (li) this.pick(this.graph.byId.get(li.dataset.id!)!);
    });
    document.addEventListener('pointerdown', (e) => {
      if (!root.contains(e.target as Node)) this.list.hidden = true;
    });
  }

  focus() { this.input.focus(); this.input.select(); }
  get active() { return document.activeElement === this.input; }

  reset(refocus = false) {
    this.input.value = '';
    this.hits = [];
    this.cursor = -1;
    this.list.hidden = true;
    this.clear.hidden = true;
    this.store.set({ searchHits: null });
    if (refocus) this.input.focus();
  }

  private update() {
    const q = this.input.value.trim();
    this.clear.hidden = !q;
    if (!q) { this.hits = []; this.list.hidden = true; this.store.set({ searchHits: null }); return; }
    const hits: Hit[] = [];
    for (const node of this.graph.nodes) {
      if (node.tier === 'root') continue;
      const candidates = [node.name, node.short ?? '', ...(node.aka ?? []), ...node.boards];
      let best: { score: number; ranges: [number, number][] } | null = null;
      let bestText = node.name;
      for (const c of candidates) {
        if (!c) continue;
        const r = fuzzy(q, c);
        if (r && (!best || r.score > best.score)) { best = r; bestText = c; }
      }
      // Also allow matching against description keywords for things like "stroke" or "cancer".
      if (!best && node.description.toLowerCase().includes(q.toLowerCase())) best = { score: 5, ranges: [] };
      if (best) hits.push({ node, score: best.score + Math.log10(node.workforce ?? 100) + (bestText === node.name ? 0 : -2), ranges: bestText === node.name ? best.ranges : [] });
    }
    hits.sort((a, b) => b.score - a.score);
    this.hits = hits.slice(0, 40);
    this.cursor = this.hits.length ? 0 : -1;
    this.renderList();
    this.store.set({ searchHits: new Set(hits.map((h) => h.node.id)) });
  }

  private renderList() {
    if (!this.hits.length) {
      this.list.innerHTML = `<li class="empty">No matches</li>`;
      this.list.hidden = false;
      return;
    }
    this.list.innerHTML = this.hits.slice(0, 12).map((h, i) => {
      const c = CATEGORY_META[h.node.category].color;
      let name = '';
      let last = 0;
      for (const [a, b] of h.ranges) { name += esc(h.node.name.slice(last, a)) + '<mark>' + esc(h.node.name.slice(a, b)) + '</mark>'; last = b; }
      name += esc(h.node.name.slice(last));
      const meta = h.node.tier === 'specialty' ? 'Specialty' : h.node.tier === 'fellowship' ? 'Fellowship' : h.node.isBridge ? `${h.node.parentNodes.length}-board` : (h.node.parentNodes[0]?.short ?? h.node.parentNodes[0]?.name ?? '');
      return `<li role="option" data-id="${h.node.id}" aria-selected="${i === this.cursor}"><span class="dot" style="color:${c};background:${c}"></span><span class="name">${name}</span><span class="meta">${esc(meta)}</span></li>`;
    }).join('') + (this.hits.length > 12 ? `<li class="empty">…and ${this.hits.length - 12} more highlighted in the map</li>` : '');
    this.list.hidden = false;
  }

  private onKey(e: KeyboardEvent) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!this.hits.length) return;
      const n = Math.min(12, this.hits.length);
      this.cursor = (this.cursor + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
      this.renderList();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const h = this.hits[this.cursor] ?? this.hits[0];
      if (h) this.pick(h.node);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      if (this.input.value) this.reset(true); else this.input.blur();
    }
  }

  private pick(n: GraphNode) {
    this.list.hidden = true;
    this.input.blur();
    this.onPick(n);
  }
}
