import { DOMAIN_BY_ID, SYSTEM_BY_ID } from '../data/domains';
import type { Graph } from '../data/graph';
import type { SpecialtyNode } from '../data/types';
import type { Store } from '../state';
import { withAlpha } from '../util/color';
import { CERTIFICATION_LABEL, escapeHtml, LEVEL_LABEL, yearsLabel } from './format';

/** The detail panel on the right: pathway, description, children and related disciplines. */
export function setupPanel(graph: Graph, store: Store): void {
  const panel = document.getElementById('panel')!;
  const content = document.getElementById('panel-content')!;
  const close = document.getElementById('panel-close')!;

  close.addEventListener('click', () => store.select(null));

  content.addEventListener('click', (ev) => {
    const target = (ev.target as HTMLElement).closest<HTMLElement>('[data-go]');
    if (target?.dataset.go) store.select(target.dataset.go);
  });

  const render = (node: SpecialtyNode | null) => {
    if (!node) {
      panel.classList.remove('open');
      document.body.classList.remove('panel-open');
      panel.setAttribute('hidden', '');
      return;
    }
    panel.removeAttribute('hidden');
    requestAnimationFrame(() => panel.classList.add('open'));
    document.body.classList.add('panel-open');
    content.innerHTML = html(graph, node);
    content.scrollTop = 0;
    (document.querySelector('.panel-inner') as HTMLElement).scrollTop = 0;
  };

  store.on('select', (s) => render(s.selected ? graph.byId.get(s.selected) ?? null : null));
}

function chip(graph: Graph, id: string): string {
  const n = graph.byId.get(id)!;
  const color = n.level === 'root' ? '#ffffff' : DOMAIN_BY_ID[n.domain]!.color;
  return `<li><button type="button" data-go="${n.id}"><span class="swatch" style="color:${color};background:${color}"></span>${escapeHtml(
    n.name,
  )}</button></li>`;
}

function html(graph: Graph, node: SpecialtyNode): string {
  const domain = node.level === 'root' ? null : DOMAIN_BY_ID[node.domain]!;
  const color = domain?.color ?? '#ffffff';
  const system = SYSTEM_BY_ID[node.system]!;

  const crumbNodes = node.path.slice(0, -1).map((id) => graph.byId.get(id)!);
  const crumbs = crumbNodes
    // A domain that shares its name with the specialty below it (Pediatrics) is shown once.
    .filter((c, i) => c.name !== (crumbNodes[i + 1] ?? node).name)
    .map((c) => `<button type="button" data-go="${c.id}">${escapeHtml(c.name)}</button>`)
    .join('<span class="sep">›</span>');

  const badges: string[] = [];
  badges.push(`<span class="badge">${LEVEL_LABEL[node.level]}</span>`);
  if (domain)
    badges.push(
      `<span class="badge domain" style="background:${withAlpha(color, 0.22)};box-shadow: inset 0 0 0 1px ${withAlpha(color, 0.5)}">${escapeHtml(
        domain.name,
      )}</span>`,
    );
  if (node.level !== 'root' && node.level !== 'domain') badges.push(`<span class="badge">${CERTIFICATION_LABEL[node.certification]}</span>`);

  // Training pathway along the home path (skipping organisational nodes).
  const steps = node.path.map((id) => graph.byId.get(id)!).filter((n) => n.level !== 'domain');
  const pathway = steps
    .map((s) => {
      const c = s.level === 'root' ? '#ffffff' : DOMAIN_BY_ID[s.domain]!.color;
      const label = s.level === 'root' ? 'Medical school' : s.name;
      const yrs = s.level === 'root' ? '4 yrs' : yearsLabel(s.years);
      return `<li class="${s.id === node.id ? 'current' : ''}"><span class="step" style="color:${c};background:${c}"></span><button type="button" data-go="${s.id}">${escapeHtml(
        label,
      )}</button><span class="yrs">${yrs}</span></li>`;
    })
    .join('');

  const otherParents = node.parents.slice(1);
  const children = node.children;
  const related = graph.nodes
    .filter((n) => n.system === node.system && n.id !== node.id && n.level !== 'domain' && n.level !== 'root' && !children.includes(n.id) && !node.path.includes(n.id))
    .slice(0, 14);

  const totalLine =
    node.level === 'root' || node.level === 'domain'
      ? ''
      : `<p class="total">Typical total after medical school: <b>${node.totalYears} year${node.totalYears === 1 ? '' : 's'}</b>${
          node.yearsNote ? '' : ''
        }</p>${node.yearsNote ? `<p class="note">${escapeHtml(node.yearsNote)}</p>` : ''}`;

  const kv: string[] = [];
  if (node.board) kv.push(`<dt>Board</dt><dd>${escapeHtml(node.board)}</dd>`);
  const also = [...new Set([node.short, ...(node.aliases ?? [])].filter((a): a is string => !!a && a !== node.name))];
  if (also.length) kv.push(`<dt>Also</dt><dd>${escapeHtml(also.join(' · '))}</dd>`);
  kv.push(`<dt>Focus</dt><dd>${escapeHtml(system.name)}</dd>`);

  return `
    <nav class="crumbs">${crumbs}</nav>
    <h2 class="panel-title">${escapeHtml(node.name)}</h2>
    ${node.short ? `<p class="panel-sub">${escapeHtml(node.short)}</p>` : ''}
    <div class="badges">${badges.join('')}</div>
    <p class="panel-desc">${escapeHtml(node.description)}</p>
    ${
      node.level === 'root' || node.level === 'domain'
        ? ''
        : `<section class="section"><h3>Training pathway</h3><ol class="pathway">${pathway}</ol>${totalLine}</section>`
    }
    <section class="section"><h3>Details</h3><dl class="kv">${kv.join('')}</dl></section>
    ${
      otherParents.length
        ? `<section class="section"><h3>Also reached from</h3><ul class="chips">${otherParents.map((p) => chip(graph, p)).join('')}</ul></section>`
        : ''
    }
    ${
      children.length
        ? `<section class="section"><h3>${node.level === 'root' ? 'Domains' : node.level === 'domain' ? 'Specialties' : 'Subspecialties'} (${children.length})</h3><ul class="chips">${children
            .map((c) => chip(graph, c))
            .join('')}</ul></section>`
        : ''
    }
    ${
      related.length && node.level !== 'root' && node.level !== 'domain'
        ? `<section class="section"><h3>Same focus · ${escapeHtml(system.name)}</h3><ul class="chips">${related.map((r) => chip(graph, r.id)).join('')}</ul></section>`
        : ''
    }
  `;
}
