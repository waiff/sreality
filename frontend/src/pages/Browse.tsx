import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ping } from '@/lib/queries';
import { isSupabaseConfigured } from '@/lib/supabase';

export default function Browse() {
  return (
    <div className="px-6 py-8 max-w-screen-2xl mx-auto">
      <PageHeader
        title="Browse"
        subtitle="Filters, map, table, stats — Part B"
      />
      <ConnectionCard />
      <Placeholder kind="Browse" />
    </div>
  );
}

function ConnectionCard() {
  const enabled = isSupabaseConfigured();
  const { data, isLoading, error } = useQuery({
    queryKey: ['ping'],
    queryFn: ping,
    enabled,
  });

  return (
    <section className="mt-6 p-5 rounded-[var(--radius-md)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]">
      <h2 className="text-sm tracking-wide uppercase text-[var(--color-ink-3)] font-sans font-medium">
        Database connection
      </h2>
      <div className="mt-3 text-sm">
        {!enabled && (
          <p className="text-[var(--color-brick)]">
            Supabase env vars are missing. Set <Code>VITE_SUPABASE_URL</Code> and{' '}
            <Code>VITE_SUPABASE_ANON_KEY</Code>.
          </p>
        )}
        {enabled && isLoading && (
          <p className="text-[var(--color-ink-3)]">Pinging listings_public…</p>
        )}
        {enabled && error && (
          <p className="text-[var(--color-brick)]">
            Query failed: {(error as Error).message}
          </p>
        )}
        {enabled && data?.ok && (
          <p className="text-[var(--color-ink-2)]">
            Connected.{' '}
            <span className="font-mono text-[var(--color-ink)]">
              {data.count?.toLocaleString('cs-CZ') ?? '—'}
            </span>{' '}
            rows in <Code>listings_public</Code>.
          </p>
        )}
      </div>
    </section>
  );
}

function PageHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="space-y-1.5">
      <h1 className="text-3xl leading-tight">{title}</h1>
      <p className="text-sm text-[var(--color-ink-3)] tracking-wide">{subtitle}</p>
    </div>
  );
}

function Placeholder({ kind }: { kind: string }) {
  return (
    <section className="mt-6 p-12 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-center">
      <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
        TODO
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        {kind} content lands in the next checkpoint.
      </p>
    </section>
  );
}

function Code({ children }: { children: ReactNode }) {
  return (
    <code className="font-mono text-[0.85em] px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-inset)] text-[var(--color-ink)]">
      {children}
    </code>
  );
}
