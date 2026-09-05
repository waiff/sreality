/* One run, one surface.
 *
 * runSurfaceUrl decides where an estimation run "lives": a run whose subject is
 * a listing we hold lives on that listing's page (?run= selects it,
 * #estimations scrolls to it); an orphan run keeps the standalone
 * /estimation/:id surface. Several callers used to build `/estimation/${id}`
 * directly, so the SAME run resolved to two different surfaces depending on
 * which page you clicked from. These pin the contract those callers now share. */
import { describe, expect, it } from 'vitest';
import { runSurfaceUrl } from './runLinks';

describe('runSurfaceUrl', () => {
  it('sends a linked run to its listing surface', () => {
    expect(runSurfaceUrl({ id: 99, input_sreality_id: 12345 })).toBe(
      '/listing/12345?run=99#estimations',
    );
  });

  it('keeps a negative synthetic subject id intact (migration 097)', () => {
    expect(runSurfaceUrl({ id: 99, input_sreality_id: -284913 })).toBe(
      '/listing/-284913?run=99#estimations',
    );
  });

  it('falls back to the standalone surface for an orphan run', () => {
    // The default hash is '#estimations', and it is DROPPED here: the
    // standalone page has no such anchor, so keeping it would scroll nowhere.
    expect(runSurfaceUrl({ id: 99, input_sreality_id: null })).toBe('/estimation/99');
  });

  /* The standalone page renders an id="feedback" anchor but has NO
   * #estimations target, so the hash that cannot resolve is dropped rather than
   * left to scroll nowhere. Deleting this branch is a behaviour change, not a
   * cleanup. */
  it('drops #estimations on the standalone surface but keeps #feedback', () => {
    expect(runSurfaceUrl({ id: 7, input_sreality_id: null }, '#feedback')).toBe(
      '/estimation/7#feedback',
    );
    expect(runSurfaceUrl({ id: 7, input_sreality_id: null }, '#estimations')).toBe(
      '/estimation/7',
    );
  });

  it('keeps both hashes on the listing surface, which anchors both', () => {
    expect(runSurfaceUrl({ id: 7, input_sreality_id: 1 }, '#feedback')).toBe(
      '/listing/1?run=7#feedback',
    );
  });
});
