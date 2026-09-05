import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { ROUTES, withQuery, type RoutePath } from '@/lib/routes';

/* The exam surfaces address each other by (cohort, set). Spelled by hand at
 * four sites before this, each re-implementing the same optional-set rule. */
export function examHref(
  route: { build: () => RoutePath },
  cohort: string,
  setName?: string | null,
): RoutePath {
  return withQuery(route.build(), { cohort, set: setName || null });
}
import {
  answerExamQuestion,
  getExamCohorts,
  getExamSets,
  getExamState,
  getTagDefinitionCard,
  type ExamState,
  type TagHandbookCard,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
import { splitTagLabel } from '@/lib/tagLabel';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import DefinitionCard from '@/components/tag-definitions/DefinitionCard';
import { pushToast } from '@/lib/toast';

/* NEW DEDUP · Exam — the sealed holdout, answered one image at a time.
 *
 * This screen must NOT look like the labeling grid, and the reason is concrete
 * rather than aesthetic. That grid binds 1-4 to STATES (yes / no / excluded);
 * this one binds keys to TAG IDENTITIES. Same operator, same session, adjacent
 * pages, contradictory muscle memory — and a mis-keyed exam answer is the one
 * error that poisons every downstream number silently, because a wrong holdout
 * label looks exactly like a model being wrong.
 *
 * THE KEYS ARE LETTERS, NOT DIGITS (operator's ruling, 2026-08-30), and since
 * migration 466 there are eighteen of them — one block per hand, each block the
 * three-by-three the fingers already know:
 *
 *     q w e        i o p
 *     a s d        j k l
 *     y x c        b n m
 *
 * The blocks stack rather than sitting in one six-wide strip, and that is a
 * LEGIBILITY decision, not a cosmetic one: six columns across a 68rem page gave
 * each button ~75px of text and broke Czech labels mid-word ("koupeln / a",
 * "ohraniče / ním"), which is how a mis-click starts. Three columns in a wide
 * column give a label room to wrap between words. Array order stays key order,
 * read left-to-right through the left block then the right one.
 *
 * A MACHINE SUGGESTION APPEARS HERE — a subtle dot, never a pre-filled verdict
 * (the operator's ruling, reversing the original no-suggestion posture). The
 * honest cost is anchoring; the mitigation is provenance (every suggestion is
 * stored beside the final answer, so suggested-vs-final stays computable) and
 * restraint here: a dot marks what the machine would press, the verdict state
 * stays untouched, and nothing is shown when the stored suggestion answered a
 * different question list than this sitting asks.
 */

const EXAM_KEY = (cohort: string) => ['new-dedup', 'exam', cohort];

/* Position -> key: the left hand's nine, then the right hand's nine. 'u' stays
 * reserved for "can't tell". */
const EXAM_KEYS = ['q', 'w', 'e', 'a', 's', 'd', 'y', 'x', 'c',
                   'i', 'o', 'p', 'j', 'k', 'l', 'b', 'n', 'm'];
const HAND_SIZE = 9;

export default function NewDedupExam() {
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const cohort = params.get('cohort') || 'exam_v1';
  /* Which iteration to sit: /new-dedup/exam?cohort=exam_v1&set=set_2. Absent =
   * the first set (the sitting that predates sets). */
  const setName = params.get('set') ?? undefined;

  /* Per-tag verdict while composing one answer. Absent = negative on confirm.
   * 'picked' = positive; 'skipped' = the brief's leave-out — subject clearly
   * present, photo of something else — stored as excluded/'pruned' and neither
   * trained nor graded. A key cycles off -> picked -> skipped -> off. */
  const [verdicts, setVerdicts] = useState<Map<number, 'picked' | 'skipped'>>(new Map());
  const [hoveredTag, setHoveredTag] = useState<number | null>(null);

  const stateQ = useQuery({
    queryKey: [...EXAM_KEY(cohort), setName ?? 'routing'],
    queryFn: () => getExamState(cohort, setName),
  });
  const exam: ExamState | undefined = stateQ.data?.data;

  /* The cohort bar: since migration 464 the exam is plural — the sealed holdout
   * yardstick plus curated re-label cohorts — and switching must not mean
   * hand-editing a URL. Only rendered when there is a choice to make. */
  const cohortsQ = useQuery({
    queryKey: ['new-dedup', 'exam', 'cohorts'],
    queryFn: getExamCohorts,
    staleTime: 60_000,
  });
  const cohorts = cohortsQ.data?.data ?? [];

  /* The set bar, the cohort bar's twin: a sitting is (cohort x set), and
   * switching either must not mean hand-editing a URL. Active = the SERVER's
   * resolved set, so the bare URL's default lights the right chip. */
  const setsQ = useQuery({
    queryKey: ['new-dedup', 'exam', 'sets'],
    queryFn: getExamSets,
    staleTime: 60_000,
  });
  const sets = setsQ.data?.data ?? [];

  const tags = useMemo(() => exam?.tags ?? [], [exam]);
  const currentImageId = exam?.question?.image_id;

  /* The machine's pre-answer for the current question. */
  const suggestedIds = exam?.question?.suggested_tag_ids;
  const suggestionsKnown = suggestedIds != null;
  const suggested = useMemo(
    () => new Set(suggestionsKnown ? suggestedIds : []),
    [suggestionsKnown, suggestedIds],
  );

  const photoQ = useQuery({
    queryKey: ['new-dedup', 'exam', 'photo', currentImageId],
    queryFn: () => fetchImagesByImageIds([currentImageId as number]),
    enabled: currentImageId != null,
  });
  const photo = currentImageId != null ? photoQ.data?.get(currentImageId) : undefined;

  /* The card for the tag under the cursor. Fetched only on hover so the screen
   * does not pull eighteen documents it may never show. */
  const cardQ = useQuery({
    queryKey: ['new-dedup', 'labeling', 'definition', hoveredTag, 'card'],
    queryFn: () => getTagDefinitionCard(hoveredTag as number),
    enabled: hoveredTag != null,
    staleTime: 5 * 60_000,
  });
  const card: TagHandbookCard | null = cardQ.data?.data?.card ?? null;

  const answerMut = useMutation({
    mutationFn: (body: {
      image_id: number; picked_tag_ids: number[]; skipped_tag_ids: number[]; cant_tell: boolean;
    }) => answerExamQuestion(cohort, body),
    onSuccess: () => {
      setVerdicts(new Map());
      qc.invalidateQueries({ queryKey: EXAM_KEY(cohort) });
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  /* `toSend` is EXPLICIT, never read from state: "None of these" means none even
   * when picks are on screen, and setVerdicts(new Map()) settles after this
   * closure runs — the first version submitted the abandoned picks. */
  const advance = useCallback(
    (cantTell: boolean, toSend: Map<number, 'picked' | 'skipped'>) => {
      if (currentImageId == null) return;
      if (answerMut.isPending) return;
      const picked_tag_ids: number[] = [];
      const skipped_tag_ids: number[] = [];
      for (const [id, v] of toSend) (v === 'picked' ? picked_tag_ids : skipped_tag_ids).push(id);
      answerMut.mutate({
        image_id: currentImageId,
        picked_tag_ids,
        skipped_tag_ids,
        cant_tell: cantTell,
        ...(setName ? { set: setName } : {}),
      });
    },
    [currentImageId, answerMut, setName],
  );

  const cycle = useCallback((tagId: number) => {
    setVerdicts((prev) => {
      const next = new Map(prev);
      const cur = next.get(tagId);
      if (cur == null) next.set(tagId, 'picked');
      else if (cur === 'picked') next.set(tagId, 'skipped');
      else next.delete(tagId);
      return next;
    });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === ' ') {
        // Space = "none of these", the commonest answer by far: most random
        // photos are none of the set, and it has to cost one keystroke.
        e.preventDefault();
        setVerdicts(new Map());
        advance(false, new Map());
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        advance(false, verdicts);
        return;
      }
      const key = e.key.length === 1 ? e.key.toLowerCase() : '';
      if (key === 'u') {
        e.preventDefault();
        advance(true, new Map());
        return;
      }
      /* Matched on the CHARACTER, not the physical key: the operator's layout
       * is Czech QWERTZ, and the letter they named is the letter they press. */
      const idx = EXAM_KEYS.indexOf(key);
      if (idx >= 0 && idx < tags.length) {
        e.preventDefault();
        cycle(tags[idx].id);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [tags, cycle, advance, verdicts]);

  if (stateQ.isLoading) return <div className="p-6"><Spinner /></div>;
  if (stateQ.error) return <div className="p-6"><ErrorBanner message={(stateQ.error as Error).message} /></div>;
  if (!exam) return null;

  const { progress } = exam;
  const finished = exam.question == null;
  const hands = [tags.slice(0, HAND_SIZE), tags.slice(HAND_SIZE, HAND_SIZE * 2)]
    .filter((hand) => hand.length > 0);

  const tagButton = (t: { id: number; label: string }, i: number) => {
    const v = verdicts.get(t.id);
    const isSuggested = suggested.has(t.id);
    const { family, name } = splitTagLabel(t.label);
    return (
      <button
        key={t.id}
        type="button"
        onClick={() => cycle(t.id)}
        onMouseEnter={() => setHoveredTag(t.id)}
        onFocus={() => setHoveredTag(t.id)}
        aria-pressed={v === 'picked'}
        aria-label={t.label}
        title={t.label}
        className={`flex items-start gap-2 px-3 py-2.5 min-h-[3.5rem] text-left rounded-[var(--radius-sm)] border transition-colors ${
          v === 'picked'
            ? 'border-[var(--color-sage)] bg-[var(--color-sage)]/10 text-[var(--color-ink)]'
            : v === 'skipped'
              ? 'border-dashed border-[var(--color-copper)] text-[var(--color-ink-2)]'
              : isSuggested
                ? 'border-[var(--color-sage)]/50 text-[var(--color-ink-2)]'
                : 'border-[var(--color-rule)] text-[var(--color-ink-2)]'
        }`}
      >
        <kbd className="shrink-0 mt-0.5 font-mono text-[0.7rem] px-1.5 py-0.5 rounded border border-[var(--color-rule)] text-[var(--color-ink-3)]">
          {EXAM_KEYS[i]}
        </kbd>
        <span className="min-w-0 flex-1">
          {family && (
            /* The family, kept but demoted: two tags differ ONLY by it
             * (exterier vs interier - domovní vchod), so it cannot be dropped —
             * but inline it eats the width the name needs. */
            <span className="block text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)] leading-none mb-0.5">
              {family}
            </span>
          )}
          <span className="block text-[0.8125rem] leading-snug text-pretty">{name}</span>
          {v === 'skipped' && (
            <span className="block mt-0.5 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-copper)]">
              left out
            </span>
          )}
        </span>
        {/* The machine's mark: visible, subtle, and NEVER a pre-filled verdict
          * — aria-pressed stays false until the operator acts. */}
        {isSuggested && (
          <span
            data-testid={`suggested-${t.id}`}
            className="shrink-0 mt-1.5 w-1.5 h-1.5 rounded-full bg-[var(--color-sage)]/80"
          />
        )}
      </button>
    );
  };

  return (
    <div className="max-w-[112rem] mx-auto px-4 py-6">
      <header className="flex items-baseline justify-between flex-wrap gap-3 border-b border-[var(--color-rule)] pb-3">
        <div>
          <h1 className="text-lg font-medium text-[var(--color-ink)]">
            Exam · {exam.cohort.name}
            {exam.set !== 'routing' && (
              <span className="ml-2 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)] align-middle">
                {exam.set}
              </span>
            )}
          </h1>
          <p className="text-xs text-[var(--color-ink-3)] mt-0.5">
            {progress.answered} of {progress.total} answered · {progress.remaining} left
          </p>
        </div>
        <div className="flex items-baseline gap-3">
          <Link
            to={examHref(ROUTES.newDedupExamReview, cohort, setName)}
            className="text-xs text-[var(--color-copper)] hover:underline"
          >
            review answers →
          </Link>
          {!exam.cohort.sealed && (
            <span className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-brick)]">
              cohort not sealed
            </span>
          )}
        </div>
      </header>

      {(cohorts.length > 1 || sets.length > 1) && (
        <nav className="mt-3 flex items-center gap-2 flex-wrap">
          {cohorts.length > 1 && cohorts.map((c) => (
            <Link
              key={c.name}
              to={examHref(ROUTES.newDedupExam, c.name, setName)}
              aria-current={c.name === cohort ? 'page' : undefined}
              className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                c.name === cohort
                  ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
              }`}
            >
              {c.name}
              <span className="ml-1.5 text-[0.65rem] tracking-[0.08em] uppercase text-[var(--color-ink-4)]">
                {c.purpose === 'curated' ? 'curated' : 'holdout'} · {c.members}
              </span>
            </Link>
          ))}
          {cohorts.length > 1 && sets.length > 1 && (
            <span aria-hidden className="mx-1 text-[var(--color-rule)]">|</span>
          )}
          {sets.length > 1 && sets.map((t) => (
            <Link
              key={t.name}
              to={examHref(ROUTES.newDedupExam, cohort, t.name)}
              aria-current={t.name === exam.set ? 'page' : undefined}
              className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                t.name === exam.set
                  ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
              }`}
            >
              {t.name}
              <span className="ml-1.5 text-[0.65rem] tracking-[0.08em] uppercase text-[var(--color-ink-4)]">
                {t.tag_count} tags
              </span>
            </Link>
          ))}
        </nav>
      )}

      {finished ? (
        <p className="mt-10 text-center text-sm text-[var(--color-ink-2)]">
          Every image has a verdict on all {tags.length} tags. The exam is complete.
        </p>
      ) : (
        /* Photo left, controls right: the buttons take the whole right column
         * so eighteen labels get real width, and the definition card sits under
         * the photo where a hover can be read without leaving the buttons. */
        <section className="mt-5 grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(34rem,44rem)]">
          <div className="min-w-0">
            <div className="bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded-[var(--radius-sm)] flex items-center justify-center min-h-[24rem]">
              {photo ? (
                <img
                  src={imageSrc(photo)}
                  alt="Exam photo"
                  className="max-h-[42rem] w-auto max-w-full object-contain"
                />
              ) : (
                <Spinner />
              )}
            </div>
            <div className="mt-4">
              {card ? (
                <DefinitionCard card={card} />
              ) : (
                <p className="text-xs text-[var(--color-ink-3)] border border-dashed border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4">
                  Hover a category to see how it is defined.
                </p>
              )}
            </div>
          </div>

          <div className="min-w-0">
            <h2 className="text-base text-[var(--color-ink)]">
              Which of these, if any, is this photo a usable example of?
            </h2>

            <div className="mt-3 flex flex-col gap-3">
              {hands.map((hand, h) => (
                <div key={h} className="grid grid-cols-3 gap-2">
                  {hand.map((t, i) => tagButton(t, h * HAND_SIZE + i))}
                </div>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-2 flex-wrap">
              <button
                type="button"
                onClick={() => { setVerdicts(new Map()); advance(false, new Map()); }}
                disabled={answerMut.isPending}
                className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-50"
              >
                {suggestionsKnown && suggested.size === 0 && (
                  <span
                    data-testid="suggested-none"
                    className="inline-block w-1.5 h-1.5 rounded-full bg-[var(--color-paper)]/80 mr-1.5 align-middle"
                  />
                )}
                None of these <kbd className="ml-1 font-mono text-[0.7rem]">space</kbd>
              </button>
              <button
                type="button"
                onClick={() => advance(false, verdicts)}
                disabled={answerMut.isPending || verdicts.size === 0}
                className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink)] disabled:opacity-40"
              >
                Confirm {verdicts.size > 0 ? `(${verdicts.size})` : ''}{' '}
                <kbd className="ml-1 font-mono text-[0.7rem]">enter</kbd>
              </button>
              <button
                type="button"
                onClick={() => advance(true, new Map())}
                disabled={answerMut.isPending}
                className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40"
              >
                Can&rsquo;t tell <kbd className="ml-1 font-mono text-[0.7rem]">u</kbd>
              </button>
              {answerMut.isPending && <Spinner size={12} />}
            </div>

            {/* Permanent, not a tooltip: the grid next door binds digits to
              * states, and the legend is what stands between habit and a
              * mis-keyed exam row. */}
            <p className="mt-3 text-[0.7rem] text-[var(--color-ink-4)]">
              a letter once = it applies · again = leave it out of that tag · space = none of these · enter = confirm · u = can&rsquo;t tell at all · dot = the machine&rsquo;s suggestion
            </p>
          </div>
        </section>
      )}
    </div>
  );
}
