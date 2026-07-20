/* Location Audit domain glossary — the single source of truth for how each
 * address / coordinate / admin field is LABELLED, EXPLAINED (how it was
 * acquired), and whether it can be filtered by presence. Drives both the
 * per-listing field grid and the presence-filter chips on /location-audit.
 *
 * The field keys here match api/routes/location_audit.py `_PRESENCE_SQL` (the
 * backend's injection-safe presence allowlist) one-for-one — keep them in sync.
 */

import type { LocationAuditRow } from './api';

export interface FieldSpec {
  /** Row key (also the backend presence-filter key). */
  key: string;
  /** Column name shown to the operator (kept technical on purpose — this page
   *  exists to inspect the real schema fields). */
  label: string;
  /** How this field is acquired, in general (the per-row method for `geom` and
   *  `street` is layered on top via GEOM_METHOD / STREET_METHOD). */
  explain: string;
  /** Read the display value off a row (string form; null → empty). */
  value: (r: LocationAuditRow) => string | number | null;
  /** Can the operator filter listings by whether this field is populated? */
  presence?: boolean;
}

export interface FieldGroup {
  title: string;
  hint?: string;
  fields: FieldSpec[];
}

const s = (v: string | number | null | undefined): string | number | null =>
  v === undefined ? null : v;

export const FIELD_GROUPS: FieldGroup[] = [
  {
    title: 'Souřadnice',
    hint: 'Kotva celého geo pipeline — z ní se odvozuje admin hierarchie i dedup geo-klíč.',
    fields: [
      {
        key: 'geom',
        label: 'geom (lat, lon)',
        explain:
          'Souřadnice listingu (geography Point 4326). Způsob získání se liší podle portálu a řádku — viz značka metody. Nativní GPS ze stránky je nejspolehlivější; geokódování a přenos z minula jsou slabší.',
        value: (r) =>
          r.lat != null && r.lon != null ? `${r.lat.toFixed(6)}, ${r.lon.toFixed(6)}` : null,
        presence: true,
      },
      {
        key: 'geo_cell_key',
        label: 'geo_cell_key',
        explain:
          'Odvozený geo-blokovací klíč ze souřadnic (trigger, migrace 276/296). Jediná blokovací osa pro dům/pozemek/komerci — dedup přes něj hledá kandidáty.',
        value: (r) => s(r.geo_cell_key),
        presence: true,
      },
    ],
  },
  {
    title: 'Adresa (uliční úroveň)',
    fields: [
      {
        key: 'street',
        label: 'street',
        explain:
          'Ulice. Způsob získání se liší podle řádku (viz značka metody): strukturované pole portálu, vytěženo z volného textu, doplněno z RÚIAN podle souřadnic, nebo (výhledově) LLM enrichment. Chybná ulice je horší než NULL — proto reject_as_town guard.',
        value: (r) => s(r.street),
        presence: true,
      },
      {
        key: 'house_number',
        label: 'house_number',
        explain:
          'Číslo popisné/orientační. Jen portály se strukturovanou adresou (sreality, bezrealitky, mmreality), případně vedlejší produkt RÚIAN resolveru. HTML portály ho většinou nemají.',
        value: (r) => s(r.house_number),
        presence: true,
      },
      {
        key: 'zip',
        label: 'zip (PSČ)',
        explain:
          'PSČ. Strukturované pole (sreality/bezrealitky). Pozn.: bazos PSČ extrahuje do raw_json, ale zatím ho nezapojuje do sloupce (§7.2 auditu).',
        value: (r) => s(r.zip),
        presence: true,
      },
      {
        key: 'street_id',
        label: 'street_id',
        explain:
          'Stabilní ID ulice z portálu (sreality/bezrealitky). Umožňuje id-based street-key pro dedup (přesnější než jmenný klíč).',
        value: (r) => s(r.street_id),
        presence: true,
      },
      {
        key: 'street_name_key',
        label: 'street_name_key',
        explain:
          'Odvozený klíč pro dedup — čistá funkce z `street` (scraper.street.street_name_key), obec-scoped. Stampuje se při každém zápisu; nečte se z parseru.',
        value: (r) => s(r.street_name_key),
        presence: true,
      },
      {
        key: 'street_source',
        label: 'street_source',
        explain:
          "Původ ulice (migrace 263): 'parser' = ze stránky portálu, 'resolver' = doplněno RÚIAN podle souřadnic. Trigger při změně souřadnic NULLuje jen resolver-sourced ulice.",
        value: (r) => s(r.street_source),
        presence: true,
      },
    ],
  },
  {
    title: 'Admin hierarchie (odvozeno ze souřadnic)',
    hint: 'Trigger listings_set_admin_geo dělá PIP souřadnice do admin_boundaries — jednotné napříč všemi 9 portály, nezávislé na volném textu.',
    fields: [
      { key: 'obec', label: 'obec', explain: 'Obec (municipalita) — geo-odvozeno ze souřadnic triggerem. Nespoléhat na locality.', value: (r) => s(r.obec), presence: true },
      { key: 'obec_id', label: 'obec_id', explain: 'Integer join-key obce (= admin_boundaries.id). Použ. pro price-stats a district chip.', value: (r) => s(r.obec_id), presence: true },
      { key: 'okres', label: 'okres', explain: 'Okres — geo-odvozeno ze souřadnic (walk parent_id).', value: (r) => s(r.okres), presence: true },
      { key: 'okres_id', label: 'okres_id', explain: 'Integer join-key okresu.', value: (r) => s(r.okres_id), presence: true },
      { key: 'region', label: 'region (kraj)', explain: 'Kraj — geo-odvozeno ze souřadnic.', value: (r) => s(r.region), presence: true },
      { key: 'region_id', label: 'region_id', explain: 'Integer join-key kraje.', value: (r) => s(r.region_id), presence: true },
    ],
  },
  {
    title: 'Lokalita portálu (volný text / legacy)',
    fields: [
      {
        key: 'locality',
        label: 'locality',
        explain:
          'Volný text lokality přímo z portálu (jak ho portál zobrazuje). Portálově specifický a nespolehlivý pro seskupování — proto se admin hierarchie bere ze souřadnic, ne odtud.',
        value: (r) => s(r.locality),
        presence: true,
      },
      {
        key: 'district',
        label: 'district (legacy)',
        explain:
          'Legacy zobrazovací sloupec. Doplněn z okresu (u Prahy z obce) když je NULL, jinak z bohatšího textu sreality ("Město - Čtvrť").',
        value: (r) => s(r.district),
        presence: true,
      },
    ],
  },
  {
    title: 'sreality lokalitní ID (jen sreality)',
    hint: 'Stabilní ID sreality povýšená z raw_json (artefakt z doby jednoho portálu, migrace 004). 8 z 9 portálů je zde NULL — district/region-scoped Watchdog přes ně tiše vynechává ostatní portály (§7.2 auditu).',
    fields: [
      { key: 'locality_district_id', label: 'locality_district_id', explain: 'sreality okres-ID z raw_json. Jen sreality.', value: (r) => s(r.locality_district_id), presence: true },
      { key: 'locality_region_id', label: 'locality_region_id', explain: 'sreality kraj-ID z raw_json. Jen sreality.', value: (r) => s(r.locality_region_id), presence: true },
      { key: 'locality_municipality_id', label: 'locality_municipality_id', explain: 'sreality obec-ID z raw_json. Jen sreality.', value: (r) => s(r.locality_municipality_id), presence: true },
      { key: 'locality_quarter_id', label: 'locality_quarter_id', explain: 'sreality čtvrť-ID z raw_json. Jen sreality.', value: (r) => s(r.locality_quarter_id), presence: true },
      { key: 'locality_ward_id', label: 'locality_ward_id', explain: 'sreality ward-ID z raw_json. Jen sreality.', value: (r) => s(r.locality_ward_id), presence: true },
    ],
  },
  {
    title: 'Signály přesnosti / původu (z raw_json)',
    hint: 'Portálem deklarované signály přesnosti pinu — většinou zatím nečtené kódem; přesně to, co se tu hledá. Jen zobrazení: filtrovat podle nich nejde (dotaz do raw_json by detoastoval celý payload každého řádku — incident migrace 234).',
    fields: [
      {
        key: 'coords_source',
        label: 'coords.source',
        explain:
          'Značka původu souřadnice v raw_json. CoordResolver: page / carry_forward / geocode (stampuje jen když NENÍ nativní). bazos: street / link / locality (locality = střed obce = placeholder).',
        value: (r) => s(r.coords_source),
      },
      {
        key: 'inaccuracy_type',
        label: 'locality.inaccuracy_type (sreality)',
        explain:
          'sreality: portálem deklarovaná přesnost pinu (gps / address / street / ward / quarter / municipality). Zatím NEČTENO kódem — ward/quarter/municipality = area-centroid placeholder. Backfillovatelné bez re-scrape.',
        value: (r) => s(r.inaccuracy_type),
      },
      {
        key: 'accurate',
        label: 'accurate (mmreality)',
        explain:
          'mmreality: portálem deklarovaný příznak přesnosti pinu (true/false). Zatím NEČTENO — ~37 % je false (self-declared placeholder) i přes 100% nativní souřadnice.',
        value: (r) => (r.accurate == null ? null : r.accurate ? 'true' : 'false'),
      },
      {
        key: 'geocode_attempted_at',
        label: 'geocode_attempted_at',
        explain: 'Kdy naposledy proběhl pokus o geokódování (ledger sloupec, migrace 288). Není v raw_json — přežije refetch.',
        value: (r) => s(r.geocode_attempted_at),
      },
      {
        key: 'coord_street_attempt_version',
        label: 'coord_street_attempt_version',
        explain: 'Verze pokusu RÚIAN coord→street resolveru (migrace 222) — brání opakování stejného marného lookupu.',
        value: (r) => s(r.coord_street_attempt_version),
      },
    ],
  },
];

/* Per-row acquisition method for the coordinate. Backend `geom_method`. */
export const GEOM_METHOD: Record<string, { label: string; tip: string }> = {
  page_native: {
    label: 'nativní GPS ze stránky',
    tip: 'Souřadnici publikoval sám portál na stránce/v API (GPS pin) — nejspolehlivější třída.',
  },
  carry_forward: {
    label: 'přeneseno z minula',
    tip: 'Refetch souřadnici neměl, tak se přenesla z předchozího snímku (CoordResolver carry-forward).',
  },
  geocoded: {
    label: 'geokódováno (Mapy.cz)',
    tip: 'Bez GPS na stránce — souřadnice vznikla geokódováním uliční/lokalitní textu přes Mapy.cz.',
  },
  geocoded_street: {
    label: 'geokódováno z ulice (bazos)',
    tip: 'bazos: geokódováno z vytěžené ulice, křížově ověřeno proti pinu z odkazu na mapu.',
  },
  map_link_pin: {
    label: 'pin z odkazu na mapu (bazos)',
    tip: 'bazos: pin vložený prodejcem přes odkaz "Zobrazit na mapě". 70–76 % bazos pinů je sdílených.',
  },
  geocoded_town: {
    label: 'střed obce (bazos, placeholder)',
    tip: 'bazos: hrubé geokódování jen na střed obce — placeholder, ne přesná poloha.',
  },
};

/* Per-row acquisition method for the street. Backend `street_method`. */
export const STREET_METHOD: Record<string, { label: string; tip: string }> = {
  structured_id: {
    label: 'strukturované pole + ID ulice',
    tip: 'Portál dodal strukturovanou ulici i street_id (sreality/bezrealitky) — nejpřesnější.',
  },
  structured_text: {
    label: 'strukturované pole',
    tip: 'Portál dodal ulici jako strukturované JSON pole bez číselného ID (mmreality apod.).',
  },
  free_text: {
    label: 'vytěženo z volného textu',
    tip: 'Ulice vytěžena regexem/segmentací z volného textu lokality/titulku/popisu (idnes, remax, realitymix, maxima, ceskereality, bazos).',
  },
  ruian_resolver: {
    label: 'doplněno z RÚIAN (dle souřadnic)',
    tip: "Ulici doplnil coord→street resolver z RÚIAN address_points (street_source='resolver'). Trigger ji NULLuje při změně souřadnic.",
  },
  llm: {
    label: 'doplněno LLM enrichment',
    tip: "Ulici doplnil LLM enrichment (street_source='llm') — validováno proti RÚIAN v dané obci. (Výhledová schopnost.)",
  },
};

export function methodLabel(
  map: Record<string, { label: string; tip: string }>,
  code: string | null,
): { label: string; tip: string } | null {
  if (!code) return null;
  return map[code] ?? { label: code, tip: 'Neznámá značka — zobrazeno syrově.' };
}
