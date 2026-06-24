import { useEffect, useState } from 'react';

/* Resolve civic-archive design tokens (CSS custom properties) to concrete colours for
 * recharts — SVG stroke/fill presentation attributes don't resolve `var()` — re-reading
 * on a light/dark switch (explicit `data-theme` or system `prefers-color-scheme`). Shared
 * by every chart so the chart palette stays unified with the tokens. */
function readColors(keys: readonly string[]): Record<string, string> {
  if (typeof window === 'undefined') return {};
  const cs = getComputedStyle(document.documentElement);
  const out: Record<string, string> = {};
  for (const k of keys) out[k] = cs.getPropertyValue(k).trim();
  return out;
}

export function useTokenColors(keys: readonly string[]): Record<string, string> {
  const dep = keys.join(',');
  const [colors, setColors] = useState(() => readColors(keys));
  useEffect(() => {
    const update = () => setColors(readColors(keys));
    update();
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', update);
    return () => {
      obs.disconnect();
      mq.removeEventListener('change', update);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dep]);
  return colors;
}
