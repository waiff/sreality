/* AUTODEDUP · Pair — everything the engine knew about one pair.
 *
 * THE COURT RECORD. Groups and Residual are queues; this page is where a
 * disputed call gets settled: every feature with its value AND its presence
 * flag, both PII-free digests, both photo sets with the best cross-side Hamming
 * distance per image, and the judge's full transcript with the evidence it
 * named on each side.
 *
 * PRESENCE IS NOT A ZERO. A feature that could not be computed is rendered as
 * "absent", never as 0 — most of this engine's near-misses are absences, and a
 * table that prints them as zeros makes a missing signal look like a negative
 * one. Same rule as the progress page's "not yet".
 *
 * NO PII. The digests come from the judge's own `listing_digest`, which carries
 * no broker field of any kind and a scrubbed description. Nothing on this page
 * re-adds one.
 *
 * BLIND CARRIES IN THE URL (`?blind=1`). Reached from a blind queue, this page
 * withholds the judge's transcript until the operator has recorded a verdict on
 * the pair — otherwise the drill-down would be the hole in the blinding: one
 * click on exactly the pair being ruled on, and the verdict the operator was not
 * supposed to see yet is the largest section on the screen. Everything the
 * ENGINE knew stays visible: features, digests, photos, the score. The judge is
 * the only thing hidden, and only until the operator has answered.
 */

import { useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import {
  getAutodedupPair,
  type AutodedupDigest,
  type AutodedupJudgementRow,
  type AutodedupPairImage,
} from '@/lib/api';
import { imageSrc } from '@/lib/imageUrl';
import { ROUTES, withQuery } from '@/lib/routes';
import { fmtCzk, fmtFloor } from '@/lib/format';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import AttrDiffTable, { memberDiffRows } from '@/components/autodedup/AttrDiffTable';
import EvidenceChips, { Chip, fmtScore } from '@/components/autodedup/EvidenceChips';
import MemberText from '@/components/autodedup/MemberText';
import VerdictButtons, {
  VERDICT_LABELS,
  displayVerdict,
} from '@/components/autodedup/VerdictButtons';
import VerdictNotes, {
  annotationInput,
  useVerdictAnnotations,
} from '@/components/autodedup/VerdictNotes';
import { JudgeChip } from '@/components/autodedup/PairCard';
import { useVerdictOverlay } from './AutodedupGroups';
import {
  GenerationNotice,
  useAutodedupGenerations,
} from '@/components/autodedup/GenerationSelect';

const TH = 'py-1 pr-3 text-left font-medium whitespace-nowrap align-top';
const TD = 'py-1 pr-3 align-top';
const SECTION =
  'mt-5 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4';
const EYEBROW = 'text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]';

/* A route param is a string from the URL bar; anything that is not a positive
 * integer is a typo, not a listing, and is answered as one. */
function asId(raw: string | undefined): number | null {
  if (!raw || !/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

export default function AutodedupPair() {
  const { lo: loRaw, hi: hiRaw } = useParams<{ lo: string; hi: string }>();
  const [params, setParams] = useSearchParams();
  const generation = params.get('generation');
  /* Only the explicit '1' blinds: this page is reachable directly, and its own
   * default is the full record. */
  const blind = params.get('blind') === '1';
  const lo = asId(loRaw);
  const hi = asId(hiRaw);
  const { overlay, submit, pendingKey } = useVerdictOverlay();
  const notes = useVerdictAnnotations();
  const { latest } = useAutodedupGenerations();

  const q = useQuery({
    queryKey: ['autodedup', 'pair', lo, hi, generation],
    queryFn: () => getAutodedupPair(lo as number, hi as number, generation),
    enabled: lo != null && hi != null,
  });

  if (lo == null || hi == null) {
    return (
      <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
        <h1 className="text-2xl leading-tight">AUTODEDUP · Pair</h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          That is not a pair of listing ids.
        </p>
      </div>
    );
  }

  const data = q.data?.data ?? null;
  const storeReady = q.data?.store_ready ?? null;
  const key = `${lo}:${hi}`;
  const stored = overlay[key] ?? data?.verdicts?.[0] ?? null;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">
          AUTODEDUP · Pair <span className="font-mono text-lg">#{lo} · #{hi}</span>
        </h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Every signal the engine had on these two adverts. Recording a verdict here writes into the
          program's own schema only — a negative verdict also makes the pair permanently
          un-linkable, which is why it takes a second click.
        </p>
        <p className="mt-1 text-[0.78rem]">
          {/* Every ruling on either advert, with its history and the corrections (E919). */}
          <Link
            to={withQuery(ROUTES.autodedupRulings.build(), { listing: lo })}
            className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
          >
            Všechna rozhodnutí o inzerátu #{lo}
          </Link>
        </p>
      </header>

      {/* An evidence page reached from a queue of a superseded pass is itself a
        * view of that pass; dropping the parameter puts it back on the newest. */}
      <GenerationNotice
        generation={data?.generation ?? generation}
        latest={latest}
        onLatest={() => {
          const next = new URLSearchParams(params);
          next.delete('generation');
          setParams(next, { replace: true });
        }}
      />

      {q.isPending && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Loading the evidence…
        </p>
      )}
      {q.error && <ErrorBanner message={(q.error as Error).message} />}
      {storeReady === false && (
        <p className={SECTION}>
          Schema not migrated yet — the program's store does not exist in this database.
        </p>
      )}
      {storeReady === true && data && data.pair == null && (
        <p className={SECTION}>
          This pair was never scored, or scored below the store floor, so no row was kept for it.
        </p>
      )}

      {data && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <EvidenceChips
              zone={data.pair?.zone}
              score={data.pair?.score}
              certificate={data.pair?.certificate}
              families={data.family_names}
              guardVeto={data.pair?.guard_veto}
            />
            {data.pair?.decision && <Chip title="The rule that decided it">{data.pair.decision}</Chip>}
            {(data.pair?.probes ?? []).map((p) => (
              <Chip key={p} title="A blocking probe that proposed this pair">
                {p}
              </Chip>
            ))}
          </div>

          <div className="mt-3 space-y-2">
            <VerdictButtons
              kind="pair"
              verdict={stored}
              pending={pendingKey === key}
              annotation={notes.annotationOf(key, stored)}
              onVerdict={(value, annotation) =>
                submit(key, {
                  kind: 'pair',
                  listing_lo: lo,
                  listing_hi: hi,
                  verdict: value,
                  ...annotation,
                })
              }
            />
            {/* OPEN BY DEFAULT here: one pair is the whole page, so there is
              * nothing for the picker to push off the screen — unlike a queue
              * row, where it is collapsed. */}
            <VerdictNotes
              defaultOpen
              value={notes.annotationOf(key, stored)}
              onChange={(next) => notes.setAnnotation(key, next)}
              dirty={notes.isDirty(key, stored)}
              pending={pendingKey === key}
              onSave={() =>
                stored &&
                submit(key, {
                  kind: 'pair',
                  listing_lo: lo,
                  listing_hi: hi,
                  verdict: stored.verdict,
                  ...annotationInput(notes.annotationOf(key, stored)),
                })
              }
            />
          </div>

          {data.listings.lo && data.listings.hi && (
            <section className={SECTION}>
              <h2 className={EYEBROW}>Attributes</h2>
              <div className="mt-2">
                <AttrDiffTable
                  rows={memberDiffRows(data.listings.lo, data.listings.hi)}
                  captionA={`#${lo}`}
                  captionB={`#${hi}`}
                />
              </div>
            </section>
          )}

          <section className={SECTION}>
            <h2 className={EYEBROW}>Features</h2>
            {data.features.length === 0 ? (
              <p className="mt-1 text-[0.72rem] text-[var(--color-ink-3)]">
                No feature was stored for this pair.
              </p>
            ) : (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full text-[0.72rem]">
                  <thead>
                    <tr className={EYEBROW}>
                      <th className={TH}>Feature</th>
                      <th className={TH}>Value</th>
                      <th className={TH}>Present</th>
                      <th className={TH}>Contribution</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.features.map((f) => (
                      <tr key={f.name} className="border-t border-[var(--color-rule-soft)]">
                        <td className={`${TD} font-mono text-[var(--color-ink-2)]`}>{f.name}</td>
                        <td className={`${TD} font-mono tabular-nums`}>
                          {f.present ? fmtScore(f.value) : '—'}
                        </td>
                        <td className={`${TD} ${f.present ? '' : 'text-[var(--color-ink-4)]'}`}>
                          {f.present ? 'ano' : 'absent'}
                        </td>
                        <td className={`${TD} font-mono tabular-nums`}>
                          {f.contribution == null
                            ? '—'
                            : `${f.contribution > 0 ? '+' : ''}${fmtScore(f.contribution)}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className={SECTION}>
            <h2 className={EYEBROW}>Digests</h2>
            <div className="mt-2 grid gap-4 sm:grid-cols-2">
              <DigestPanel digest={data.digests.lo} />
              <DigestPanel digest={data.digests.hi} />
            </div>
          </section>

          <section className={SECTION}>
            <h2 className={EYEBROW}>Photos</h2>
            <p className="mt-1 text-[0.7rem] text-[var(--color-ink-3)]">
              Each photo shows its closest match on the other side — a Hamming distance over the
              perceptual hash. Small is similar; nothing here decides a merge on its own.
            </p>
            <div className="mt-2 grid gap-4 sm:grid-cols-2">
              <ImageSide title={`#${lo}`} images={data.images.lo} />
              <ImageSide title={`#${hi}`} images={data.images.hi} />
            </div>
          </section>

          <section className={SECTION}>
            <h2 className={EYEBROW}>Judgements</h2>
            {blind && stored == null ? (
              /* Said in words, never rendered as an empty section: "hidden" and
                * "nobody has judged this" are different facts, and printing the
                * second for the first would teach the operator that a blind pair
                * is an unjudged one. */
              <p className="mt-1 text-[0.72rem] text-[var(--color-ink-3)]">
                Naslepo: verdikt soudce je skrytý, dokud neuložíte vlastní verdikt.
              </p>
            ) : data.judgements.length === 0 ? (
              <p className="mt-1 text-[0.72rem] text-[var(--color-ink-3)]">
                No judge has ruled on this pair.
              </p>
            ) : (
              <ul className="mt-2 space-y-3">
                {data.judgements.map((j) => (
                  <JudgementBlock key={`${j.judge_version}:${j.tier}`} judgement={j} />
                ))}
              </ul>
            )}
          </section>

          {data.verdicts.length > 0 && (
            <section className={SECTION}>
              <h2 className={EYEBROW}>Operator verdicts</h2>
              <ul className="mt-2 space-y-1 text-[0.72rem] text-[var(--color-ink-2)]">
                {data.verdicts.map((v) => (
                  <li key={v.id}>
                    {/* The audit trail speaks the operator's vocabulary too
                      * (D39): a ruling stored under the finer values reads as
                      * "Různé", which is what it always meant to the engine. */}
                    {VERDICT_LABELS[displayVerdict(v.verdict)]} · {v.decided_by}
                    {v.note ? ` · ${v.note}` : ''}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function DigestPanel({ digest }: { digest: AutodedupDigest | null }) {
  if (!digest) {
    return (
      <p className="text-[0.72rem] text-[var(--color-ink-3)]">No digest for this side.</p>
    );
  }
  const rows: Array<[string, string]> = [
    ['portal', digest.portal ?? '—'],
    ['nabídka', digest.deal ?? '—'],
    ['druh', digest.category ?? '—'],
    ['podtyp', digest.subtype ?? '—'],
    ['dispozice', digest.disposition ?? '—'],
    ['plocha', digest.area_m2 == null ? '—' : String(digest.area_m2)],
    /* fmtFloor says the storey AND the building count, and says which scale. It
     * returns null on an absent storey, so the count keeps its own row: 77.8k active
     * rows (mostly houses) state podlaží and no patro, and on a same/different verdict
     * '2 podlaží' against '3' is exactly the distinguishing fact. */
    ['patro', fmtFloor(digest.floor, digest.total_floors) ?? '—'],
    ['podlaží', digest.total_floors == null ? '—' : String(digest.total_floors)],
    ['cena', digest.price == null ? '—' : fmtCzk(digest.price)],
    ['první', digest.first_seen ?? '—'],
    ['poslední', digest.last_seen ?? '—'],
    ['aktivní', digest.active ? 'ano' : 'ne'],
  ];
  /* The route does not emit a price trail today; an absent key is an absent
   * trail, not a crash. */
  const history = digest.price_history ?? [];
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper)] px-3 py-2">
      <p className="font-mono text-[0.7rem] text-[var(--color-ink-3)]">#{digest.listing_id}</p>
      <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[0.7rem]">
        {rows.map(([k, v]) => (
          <div key={k} className="flex items-baseline justify-between gap-2">
            <dt className="text-[var(--color-ink-4)]">{k}</dt>
            <dd className="font-mono tabular-nums text-[var(--color-ink-2)]">{v}</dd>
          </div>
        ))}
      </dl>
      {history.length > 0 && (
        <p className="mt-1 font-mono text-[0.65rem] text-[var(--color-ink-3)]">
          {history.map(([day, price]) => `${day}: ${price ?? '—'}`).join(' · ')}
        </p>
      )}
      {Object.keys(digest.attributes).length > 0 && (
        <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[0.68rem]">
          {Object.entries(digest.attributes).map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-2">
              <dt className="text-[var(--color-ink-4)]">{k}</dt>
              <dd className="text-[var(--color-ink-2)]">{v ?? '—'}</dd>
            </div>
          ))}
        </dl>
      )}
      {/* THE SAME component the group dialog uses, so the advert reads the same on
        * both surfaces and the unit tokens are marked the same way. The route now
        * sends the WHOLE scrubbed text here (the cap is the judge's token budget,
        * not this page's), so `description_truncated` is false — the ellipsis stays
        * for the day some other caller sends a capped digest. */}
      <MemberText text={digest.description} headingLevel={4} label={`#${digest.listing_id}`} />
      {digest.description_truncated && (
        <p className="text-[0.65rem] text-[var(--color-ink-4)]">popis je zkrácen…</p>
      )}
      {digest.absent.length > 0 && (
        <p className="mt-1 text-[0.65rem] text-[var(--color-ink-4)]">
          chybí: {digest.absent.join(', ')}
        </p>
      )}
    </div>
  );
}

/* THE EVIDENCE GRID IS THE POINT OF THIS PAGE, so a photo that will not load has
 * to say so. `imageSrc` falls back to the portal's own CDN when the image was
 * never copied into R2, and several portals refuse that request cross-origin
 * (ERR_BLOCKED_BY_ORB) — which renders as an empty box. Judging a per-image
 * Hamming delta against an empty box is exactly the wrong thing to ask of an
 * operator: the tile below says "foto nedostupné" and the caption keeps the Δ. */
function ImageSide({ title, images }: { title: string; images: AutodedupPairImage[] }) {
  const [broken, setBroken] = useState<Record<string, boolean>>({});
  return (
    <div>
      <h3 className="font-mono text-[0.7rem] text-[var(--color-ink-3)]">{title}</h3>
      {images.length === 0 ? (
        <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">No photo stored.</p>
      ) : (
        <ul className="mt-1 grid grid-cols-3 gap-2">
          {images.map((img, i) => {
            const key = String(img.image_id ?? `${img.sequence}:${i}`);
            return (
            <li key={key}>
              <div className="aspect-[4/3] overflow-hidden rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
                {broken[key] ? (
                  <div className="h-full w-full flex items-center justify-center px-1 text-center">
                    <span className="text-[0.58rem] text-[var(--color-ink-4)]">
                      foto nedostupné
                    </span>
                  </div>
                ) : (
                <img
                  src={imageSrc(img)}
                  alt=""
                  loading="lazy"
                  onError={() => setBroken((b) => ({ ...b, [key]: true }))}
                  className="h-full w-full object-cover"
                />
                )}
              </div>
              <p className="mt-0.5 font-mono text-[0.6rem] text-[var(--color-ink-4)] tabular-nums">
                {img.best_hamming == null ? 'no match' : `Δ${img.best_hamming}`}
                {img.best_match_image_id != null && ` → ${img.best_match_image_id}`}
              </p>
            </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function JudgementBlock({ judgement }: { judgement: AutodedupJudgementRow }) {
  return (
    <li className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper)] px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <JudgeChip judgement={judgement} />
        <span className="text-[0.65rem] text-[var(--color-ink-3)]">
          {judgement.tier} · {judgement.model} · {judgement.judge_version}
        </span>
        {judgement.developer_project_suspected && (
          <Chip tone="warn" title="The judge suspected a developer project">
            developer project
          </Chip>
        )}
      </div>
      {judgement.unit_discriminator && (
        <p className="mt-1 text-[0.7rem] text-[var(--color-ink-2)]">
          <span className="text-[var(--color-ink-4)]">Unit discriminator: </span>
          {judgement.unit_discriminator}
        </p>
      )}
      <div className="mt-1 grid gap-3 sm:grid-cols-2 text-[0.7rem]">
        <ul className="space-y-0.5 text-[var(--color-ink-2)]">
          {(judgement.key_evidence ?? []).map((e) => (
            <li key={e}>+ {e}</li>
          ))}
        </ul>
        <ul className="space-y-0.5 text-[var(--color-brick)]">
          {(judgement.contradicting_evidence ?? []).map((e) => (
            <li key={e}>− {e}</li>
          ))}
        </ul>
      </div>
    </li>
  );
}
