import { useParams } from 'react-router-dom';

export default function ListingDetail() {
  const { sreality_id } = useParams();
  return (
    <div className="px-6 py-8 max-w-screen-2xl mx-auto">
      <div className="space-y-1.5">
        <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Listing
        </p>
        <h1 className="text-3xl leading-tight font-mono">
          {sreality_id ?? '—'}
        </h1>
        <p className="text-sm text-[var(--color-ink-3)]">
          Detail page with snapshot timeline strip — Part C
        </p>
      </div>
      <section className="mt-6 p-12 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-center">
        <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
          TODO
        </p>
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">
          Detail view, snapshot history, freshness log land in Part C.
        </p>
      </section>
    </div>
  );
}
