export default function Health() {
  return (
    <div className="px-6 py-8 max-w-screen-2xl mx-auto">
      <div className="space-y-1.5">
        <h1 className="text-3xl leading-tight">Health</h1>
        <p className="text-sm text-[var(--color-ink-3)] tracking-wide">
          Scraper health dashboard — Part E
        </p>
      </div>
      <section className="mt-6 p-12 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-center">
        <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
          TODO
        </p>
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">
          Last-scrape tile, snapshot density, freshness checks, fetch failures
          land in Part E.
        </p>
      </section>
    </div>
  );
}
