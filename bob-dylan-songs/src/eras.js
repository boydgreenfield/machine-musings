// Eight eras of the catalogue. Colours were checked with a colour-vision validator
// against the dark surface: every chronologically adjacent pair stays distinct under
// protan / deutan / tritan simulation. Labels and spatial grouping carry identity too.
export const ERAS = [
  { key: 'folk',       name: 'Folk prophet',        years: '1961–64', color: '#f2d479',
    blurb: 'Four acoustic albums in two years. A Guthrie disciple becomes the voice of a generation and promptly resigns the post.' },
  { key: 'electric',   name: 'Going electric',      years: '1965–66', color: '#5fe3ff',
    blurb: 'Three albums in fourteen months. Newport, the Judas shout, the thin wild mercury sound, then the motorcycle crash.' },
  { key: 'woodstock',  name: 'Woodstock & Nashville', years: '1967–73', color: '#ff9c3c',
    blurb: 'Retreat. Basement songs with The Band, biblical parables, a country croon, a self-portrait nobody asked for, a soundtrack.' },
  { key: 'blood',      name: 'Blood & Desire',      years: '1974–78', color: '#ff4757',
    blurb: 'The marriage comes apart on tape. Rolling Thunder, gypsy violins, and the big-band apocalypse of Street-Legal.' },
  { key: 'gospel',     name: 'Gospel years',        years: '1979–81', color: '#b58cff',
    blurb: 'Born again. Three albums of testimony, booed in concert, ending with Every Grain of Sand.' },
  { key: 'wilderness', name: 'The wilderness',      years: '1983–90', color: '#3ed9b4',
    blurb: 'Great songs left in the vault, uneven albums released, a Traveling Wilbury, and a New Orleans rescue by Daniel Lanois.' },
  { key: 'late',       name: 'Late renaissance',    years: '1992–2020', color: '#ff6ad5',
    blurb: 'Two solo acoustic albums of old blues clear the ground. Then Time Out of Mind and twenty-three years of late masterpieces.' },
  { key: 'songbook',   name: 'American Songbook',   years: '2015–17', color: '#dfe6f0',
    blurb: 'Fifty-two standards Sinatra sang, recorded live in the room with a five-piece band. "I\'m uncovering them."' },
];

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
