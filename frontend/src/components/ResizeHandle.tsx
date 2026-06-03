import { useRef } from 'react';

/* A thin vertical divider the operator can drag to resize the column to
 * its left/right. It reports the raw pointer `clientX` on each move; the
 * parent owns the geometry (which container edge to measure against) and
 * the clamping, so this component stays layout-agnostic.
 *
 * Visual: transparent at rest (the neighbouring column borders read as
 * the divider), a centred copper hairline on hover / while dragging —
 * the standard "grip appears on hover" affordance. Borders-only depth,
 * oxidised-copper accent: matches the civic-archive tokens. */
interface ResizeHandleProps {
  onMove: (clientX: number) => void;
  onEnd: () => void;
  onReset?: () => void;
  ariaLabel: string;
  className?: string;
}

export default function ResizeHandle({
  onMove,
  onEnd,
  onReset,
  ariaLabel,
  className = '',
}: ResizeHandleProps) {
  const draggingRef = useRef(false);

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    draggingRef.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    /* Pin the resize cursor and kill text selection for the whole drag,
     * even when the pointer strays off the 16px hit strip. */
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    onMove(e.clientX);
  };

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* pointer already released */
    }
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    onEnd();
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={ariaLabel}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={onReset}
      title={onReset ? 'Drag to resize · double-click to reset' : 'Drag to resize'}
      className={`group relative flex shrink-0 cursor-col-resize touch-none items-stretch justify-center ${className}`}
    >
      <span
        aria-hidden
        className="w-px bg-transparent transition-colors group-hover:bg-[var(--color-copper)] group-active:bg-[var(--color-copper)]"
      />
    </div>
  );
}
