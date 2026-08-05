import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import RowGrainNotice from './RowGrainNotice';
import type { GrainNoticeState, GrainVariant } from '@/lib/browseLayout';

const notice = (
  dismissedSet: ReadonlySet<GrainVariant> = new Set(),
  dismiss: (v: GrainVariant) => void = () => {},
): GrainNoticeState => ({
  dismissed: (v) => dismissedSet.has(v),
  dismiss,
});

describe('RowGrainNotice', () => {
  it('explains LISTING grain when exactly one portal is selected', () => {
    render(<RowGrainNotice portalMirror="bazos" notice={notice()} />);
    const note = screen.getByRole('note');
    expect(note.textContent).toContain('Mirroring bazos');
    expect(note.textContent).toContain('appears twice');
    expect(note.textContent).not.toContain('One row per property');
  });

  /* Two portals selected reads the property-grain model, exactly like no portal
   * at all — so both must get the merged-record copy, not just the default
   * view. `portalMirror` is already null in both cases (queries.ts:
   * portalMirrorSource returns non-null only at length === 1), which is why
   * this component keys off it rather than the portal array. */
  it('explains PROPERTY grain when no single portal is selected', () => {
    render(<RowGrainNotice portalMirror={null} notice={notice()} />);
    const note = screen.getByRole('note');
    expect(note.textContent).toContain('One row per property');
    expect(note.textContent).toContain('merged record');
    expect(note.textContent).not.toContain('Mirroring');
  });

  it('renders nothing once that variant is dismissed', () => {
    const { container } = render(
      <RowGrainNotice portalMirror={null} notice={notice(new Set(['merged']))} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  /* A variant dismissed elsewhere must not suppress the OTHER one: the two say
   * opposite things about what a row is. */
  it('still shows the mirror note when only the merged note was dismissed', () => {
    render(
      <RowGrainNotice portalMirror="idnes" notice={notice(new Set(['merged']))} />,
    );
    expect(screen.getByRole('note').textContent).toContain('Mirroring idnes');
  });

  it('dismisses the variant that is actually on screen', async () => {
    const dismiss = vi.fn();
    render(<RowGrainNotice portalMirror="remax" notice={notice(new Set(), dismiss)} />);
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(dismiss).toHaveBeenCalledWith('mirror');
  });
});
