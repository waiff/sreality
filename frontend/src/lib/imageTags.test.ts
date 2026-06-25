import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { IMAGE_TAG_LABELS, imageTagLabel } from './imageTags';

/* The CLIP tag vocabulary is owned by the backend (data/clip_taxonomy.json for
 * the fine anchors + their collapse to the logical ROOM_TYPES). This frontend
 * label map MIRRORS it; the drift guard below fails the build if the backend
 * ever emits a tag we don't label, so the two can't silently diverge.
 * vitest runs with cwd = frontend/, so the repo-root data/ dir is one up. */
const taxonomy = JSON.parse(
  readFileSync(resolve(process.cwd(), '../data/clip_taxonomy.json'), 'utf8'),
) as { prompts: Record<string, string>; collapse: Record<string, string> };

describe('imageTagLabel', () => {
  it('labels canonical logical tags in Czech', () => {
    expect(imageTagLabel('kitchen')).toBe('kuchyně');
    expect(imageTagLabel('floor_plan')).toBe('půdorys');
    expect(imageTagLabel('site_plan')).toBe('situační plán');
  });

  it('labels the CLIP fine sub-styles distinctly from the logical collapse', () => {
    expect(imageTagLabel('aerial_plot')).toBe('letecký snímek');
    expect(imageTagLabel('cadastral_map')).toBe('katastrální mapa');
    // collapses to site_plan in the engine, but we show the finer label
    expect(imageTagLabel('aerial_plot')).not.toBe(imageTagLabel('site_plan'));
  });

  it('falls back to the raw tag for an unknown value, null for empty', () => {
    expect(imageTagLabel('mystery_tag')).toBe('mystery_tag');
    expect(imageTagLabel(null)).toBeNull();
    expect(imageTagLabel(undefined)).toBeNull();
    expect(imageTagLabel('')).toBeNull();
  });
});

describe('IMAGE_TAG_LABELS drift guard (vs data/clip_taxonomy.json)', () => {
  it('labels every CLIP fine anchor and every collapsed logical tag', () => {
    const fine = Object.keys(taxonomy.prompts);
    const logical = Object.values(taxonomy.collapse);
    for (const tag of new Set([...fine, ...logical])) {
      expect(IMAGE_TAG_LABELS[tag], `missing Czech label for backend tag "${tag}"`).toBeTruthy();
    }
  });
});
