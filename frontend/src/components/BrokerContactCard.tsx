import { useState } from 'react';
import {
  contactState,
  prettyPhone,
  type BrokerContactFields,
  type ContactState,
} from '@/lib/brokers';

/* The broker's two reachable channels — one card, shared by /brokers/:id and the
   ListingDetail vizitka so the same broker reads identically on both surfaces.
   Typed on BrokerContactFields, not BrokerPublic: the masked pair is all the card
   consumes, and which half arrives (primary_* for an admin, has_* otherwise) is a
   property of the CALLER, not of the row. */
export default function BrokerContactCard({
  broker,
}: {
  broker: BrokerContactFields;
}) {
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-3)] px-4 py-3 min-w-[15rem]">
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-3)]">
        Kontakt pro oslovení
      </p>
      <div className="mt-2 space-y-1.5">
        <ContactRow
          kind="tel"
          state={contactState(broker.primary_phone, broker.has_phone)}
          format={prettyPhone}
        />
        <ContactRow
          kind="mailto"
          state={contactState(broker.primary_email, broker.has_email)}
          format={(v) => v}
        />
      </div>
    </div>
  );
}

const CONTACT_LABEL: Record<'tel' | 'mailto', string> = {
  tel: 'telefon',
  mailto: 'e-mail',
};

function ContactRow({
  kind,
  state,
  format,
}: {
  kind: 'tel' | 'mailto';
  state: ContactState;
  format: (v: string) => string;
}) {
  const [copied, setCopied] = useState(false);
  if (state.state !== 'value') {
    // "masked" means a contact IS on file but this session may not see it —
    // rendering it as the empty dash would claim the broker has none.
    const masked = state.state === 'masked';
    return (
      <p
        className="text-sm text-[var(--color-ink-4)] font-[family-name:var(--font-mono)]"
        title={masked ? 'Kontakt je viditelný jen pro administrátory.' : undefined}
      >
        {CONTACT_LABEL[kind]} {masked ? '· kontakt na vyžádání' : '—'}
      </p>
    );
  }
  const { value } = state;
  const copy = () => {
    // writeText rejects on a denied permission or a non-secure context (any
    // non-HTTPS host, e.g. a LAN preview) — without a catch that's an unhandled
    // rejection and a button that silently does nothing.
    navigator.clipboard
      ?.writeText(value)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => {});
  };
  return (
    <div className="flex items-center justify-between gap-3">
      <a
        href={`${kind}:${value}`}
        className="text-sm font-[family-name:var(--font-mono)] text-[var(--color-copper-2)] hover:underline underline-offset-2 truncate"
      >
        {format(value)}
      </a>
      <button
        type="button"
        onClick={copy}
        aria-label={`kopírovat ${CONTACT_LABEL[kind]}`}
        className="shrink-0 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)] hover:text-[var(--color-ink)] transition-colors"
      >
        {copied ? 'zkopírováno' : 'kopírovat'}
      </button>
    </div>
  );
}
