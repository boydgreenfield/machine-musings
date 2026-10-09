import { DOMAIN_BY_ID } from '../data/domains';
import type { Graph } from '../data/graph';
import type { Store } from '../state';
import { escapeHtml } from './format';

export function setupSearch(graph: Graph, store: Store): { focus: () => void } {
  const form = document.getElementById('search') as HTMLFormElement;
  const input = document.getElementById('search-input') as HTMLInputElement;
  const clear = document.getElementById('search-clear') as HTMLButtonElement;
  const results = document.getElementById('search-results') as HTMLUListElement;
  let active = -1;

  const renderResults = () => {
    const ids = store.state.matchList;
    results.innerHTML = ids
      .map((id, i) => {
        const n = graph.byId.get(id)!;
        const color = DOMAIN_BY_ID[n.domain]!.color;
        const path = n.path
          .slice(1, -1)
          .map((p) => graph.byId.get(p)!.name)
          .join(' › ');
        return `<li role="option" data-id="${id}" aria-selected="${i === active}"><span class="swatch" style="color:${color};background:${color}"></span><span class="r-name">${escapeHtml(
          n.name,
        )}</span><span class="r-path">${escapeHtml(path)}</span></li>`;
      })
      .join('');
    results.toggleAttribute('hidden', ids.length === 0 || document.activeElement !== input);
    clear.toggleAttribute('hidden', !input.value);
  };

  const choose = (id: string) => {
    store.select(id);
    input.value = '';
    store.setQuery('');
    active = -1;
    renderResults();
    input.blur();
  };

  input.addEventListener('input', () => {
    active = -1;
    store.setQuery(input.value);
    renderResults();
  });
  input.addEventListener('focus', renderResults);
  input.addEventListener('blur', () => setTimeout(() => results.setAttribute('hidden', ''), 120));
  input.addEventListener('keydown', (ev) => {
    const ids = store.state.matchList;
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      active = Math.min(ids.length - 1, active + 1);
      renderResults();
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      active = Math.max(-1, active - 1);
      renderResults();
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      const id = ids[active >= 0 ? active : 0];
      if (id) choose(id);
    } else if (ev.key === 'Escape') {
      input.value = '';
      store.setQuery('');
      renderResults();
      input.blur();
    }
    ev.stopPropagation();
  });
  form.addEventListener('submit', (ev) => ev.preventDefault());
  results.addEventListener('mousedown', (ev) => {
    const li = (ev.target as HTMLElement).closest<HTMLLIElement>('li[data-id]');
    if (li?.dataset.id) {
      ev.preventDefault();
      choose(li.dataset.id);
    }
  });
  clear.addEventListener('click', () => {
    input.value = '';
    store.setQuery('');
    renderResults();
    input.focus();
  });

  return {
    focus: () => {
      input.focus();
      input.select();
    },
  };
}
