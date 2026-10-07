import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  COVER_OPTIONS,
  HEAD_SCORE_FLOOR,
  IMAGE_TAG_LABELS,
  coverIndex,
  imageTagLabel,
} from './imageTags';

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
  /* A photo CLIP tagged `clip` at `confidence`, optionally scored by the tag model. */
  const photo = (
    clip: string | null,
    confidence: number | null = 0.9,
    heads: Record<string, number> | null = null,
  ) => ({ clip_fine_tag: clip, clip_confidence: confidence, tag_head_scores: heads });
  /* The v1 model's eleven heads, every one a no, with `yes` overriding. */
  const v1 = (yes: Record<string, number> = {}) => ({
    ...Object.fromEntries(['3', '17', '22', '25', '28', '39', '42', '43', '45', '46', '48'].map((k) => [k, 0.05])),
    ...yes,
  });

  it('keeps the first photo for the default cover', () => {
    expect(coverIndex([photo('hallway'), photo('kitchen')], 'default')).toBe(0);
  });

  describe('where the tag model has NOT scored the photos — CLIP decides', () => {
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

    it('matches the fine tag, so a cadastral map is not a situation plan', () => {
      expect(coverIndex([photo('floor_plan'), photo('cadastral_map')], 'site_plan')).toBe(0);
      expect(coverIndex([photo('floor_plan'), photo('situation_plan')], 'site_plan')).toBe(1);
    });
  });

  describe('where the tag model HAS scored the photo — its head decides', () => {
    it('opens on the head\'s most confident yes', () => {
      const photos = [photo('hallway', 0.9, v1()), photo('hallway', 0.9, v1({ '25': 0.62 })), photo('hallway', 0.9, v1({ '25': 0.97 }))];
      expect(coverIndex(photos, 'kitchen')).toBe(2);
    });

    it('a head\'s no overrules CLIP on the same photo', () => {
      expect(coverIndex([photo('hallway', 0.9, v1()), photo('kitchen', 0.99, v1({ '25': 0.3 }))], 'kitchen')).toBe(0);
    });

    it('a head\'s yes outranks a CLIP match on a photo the model did not score', () => {
      expect(coverIndex([photo('kitchen', 0.99), photo('hallway', 0.2, v1({ '25': 0.55 }))], 'kitchen')).toBe(1);
    });

    it('a score at the floor is a yes', () => {
      expect(coverIndex([photo(null), photo(null, null, v1({ '25': HEAD_SCORE_FLOOR }))], 'kitchen')).toBe(1);
    });

    it('leaves a tag the active model has no head for to CLIP', () => {
      expect(coverIndex([photo('hallway', 0.9, v1()), photo('bedroom', 0.8, v1())], 'bedroom')).toBe(1);
    });

    it('hands that tag to the head as soon as a model with it is active', () => {
      const photos = [photo('bedroom', 0.95, { ...v1(), '26': 0.1 }), photo('hallway', 0.5, { ...v1(), '26': 0.9 })];
      expect(coverIndex(photos, 'bedroom')).toBe(1);
    });

    it('finds a head-only tag CLIP has no word for', () => {
      expect(coverIndex([photo('exterior_facade', 0.9, v1()), photo('other', 0.4, v1({ '17': 0.8 }))], 'garage')).toBe(1);
      expect(coverIndex([photo('exterior_facade'), photo('other')], 'garage')).toBe(0);
    });
  });

  it('names only CLIP tags CLIP can produce', () => {
    const fine = new Set(Object.keys(taxonomy.prompts));
    for (const o of COVER_OPTIONS) {
      if (o.clipTag != null) expect(fine.has(o.clipTag), `"${o.key}" names CLIP tag "${o.clipTag}"`).toBe(true);
    }
  });

  it('gives every option but Default a way to be found', () => {
    for (const o of COVER_OPTIONS.filter((x) => x.key !== 'default')) {
      expect(o.headTagId != null || o.clipTag != null, o.key).toBe(true);
    }
  });
});
