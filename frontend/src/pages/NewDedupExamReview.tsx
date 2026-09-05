import { useMemo, useState } from 'react';

import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { ROUTES } from '@/lib/routes';
import { examHref } from './NewDedupExam';
import {
  answerExamQuestion,
  dismissExamMachineProposal,
  getExamAnswers,
  type ExamAnswerRow,
  type ExamTag,
  type MachineVerdict,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
import { splitTagLabel } from '@/lib/tagLabel';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import { pushToast } from '@/lib/toast';

/* NEW DEDUP · Exam review — every answered exam image in a list, editable.
 *
 * The exam is one-image-at-a-time on purpose; this subpage exists for the pass
 * AFTER: scan what you answered, catch the slip, fix it in place. Same click
 * semantics as the exam buttons (once = it applies, again = leave it out of
 * that tag, again = untouched/negative), plus a per-row "can't tell".
 *
 * ONE WRITE PATH. Every edit re-answers the WHOLE image through the same
 * /answer route the exam sitting uses — review can never produce a row shape
 * the exam could not, and the anchoring audit (suggested-vs-final) keeps
 * working because "final" is simply the latest answer.
 *
 * Edits patch local state and never invalidate the visible list — the review
 * grid's own lesson: a list that reorders or refetches under a correcting
 * hand causes the very mis-clicks it exists to fix.
 */

type PhotoSize = 's' | 'm' | 'l';

/* Per-viewer convenience only — safe to lose. localStorage can throw (blocked
 * site data), so both sides are guarded and 's' is the working fallback. */
const PHOTO_SIZE_KEY = 'new-dedup-exam-review-photo-size';
const readPhotoSize = (): PhotoSize => {
  try {
    const v = localStorage.getItem(PHOTO_SIZE_KEY);
    return v === 'm' || v === 'l' ? v : 's';
  } catch { return 's'; }
};
const PHOTO_BOX: Record<PhotoSize, string> = {
  s: 'w-40 min-h-[6rem]',
  m: 'w-72 min-h-[10rem]',
  l: 'w-[30rem] min-h-[14rem]',
};
const PHOTO_IMG: Record<PhotoSize, string> = {
  s: 'max-h-40', m: 'max-h-80', l: 'max-h-[32rem]',
};

type RowState = { picked: Set<number>; skipped: Set<number>; cantTell: boolean };

const rowStateOf = (r: ExamAnswerRow): RowState => ({
  picked: new Set(r.picked_tag_ids),
  skipped: new Set(r.skipped_tag_ids),
  cantTell: r.cant_tell,
});

/* The human's verdict on one cell in the machine's vocabulary, so the two can
 * be compared cell by cell. "Can't tell" is excluded everywhere = skip. */
export const humanVerdictOf = (st: RowState, tagId: number): MachineVerdict =>
  st.cantTell ? 'skip' : st.picked.has(tagId) ? 'yes' : st.skipped.has(tagId) ? 'skip' : 'no';

export type Proposal = { tag: ExamTag; machine: MachineVerdict; human: MachineVerdict };

/* Where the machine's definition-driven verdict differs from the row's LIVE
 * state (not the server's copy, so an applied proposal disappears at once).
 * On a can't-tell row only the machine's yes is worth raising: proposing "no"
 * against "I can't tell" would turn a whole-row abstention into one negative. */
export const proposalsOf = (
  tags: ExamTag[], st: RowState, verdicts: Record<string, MachineVerdict> | undefined,
  dismissed: Set<number>,
): Proposal[] => {
  if (!verdicts) return [];
  const out: Proposal[] = [];
  for (const tag of tags) {
    const machine = verdicts[String(tag.id)];
    if (!machine || dismissed.has(tag.id)) continue;
    const human = humanVerdictOf(st, tag.id);
    if (machine === human) continue;
    if (st.cantTell && machine !== 'yes') continue;
    out.push({ tag, machine, human });
  }
  return out;
};

/* The row after accepting one proposal: that cell takes the machine's verdict,
 * every other cell keeps the human's. A can't-tell row starts over from the
 * accepted pick, exactly as a tag click on such a row does. */
export const applyProposal = (st: RowState, tagId: number, machine: MachineVerdict): RowState => {
  const next: RowState = st.cantTell
    ? { picked: new Set(), skipped: new Set(), cantTell: false }
    : { picked: new Set(st.picked), skipped: new Set(st.skipped), cantTell: false };
  next.picked.delete(tagId);
  next.skipped.delete(tagId);
  if (machine === 'yes') next.picked.add(tagId);
  if (machine === 'skip') next.skipped.add(tagId);
  return next;
};

const VERDICT_WORD: Record<MachineVerdict, string> = {
  yes: 'applies', no: 'negative', skip: 'left out',
};

export default function NewDedupExamReview() {
  const [params] = useSearchParams();
  const cohort = params.get('cohort') || 'exam_v1';
  const setName = params.get('set') ?? undefined;

  const answersQ = useQuery({
    queryKey: ['new-dedup', 'exam', cohort, 'answers', setName ?? 'default'],
    queryFn: () => getExamAnswers(cohort, setName),
  });
  const tags: ExamTag[] = useMemo(
    () => answersQ.data?.data.tags ?? [], [answersQ.data]);
  const rows: ExamAnswerRow[] = useMemo(
    () => answersQ.data?.data.rows ?? [], [answersQ.data]);

  const imagesQ = useQuery({
    queryKey: ['new-dedup', 'exam', cohort, 'answer-photos',
      rows.map((r) => r.image_id).join(',')],
    queryFn: () => fetchImagesByImageIds(rows.map((r) => r.image_id)),
    enabled: rows.length > 0,
  });

  /* Server state overlaid with local edits. Only edited rows live here, so a
   * refetch elsewhere cannot silently drop an in-flight correction. */
  const [edited, setEdited] = useState<Map<number, RowState>>(new Map());
  const [photoSize, setPhotoSize] = useState<PhotoSize>(readPhotoSize);
  const pickPhotoSize = (v: PhotoSize) => {
    setPhotoSize(v);
    try { localStorage.setItem(PHOTO_SIZE_KEY, v); } catch { /* convenience only */ }
  };
  const stateOf = (r: ExamAnswerRow): RowState => edited.get(r.image_id) ?? rowStateOf(r);

  const saveMut = useMutation({
    mutationFn: (body: {
      image_id: number; picked_tag_ids: number[]; skipped_tag_ids: number[];
      cant_tell: boolean; set?: string;
    }) => answerExamQuestion(cohort, body),
    onError: (e: Error, body) => {
      pushToast('err', e.message);
      // Revert the failed row to the server's version; other edits stand.
      setEdited((prev) => {
        const next = new Map(prev);
        next.delete(body.image_id);
        return next;
      });
    },
  });

  /* Dismissals patch local state like edits do; the server copy is the seed. */
  const [dismissed, setDismissed] = useState<Map<number, Set<number>>>(new Map());
  const dismissedOf = (r: ExamAnswerRow): Set<number> =>
    dismissed.get(r.image_id) ?? new Set(r.machine?.dismissed_tag_ids ?? []);

  const dismissMut = useMutation({
    mutationFn: (body: { image_id: number; tag_id: number }) =>
      dismissExamMachineProposal(cohort, body),
    onError: (e: Error, body) => {
      pushToast('err', e.message);
      setDismissed((prev) => {
        const next = new Map(prev);
        const cur = new Set(next.get(body.image_id) ?? []);
        cur.delete(body.tag_id);
        next.set(body.image_id, cur);
        return next;
      });
    },
  });

  const keepMine = (r: ExamAnswerRow, tagId: number) => {
    setDismissed((prev) => {
      const next = new Map(prev);
      next.set(r.image_id, new Set([...dismissedOf(r), tagId]));
      return next;
    });
    dismissMut.mutate({ image_id: r.image_id, tag_id: tagId });
  };

  const commit = (imageId: number, next: RowState) => {
    setEdited((prev) => new Map(prev).set(imageId, next));
    saveMut.mutate({
      image_id: imageId,
      picked_tag_ids: [...next.picked].sort((a, b) => a - b),
      skipped_tag_ids: [...next.skipped].sort((a, b) => a - b),
      cant_tell: next.cantTell,
      ...(setName ? { set: setName } : {}),
    });
  };

  const cycleTag = (r: ExamAnswerRow, tagId: number) => {
    const cur = stateOf(r);
    const next: RowState = {
      picked: new Set(cur.picked), skipped: new Set(cur.skipped), cantTell: false,
    };
    if (cur.cantTell) {
      // "Actually I can tell": the click IS the new answer, everything else
      // starts over as untouched (= negative on save).
      next.picked = new Set([tagId]);
      next.skipped = new Set();
    } else if (cur.picked.has(tagId)) {
      next.picked.delete(tagId);
      next.skipped.add(tagId);
    } else if (cur.skipped.has(tagId)) {
      next.skipped.delete(tagId);
    } else {
      next.picked.add(tagId);
    }
    commit(r.image_id, next);
  };

  const toggleCantTell = (r: ExamAnswerRow) => {
    const cur = stateOf(r);
    commit(r.image_id, cur.cantTell
      ? { picked: new Set(), skipped: new Set(), cantTell: false }
      : { picked: new Set(), skipped: new Set(), cantTell: true });
  };

  const reviewButton = (r: ExamAnswerRow, st: RowState, t: ExamTag) => {
    const v = st.cantTell ? null
      : st.picked.has(t.id) ? 'picked'
        : st.skipped.has(t.id) ? 'skipped' : null;
    const { family, name } = splitTagLabel(t.label);
    return (
      <button
        key={t.id}
        type="button"
        onClick={() => cycleTag(r, t.id)}
        aria-pressed={v === 'picked'}
        aria-label={t.label}
        title={t.label}
        data-verdict={v ?? (st.cantTell ? 'excluded' : 'negative')}
        className={`flex items-start gap-1.5 px-2.5 py-2 text-left rounded-[var(--radius-sm)] border transition-colors ${
          v === 'picked'
            ? 'border-[var(--color-sage)] bg-[var(--color-sage)]/10 text-[var(--color-ink)]'
            : v === 'skipped'
              ? 'border-dashed border-[var(--color-copper)] text-[var(--color-ink-2)]'
              : 'border-[var(--color-rule)] text-[var(--color-ink-2)]'
        } ${st.cantTell ? 'opacity-60' : ''}`}
      >
        {/* An unmarked cell here is NOT "untagged" — review only shows fully
          * answered images, so plain = a recorded NEGATIVE. The dash says so,
          * and it disappears under "can't tell" (excluded, not negative). */}
        {v == null && !st.cantTell && (
          <span aria-hidden className="mt-0.5 text-[var(--color-ink-4)]">&ndash;</span>
        )}
        {/* The machine's pre-answer beside your final — audit, never a verdict. */}
        {(r.suggested_tag_ids ?? []).includes(t.id) && (
          <span
            data-testid={`review-suggested-${t.id}`}
            className="shrink-0 mt-1.5 w-1.5 h-1.5 rounded-full bg-[var(--color-sage)]/80"
          />
        )}
        <span className="min-w-0 flex-1">
          {/* The family is PART of the tag, shown inline at full size: two tags
            * differ only by it (exterier vs interier - domovní vchod), and a
            * demoted eyebrow was measured to fail exactly there. */}
          <span className="block text-[0.8125rem] leading-snug text-pretty">
            {family && (
              <span className="text-[var(--color-ink-3)]">{family} &ndash; </span>
            )}
            {name}
          </span>
        </span>
        {v === 'skipped' && (
          <span className="shrink-0 mt-0.5 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-copper)]">
            left out
          </span>
        )}
      </button>
    );
  };

  if (answersQ.isLoading) return <div className="p-6"><Spinner /></div>;
  if (answersQ.error) {
    return <div className="p-6"><ErrorBanner message={(answersQ.error as Error).message} /></div>;
  }

  const backToExam = examHref(ROUTES.newDedupExam, cohort, setName);

  return (
    <div className="max-w-[112rem] mx-auto px-4 py-6">
      <header className="flex items-baseline justify-between flex-wrap gap-3 border-b border-[var(--color-rule)] pb-3">
        <div>
          <h1 className="text-lg font-medium text-[var(--color-ink)]">
            Exam review · {cohort}
            {answersQ.data?.data.set && answersQ.data.data.set !== 'routing' && (
              <span className="ml-2 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)] align-middle">
                {answersQ.data.data.set}
              </span>
            )}
          </h1>
          <p className="text-xs text-[var(--color-ink-3)] mt-0.5">
            {rows.length} answered · everything here is a decision: &ndash; = negative · a click once = it applies · again = leave it out of that tag · again = back to negative · dot = the machine\u2019s suggestion · proposals = where the machine\u2019s definition-driven verdict differs from yours · every change saves immediately
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1" role="group" aria-label="photo size">
            <span className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)] mr-0.5">photo</span>
            {(['s', 'm', 'l'] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => pickPhotoSize(v)}
                aria-pressed={photoSize === v}
                className={`px-2 py-1 text-xs uppercase rounded-[var(--radius-sm)] border ${
                  photoSize === v
                    ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                    : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
                }`}
              >
                {v}
              </button>
            ))}
          </span>
          <Link to={backToExam} className="text-xs text-[var(--color-copper)] hover:underline">
            ← back to the exam
          </Link>
        </div>
      </header>

      {rows.length === 0 ? (
        <p className="mt-10 text-center text-sm text-[var(--color-ink-2)]">
          Nothing answered in this sitting yet — answers appear here as you confirm them.
        </p>
      ) : (
        <ul className="mt-4 flex flex-col gap-3">
          {rows.map((r) => {
            const st = stateOf(r);
            const photo = imagesQ.data?.get(r.image_id);
            return (
              <li
                key={r.image_id}
                className="flex gap-4 items-start border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3"
              >
                <div className={`shrink-0 bg-[var(--color-paper-2)] rounded-[var(--radius-sm)] flex items-center justify-center ${PHOTO_BOX[photoSize]}`}>
                  {photo ? (
                    /* Opens the full image in a new tab for close inspection. */
                    <a href={imageSrc(photo)} target="_blank" rel="noreferrer">
                      <img
                        src={imageSrc(photo)}
                        alt={`Exam photo ${r.position}`}
                        loading="lazy"
                        className={`w-auto max-w-full object-contain rounded-[var(--radius-sm)] ${PHOTO_IMG[photoSize]}`}
                      />
                    </a>
                  ) : (
                    <Spinner size={14} />
                  )}
                </div>

                <div className="min-w-0 flex-1">
                  <p className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)] mb-2">
                    #{r.position}
                    {st.cantTell && (
                      <span className="ml-2 text-[var(--color-brick)]">can&rsquo;t tell</span>
                    )}
                  </p>
                  {/* One shared button renderer for both groups: the family
                    * is demoted to an eyebrow so the NAME gets the width (six
                    * inline labels per row broke Czech words mid-syllable), and
                    * the full label stays the accessible name. */}
                  <div className="grid gap-1.5 [grid-template-columns:repeat(auto-fill,minmax(12rem,1fr))]">
                    {tags.filter((t) => !(r.auto_tag_ids ?? []).includes(t.id))
                      .map((t) => reviewButton(r, st, t))}
                  </div>

                  {(r.auto_tag_ids ?? []).length > 0 && (
                    /* The 466 backfill: declared defaults, not judgments. The
                     * fence makes the unreviewed columns scannable; it empties
                     * (on reload) as rows are re-answered. */
                    <div className="mt-2 border border-dashed border-[var(--color-copper)]/60 rounded-[var(--radius-sm)] p-2">
                      <p className="text-[0.6rem] tracking-[0.12em] uppercase text-[var(--color-copper)] mb-1.5">
                        new · auto-negative — confirm or fix
                      </p>
                      <div className="grid gap-1.5 [grid-template-columns:repeat(auto-fill,minmax(12rem,1fr))]">
                        {tags.filter((t) => (r.auto_tag_ids ?? []).includes(t.id))
                          .map((t) => reviewButton(r, st, t))}
                      </div>
                    </div>
                  )}

                  {r.machine && (() => {
                    const proposals = proposalsOf(tags, st, r.machine.verdicts, dismissedOf(r));
                    return (
                      /* The definition-driven machine review (467) beside the
                       * answer. A proposal is a DISAGREEMENT with the row as it
                       * stands; "apply" is a normal whole-image re-answer with
                       * that one cell changed, "keep mine" records a dismissal. */
                      <section
                        data-testid={`machine-review-${r.image_id}`}
                        className="mt-2 border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-2 bg-[var(--color-paper-2)]/40"
                      >
                        <p className="text-[0.6rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)] mb-1.5">
                          machine review · {proposals.length === 0
                            ? 'agrees with every cell'
                            : `${proposals.length} proposal${proposals.length === 1 ? '' : 's'}`}
                        </p>
                        {proposals.length > 0 && (
                          <ul className="flex flex-col gap-1">
                            {proposals.map((p) => {
                              return (
                                <li
                                  key={p.tag.id}
                                  data-testid={`proposal-${r.image_id}-${p.tag.id}`}
                                  className="flex items-center gap-2 flex-wrap text-[0.8125rem]"
                                >
                                  <span className="text-[var(--color-ink)]">{p.tag.label}</span>
                                  <span className="text-[var(--color-ink-3)]">
                                    machine: <b className="font-medium text-[var(--color-ink-2)]">{VERDICT_WORD[p.machine]}</b>
                                    {' · '}you: {VERDICT_WORD[p.human]}
                                  </span>
                                  <span className="ml-auto flex gap-1">
                                    <button
                                      type="button"
                                      onClick={() => commit(r.image_id, applyProposal(st, p.tag.id, p.machine))}
                                      className="px-2 py-0.5 text-xs rounded-[var(--radius-sm)] border border-[var(--color-sage)] text-[var(--color-ink)] hover:bg-[var(--color-sage)]/10"
                                    >
                                      apply
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => keepMine(r, p.tag.id)}
                                      className="px-2 py-0.5 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
                                    >
                                      keep mine
                                    </button>
                                  </span>
                                </li>
                              );
                            })}
                          </ul>
                        )}
                      </section>
                    );
                  })()}

                  <div className="mt-2">
                    <button
                      type="button"
                      onClick={() => toggleCantTell(r)}
                      aria-pressed={st.cantTell}
                      className={`px-2.5 py-1.5 text-xs rounded-[var(--radius-sm)] border transition-colors ${
                        st.cantTell
                          ? 'border-[var(--color-brick)] bg-[var(--color-brick)]/10 text-[var(--color-ink)]'
                          : 'border-[var(--color-rule)] text-[var(--color-ink-3)]'
                      }`}
                    >
                      can&rsquo;t tell
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
