/* The deal pipeline's cache patches — pure, so pinned without a QueryClient.
 * What they must keep true: a write paints BOTH caches every funnel and the
 * board read (rule #22), and a new card never reaches the board half-built. */

import { describe, expect, it } from 'vitest';

import { dropCard, placeCard } from './pipelineCache';
import { pipelineKeys, type PipelineMembers } from './queries';
import type { PipelineBoardCard, PipelineStage } from './types';

const STAGE: PipelineStage = {
  id: 2, key: 'call', label: '2. For Call', position: 2, color: 'ochre',
  is_terminal: false, is_entry: false, code: '2',
};

const card = (property_id: number, stage_id: number) =>
  ({ property_id, stage_id }) as PipelineBoardCard;

const members = (): PipelineMembers =>
  new Map([[42, {
    property_id: 42, stage_id: 1, stage_label: '1. For Review', stage_color: 'copper',
    stage_code: '1', stage_position: 1, is_terminal: false,
  }]]);

describe('placeCard', () => {
  it('patches exactly the members map and the board', () => {
    expect(placeCard(42, STAGE).map((p) => p.filters.queryKey)).toEqual([
      pipelineKeys.members,
      pipelineKeys.board,
    ]);
  });

  it('sets the members entry from the stage', () => {
    const [toMembers] = placeCard(7, STAGE);
    const next = toMembers.update(members()) as PipelineMembers;
    expect(next.get(7)).toEqual({
      property_id: 7, stage_id: 2, stage_label: '2. For Call', stage_color: 'ochre',
      stage_code: '2', stage_position: 2, is_terminal: false,
    });
    expect(next.get(42)?.stage_id).toBe(1);
  });

  it("moves a card already on the board, and never adds one it can't build", () => {
    const [, toBoard] = placeCard(42, STAGE);
    expect(toBoard.update([card(42, 1), card(43, 1)])).toEqual([card(42, 2), card(43, 1)]);
    const [, toBoard7] = placeCard(7, STAGE);
    expect(toBoard7.update([card(42, 1)])).toEqual([card(42, 1)]);
  });

  it('leaves an unloaded cache unloaded', () => {
    for (const p of placeCard(42, STAGE)) expect(p.update(undefined)).toBeUndefined();
  });
});

describe('dropCard', () => {
  it('removes the property from both caches', () => {
    const [toMembers, toBoard] = dropCard(42);
    expect((toMembers.update(members()) as PipelineMembers).has(42)).toBe(false);
    expect(toBoard.update([card(42, 1), card(43, 1)])).toEqual([card(43, 1)]);
    expect(dropCard(42).map((p) => p.filters.queryKey)).toEqual([
      pipelineKeys.members,
      pipelineKeys.board,
    ]);
  });
});
