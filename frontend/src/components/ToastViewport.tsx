/* The single app-wide toast surface. Mounted once in Shell; renders whatever
 * lib/toast.ts holds. Palette uses existing civic-archive tokens only. */

import { useToasts, dismissToast, type ToastKind } from '@/lib/toast';

const TONE: Record<ToastKind, string> = {
  err: 'border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-[var(--color-brick)]',
  ok: 'border-[var(--color-sage)]/30 bg-[var(--color-sage)]/10 text-[var(--color-sage)]',
  info: 'border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)] text-[var(--color-copper)]',
};

export default function ToastViewport() {
  const toasts = useToasts();
  if (toasts.length === 0) return null;
  return (
    <div
      className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm"
      role="status"
      aria-live="polite"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`flex items-start gap-3 p-3 rounded-[var(--radius-sm)] border text-sm shadow-sm ${TONE[t.kind]}`}
        >
          <span className="flex-1 break-words">{t.message}</span>
          <button
            type="button"
            onClick={() => dismissToast(t.id)}
            className="shrink-0 leading-none opacity-60 hover:opacity-100"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
