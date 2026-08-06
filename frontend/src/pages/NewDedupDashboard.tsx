/* Wave 0 scaffold. Placeholder for the Wave 1 dashboard (funnel + costs) —
 * it exists so the NEW DEDUP nav structure is in place. Keep it minimal. */
export default function NewDedupDashboard() {
  return (
    <div className="px-6 py-12 max-w-2xl mx-auto">
      <h1
        className="text-[1.6rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        NEW DEDUP
      </h1>
      <p className="mt-3 text-sm text-[var(--color-ink-2)] leading-relaxed">
        The rebuilt deduplication program. For scope, waves and open questions,
        see <code>docs/design/new-dedup/PROGRAM.md</code> in the repo.
      </p>
      <p className="mt-4 text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Wave 0 in progress
      </p>
    </div>
  );
}
