/* The explore-broker modal's contract with <BrowseExperience>, pinned without
 * rendering the real experience (that fires the map/cards/count queries and
 * tests never render a live map — see vitest.setup.ts). What matters here:
 *
 *   - the cohort it opens is the broker scope (brokerId) seeded from the page's
 *     Typ/Nabídka selection, with NO viewport constraint;
 *   - the sidebar AND the Stats tab are switched off. Stats is property-grain
 *     and deliberately not scoped by a broker, so showing it here would put the
 *     whole market's numbers beside one broker's map — hiding it is the whole
 *     point of the `stats` feature flag, and a regression here reads as a bug;
 *   - and, since it moved onto <Dialog>, the shared dialog contract plus the
 *     close-on-navigation property its provider now carries.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { expectDialogContract } from '@/test/a11y';

import { ExploreBrokerProvider, useExploreBrokerModal } from './ExploreBrokerModal';
import type { BrowseFeatures } from './BrowseExperience';
import type { BrowseViewState } from '@/lib/browseState';

const { experienceProps } = vi.hoisted(() => ({
  experienceProps: [] as Array<{ view: BrowseViewState; layout?: string; features?: BrowseFeatures }>,
}));

vi.mock('./BrowseExperience', () => ({
  default: (props: { view: BrowseViewState; layout?: string; features?: BrowseFeatures }) => {
    experienceProps.push(props);
    return <div data-testid="browse-experience" />;
  },
}));

function Trigger() {
  const { open } = useExploreBrokerModal();
  return (
    <button
      type="button"
      onClick={() =>
        open({ brokerId: 527, brokerName: 'Jan Novák', categoryMain: 'dum', categoryType: 'prodej' })
      }
    >
      go
    </button>
  );
}

function renderHost() {
  experienceProps.length = 0;
  return render(
    <MemoryRouter>
      <ExploreBrokerProvider>
        <Trigger />
      </ExploreBrokerProvider>
    </MemoryRouter>,
  );
}

describe('<ExploreBrokerModal>', () => {
  it('opens the Browse experience scoped to the broker, sidebar and Stats off', () => {
    renderHost();
    fireEvent.click(screen.getByText('go'));

    expect(screen.getByRole('dialog', { name: 'Explore broker' })).toBeInTheDocument();
    expect(screen.getByText('Jan Novák')).toBeInTheDocument();
    expect(screen.getByTestId('browse-experience')).toBeInTheDocument();

    const props = experienceProps.at(-1)!;
    expect(props.layout).toBe('modal');
    expect(props.features).toMatchObject({ sidebar: false, stats: false, presetBar: false, watchdog: false, mergeMode: false, title: false });

    const { filters } = props.view;
    expect(filters.brokerId).toBe(527);
    expect(filters.categoryMain).toEqual(['dum']);
    expect(filters.categoryType).toBe('prodej');
    expect(filters.bounds).toBeNull();
  });

  it('carries the broker scope into the "Go to Browse" link', () => {
    renderHost();
    fireEvent.click(screen.getByText('go'));
    const link = screen.getByRole('link', { name: /Go to Browse/ });
    const href = link.getAttribute('href') ?? '';
    expect(href.startsWith('/browse?')).toBe(true);
    expect(new URLSearchParams(href.slice('/browse?'.length)).get('broker')).toBe('527');
  });

  it('closes on the close button and on Escape', () => {
    renderHost();
    fireEvent.click(screen.getByText('go'));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('go'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('honours the shared dialog contract', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'go' });
    expectDialogContract({ trigger, open: () => fireEvent.click(trigger) });
  });

  it('names the panel, not the backdrop', () => {
    renderHost();
    fireEvent.click(screen.getByText('go'));
    const panel = screen.getByRole('dialog', { name: 'Explore broker' });
    // It used to be the viewport-sized overlay that claimed the role; the
    // panel was an anonymous div inside it.
    expect(panel.parentElement!.getAttribute('role')).toBe('presentation');
    expect(panel.className).toContain('h-[90vh]');
  });

  it('leaves no scroll lock behind when an in-modal link navigates away', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'go' });
    trigger.focus();
    fireEvent.click(trigger);
    expect(document.body.style.overflow).toBe('hidden');

    /* The provider mounts above <Outlet> in Shell: without
     * useCloseOnNavigation this navigation repainted the page BEHIND the open
     * modal and stranded the lock. The link carries no close handler — the
     * pathname change is what closes it. */
    fireEvent.click(screen.getByRole('link', { name: /Go to Browse/ }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe('');
  });

  it('leaves the modal open on a modifier click, which never navigates', () => {
    renderHost();
    fireEvent.click(screen.getByText('go'));
    fireEvent.click(screen.getByRole('link', { name: /Go to Browse/ }), { ctrlKey: true });
    expect(screen.getByRole('dialog', { name: 'Explore broker' })).toBeInTheDocument();
  });
});
