import type { Domain, OrganSystem } from './types';

/**
 * Domain colours. `color` is the luminous scene colour (bloom-friendly);
 * `ink` is a toned step of the same hue that passes the categorical palette
 * checks on the dark UI surface (legend chips, badges, text accents).
 */
export const DOMAINS: Domain[] = [
  {
    id: 'internal',
    name: 'Internal Medicine',
    blurb: 'Adult medicine and its twenty-plus organ-system fellowships.',
    color: '#4fd2ff',
    ink: '#2d91ea',
  },
  {
    id: 'pediatrics',
    name: 'Pediatrics',
    blurb: 'Care of infants, children and adolescents, with a parallel set of subspecialties.',
    color: '#ffd64a',
    ink: '#b67d1b',
  },
  {
    id: 'primary',
    name: 'Family & Community Medicine',
    blurb: 'Whole-person, population and preventive care across the lifespan.',
    color: '#5ef08a',
    ink: '#228442',
  },
  {
    id: 'surgery',
    name: 'Surgical Specialties',
    blurb: 'Operative disciplines from general surgery to neurosurgery.',
    color: '#ff6b7a',
    ink: '#d06064',
  },
  {
    id: 'womens',
    name: 'Obstetrics & Gynecology',
    blurb: 'Pregnancy, childbirth, reproductive and pelvic health.',
    color: '#ff8fd0',
    ink: '#ad4c7f',
  },
  {
    id: 'neuro-psych',
    name: 'Neurology & Psychiatry',
    blurb: 'Disorders of the brain, nervous system, mind and behaviour.',
    color: '#b08cff',
    ink: '#857edc',
  },
  {
    id: 'acute',
    name: 'Anesthesia & Emergency',
    blurb: 'Perioperative, emergency and critical-care medicine.',
    color: '#ff9f4a',
    ink: '#b44c0a',
  },
  {
    id: 'diagnostic',
    name: 'Diagnostics & Laboratory',
    blurb: 'Imaging, pathology, nuclear medicine and medical genetics.',
    color: '#58f0d8',
    ink: '#1d9a86',
  },
  {
    id: 'medical',
    name: 'Skin, Allergy & Rehabilitation',
    blurb: 'Dermatology, allergy-immunology and physical medicine.',
    color: '#d6e04f',
    ink: '#8d8e2d',
  },
];

export const DOMAIN_BY_ID: Record<string, Domain> = Object.fromEntries(DOMAINS.map((d) => [d.id, d]));

/** Ordered so that neighbouring systems on the ring are related. */
export const SYSTEMS: OrganSystem[] = [
  { id: 'general', name: 'Whole-person care', blurb: 'Generalist and undifferentiated care across organ systems.' },
  { id: 'children', name: 'Children & adolescents', blurb: 'General and hospital pediatrics.' },
  { id: 'reproductive', name: 'Reproductive & pelvic health', blurb: 'Obstetrics, gynecology and reproductive medicine.' },
  { id: 'renal', name: 'Kidneys & urinary tract', blurb: 'Nephrology and urology.' },
  { id: 'digestive', name: 'Digestive system & liver', blurb: 'Gastroenterology, hepatology and abdominal surgery.' },
  { id: 'endocrine', name: 'Hormones & metabolism', blurb: 'Endocrine glands, diabetes and metabolic disease.' },
  { id: 'hemeonc', name: 'Blood & cancer', blurb: 'Hematology, oncology and cancer surgery.' },
  { id: 'immune', name: 'Immunity & infection', blurb: 'Infectious disease, allergy, immunology and rheumatology.' },
  { id: 'respiratory', name: 'Lungs & airway', blurb: 'Pulmonary medicine and thoracic surgery.' },
  { id: 'cardiovascular', name: 'Heart & vessels', blurb: 'Cardiac and vascular medicine, surgery and anesthesia.' },
  { id: 'critical', name: 'Emergency, critical & perioperative', blurb: 'Resuscitation, intensive care and anesthesia.' },
  { id: 'imaging', name: 'Imaging & radiation', blurb: 'Diagnostic imaging, interventional and radiation therapy.' },
  { id: 'laboratory', name: 'Laboratory & genetics', blurb: 'Pathology, laboratory medicine and genomics.' },
  { id: 'population', name: 'Population & environment', blurb: 'Public, occupational, aerospace and preventive health.' },
  { id: 'rehab', name: 'Rehabilitation, pain & palliative', blurb: 'Function, comfort and quality of life.' },
  { id: 'musculoskeletal', name: 'Bones, joints & muscles', blurb: 'Orthopaedics, hand surgery and sports medicine.' },
  { id: 'skin', name: 'Skin', blurb: 'Dermatology and dermatologic surgery.' },
  { id: 'senses', name: 'Eyes, ears, nose & throat', blurb: 'Ophthalmology and otolaryngology.' },
  { id: 'nervous', name: 'Brain, spine & nerves', blurb: 'Neurology, neurosurgery and the neurosciences.' },
  { id: 'mental', name: 'Mind & behaviour', blurb: 'Psychiatry and behavioural health.' },
];

export const SYSTEM_BY_ID: Record<string, OrganSystem> = Object.fromEntries(SYSTEMS.map((s) => [s.id, s]));
