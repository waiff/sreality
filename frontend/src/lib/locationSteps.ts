/* W16 — the ONE place a location step's WORDING lives (operator ruling 2026-09-14).
 *
 * Two readouts asked the same first questions in different words: the location
 * audit page's waterfall (`/new-dedup/pin-audit`, Czech, hourly) and the NEW
 * DEDUP candidates funnel (`/new-dedup` and `/new-dedup/candidates`, English,
 * frozen per run). "Known to the location engine" read as *answered* while it was
 * the same set the audit calls *judged*; and the funnel's town step was cut
 * without the consumer rule, so its one "lost 99,889" silently added three unlike
 * things together — judged-but-not-located, ABROAD (an answer, not a loss) and a
 * Czech point with no town.
 *
 * THE KEY IS THE CONTRACT; THE WORDING IS HERE. The step keys, their order and
 * each key's shape are declared once in the backend
 * (location_data/location_steps.py) and rendered into both producers —
 * `refresh_location_audit_waterfall()` and the candidate lane's `stats.waterfall`.
 * Nothing about a step's NAME is stored: `location_audit_waterfall.label_cs` was
 * dropped in migration 526, because a better sentence must never cost a
 * migration and a label in two places is a label that can differ in one.
 *
 * NO PAGE MAY HARDCODE A STEP NAME. tests/test_location_steps_vocabulary.py
 * greps the three components for these strings; a step renamed here is renamed on
 * every surface, in both languages, at once.
 *
 * `livesOn` says which readout carries a key at all: `both` for the shared prefix,
 * `audit` for the hidden set's states, `candidates` for the two dedup-only steps.
 * It is documentation, not a filter — each page renders the rows its producer
 * stamped.
 */

export type StepSurface = 'both' | 'audit' | 'candidates';

export interface LocationStepWording {
  /* Czech — the audit page. These nine strings are exactly what migrations 523
   * and 524 wrote into `label_cs` before 526 dropped the column. */
  cs: string;
  en: string;
  note_cs: string;
  note_en: string;
  livesOn: StepSurface;
}

export const LOCATION_STEPS: Record<string, LocationStepWording> = {
  all_listings: {
    cs: 'Inzerátů v databázi celkem',
    en: 'Listings in the database',
    note_cs:
      'Všechno, co jsme kdy z devíti portálů sebrali — běžící i stažené. Nic se nikdy nemaže, takže tohle je celá databáze, a poloha se řeší u každého z nich.',
    note_en:
      'Every listing row this count looked at, across all nine portals. On a candidate run the run’s scope decides whether that means every listing ever collected or only the ones still on sale — the scope is printed next to the chain.',
    livesOn: 'both',
  },
  with_verdict: {
    cs: 'U kterých už systém polohu řešil',
    en: 'Judged by the location engine',
    note_cs:
      'Systém u nich polohu už řešil a má uložený výsledek. Rozdíl jsou inzeráty, na které zatím nedošla řada.',
    note_en:
      'The location engine has already worked this listing and stored a verdict — whatever the verdict says. What is lost here has simply not had its turn yet.',
    livesOn: 'both',
  },
  located: {
    cs: 'Se známou polohou (bod v mapě, nebo zahraničí)',
    en: 'Has a location (a point, or abroad)',
    note_cs:
      'Mají bod na mapě, nebo systém rozhodl, že jsou v zahraničí. Obojí je odpověď, se kterou už umí zákaznické stránky pracovat.',
    note_en:
      'A point on the map, or the determination that the listing is abroad. Both are answers, and this is the one rule Browse, the watchdog and the pairing lane all serve from. What is lost here was judged and still has no location.',
    livesOn: 'both',
  },
  located_town: {
    cs: 'mají přiřazenou obec',
    en: 'Placed in a named town (obec)',
    note_cs:
      'Odpověď je aspoň v přesnosti obce, takže se dá použít jako blok pro párování.',
    note_en:
      'The answer names a municipality and is at least town-grain, which is what the pairing rule blocks on. Its loss is exactly the two rows above it added together — abroad, and a Czech point with no town.',
    livesOn: 'both',
  },
  located_foreign: {
    cs: 'jsou v zahraničí (systém tak rozhodl)',
    en: 'Abroad (an answer, not a loss)',
    note_cs:
      'Zahraničí je rozhodnutí, ne chybějící údaj — proto je to část polohy, ne úbytek.',
    note_en:
      'Abroad is an ANSWER, not a loss: the engine decided it, and these listings sit inside the located set. They cannot be paired by a Czech town, which is why they leave the chain at the next step rather than at this one.',
    livesOn: 'both',
  },
  located_no_town: {
    cs: 'mají bod v ČR, ale bez obce',
    en: 'A point in Czechia, but no town',
    note_cs:
      'Bod známe, obec k němu ale zatím přiřazená není — malý zbytek, který se dá dořešit.',
    note_en:
      'The point is known but no municipality is attached to it yet, or the answer is coarser than a town. A small remainder, and the only other part of the town step’s loss.',
    livesOn: 'both',
  },
  hidden: {
    cs: 'Bez rozhodnuté polohy — tento seznam; nikde je neukazujeme, dokud polohu nemají',
    en: 'No decided location (not shown anywhere)',
    note_cs:
      'Tento seznam. Každý inzerát bez rozhodnuté polohy — dokud ji nemá, není vidět nikde. Jestli běží, nebo je stažený, je jen filtr níže.',
    note_en:
      'Every listing that fails the rule, read against the whole database. It is a set carved out, not a step: what it holds was already lost at the two steps above.',
    livesOn: 'audit',
  },
  /* The two hidden states are ALSO the audit page's list filter, so this is the one
   * Czech name each of them has anywhere on that page — the waterfall's split row, the
   * header total, the filter pill and the note under it all read it from here. Two
   * names for one number is the same fault as two names for one step. */
  hidden_unresolved: {
    cs: 'zpracováno, nerozhodnuto',
    en: 'Unresolved — the actual issue',
    note_cs: 'Rozhodnuto bylo, poloha z toho ale nevyšla — tohle je ten problém.',
    note_en:
      'Judged, not queued, no newer snapshot, and still no location. This is the genuine issue.',
    livesOn: 'audit',
  },
  hidden_pending: {
    cs: 'čeká na zpracování',
    en: 'Waiting to be processed',
    note_cs: 'Zatím na ně nedošla řada, nebo přišel novější sběr — není to nález.',
    note_en:
      'No verdict yet, queued, or a snapshot newer than the verdict. Not a finding.',
    livesOn: 'audit',
  },
  eligible: {
    cs: 'mají údaj, který umí pravidlo porovnat',
    en: 'Has an attribute the rule can compare',
    note_cs:
      'K obci navíc uvádějí dispozici, nebo plochu — jedno z toho, co pravidlo porovnává.',
    note_en:
      'On top of a town, the listing states a disposition (2+kk and the like) or a floor area — one of the two things the pairing rule compares. A listing with a town and neither attribute is counted in the missing-data table below.',
    livesOn: 'candidates',
  },
  paired: {
    cs: 'skončily aspoň v jedné dvojici',
    en: 'Ended up in at least one candidate pair',
    note_cs: 'Pro inzerát se našel aspoň jeden protějšek, se kterým ho pravidlo spárovalo.',
    note_en:
      'The run actually found another listing to pair it with. A listing can be perfectly eligible and still land here at zero — that only means nothing else in its town matched it.',
    livesOn: 'candidates',
  },
};

/* The label for a step, in one language, with the key itself as the last resort:
 * a producer that stamps a key this module does not word renders the key rather
 * than a blank cell. */
export const stepLabel = (key: string, lang: 'cs' | 'en'): string =>
  LOCATION_STEPS[key]?.[lang] ?? key;

export const stepNote = (key: string, lang: 'cs' | 'en'): string | null =>
  LOCATION_STEPS[key]?.[lang === 'cs' ? 'note_cs' : 'note_en'] ?? null;
