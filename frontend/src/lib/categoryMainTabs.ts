import { FILTER_REGISTRY } from '@/lib/filterRegistry.generated';

// Property-type tabs from the SAME generated registry as Browse's TYPE chips —
// one enum, one Czech label set, shared by every surface that lets the operator
// narrow by property type (Decision history, the manual review Queue).
const CATEGORY_MAIN_ENUM =
  FILTER_REGISTRY.filters.find((f) => f.id === 'category_main')?.enum_values ?? [];

export const CATEGORY_MAIN_TABS = [
  { id: '', label: 'Vše' },
  ...CATEGORY_MAIN_ENUM.map((o) => ({ id: String(o.value), label: o.label_cs })),
];
