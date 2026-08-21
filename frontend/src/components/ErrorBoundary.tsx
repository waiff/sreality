import { Component, type ErrorInfo, type ReactNode } from 'react';
import { UserFacingError } from '@/lib/errors';

interface Props {
  children: ReactNode;
  /* Shown in place of the crashed subtree. Omit for the full-page default
   * (used at the app root); pass a small inline node to degrade one section
   * (e.g. a chart) without taking out the rest of the page. */
  fallback?: ReactNode;
  /* Tags the console.error so a future blank-screen is traceable to the
   * boundary that caught it. */
  label?: string;
}

interface State {
  error: Error | null;
}

/* The SPA's only render-error net. Without it, any throw during render
 * unmounts the whole React tree and the user gets a silent white screen
 * (which is exactly how the recharts #310 crash on Listing Detail surfaced). */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    const tag = this.props.label ? `[ErrorBoundary ${this.props.label}]` : '[ErrorBoundary]';
    console.error(tag, error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return this.props.fallback !== undefined ? (
        this.props.fallback
      ) : (
        <DefaultFallback error={this.state.error} />
      );
    }
    return this.props.children;
  }
}

function DefaultFallback({ error }: { error: Error }) {
  /* An anticipated failure carries copy written for a person (lib/errors.ts):
   * show that, and keep the technical text available but out of the way. A
   * plain Error has no such copy, so it keeps the generic crash wording. */
  const userFacing = error instanceof UserFacingError ? error : null;
  /* For an anticipated error the headline IS its message, so repeating it under
   * "Technical details" says nothing. The useful text there is what actually
   * failed underneath — the cause. */
  const cause = userFacing?.cause;
  const technical =
    cause instanceof Error ? `${cause.name}: ${cause.message}` : error.message;
  return (
    <div className="px-6 py-12 max-w-3xl mx-auto">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {userFacing ? 'Heads up' : 'Something broke'}
      </p>
      <h1
        className="mt-2 text-2xl"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {userFacing ? userFacing.userMessage : 'This page hit an error'}
      </h1>
      <p className="mt-3 text-sm text-[var(--color-ink-3)]">
        {userFacing
          ? userFacing.recovery
          : 'Reload to try again, or use the back button.'}
      </p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="mt-4 inline-flex items-center px-3 py-1.5 text-[0.78rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
      >
        Reload
      </button>
      {/* Kept, but folded away: the raw message is for reporting a bug, not for
        * reading. Leaving it in the open is how a TypeError ended up presented
        * to the operator as if it were the headline. */}
      <details className="mt-5">
        <summary className="cursor-pointer text-[0.72rem] text-[var(--color-ink-3)]">
          Technical details
        </summary>
        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-3 text-[0.72rem] text-[var(--color-brick)]">
          {technical}
        </pre>
      </details>
    </div>
  );
}
