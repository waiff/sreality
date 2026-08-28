/* The handbook card — a tag definition as a person reads it while labeling.
 *
 * Every string here is rendered by toolkit/tag_definition_render.py and arrives
 * over the wire. This component decides LAYOUT only; it must never assemble a
 * sentence, resolve a tag label, or decide what belongs in which list. The moment
 * it does, there are two answers to "what does this tag mean" and they drift.
 *
 * The headings are deliberately plain. The operator never meets `counts`,
 * `does_not_count`, `confusable_with` or `leave_out_when` — those are the storage
 * shape, and being shown them is what made definition-writing confusing. */

import type { TagHandbookCard } from '@/lib/api';

interface Props {
  card: TagHandbookCard;
  /* Set when the card previews an unsaved draft, so the reader knows they are
   * looking at what they are typing rather than at what is stored. */
  draft?: boolean;
}

function Section({
  heading, tone, items,
}: {
  heading: string;
  tone: 'yes' | 'no' | 'unsure';
  items: string[];
}) {
  /* An empty section is omitted, never rendered as a heading over nothing: a
   * definition with no exclusions and one whose exclusions failed to load must
   * not look the same. */
  if (items.length === 0) return null;
  const color =
    tone === 'yes' ? 'var(--color-sage)'
      : tone === 'no' ? 'var(--color-brick)'
        : 'var(--color-ink-3)';
  return (
    <div className="mt-3 first:mt-0">
      <span
        className="block text-[0.65rem] tracking-[0.14em] uppercase"
        style={{ color }}
      >
        {heading}
      </span>
      <ul className="mt-1 list-disc pl-4 text-sm text-[var(--color-ink-2)] space-y-0.5">
        {items.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </div>
  );
}

export default function DefinitionCard({ card, draft = false }: Props) {
  return (
    <section
      aria-label={`How to label ${card.tag_label}`}
      className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4 bg-[var(--color-paper-2)]"
    >
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <h4 className="text-[0.95rem] font-medium text-[var(--color-ink)]">
          {card.headline}
        </h4>
        {draft && (
          <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            unsaved draft
          </span>
        )}
      </div>

      <Section heading="Count it" tone="yes" items={card.count_it} />
      <Section heading="Don't count it" tone="no" items={card.dont_count_it} />
      <Section heading="Can't tell — skip" tone="unsure" items={card.cant_tell} />

      {card.count_it.length === 0
        && card.dont_count_it.length === 0
        && card.cant_tell.length === 0 && (
        <p className="mt-2 text-xs text-[var(--color-ink-3)]">
          Nothing to show yet — write what this tag means and the card fills in.
        </p>
      )}
    </section>
  );
}
