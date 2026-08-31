import { useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import {
  answerExamQuestion,
  getExamAnswers,
  type ExamAnswerRow,
  type ExamTag,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
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

type RowState = { picked: Set<number>; skipped: Set<number>; cantTell: boolean };

const rowStateOf = (r: ExamAnswerRow): RowState => ({
  picked: new Set(r.picked_tag_ids),
  skipped: new Set(r.skipped_tag_ids),
  cantTell: r.cant_tell,
});

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

  if (answersQ.isLoading) return <div className="p-6"><Spinner /></div>;
  if (answersQ.error) {
    return <div className="p-6"><ErrorBanner message={(answersQ.error as Error).message} /></div>;
  }

  const examHref = `/new-dedup/exam?cohort=${encodeURIComponent(cohort)}${
    setName ? `&set=${encodeURIComponent(setName)}` : ''}`;

  return (
    <div className="max-w-[68rem] mx-auto px-4 py-6">
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
            {rows.length} answered · everything here is a decision: &ndash; = negative · a click once = it applies · again = leave it out of that tag · again = back to negative · every change saves immediately
          </p>
        </div>
        <Link to={examHref} className="text-xs text-[var(--color-copper)] hover:underline">
          ← back to the exam
        </Link>
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
                <div className="w-40 shrink-0 bg-[var(--color-paper-2)] rounded-[var(--radius-sm)] flex items-center justify-center min-h-[6rem]">
                  {photo ? (
                    <img
                      src={imageSrc(photo)}
                      alt={`Exam photo ${r.position}`}
                      loading="lazy"
                      className="max-h-40 w-auto max-w-full object-contain rounded-[var(--radius-sm)]"
                    />
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
                  <div className="flex flex-wrap gap-1.5">
                    {tags.map((t) => {
                      const v = st.cantTell ? null
                        : st.picked.has(t.id) ? 'picked'
                          : st.skipped.has(t.id) ? 'skipped' : null;
                      return (
                        <button
                          key={t.id}
                          type="button"
                          onClick={() => cycleTag(r, t.id)}
                          aria-pressed={v === 'picked'}
                          className={`px-2.5 py-1.5 text-xs text-left rounded-[var(--radius-sm)] border transition-colors ${
                            v === 'picked'
                              ? 'border-[var(--color-sage)] bg-[var(--color-sage)]/10 text-[var(--color-ink)]'
                              : v === 'skipped'
                                ? 'border-dashed border-[var(--color-copper)] text-[var(--color-ink-2)]'
                                : 'border-[var(--color-rule)] text-[var(--color-ink-2)]'
                          } ${st.cantTell ? 'opacity-60' : ''}`}
                        >
                          {/* An unmarked cell here is NOT "untagged" — review only
                            * shows fully answered images, so plain = a recorded
                            * NEGATIVE. The dash says so, and it disappears under
                            * "can't tell" (those cells are excluded, not negative). */}
                          {v == null && !st.cantTell && (
                            <span aria-hidden className="mr-1 text-[var(--color-ink-4)]">&ndash;</span>
                          )}
                          {t.label}
                          {v === 'skipped' && (
                            <span className="ml-1.5 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-copper)]">
                              left out
                            </span>
                          )}
                        </button>
                      );
                    })}
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
