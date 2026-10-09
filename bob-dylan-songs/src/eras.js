// Eight eras of the catalogue. Colours were checked with a colour-vision validator
// against the paper surface: every chronologically adjacent pair stays distinct under
// protan / deutan / tritan simulation. Labels and spatial grouping carry identity too.
export const ERAS = [
  { key: 'folk',       name: 'Folk prophet',        years: '1961–64', color: '#a8862a',
    blurb: 'Four acoustic albums in two years. A Guthrie disciple becomes the voice of a generation and promptly resigns the post.' },
  { key: 'electric',   name: 'Going electric',      years: '1965–66', color: '#2b78c2',
    blurb: 'Three albums in fourteen months. Newport, the Judas shout, the thin wild mercury sound, then the motorcycle crash.' },
  { key: 'woodstock',  name: 'Woodstock & Nashville', years: '1967–73', color: '#e0701a',
    blurb: 'Retreat. Basement songs with The Band, biblical parables, a country croon, a self-portrait nobody asked for, a soundtrack.' },
  { key: 'blood',      name: 'Blood & Desire',      years: '1974–78', color: '#a8232f',
    blurb: 'The marriage comes apart on tape. Rolling Thunder, gypsy violins, and the big-band apocalypse of Street-Legal.' },
  { key: 'gospel',     name: 'Gospel years',        years: '1979–81', color: '#6f4fae',
    blurb: 'Born again. Three albums of testimony, booed in concert, ending with Every Grain of Sand.' },
  { key: 'wilderness', name: 'The wilderness',      years: '1983–90', color: '#2d8a3e',
    blurb: 'Great songs left in the vault, uneven albums released, a Traveling Wilbury, and a New Orleans rescue by Daniel Lanois.' },
  { key: 'late',       name: 'Late renaissance',    years: '1992–2020', color: '#b8307a',
    blurb: 'Two solo acoustic albums of old blues clear the ground. Then Time Out of Mind and twenty-three years of late masterpieces.' },
  { key: 'songbook',   name: 'American Songbook',   years: '2015–17', color: '#4f6684',
    blurb: 'Fifty-two standards Sinatra sang, recorded live in the room with a five-piece band. "I\'m uncovering them."' },
];

/** Paper tint for an index card of this era: the hue washed into cream stock. */
export function cardTint(hex, paper = '#efe4cb', k = 0.42) {
  const h = (x) => parseInt(x, 16);
  const a = [h(hex.slice(1, 3)), h(hex.slice(3, 5)), h(hex.slice(5, 7))];
  const b = [h(paper.slice(1, 3)), h(paper.slice(3, 5)), h(paper.slice(5, 7))];
  return '#' + a.map((v, i) => Math.round(b[i] + (v - b[i]) * k).toString(16).padStart(2, '0')).join('');
}

export const ERA_BY_KEY = Object.fromEntries(ERAS.map((e) => [e.key, e]));

/** Era for a song that has no album, by the year it was recorded. */
export function eraForYear(y) {
  if (y <= 1964) return 'folk';
  if (y <= 1966) return 'electric';
  if (y <= 1973) return 'woodstock';
  if (y <= 1978) return 'blood';
  if (y <= 1981) return 'gospel';
  if (y <= 1990) return 'wilderness';
  return 'late';
}
