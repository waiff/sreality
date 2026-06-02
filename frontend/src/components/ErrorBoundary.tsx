import { Component, type ErrorInfo, type ReactNode } from 'react';

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
  return (
    <div className="px-6 py-12 max-w-3xl mx-auto">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Something broke
      </p>
      <h1
        className="mt-2 text-2xl"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        This page hit an error
      </h1>
      <p className="mt-3 text-sm text-[var(--color-ink-3)]">
        The rest of the app is fine — reload to try again, or use the back button.
      </p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="mt-4 inline-flex items-center px-3 py-1.5 text-[0.78rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
      >
        Reload
      </button>
      <pre className="mt-5 overflow-x-auto whitespace-pre-wrap rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-3 text-[0.72rem] text-[var(--color-brick)]">
        {error.message}
      </pre>
    </div>
  );
}
