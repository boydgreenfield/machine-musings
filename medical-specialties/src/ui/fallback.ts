import type { Graph } from '../data/graph';
import { DOMAIN_BY_ID } from '../data/domains';
import { escapeHtml, yearsLabel } from './format';

/** When WebGL is unavailable, render the hierarchy as a plain nested list. */
export function renderFallback(graph: Graph, reason: string): void {
  const app = document.getElementById('app')!;
  const el = document.createElement('div');
  el.id = 'fallback';
  const item = (id: string): string => {
    const n = graph.byId.get(id)!;
    const color = n.level === 'root' ? '#fff' : DOMAIN_BY_ID[n.domain]!.color;
    const years = n.level === 'root' || n.level === 'domain' ? '' : ` <span class="fb-years">${yearsLabel(n.years)}</span>`;
    const kids = n.children.length ? `<ul>${n.children.map(item).join('')}</ul>` : '';
    return `<li><span class="swatch" style="color:${color};background:${color}"></span><strong>${escapeHtml(n.name)}</strong>${years}<p>${escapeHtml(
      n.description,
    )}</p>${kids}</li>`;
  };
  el.innerHTML = `
    <div class="fb-inner">
      <h1>Atlas of Medical Specialties</h1>
      <p class="fb-note">The three-dimensional atlas needs WebGL, which this browser could not start (${escapeHtml(reason)}). Here is the same map as a list.</p>
      <ul class="fb-tree">${item(graph.root.id)}</ul>
    </div>`;
  app.replaceChildren(el);
}
