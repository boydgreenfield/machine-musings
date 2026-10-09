import albums1 from '../data/albums-1.json';
import albums2 from '../data/albums-2.json';
import nonAlbum from '../data/non-album.json';
import { ERA_BY_KEY, eraForYear } from './eras.js';

// 'roots' was folded into 'late' for the colour scheme; keep data files readable.
const ERA_ALIAS = { roots: 'late' };

export function parseDuration(d) {
  const [m, s] = d.split(':').map(Number);
  return m * 60 + s;
}

export function fmtDuration(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function fmtHours(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

const STOP = new Set(('a an the of to in on and or for with at by from is it its i you me my your ' +
  'we us our he she they them his her their be been am are was were do don\'t does did no not so as ' +
  'that this these those there here if but than then too very just only one all some any ' +
  'll ve re s t d m up down out off over into about like oh ya ma now ' +
  'when gonna go ill youre o let more way comes shall new got get can').split(' '));

export function titleWords(title) {
  return title
    .toLowerCase()
    .replace(/\(.*?\)/g, ' ')
    .replace(/['’]s\b/g, '')
    .replace(/['’]/g, '')
    .replace(/[^a-z0-9#]+/g, ' ')
    .split(' ')
    .filter((w) => w && !STOP.has(w) && !/^\d+$/.test(w));
}

export function buildCatalog() {
  const albums = [];
  const songs = [];
  const rawAlbums = [...albums1, ...albums2];

  rawAlbums.forEach((a) => {
    const released = new Date(a.released);
    const timeline = a.recorded ? new Date(a.recorded) : released;
    const album = {
      id: a.id,
      title: a.title,
      released,
      year: released.getUTCFullYear(),
      timelineYear: timeline.getUTCFullYear() + (timeline.getUTCMonth() + 0.5) / 12,
      recorded: a.recorded ? new Date(a.recorded).getUTCFullYear() : null,
      era: ERA_ALIAS[a.era] || a.era,
      label: a.label,
      producer: a.producer,
      feat: a.feat || null,
      note: a.note || '',
      tracks: [],
      duration: 0,
      index: albums.length,
      kind: 'album',
    };
    a.tracks.forEach((t) => {
      const sec = parseDuration(t.d);
      const song = {
        id: `${a.id}-${t.n}`,
        title: t.t,
        album,
        albumIndex: album.index,
        n: t.n,
        disc: t.disc || null,
        sec,
        year: album.year,
        timelineYear: album.timelineYear,
        era: album.era,
        writer: t.w || 'Bob Dylan',
        cover: !!t.w && !/^Bob Dylan( \/|$)/.test(t.w),
        cowrite: !!t.w && /^Bob Dylan \//.test(t.w),
        live: !!t.live,
        inst: !!t.inst,
        feat: t.feat || album.feat || null,
        note: t.note || '',
        nonAlbum: false,
      };
      album.tracks.push(song);
      album.duration += sec;
      songs.push(song);
    });
    albums.push(album);
  });

  // Non-album songs form a virtual group so layouts can treat them uniformly.
  const beyond = {
    id: 'beyond',
    title: 'Beyond the albums',
    released: null,
    year: null,
    timelineYear: null,
    era: null,
    note: nonAlbum.note,
    tracks: [],
    duration: 0,
    index: albums.length,
    kind: 'group',
  };
  const sortedNA = [...nonAlbum.tracks].sort((a, b) => a.y - b.y);
  sortedNA.forEach((t, i) => {
    const sec = parseDuration(t.d);
    const song = {
      id: `na-${i}`,
      title: t.t,
      album: beyond,
      albumIndex: beyond.index,
      n: i + 1,
      disc: null,
      sec,
      year: t.y,
      timelineYear: t.y + 0.5,
      era: eraForYear(t.y),
      writer: t.w || 'Bob Dylan',
      cover: !!t.w && !/^Bob Dylan/.test(t.w),
      cowrite: !!t.w && /^Bob Dylan \//.test(t.w),
      live: false,
      inst: false,
      feat: null,
      note: t.note || '',
      source: t.source,
      nonAlbum: true,
    };
    beyond.tracks.push(song);
    beyond.duration += sec;
    songs.push(song);
  });
  albums.push(beyond);

  songs.forEach((s, i) => {
    s.index = i;
    s.minutes = s.sec / 60;
    s.color = ERA_BY_KEY[s.era].color;
    s.words = titleWords(s.title);
  });

  // Word frequency across titles, for the Lexicon layout.
  const wordCounts = new Map();
  songs.forEach((s) => new Set(s.words).forEach((w) => wordCounts.set(w, (wordCounts.get(w) || 0) + 1)));
  const topWords = [...wordCounts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 16)
    .map(([word, count]) => ({ word, count }));
  const topSet = new Map(topWords.map((w, i) => [w.word, i]));
  songs.forEach((s) => {
    // Assign to the rarest of its top words so small clusters still fill.
    let best = -1;
    s.words.forEach((w) => {
      const i = topSet.get(w);
      if (i !== undefined && (best === -1 || topWords[i].count < topWords[best].count)) best = i;
    });
    s.wordHub = best;
  });

  const studio = albums.filter((a) => a.kind === 'album');
  const stats = {
    songs: songs.length,
    albums: studio.length,
    originals: songs.filter((s) => !s.cover).length,
    covers: songs.filter((s) => s.cover).length,
    nonAlbum: beyond.tracks.length,
    totalSec: songs.reduce((n, s) => n + s.sec, 0),
    longest: songs.reduce((a, b) => (b.sec > a.sec ? b : a)),
    shortest: songs.filter((s) => !s.inst).reduce((a, b) => (b.sec < a.sec ? b : a)),
    firstYear: Math.min(...songs.map((s) => s.year)),
    lastYear: Math.max(...songs.map((s) => s.year)),
  };

  return { albums, songs, stats, topWords };
}
