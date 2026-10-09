// Validates the catalogue JSON: shapes, durations, numbering, duplicates. Prints a summary.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (f) => JSON.parse(readFileSync(join(root, 'data', f), 'utf8'));
const albums = [...read('albums-1.json'), ...read('albums-2.json')];
const nonAlbum = read('non-album.json');
const ERAS = new Set(['folk', 'electric', 'woodstock', 'blood', 'gospel', 'wilderness', 'roots', 'late', 'songbook']);
const errors = [];
const dur = (d, where) => {
  const m = /^(\d+):([0-5]\d)$/.exec(d);
  if (!m) { errors.push(`${where}: bad duration "${d}"`); return 0; }
  return +m[1] * 60 + +m[2];
};

let total = 0, nTracks = 0;
const ids = new Set();
albums.forEach((a) => {
  const w = `album ${a.id}`;
  if (ids.has(a.id)) errors.push(`${w}: duplicate id`); ids.add(a.id);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(a.released)) errors.push(`${w}: bad release date ${a.released}`);
  if (!ERAS.has(a.era)) errors.push(`${w}: unknown era ${a.era}`);
  if (!a.tracks?.length) errors.push(`${w}: no tracks`);
  const seen = new Set();
  let lastN = 0;
  a.tracks.forEach((t) => {
    const tw = `${w} #${t.n} ${t.t}`;
    if (!t.t) errors.push(`${tw}: missing title`);
    if (typeof t.n !== 'number' || t.n <= lastN) errors.push(`${tw}: track numbers must increase`);
    lastN = t.n;
    if (seen.has(t.t)) errors.push(`${tw}: duplicate title within album`); seen.add(t.t);
    const s = dur(t.d, tw);
    if (s < 40 || s > 18 * 60) errors.push(`${tw}: implausible length ${t.d}`);
    total += s; nTracks += 1;
  });
});
const titles = new Set(albums.flatMap((a) => a.tracks.map((t) => t.t.toLowerCase())));
nonAlbum.tracks.forEach((t) => {
  const w = `non-album ${t.t}`;
  if (!t.y || t.y < 1960 || t.y > 2025) errors.push(`${w}: bad year ${t.y}`);
  if (!t.source) errors.push(`${w}: missing source`);
  if (titles.has(t.t.toLowerCase())) errors.push(`${w}: duplicates an album track`);
  total += dur(t.d, w); nTracks += 1;
});

const h = Math.floor(total / 3600), m = Math.round((total % 3600) / 60);
console.log(`${albums.length} albums, ${nTracks} songs (${nonAlbum.tracks.length} non-album), ${h}h ${String(m).padStart(2, '0')}m of music`);
if (errors.length) { console.error(errors.join('\n')); process.exit(1); }
console.log('data ok');
