import { ERAS, ERA_BY_KEY } from './eras.js';
import { fmtDuration, fmtHours } from './data.js';

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const MODE_NOTES = {
  highway: 'Album by album down Highway 61. Every song is a sign on a stake; the taller the stake, the longer the song. The far shoulder carries songs that never made an album.',
  years: 'One column per year, 1961 to 2020. Every card is a song, stacked in the order it was released.',
  records: 'Each album is a record with its cards fanned around the rim. A song\'s place on the rim is its share of the running time, from twelve o\'clock.',
  lexicon: 'The sixteen words that recur most in Dylan\'s titles, and the songs that carry them.',
};

export function setupUI(catalog, on) {
  const { songs, albums, stats } = catalog;
  const legend = $('#legend');
  const panel = $('#panel');
  const tooltip = $('#tooltip');
  const search = $('#search');
  const results = $('#results');
  const modeBtns = [...document.querySelectorAll('.mode')];
  const eraCounts = {};
  songs.forEach((s) => (eraCounts[s.era] = (eraCounts[s.era] || 0) + 1));

  // stats line
  $('#stats').innerHTML = `<b>${stats.songs}</b> songs · <b>${stats.albums}</b> albums · <b>${fmtHours(stats.totalSec)}</b> of music<br>` +
    `${stats.originals} originals · ${stats.covers} covers &amp; traditionals · ${stats.nonAlbum} never on an album`;
  $('#intro-sub').textContent = `${stats.songs} songs. ${stats.albums} albums. ${fmtHours(stats.totalSec)} of music, ${stats.firstYear} to ${stats.lastYear}.`;

  // legend
  ERAS.forEach((e) => {
    const b = document.createElement('button');
    b.className = 'era'; b.dataset.era = e.key; b.style.color = e.color;
    b.innerHTML = `<span class="sw" style="background:${e.color}"></span><span class="nm">${esc(e.name)}</span><span class="yr">${esc(e.years)}</span><span class="ct">${eraCounts[e.key] || 0}</span>`;
    b.title = e.blurb;
    b.addEventListener('click', (ev) => on.era(e.key, ev.shiftKey));
    b.addEventListener('mouseenter', () => on.hoverEra(e.key));
    b.addEventListener('mouseleave', () => on.hoverEra(null));
    legend.appendChild(b);
  });

  // modes
  modeBtns.forEach((b) => b.addEventListener('click', () => on.mode(b.dataset.mode)));

  // search
  let active = -1, matches = [];
  const renderResults = () => {
    results.innerHTML = matches.map((s, i) => `<li data-i="${s.index}" class="${i === active ? 'active' : ''}"><span class="dot" style="background:${s.color}"></span><span class="t">${esc(s.title)}</span><span class="a">${esc(s.nonAlbum ? s.year : s.album.title + ' · ' + s.year)}</span></li>`).join('');
    results.classList.toggle('open', matches.length > 0);
  };
  const runSearch = () => {
    const q = search.value.trim().toLowerCase();
    if (q.length < 2) { matches = []; active = -1; renderResults(); on.search(''); return; }
    const scored = [];
    songs.forEach((s) => {
      const t = s.title.toLowerCase();
      let sc = -1;
      if (t.startsWith(q)) sc = 3; else if (t.includes(q)) sc = 2;
      else if (s.album.title.toLowerCase().includes(q)) sc = 1;
      else if (s.writer.toLowerCase().includes(q) && s.cover) sc = 0.5;
      if (sc > 0) scored.push([sc, s]);
    });
    scored.sort((a, b) => b[0] - a[0] || a[1].index - b[1].index);
    matches = scored.slice(0, 14).map((x) => x[1]);
    active = matches.length ? 0 : -1;
    renderResults();
    on.search(q);
  };
  search.addEventListener('input', runSearch);
  search.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') { active = Math.min(matches.length - 1, active + 1); renderResults(); ev.preventDefault(); }
    else if (ev.key === 'ArrowUp') { active = Math.max(0, active - 1); renderResults(); ev.preventDefault(); }
    else if (ev.key === 'Enter') { if (matches[active]) { on.select(matches[active]); closeSearch(); } }
    else if (ev.key === 'Escape') { search.value = ''; runSearch(); search.blur(); }
    ev.stopPropagation();
  });
  results.addEventListener('click', (ev) => {
    const li = ev.target.closest('li'); if (!li) return;
    on.select(songs[+li.dataset.i]); closeSearch();
  });
  const closeSearch = () => { results.classList.remove('open'); search.blur(); };
  document.addEventListener('pointerdown', (ev) => { if (!ev.target.closest('#search-wrap')) results.classList.remove('open'); });

  // tour / help
  $('#tour').addEventListener('click', () => on.tour());
  $('#random').addEventListener('click', () => on.random());
  $('#help-btn').addEventListener('click', () => $('#help').classList.remove('hidden'));
  $('#help-close').addEventListener('click', () => $('#help').classList.add('hidden'));
  $('#help').addEventListener('click', (ev) => { if (ev.target.id === 'help') $('#help').classList.add('hidden'); });

  const songMeta = (s) => {
    const era = ERA_BY_KEY[s.era];
    const where = s.nonAlbum
      ? `Recorded <b>${s.year}</b> · first released on <b>${esc(s.source)}</b>`
      : `Track <b>${s.disc ? `${s.disc}.${((s.n - 1) % 10) + 1}` : s.n}</b> on <button class="album-link" data-album="${s.album.index}">${esc(s.album.title)}</button> · <b>${s.year}</b>`;
    const by = s.cover ? `Written by <b>${esc(s.writer)}</b>` : s.cowrite ? `Written by <b>${esc(s.writer)}</b>` : 'Written by <b>Bob Dylan</b>';
    const extras = [s.live ? 'live recording' : null, s.inst ? 'instrumental' : null, s.feat ? `with ${s.feat}` : null].filter(Boolean).join(' · ');
    return `<span class="era-tag" style="color:${era.color}"><i style="background:${era.color}"></i>${esc(era.name)}</span>
      <h2>${esc(s.title)}</h2>
      <div class="meta">${where}<br>${by} · <b>${fmtDuration(s.sec)}</b>${extras ? `<br>${esc(extras)}` : ''}</div>
      ${s.note ? `<p class="note">${esc(s.note)}</p>` : ''}
      <div class="row"><button class="btn" data-nav="-1">← prev</button><button class="btn" data-nav="1">next →</button></div>`;
  };

  const albumMeta = (a) => {
    const era = a.era ? ERA_BY_KEY[a.era] : null;
    const covers = a.tracks.filter((t) => t.cover).length;
    const head = era ? `<span class="era-tag" style="color:${era.color}"><i style="background:${era.color}"></i>${esc(era.name)}</span>` : '<span class="era-tag">Singles, B-sides &amp; outtakes</span>';
    const meta = a.kind === 'album'
      ? `<b>${a.year}</b>${a.recorded ? ` (recorded ${a.recorded})` : ''} · ${esc(a.label)} · produced by ${esc(a.producer)}<br><b>${a.tracks.length}</b> tracks · <b>${fmtDuration(a.duration)}</b>${covers ? ` · ${covers} cover${covers > 1 ? 's' : ''}` : ' · all originals'}`
      : `<b>${a.tracks.length}</b> songs · <b>${fmtHours(a.duration)}</b>`;
    const list = a.tracks.map((t) => `<li data-i="${t.index}"><span class="n">${a.kind === 'album' ? t.n : t.year}</span><span class="${t.cover ? 'cv' : ''}">${esc(t.title)}</span><span class="d">${fmtDuration(t.sec)}</span></li>`).join('');
    return `${head}<h2>${esc(a.title)}</h2><div class="meta">${meta}</div>${a.note ? `<p class="note">${esc(a.note)}</p>` : ''}<ul class="tracklist">${list}</ul>`;
  };

  const eraMeta = (e) => {
    const list = albums.filter((a) => a.era === e.key);
    const es = songs.filter((x) => x.era === e.key);
    const originals = es.filter((x) => !x.cover).length;
    const sec = es.reduce((n, x) => n + x.sec, 0);
    return `<span class="era-tag" style="color:${e.color}"><i style="background:${e.color}"></i>${esc(e.years)}</span>
      <h2>${esc(e.name)}</h2>
      <div class="meta"><b>${es.length}</b> songs · <b>${originals}</b> originals · <b>${fmtHours(sec)}</b><br>${es.length - list.reduce((n, a) => n + a.tracks.length, 0)} of them never on an album</div>
      <p class="note">${esc(e.blurb)}</p>
      <ul class="tracklist">${list.map((a) => `<li data-album="${a.index}"><span class="n">${a.year}</span><span>${esc(a.title)}</span><span class="d">${a.tracks.length} tracks</span></li>`).join('')}</ul>`;
  };

  const numbers = () => {
    const byYear = new Map();
    songs.forEach((x) => byYear.set(x.year, (byYear.get(x.year) || 0) + 1));
    const [bestYear, bestN] = [...byYear.entries()].sort((a, b) => b[1] - a[1])[0];
    const studio = albums.filter((a) => a.kind === 'album');
    const longestAlbum = studio.reduce((a, b) => (b.duration > a.duration ? b : a));
    const shortestAlbum = studio.reduce((a, b) => (b.duration < a.duration ? b : a));
    const mostCovers = studio.map((a) => [a, a.tracks.filter((t) => t.cover).length]).sort((a, b) => b[1] - a[1])[0];
    const allOriginal = studio.filter((a) => a.tracks.every((t) => !t.cover)).length;
    const avg = songs.reduce((n, x) => n + x.sec, 0) / songs.length;
    const over10 = songs.filter((x) => x.sec >= 600);
    return [
      ['Longest song', `${stats.longest.title} · ${fmtDuration(stats.longest.sec)}`],
      ['Shortest song', `${stats.shortest.title} · ${fmtDuration(stats.shortest.sec)}`],
      ['Over ten minutes', `${over10.length} songs: ${over10.map((x) => x.title).join(', ')}`],
      ['Average length', fmtDuration(avg)],
      ['Busiest year', `${bestYear}, with ${bestN} songs`],
      ['Longest album', `${longestAlbum.title} · ${fmtDuration(longestAlbum.duration)}`],
      ['Shortest album', `${shortestAlbum.title} · ${fmtDuration(shortestAlbum.duration)}`],
      ['Most covers', `${mostCovers[0].title} · ${mostCovers[1]} of ${mostCovers[0].tracks.length}`],
      ['All-original albums', `${allOriginal} of ${studio.length}`],
    ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  };
  $('#numbers').innerHTML = numbers();

  let toastTimer = null;
  const toast = (msg) => {
    const t = $('#toast'); t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 1800);
  };
  $('#share').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(location.href); toast('Link copied'); }
    catch { toast(location.href); }
  });

  const openPanel = (html) => {
    panel.innerHTML = `<button class="close" aria-label="close">×</button>${html}`;
    panel.classList.add('open');
  };
  panel.addEventListener('click', (ev) => {
    const t = ev.target;
    if (t.closest('.close')) { on.clear(); return; }
    const nav = t.closest('[data-nav]'); if (nav) { on.step(+nav.dataset.nav); return; }
    const al = t.closest('[data-album]'); if (al) { on.album(albums[+al.dataset.album]); return; }
    const li = t.closest('li[data-i]'); if (li) { on.select(songs[+li.dataset.i]); }
  });

  const api = {
    setMode(name) {
      modeBtns.forEach((b) => b.classList.toggle('active', b.dataset.mode === name));
      $('#mode-note').textContent = MODE_NOTES[name] || '';
    },
    setEraFilter(set) {
      legend.classList.toggle('filtering', set.size > 0);
      [...legend.children].forEach((b) => b.classList.toggle('on', set.has(b.dataset.era)));
    },
    showSong(s) { openPanel(songMeta(s)); },
    showEra(key) { openPanel(eraMeta(ERA_BY_KEY[key])); },
    toast,
    showAlbum(a, current) {
      openPanel(albumMeta(a));
      if (current) {
        const li = panel.querySelector(`li[data-i="${current.index}"]`);
        if (li) { li.classList.add('cur'); li.scrollIntoView({ block: 'nearest' }); }
      }
    },
    hidePanel() { panel.classList.remove('open'); },
    tooltip(s, x, y) {
      if (!s) { tooltip.classList.remove('show'); return; }
      tooltip.innerHTML = `<div class="t">${esc(s.title)}</div><div class="a">${esc(s.nonAlbum ? `${s.year} · ${s.source}` : `${s.album.title} · ${s.year}`)} · ${fmtDuration(s.sec)}</div>`;
      const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
      tooltip.style.left = `${Math.min(x, innerWidth - w - 24)}px`;
      tooltip.style.top = `${Math.min(y, innerHeight - h - 24)}px`;
      tooltip.classList.add('show');
    },
    setTour(onOff) { $('#tour').classList.toggle('on', onOff); $('#tour').textContent = onOff ? '■ Stop tour' : '▶ Tour'; },
    focusSearch() { search.focus(); search.select(); },
    clearSearch() { search.value = ''; matches = []; renderResults(); },
    toggleHelp() { $('#help').classList.toggle('hidden'); },
  };
  return api;
}
