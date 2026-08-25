import { describe, expect, it } from 'vitest';
import {
  normalizePipelineStatus,
  pipelineCheckLabel,
  pipelineCheckValueLabel,
  sortPipelineChecks,
  summarizePipelineChecks,
} from './pipelineChecks';
import { fmtCount } from './format';
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
    expect(pipelineCheckLabel('llm_errors')).toBe('LLM errors');
    expect(pipelineCheckLabel('dual_write_parity')).toBe('Dual-write parity');
  });
  it('title-cases an unknown key (incl. retired ones) rather than throwing', () => {
    expect(pipelineCheckLabel('some_new_check')).toBe('Some New Check');
    expect(pipelineCheckLabel('street_debt')).toBe('Street Debt');
  });

  it('names the three per-m\u00b2 plausibility checks', () => {
    expect(pipelineCheckLabel('ppm2_basis_floor_share')).toBe('K\u010d/m\u00b2 price floor');
    expect(pipelineCheckLabel('area_vs_usable_divergence')).toBe('Area vs usable area');
  });
});

describe('pipelineCheckValueLabel', () => {
  it('renders decimals to 2 dp', () => {
    expect(pipelineCheckValueLabel(0.9583)).toBe('0.96');
    expect(pipelineCheckValueLabel(1351.76)).toBe('1351.76');
  });
  it('renders integers compactly', () => {
    expect(pipelineCheckValueLabel(35_479)).toBe(fmtCount(35_479));
  });
  it('renders an em-dash for a null value', () => {
    expect(pipelineCheckValueLabel(null)).toBe('—');
  });

  it('suffixes the unit for the per-m\u00b2 checks and leaves every other check bare', () => {
    // "20.03" reads as a count; the share it actually is has to say so.
    expect(pipelineCheckValueLabel(20.03, 'ppm2_basis_floor_share')).toBe('20.03%');
    expect(pipelineCheckValueLabel(7.455, 'ppm2_median_shift')).toBe('7.46\u00d7');
    expect(pipelineCheckValueLabel(1351.76, 'llm_burn_rate')).toBe('1351.76');
    expect(pipelineCheckValueLabel(1351.76)).toBe('1351.76');
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
