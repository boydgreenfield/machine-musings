/**
 * Data model for the atlas of medical specialties.
 *
 * The taxonomy follows the American Board of Medical Specialties (ABMS) member
 * boards and their certificates, supplemented by ACGME-accredited (or widely
 * recognised society-accredited) fellowships that do not carry an ABMS
 * certificate. Workforce figures are approximate counts of active US
 * physicians, rounded, drawn from public AAMC / ABMS reports; they are used for
 * relative sizing only.
 */

export type Tier = 'root' | 'specialty' | 'subspecialty' | 'fellowship';

/** Broad "family" of practice – drives the primary colour lens. */
export type Category =
  | 'primary'     // Primary & generalist care
  | 'medical'     // Medical (cognitive / procedural non-operative) specialties
  | 'surgical'    // Operative specialties
  | 'acute'       // Hospital-based, perioperative & acute care
  | 'diagnostic'  // Imaging, pathology & laboratory medicine
  | 'mind'        // Psychiatry & behavioural health
  | 'population'; // Preventive, occupational & population health

/** Body system / domain of practice – the second colour lens. */
export type Domain =
  | 'whole'   // Whole person, across systems
  | 'child'   // Children & adolescents
  | 'heart'   // Heart & circulation
  | 'lung'    // Lungs & breathing
  | 'gut'     // Digestive system & liver
  | 'kidney'  // Kidneys & urinary tract
  | 'brain'   // Brain, spine & nerves
  | 'bone'    // Bones, joints & muscles
  | 'skin'    // Skin
  | 'hormone' // Hormones & metabolism
  | 'blood'   // Blood & cancer
  | 'immune'  // Infection & immunity
  | 'repro'   // Reproductive & women's health
  | 'mind'    // Mind & behaviour
  | 'sense'   // Eyes, ears, nose & throat
  | 'acute'   // Emergency, critical & perioperative care
  | 'image'   // Imaging, laboratory & pathology
  | 'pop';    // Populations, environments & systems

/** Kind of credential the node represents. */
export type Credential =
  | 'board'            // A primary ABMS certificate (own board)
  | 'subspecialty'     // An ABMS subspecialty certificate
  | 'focused-practice' // An ABMS "focused practice" designation
  | 'fellowship';      // Accredited fellowship, no ABMS certificate

export interface NodeDef {
  id: string;
  name: string;
  /** Shorter label for dense views. */
  short?: string;
  tier: Tier;
  /** Ids of the node(s) whose training this builds on. Multi-parent nodes are "bridges". */
  parents: string[];
  /** Optional secondary training pathways, drawn as faint dotted links (e.g. integrated vs. traditional). */
  via?: string[];
  category: Category;
  domain: Domain;
  credential: Credential;
  /** Certifying board acronym(s). */
  boards: string[];
  /** Residency length in years (specialties, counted from medical-school graduation). */
  residency?: number;
  /** Fellowship length in years beyond the parent training (subspecialties & fellowships). */
  fellowship?: number;
  /** Approximate number of active US physicians (rounded). */
  workforce?: number;
  /** One- or two-sentence description. */
  description: string;
  /** Alternative or historical names. */
  aka?: string[];
  /** A short, interesting fact surfaced in the detail panel. */
  note?: string;
}

export interface BoardDef {
  id: string;
  name: string;
  founded: number;
}

export const CATEGORY_META: Record<Category, { label: string; color: string; blurb: string }> = {
  primary:    { label: 'Primary & generalist care',       color: '#ffd166', blurb: 'The front door of medicine: broad, continuous care across the lifespan.' },
  medical:    { label: 'Medical specialties',             color: '#2ec4b6', blurb: 'Deep expertise in one organ system or disease family, mostly non-operative.' },
  surgical:   { label: 'Surgical specialties',            color: '#ff5c4d', blurb: 'Treatment through operation, from microsurgery to transplantation.' },
  acute:      { label: 'Hospital, acute & perioperative', color: '#c4b5fd', blurb: 'Medicine at its most time-critical: emergencies, anaesthesia, intensive care.' },
  diagnostic: { label: 'Imaging, pathology & lab',        color: '#1e88e5', blurb: 'Seeing inside the body and reading tissue, cells, and molecules.' },
  mind:       { label: 'Mind & behaviour',                color: '#d64fa3', blurb: 'Mental health across the lifespan, from the clinic to the courtroom.' },
  population: { label: 'Preventive & population health',  color: '#8fd14f', blurb: 'Keeping whole populations well: workplaces, environments, flight, data.' },
};

export const DOMAIN_META: Record<Domain, { label: string; color: string }> = {
  whole:   { label: 'Whole person',               color: '#ffd166' },
  child:   { label: 'Children & adolescents',     color: '#ffb3c6' },
  heart:   { label: 'Heart & circulation',        color: '#ff5c4d' },
  lung:    { label: 'Lungs & breathing',          color: '#7fd8ff' },
  gut:     { label: 'Digestive system & liver',   color: '#f4a261' },
  kidney:  { label: 'Kidneys & urinary tract',    color: '#ffe08a' },
  brain:   { label: 'Brain, spine & nerves',      color: '#c4b5fd' },
  bone:    { label: 'Bones, joints & muscles',    color: '#e9e4d8' },
  skin:    { label: 'Skin',                       color: '#f7c59f' },
  hormone: { label: 'Hormones & metabolism',      color: '#b5e48c' },
  blood:   { label: 'Blood & cancer',             color: '#d64fa3' },
  immune:  { label: 'Infection & immunity',       color: '#8fd14f' },
  repro:   { label: "Reproductive & women's health", color: '#ff8fab' },
  mind:    { label: 'Mind & behaviour',           color: '#b388eb' },
  sense:   { label: 'Eyes, ears, nose & throat',  color: '#2ec4b6' },
  acute:   { label: 'Emergency & critical care',  color: '#ffffff' },
  image:   { label: 'Imaging, lab & pathology',   color: '#1e88e5' },
  pop:     { label: 'Populations & systems',      color: '#6ee7b7' },
};

export const CREDENTIAL_META: Record<Credential, { label: string; short: string }> = {
  board:              { label: 'Primary ABMS certificate', short: 'Board' },
  subspecialty:       { label: 'ABMS subspecialty certificate', short: 'Subspecialty' },
  'focused-practice': { label: 'ABMS focused-practice designation', short: 'Focused practice' },
  fellowship:         { label: 'Accredited fellowship (no ABMS certificate)', short: 'Fellowship' },
};
