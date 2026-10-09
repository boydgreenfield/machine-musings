/**
 * Data model for the Medical Specialties Atlas.
 *
 * The atlas is a directed acyclic graph rooted at "Medicine". Every node below
 * the root belongs to exactly one *domain* (a colour family in the scene) and
 * sits at one of three levels:
 *
 *   domain       → a broad family of practice (e.g. "Surgical Specialties")
 *   specialty    → a primary residency / primary board certificate
 *   subspecialty → a fellowship or subspecialty certificate reached from a specialty
 *
 * A node may have several parents when a subspecialty is reachable from more
 * than one specialty (Sleep Medicine, Hospice & Palliative Medicine, …). The
 * first parent is the "home" board used for layout; the others are drawn as
 * cross-board pathways.
 */

export type DomainId =
  | 'internal'
  | 'pediatrics'
  | 'primary'
  | 'surgery'
  | 'womens'
  | 'neuro-psych'
  | 'acute'
  | 'diagnostic'
  | 'medical';

export type SystemId =
  | 'general'
  | 'cardiovascular'
  | 'respiratory'
  | 'digestive'
  | 'renal'
  | 'endocrine'
  | 'hemeonc'
  | 'immune'
  | 'nervous'
  | 'mental'
  | 'musculoskeletal'
  | 'skin'
  | 'senses'
  | 'reproductive'
  | 'children'
  | 'critical'
  | 'imaging'
  | 'laboratory'
  | 'population'
  | 'rehab';

export type Level = 'root' | 'domain' | 'specialty' | 'subspecialty';

/** How the credential is recognised in the United States. */
export type Certification =
  /** Primary certificate issued by an ABMS member board after residency. */
  | 'abms-primary'
  /** Subspecialty certificate issued by one or more ABMS member boards. */
  | 'abms-sub'
  /** Certified by the United Council for Neurologic Subspecialties. */
  | 'ucns'
  /** Accredited or widely recognised fellowship without a separate board certificate. */
  | 'fellowship'
  /** Focused practice designation (e.g. Hospital Medicine). */
  | 'focused'
  /** Recognised outside the ABMS system (dental boards, international). */
  | 'other';

export interface Domain {
  id: DomainId;
  name: string;
  /** Short blurb used in the legend and detail panel. */
  blurb: string;
  /** Colour used for nodes/edges in the scene (bright, bloom-friendly). */
  color: string;
  /** Colour used for UI chips and text on the dark panel surface. */
  ink: string;
}

export interface OrganSystem {
  id: SystemId;
  name: string;
  blurb: string;
}

export interface SpecialtyInput {
  id: string;
  name: string;
  /** Common abbreviation, e.g. "IM", "ENT". */
  short?: string;
  level: Exclude<Level, 'root'>;
  domain: DomainId;
  /** Parent ids. The first entry is the home placement for layout. Domain nodes have no parents. */
  parents?: string[];
  /** Certifying board (ABMS member board, UCNS, …). */
  board?: string;
  certification?: Certification;
  /** Typical additional years of training for this step (residency length or fellowship length). */
  years?: number;
  yearsNote?: string;
  system?: SystemId;
  description: string;
  aliases?: string[];
}

export interface SpecialtyNode extends Omit<SpecialtyInput, 'level'> {
  level: Exclude<Level, 'root'> | 'root';
  parents: string[];
  certification: Certification;
  years: number;
  system: SystemId;
  /** 0 = root, 1 = domain, 2 = specialty, 3 = subspecialty. */
  depth: number;
  /** Children sorted by name. */
  children: string[];
  /** Home ancestry from the root down to (and including) this node. */
  path: string[];
  /** Cumulative years of post-graduate training along the home path. */
  totalYears: number;
}
