import type { Confidence } from '@/lib/types';

/* Per-field marker showing how confident the URL parser was in a
 * given extracted value. Renders nothing for null (the case for
 * sreality runs and manual entry — no parser confidence to report).
 *
 * Visual language: small SVG glyphs sitting in the field-label's
 * existing right-edge "hint" slot, sized to baseline-align with the
 * uppercase tracked-out label. Tone follows the project's semantic
 * palette (sage = success, ochre = warn). Same-glyph + colour-shift
 * for high vs medium so the eye reads them as a continuum, not two
 * unrelated states.
 *
 * Tooltips use the native `title` attribute. Desktop-first;
 * touch users can long-press / focus to see the tooltip if their
 * platform supports it. */

interface Props {
  confidence: Confidence | null;
  fieldName: string;
}

interface State {
  glyph: 'tick' | 'triangle' | 'question';
  toneClass: string;
  description: string;
}

const STATES: Record<Confidence, State> = {
  high: {
    glyph: 'tick',
    toneClass: 'text-[var(--color-sage)]',
    description: 'Parser is confident in this value.',
  },
  medium: {
    glyph: 'tick',
    toneClass: 'text-[var(--color-ink-3)]',
    description: 'Parser found this value but recommends review.',
  },
  low: {
    glyph: 'triangle',
    toneClass: 'text-[var(--color-ochre)]',
    description: 'Parser is unsure about this value — please verify.',
  },
  best_effort: {
    glyph: 'question',
    toneClass: 'text-[var(--color-ochre)]',
    description:
      'Parser could not determine this value confidently. Please enter manually.',
  },
};

export default function ConfidenceIndicator({ confidence, fieldName }: Props) {
  if (confidence == null) return null;
  const state = STATES[confidence];
  const tooltip = `${fieldName} · ${state.description}`;
  return (
    <span
      title={tooltip}
      aria-label={tooltip}
      className={[
        'inline-flex items-center justify-center align-baseline cursor-help',
        state.toneClass,
      ].join(' ')}
      style={{ width: 11, height: 11, lineHeight: 0 }}
    >
      <Glyph kind={state.glyph} />
    </span>
  );
}

function Glyph({ kind }: { kind: State['glyph'] }) {
  if (kind === 'tick') {
    return (
      <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden focusable="false">
        <path
          d="M2 6.5 L5 9.5 L10 3.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  if (kind === 'triangle') {
    return (
      <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden focusable="false">
        <path d="M6 1.6 L11 10.4 L1 10.4 Z" fill="currentColor" />
      </svg>
    );
  }
  // question
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden focusable="false">
      <text
        x="6"
        y="9.4"
        textAnchor="middle"
        fontSize="10"
        fontWeight="700"
        fontFamily="var(--font-sans)"
        fill="currentColor"
      >
        ?
      </text>
    </svg>
  );
}
