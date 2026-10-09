import { DOMAIN_BY_ID } from '../data/domains';
import type { Graph } from '../data/graph';
import type { Store } from '../state';
import { escapeHtml, LEVEL_LABEL, yearsLabel } from './format';

/** A small quick-facts card that follows the pointer over a hovered node. */
export function setupTooltip(graph: Graph, store: Store, canvasHost: HTMLElement): void {
  const el = document.getElementById('tooltip')!;
  let x = 0;
  let y = 0;
  const place = () => {
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const left = x + 14 + w > vw - 8 ? x - w - 14 : x;
    const top = y + 14 + h > vh - 8 ? y - h - 14 : y;
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  };
  canvasHost.addEventListener('pointermove', (ev) => {
    x = ev.clientX;
    y = ev.clientY;
    if (!el.hasAttribute('hidden')) place();
  });
  store.on('hover', (s) => {
    const n = s.hovered ? graph.byId.get(s.hovered) : null;
    if (!n || n.id === s.selected) {
      el.setAttribute('hidden', '');
      return;
    }
    const domain = n.level === 'root' ? null : DOMAIN_BY_ID[n.domain]!;
    const meta =
      n.level === 'root' || n.level === 'domain'
        ? LEVEL_LABEL[n.level]
        : `${LEVEL_LABEL[n.level]} · ${yearsLabel(n.years)}${n.totalYears !== n.years ? ` (${n.totalYears} yrs total)` : ''}`;
    el.innerHTML = `<div class="t-name">${domain ? `<span class="swatch" style="color:${domain.color};background:${domain.color};display:inline-block;margin-right:6px"></span>` : ''}${escapeHtml(
      n.name,
    )}</div><div class="t-meta">${escapeHtml(meta)}</div>${n.board ? `<div class="t-board">${escapeHtml(n.board)}</div>` : ''}`;
    el.removeAttribute('hidden');
    place();
  });
  store.on('select', () => el.setAttribute('hidden', ''));
}
