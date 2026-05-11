import ConfidenceIndicator from '@/components/ConfidenceIndicator';
import type { Confidence } from '@/lib/types';

/* Temporary review surface for the ConfidenceIndicator component
 * (estimation-5 Part C1 design gate). Renders the indicator at every
 * defined confidence level and in the surrounding chrome it'll live
 * in once Part C2-4 wires it into EstimateForm. Delete this file +
 * the route in routes.tsx once the icon is in real use. */

type Row = {
  confidence: Confidence | null;
  fieldName: string;
  exampleValue: string;
};

const ROWS: Row[] = [
  { confidence: 'high',        fieldName: 'Area',         exampleValue: '65 m²'  },
  { confidence: 'medium',      fieldName: 'Disposition',  exampleValue: '2+kk'   },
  { confidence: 'low',         fieldName: 'Floor',        exampleValue: '3'      },
  { confidence: 'best_effort', fieldName: 'Building type', exampleValue: 'panel' },
  { confidence: null,          fieldName: 'Energy class',  exampleValue: 'C'     },
];

export default function DevConfidencePreview() {
  return (
    <div className="px-6 py-12 max-w-3xl mx-auto">
      <header className="mb-8">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          estimation-5 · design review
        </p>
        <h1
          className="mt-2 text-[1.6rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Per-field confidence indicators
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)] leading-relaxed">
          Each indicator sits in the field-label's right-edge "hint" slot
          on the editable spec form. Hover to read the tooltip. Delete
          this page after design approval.
        </p>
      </header>

      {/* In-context preview: how the indicator looks next to the
          uppercase tracked-out FieldHeader label that EstimateForm
          uses today. */}
      <section className="mb-10">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium mb-3">
          In-context (uppercase tracked label · indicator · input)
        </p>
        <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-5 space-y-4">
          {ROWS.map((r, i) => (
            <FieldRow key={i} row={r} />
          ))}
        </div>
      </section>

      {/* Bare-glyph grid: see the icons at full attention without
          form chrome competing for visual weight. */}
      <section>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium mb-3">
          Bare glyphs (full attention)
        </p>
        <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-5">
          <div className="grid grid-cols-5 gap-4">
            {ROWS.map((r, i) => (
              <div key={i} className="flex flex-col items-center gap-3">
                <div className="flex items-center justify-center h-10 w-10 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] border border-[var(--color-rule)]">
                  <ConfidenceIndicator
                    confidence={r.confidence}
                    fieldName={r.fieldName}
                  />
                </div>
                <code className="text-[0.7rem] text-[var(--color-ink-3)] font-mono">
                  {r.confidence ?? 'null'}
                </code>
              </div>
            ))}
          </div>
          <p className="mt-5 text-[0.7rem] text-[var(--color-ink-3)] leading-relaxed">
            null renders nothing — the form looks identical to today for
            sreality runs (no parser confidence) and manual entry.
          </p>
        </div>
      </section>
    </div>
  );
}

function FieldRow({ row }: { row: Row }) {
  return (
    <div className="grid grid-cols-[160px_1fr] items-center gap-4">
      <div className="flex items-baseline justify-between gap-2">
        <label className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          {row.fieldName}
        </label>
        <ConfidenceIndicator
          confidence={row.confidence}
          fieldName={row.fieldName}
        />
      </div>
      <input
        type="text"
        defaultValue={row.exampleValue}
        readOnly
        className="w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none"
      />
    </div>
  );
}
