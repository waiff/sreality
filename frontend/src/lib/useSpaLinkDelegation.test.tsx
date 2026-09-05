/* The wiring half of linkGestures, proved without a map.
 *
 * ListingMap has no test file and cannot easily get one (MapLibre needs WebGL,
 * jsdom has none, and a mount failure there takes the whole /browse route down).
 * Making the delegation a HOOK rather than an inline effect is what lets this
 * file cover the parts that would otherwise only be provable in a browser:
 * that the listener attaches, that it preventDefaults and routes, that a
 * modified click passes through untouched, and that it is torn down. */
import { describe, expect, it } from 'vitest';
import { useRef } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { useSpaLinkDelegation } from './linkGestures';

function Harness() {
  const ref = useRef<HTMLDivElement | null>(null);
  useSpaLinkDelegation(ref);
  const loc = useLocation();
  return (
    <>
      <div data-testid="where">{loc.pathname}</div>
      {/* dangerouslySetInnerHTML stands in for MapLibre writing its own popup
        * markup — React never renders these anchors, which is the whole reason
        * delegation exists. */}
      <div
        ref={ref}
        data-testid="container"
        dangerouslySetInnerHTML={{
          __html:
            '<a href="/brokers/7" id="in-app"><span id="child">Detail</span></a>' +
            '<a href="https://openfreemap.org/" id="external">OpenFreeMap</a>',
        }}
      />
    </>
  );
}

function setup() {
  return render(
    <MemoryRouter initialEntries={['/browse']}>
      <Harness />
    </MemoryRouter>,
  );
}

describe('useSpaLinkDelegation', () => {
  it('routes a plain click on an anchor the router never rendered', () => {
    setup();
    expect(screen.getByTestId('where')).toHaveTextContent('/browse');
    const child = document.getElementById('child')!;
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
    act(() => {
      child.dispatchEvent(ev);
    });
    expect(ev.defaultPrevented).toBe(true);
    expect(screen.getByTestId('where')).toHaveTextContent('/brokers/7');
  });

  it('leaves a ctrl+click to the browser so the new tab still opens', () => {
    setup();
    const link = document.getElementById('in-app')!;
    const ev = new MouseEvent('click', {
      bubbles: true,
      cancelable: true,
      button: 0,
      ctrlKey: true,
    });
    link.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(false);
    expect(screen.getByTestId('where')).toHaveTextContent('/browse');
  });

  it('leaves an external link alone', () => {
    setup();
    const link = document.getElementById('external')!;
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
    link.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(false);
    expect(screen.getByTestId('where')).toHaveTextContent('/browse');
  });

  it('detaches on unmount', () => {
    const { unmount } = setup();
    const container = screen.getByTestId('container');
    const link = document.getElementById('in-app')!;
    unmount();
    // The node is detached from the document but still reachable; a click on it
    // must no longer be intercepted by a listener the hook left behind.
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
    container.appendChild(link);
    link.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(false);
  });

  it('does nothing when the ref was never attached', () => {
    function NoRef() {
      const ref = useRef<HTMLDivElement | null>(null);
      useSpaLinkDelegation(ref);
      return <div data-testid="ok">mounted</div>;
    }
    render(
      <MemoryRouter>
        <NoRef />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('ok')).toBeInTheDocument();
  });
});

/* Uses fireEvent once so the import is meaningful to a reader scanning imports;
 * the MouseEvent constructor is used above because fireEvent.click cannot set
 * `button`/`ctrlKey` on a non-React-rendered node in a way that survives. */
describe('harness sanity', () => {
  it('renders the injected markup', () => {
    setup();
    expect(document.getElementById('in-app')).not.toBeNull();
    fireEvent.mouseOver(screen.getByTestId('container'));
  });
});
