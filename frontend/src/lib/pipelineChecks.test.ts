import { describe, expect, it } from 'vitest';
import {
  normalizePipelineStatus,
  pipelineCheckLabel,
  pipelineCheckValueLabel,
  sortPipelineChecks,
  summarizePipelineChecks,
} from './pipelineChecks';
import type { PipelineCheckRow } from './queries';

function check(
  check_key: string,
  status: string,
  value: number | null = null,
): PipelineCheckRow {
  return { check_key, status, value, details: null, run_at: '2026-07-05T13:34:00Z' };
}

describe('pipelineCheckLabel', () => {
  it('maps known keys to a humanized label', () => {
    expect(pipelineCheckLabel('street_debt')).toBe('Street debt');
    expect(pipelineCheckLabel('merge_precision_sample')).toBe('Merge precision (sample)');
  });
  it('title-cases an unknown key rather than throwing', () => {
    expect(pipelineCheckLabel('some_new_check')).toBe('Some New Check');
  });
});

describe('pipelineCheckValueLabel', () => {
  it('labels street/geo debt as suspect-pair counts', () => {
    expect(pipelineCheckValueLabel('street_debt', 35_479)).toMatch(/pairs$/);
    expect(pipelineCheckValueLabel('geo_debt', 98_830)).toMatch(/pairs$/);
  });
  it('renders decimals to 2 dp for ratio/percent checks', () => {
    expect(pipelineCheckValueLabel('llm_errors', 0.9583)).toBe('0.96');
    expect(pipelineCheckValueLabel('merge_latency', 1351.76)).toBe('1351.76');
  });
  it('renders an em-dash for a null value (e.g. engine_health)', () => {
    expect(pipelineCheckValueLabel('engine_health', null)).toBe('—');
  });
});

describe('normalizePipelineStatus', () => {
  it('passes warn/fail through and folds everything else to ok', () => {
    expect(normalizePipelineStatus('fail')).toBe('fail');
    expect(normalizePipelineStatus('warn')).toBe('warn');
    expect(normalizePipelineStatus('ok')).toBe('ok');
    expect(normalizePipelineStatus('mystery')).toBe('ok');
  });
});

describe('summarizePipelineChecks', () => {
  it('counts each band and reports the worst', () => {
    const s = summarizePipelineChecks([
      check('a', 'ok'),
      check('b', 'warn'),
      check('c', 'fail'),
      check('d', 'ok'),
    ]);
    expect(s).toEqual({ ok: 2, warn: 1, fail: 1, worst: 'fail' });
  });
  it('worst is warn when there are no fails', () => {
    expect(summarizePipelineChecks([check('a', 'ok'), check('b', 'warn')]).worst).toBe('warn');
  });
  it('worst is ok on an empty set', () => {
    expect(summarizePipelineChecks([]).worst).toBe('ok');
  });
});

describe('sortPipelineChecks', () => {
  it('orders fail, then warn, then ok, stable by key', () => {
    const sorted = sortPipelineChecks([
      check('zeta', 'ok'),
      check('alpha', 'ok'),
      check('beta', 'fail'),
      check('gamma', 'warn'),
    ]);
    expect(sorted.map((c) => c.check_key)).toEqual(['beta', 'gamma', 'alpha', 'zeta']);
  });
});
