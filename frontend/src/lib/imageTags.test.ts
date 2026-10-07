import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { COVER_TAGS, IMAGE_TAG_LABELS, coverIndex, imageTagLabel } from './imageTags';

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

describe('coverIndex', () => {
  const photo = (clip_logical_tag: string | null, clip_confidence: number | null = 0.9) => ({
    clip_logical_tag,
    clip_confidence,
  });

  it('keeps the first photo for the default cover', () => {
    expect(coverIndex([photo('hallway'), photo('kitchen')], 'default')).toBe(0);
  });

  it('opens on the photo CLIP is most sure carries the tag', () => {
    const photos = [photo('exterior_facade'), photo('kitchen', 0.41), photo('bedroom'), photo('kitchen', 0.88)];
    expect(coverIndex(photos, 'kitchen')).toBe(3);
  });

  it('takes the earlier photo on a tie', () => {
    expect(coverIndex([photo('garden'), photo('kitchen', 0.7), photo('kitchen', 0.7)], 'kitchen')).toBe(1);
  });

  it('falls back to the first photo when the ad has no photo with the tag', () => {
    expect(coverIndex([photo('hallway'), photo('bedroom'), photo(null, null)], 'kitchen')).toBe(0);
    expect(coverIndex([], 'kitchen')).toBe(0);
  });

  it('matches the LOGICAL tag, so a cadastral map counts as a site plan', () => {
    expect(coverIndex([photo('floor_plan'), photo('site_plan', 0.6)], 'site_plan')).toBe(1);
  });

  it('offers only tags CLIP can produce as a logical tag', () => {
    const logical = new Set(Object.values(taxonomy.collapse));
    for (const tag of COVER_TAGS.filter((t) => t !== 'default')) {
      expect(logical.has(tag), `cover tag "${tag}" is not a CLIP logical tag`).toBe(true);
    }
  });
});
