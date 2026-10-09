import type { Graph } from './data/graph';
import { neighbours, subtree } from './data/graph';
import type { DomainId, SpecialtyNode } from './data/types';
import type { LayoutId } from './graph/layouts';
import { LAYOUT_BY_ID } from './graph/layouts';
import { bestScore } from './util/fuzzy';

export type LabelMode = 'auto' | 'all' | 'none';

export interface AppState {
  layout: LayoutId;
  hovered: string | null;
  selected: string | null;
  hiddenDomains: Set<DomainId>;
  showCross: boolean;
  labels: LabelMode;
  query: string;
  /** Ids matching the query, or null when the query is empty. */
  matches: Set<string> | null;
  /** Ordered best matches for the search dropdown. */
  matchList: string[];
}

export type StateEvent = 'change' | 'select' | 'hover' | 'layout' | 'query';
type Listener = (state: AppState) => void;

/** Per-node visual summary derived from state. */
export interface NodeVisual {
  visible: boolean;
  /** 0 = fully dimmed, 1 = normal, >1 = emphasised. */
  emphasis: number;
  /** Whether the label should be forced on. */
  labelPinned: boolean;
}

export class Store {
  readonly state: AppState = {
    layout: 'constellation',
    hovered: null,
    selected: null,
    hiddenDomains: new Set(),
    showCross: false,
    labels: 'auto',
    query: '',
    matches: null,
    matchList: [],
  };
  private listeners = new Map<StateEvent, Set<Listener>>();

  constructor(readonly graph: Graph) {}

  on(event: StateEvent, fn: Listener): () => void {
    let set = this.listeners.get(event);
    if (!set) {
      set = new Set();
      this.listeners.set(event, set);
    }
    set.add(fn);
    return () => set!.delete(fn);
  }

  private emit(event: StateEvent): void {
    this.listeners.get(event)?.forEach((fn) => fn(this.state));
    if (event !== 'change') this.listeners.get('change')?.forEach((fn) => fn(this.state));
  }

  setLayout(layout: LayoutId): void {
    if (this.state.layout === layout) return;
    this.state.layout = layout;
    this.emit('layout');
  }

  setHovered(id: string | null): void {
    if (this.state.hovered === id) return;
    this.state.hovered = id;
    this.emit('hover');
  }

  select(id: string | null): void {
    if (this.state.selected === id) return;
    this.state.selected = id;
    this.emit('select');
  }

  toggleDomain(id: DomainId): void {
    if (this.state.hiddenDomains.has(id)) this.state.hiddenDomains.delete(id);
    else this.state.hiddenDomains.add(id);
    this.emit('change');
  }

  /** Show only this domain, or all domains if it is already the only one shown. */
  soloDomain(id: DomainId): void {
    const all = this.graph.domains.map((d) => d.id);
    const onlyThis = this.state.hiddenDomains.size === all.length - 1 && !this.state.hiddenDomains.has(id);
    this.state.hiddenDomains = onlyThis ? new Set() : new Set(all.filter((d) => d !== id));
    this.emit('change');
  }

  showAllDomains(): void {
    if (!this.state.hiddenDomains.size) return;
    this.state.hiddenDomains = new Set();
    this.emit('change');
  }

  setShowCross(v: boolean): void {
    if (this.state.showCross === v) return;
    this.state.showCross = v;
    this.emit('change');
  }

  setLabels(mode: LabelMode): void {
    if (this.state.labels === mode) return;
    this.state.labels = mode;
    this.emit('change');
  }

  setQuery(q: string): void {
    this.state.query = q;
    const trimmed = q.trim();
    if (!trimmed) {
      this.state.matches = null;
      this.state.matchList = [];
    } else {
      const scored: { id: string; score: number }[] = [];
      for (const n of this.graph.nodes) {
        if (n.level === 'root') continue;
        const score = bestScore(trimmed, [n.name, n.short ?? '', ...(n.aliases ?? []), n.board ?? '']);
        if (score > 0) scored.push({ id: n.id, score });
      }
      scored.sort((a, b) => b.score - a.score);
      this.state.matchList = scored.slice(0, 12).map((s) => s.id);
      this.state.matches = new Set(scored.map((s) => s.id));
    }
    this.emit('query');
  }

  /** Whether a node is hidden by the domain filter or layout. */
  isNodeVisible(node: SpecialtyNode): boolean {
    if (node.level === 'root') return true;
    if (this.state.hiddenDomains.has(node.domain)) return false;
    if (node.level === 'domain' && !LAYOUT_BY_ID[this.state.layout].showDomains) return false;
    return true;
  }

  /** Compute the per-node visual summary for the current state. */
  computeVisuals(): Map<string, NodeVisual> {
    const { graph, state } = this;
    const out = new Map<string, NodeVisual>();
    const focusId = state.selected ?? null;
    let focusSet: Set<string> | null = null;
    if (focusId) {
      focusSet = subtree(graph, focusId);
      const node = graph.byId.get(focusId)!;
      for (const p of node.path) focusSet.add(p);
      for (const nb of neighbours(graph, focusId)) focusSet.add(nb);
      // Include the ancestry of secondary parents so cross-board routes read as paths.
      for (const p of node.parents.slice(1)) {
        const parent = graph.byId.get(p);
        if (parent) for (const a of parent.path) focusSet.add(a);
      }
    }
    const hoverSet = state.hovered ? neighbours(graph, state.hovered) : null;

    for (const n of graph.nodes) {
      const visible = this.isNodeVisible(n);
      let emphasis = 1;
      let labelPinned = false;
      if (state.matches) {
        emphasis = state.matches.has(n.id) ? 1.15 : 0.12;
        labelPinned = state.matches.has(n.id) && state.matchList.includes(n.id);
      } else if (focusSet) {
        if (n.id === focusId) {
          emphasis = 1.4;
          labelPinned = true;
        } else if (focusSet.has(n.id)) {
          emphasis = 1.05;
          labelPinned = true;
        } else emphasis = 0.14;
      }
      if (state.hovered === n.id) {
        emphasis = Math.max(emphasis, 1.5);
        labelPinned = true;
      } else if (hoverSet?.has(n.id)) {
        emphasis = Math.max(emphasis, 1.1);
        labelPinned = labelPinned || !state.matches;
      }
      out.set(n.id, { visible, emphasis, labelPinned });
    }
    return out;
  }
}
