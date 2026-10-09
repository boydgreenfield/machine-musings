import type { SpecialtyInput } from '../types';
import { INTERNAL_MEDICINE } from './internal';
import { PEDIATRICS } from './pediatrics';
import { PRIMARY_CARE } from './primary';
import { SURGERY } from './surgery';
import { WOMENS_HEALTH } from './womens';
import { NEURO_PSYCH } from './neuro-psych';
import { ACUTE_CARE } from './acute';
import { DIAGNOSTIC } from './diagnostic';
import { MEDICAL_SPECIALTIES } from './medical';

/** Every authored node, in domain order. */
export const RAW_NODES: SpecialtyInput[] = [
  ...INTERNAL_MEDICINE,
  ...PEDIATRICS,
  ...PRIMARY_CARE,
  ...SURGERY,
  ...WOMENS_HEALTH,
  ...NEURO_PSYCH,
  ...ACUTE_CARE,
  ...DIAGNOSTIC,
  ...MEDICAL_SPECIALTIES,
];
