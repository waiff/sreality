import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  answerExamQuestion,
  getExamState,
  getExamWarmup,
  getTagDefinitionCard,
  type ExamState,
  type TagHandbookCard,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import DefinitionCard from '@/components/tag-definitions/DefinitionCard';
import { pushToast } from '@/lib/toast';

/* NEW DEDUP · Exam — the sealed holdout, answered one image at a time.
 *
 * This screen must NOT look like the labeling grid, and the reason is concrete
 * rather than aesthetic. That grid binds 1-4 to STATES (yes / no / excluded);
 * this one binds 1-8 to TAG IDENTITIES. Same operator, same session, adjacent
 * pages, contradictory muscle memory — and a mis-keyed exam answer is the one
 * error that poisons every downstream number silently, because a wrong holdout
 * label looks exactly like a model being wrong.
 *
 * So: one large image instead of a grid, a permanent key legend rather than a
 * tooltip, and a warm-up on throwaway images before the first real question.
 *
 * NO MACHINE SUGGESTION APPEARS HERE. The screener's guesses exist in the
 * database and are deliberately not requested: a pre-ticked answer would anchor
 * the operator, and an exam the machine helped answer cannot grade the machine.
 */

const EXAM_KEY = (cohort: string) => ['new-dedup', 'exam', cohort];
const WARMUP_KEY = (cohort: string) => ['new-dedup', 'exam', cohort, 'warmup'];
const WARMUP_COUNT = 10;

export default function NewDedupExam() {
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const cohort = params.get('cohort') || 'exam_v1';

  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [warmupIndex, setWarmupIndex] = useState(0);
  const [warmupDone, setWarmupDone] = useState(false);
  const [hoveredTag, setHoveredTag] = useState<number | null>(null);

  const stateQ = useQuery({
    queryKey: EXAM_KEY(cohort),
    queryFn: () => getExamState(cohort),
  });
  const warmupQ = useQuery({
    queryKey: WARMUP_KEY(cohort),
    queryFn: () => getExamWarmup(cohort, WARMUP_COUNT),
    enabled: !warmupDone,
  });

  const exam: ExamState | undefined = stateQ.data?.data;
  const tags = useMemo(() => exam?.tags ?? [], [exam]);
  const warmupRows = warmupQ.data?.data ?? [];
  const inWarmup = !warmupDone && warmupRows.length > 0 && warmupIndex < warmupRows.length;

  const currentImageId = inWarmup
    ? warmupRows[warmupIndex]?.image_id
    : exam?.question?.image_id;

  const photoQ = useQuery({
    queryKey: ['new-dedup', 'exam', 'photo', currentImageId],
    queryFn: () => fetchImagesByImageIds([currentImageId as number]),
    enabled: currentImageId != null,
  });
  const photo = currentImageId != null ? photoQ.data?.get(currentImageId) : undefined;

  /* The card for the tag under the cursor. Fetched only on hover so the screen
   * does not pull eight documents it may never show. */
  const cardQ = useQuery({
    queryKey: ['new-dedup', 'labeling', 'definition', hoveredTag, 'card'],
    queryFn: () => getTagDefinitionCard(hoveredTag as number),
    enabled: hoveredTag != null,
    staleTime: 5 * 60_000,
  });
  const card: TagHandbookCard | null = cardQ.data?.data?.card ?? null;

  const answerMut = useMutation({
    mutationFn: (body: { image_id: number; picked_tag_ids: number[]; cant_tell: boolean }) =>
      answerExamQuestion(cohort, body),
    onSuccess: () => {
      setPicked(new Set());
      qc.invalidateQueries({ queryKey: EXAM_KEY(cohort) });
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  const advance = useCallback(
    (cantTell: boolean) => {
      if (currentImageId == null) return;
      if (inWarmup) {
        /* Practice is never written. The server would refuse it anyway — a
         * warm-up image is not a cohort member — but not sending it keeps the
         * refusal a rail rather than a routine 422. */
        setPicked(new Set());
        setWarmupIndex((i) => i + 1);
        return;
      }
      if (answerMut.isPending) return;
      answerMut.mutate({
        image_id: currentImageId,
        picked_tag_ids: [...picked],
        cant_tell: cantTell,
      });
    },
    [currentImageId, inWarmup, picked, answerMut],
  );

  const toggle = useCallback((tagId: number) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(tagId)) next.delete(tagId);
      else next.add(tagId);
      return next;
    });
  }, []);

  useEffect(() => {
    if (!inWarmup && warmupRows.length > 0 && warmupIndex >= warmupRows.length) {
      setWarmupDone(true);
    }
  }, [inWarmup, warmupIndex, warmupRows.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key >= '1' && e.key <= '9') {
        const idx = Number(e.key) - 1;
        if (idx < tags.length) {
          e.preventDefault();
          toggle(tags[idx].id);
        }
        return;
      }
      if (e.key === ' ') {
        // Space = "none of these", the commonest answer by far: most random
        // photos are none of the eight, and it has to cost one keystroke.
        e.preventDefault();
        setPicked(new Set());
        advance(false);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        advance(false);
        return;
      }
      if (e.key.toLowerCase() === 'u') {
        e.preventDefault();
        advance(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [tags, toggle, advance]);

  if (stateQ.isLoading) return <div className="p-6"><Spinner /></div>;
  if (stateQ.error) return <div className="p-6"><ErrorBanner message={(stateQ.error as Error).message} /></div>;
  if (!exam) return null;

  const { progress } = exam;
  const finished = !inWarmup && exam.question == null;

  return (
    <div className="max-w-[68rem] mx-auto px-4 py-6">
      <header className="flex items-baseline justify-between flex-wrap gap-3 border-b border-[var(--color-rule)] pb-3">
        <div>
          <h1 className="text-lg font-medium text-[var(--color-ink)]">
            Exam · {exam.cohort.name}
          </h1>
          <p className="text-xs text-[var(--color-ink-3)] mt-0.5">
            {inWarmup
              ? `Warm-up ${warmupIndex + 1} of ${warmupRows.length} — these do not count`
              : `${progress.answered} of ${progress.total} answered · ${progress.remaining} left`}
          </p>
        </div>
        {!exam.cohort.sealed && (
          <span className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-brick)]">
            cohort not sealed
          </span>
        )}
      </header>

      {finished ? (
        <p className="mt-10 text-center text-sm text-[var(--color-ink-2)]">
          Every image has a verdict on all {tags.length} tags. The exam is complete.
        </p>
      ) : (
        <>
          <section className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
            <div className="min-w-0">
              {/* ONE large image, deliberately unlike the review grid next door. */}
              <div className="bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded-[var(--radius-sm)] flex items-center justify-center min-h-[24rem]">
                {photo ? (
                  <img
                    src={imageSrc(photo)}
                    alt="Exam photo"
                    className="max-h-[34rem] w-auto max-w-full object-contain"
                  />
                ) : (
                  <Spinner />
                )}
              </div>

              <h2 className="mt-4 text-base text-[var(--color-ink)]">
                Which of these, if any, is this photo a usable example of?
              </h2>

              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {tags.map((t, i) => {
                  const on = picked.has(t.id);
                  return (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => toggle(t.id)}
                      onMouseEnter={() => setHoveredTag(t.id)}
                      onFocus={() => setHoveredTag(t.id)}
                      aria-pressed={on}
                      className={`flex items-center gap-2 px-3 py-2 text-sm text-left rounded-[var(--radius-sm)] border transition-colors ${
                        on
                          ? 'border-[var(--color-sage)] bg-[var(--color-sage)]/10 text-[var(--color-ink)]'
                          : 'border-[var(--color-rule)] text-[var(--color-ink-2)]'
                      }`}
                    >
                      <kbd className="font-mono text-[0.7rem] px-1.5 py-0.5 rounded border border-[var(--color-rule)] text-[var(--color-ink-3)]">
                        {i + 1}
                      </kbd>
                      <span className="truncate">{t.label}</span>
                    </button>
                  );
                })}
              </div>

              <div className="mt-4 flex items-center gap-2 flex-wrap">
                <button
                  type="button"
                  onClick={() => { setPicked(new Set()); advance(false); }}
                  disabled={answerMut.isPending}
                  className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-50"
                >
                  None of these <kbd className="ml-1 font-mono text-[0.7rem]">space</kbd>
                </button>
                <button
                  type="button"
                  onClick={() => advance(false)}
                  disabled={answerMut.isPending || picked.size === 0}
                  className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink)] disabled:opacity-40"
                >
                  Confirm {picked.size > 0 ? `(${picked.size})` : ''}{' '}
                  <kbd className="ml-1 font-mono text-[0.7rem]">enter</kbd>
                </button>
                <button
                  type="button"
                  onClick={() => advance(true)}
                  disabled={answerMut.isPending}
                  className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40"
                >
                  Can&rsquo;t tell <kbd className="ml-1 font-mono text-[0.7rem]">u</kbd>
                </button>
                {answerMut.isPending && <Spinner size={12} />}
              </div>

              {/* Permanent, not a tooltip: the grid next door binds these same
                * digits to states, and the legend is the only thing standing
                * between that habit and a mis-keyed exam row. */}
              <p className="mt-3 text-[0.7rem] text-[var(--color-ink-4)]">
                1–{tags.length} pick a category · space = none of these · enter = confirm · u = can&rsquo;t tell
              </p>
            </div>

            <aside className="min-w-0">
              {card ? (
                <DefinitionCard card={card} />
              ) : (
                <p className="text-xs text-[var(--color-ink-3)] border border-dashed border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4">
                  Hover a category to see how it is defined.
                </p>
              )}
            </aside>
          </section>
        </>
      )}
    </div>
  );
}
