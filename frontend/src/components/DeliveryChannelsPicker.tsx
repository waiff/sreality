/* The single delivery-channel control, shared by watchdogs
 * (notification_subscriptions.channels) and monitored collections
 * (collections.notify_channels) — one vocabulary for "where do these alerts go"
 * on every surface (Sprint N). The producer folds the picked channels into the
 * dispatch's target_channels; the outbox delivers them.
 *
 * in-app is implicit (the Notifications feed always shows every event), so it
 * renders as a static chip — the operator only toggles the EXTERNAL channels.
 * Channels are dark until a transport key + a recipient are set; the footnote
 * points the operator to Settings → Delivery rather than failing silently. */

import { Link } from 'react-router-dom';

export const DELIVERY_CHANNELS: ReadonlyArray<{ id: string; label: string }> = [
  { id: 'email', label: 'Email' },
  { id: 'telegram', label: 'Telegram' },
];

function Dot({ on }: { on: boolean }) {
  return (
    <span
      className={[
        'w-1.5 h-1.5 rounded-full',
        on ? 'bg-[var(--color-copper)]' : 'bg-[var(--color-ink-4)]',
      ].join(' ')}
      aria-hidden
    />
  );
}

export function DeliveryChannelsPicker({
  value,
  onChange,
  disabled = false,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const selected = new Set(value);
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    // Keep a stable order (registry order) so the persisted array is tidy.
    onChange(DELIVERY_CHANNELS.filter((c) => next.has(c.id)).map((c) => c.id));
  };
  const anyExternal = DELIVERY_CHANNELS.some((c) => selected.has(c.id));

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[0.78rem] text-[var(--color-ink-3)]"
          title="Every alert always appears in the in-app Notifications feed."
        >
          <Dot on={false} />
          In-app · always
        </span>
        {DELIVERY_CHANNELS.map((c) => {
          const on = selected.has(c.id);
          return (
            <button
              key={c.id}
              type="button"
              role="switch"
              aria-checked={on}
              aria-label={c.label}
              disabled={disabled}
              onClick={() => toggle(c.id)}
              className={[
                'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-sm)] border text-[0.78rem] transition-colors disabled:opacity-50',
                on
                  ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]',
              ].join(' ')}
            >
              <Dot on={on} />
              {c.label}
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-[0.72rem] text-[var(--color-ink-4)]">
        {anyExternal ? (
          <>
            Email / Telegram also need a recipient in{' '}
            <Link
              to="/settings"
              className="underline underline-offset-2 hover:text-[var(--color-ink-2)]"
            >
              Settings → Delivery
            </Link>
            . Until then the alert still shows in-app.
          </>
        ) : (
          'In-app only. Add a channel to also be alerted by email or Telegram.'
        )}
      </p>
    </div>
  );
}
