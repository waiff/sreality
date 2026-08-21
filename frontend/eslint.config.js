import reactHooks from 'eslint-plugin-react-hooks';
import tseslint from 'typescript-eslint';

// Minimal, surgical lint: ONLY the Rules of Hooks (a runtime React contract
// that tsc can't see). A hook after an early return — exactly the bug that
// white-screened Listing Detail — is `rules-of-hooks` = error. exhaustive-deps
// is a non-blocking warning. Not a full style lint; deliberately narrow.
export default [
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: { parser: tseslint.parser },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },
  {
    // ONE `no-restricted-syntax` block on purpose. ESLint flat config REPLACES
    // a rule's options when a later block re-declares it, so two blocks both
    // matching `src/**` would silently leave only the last one's selectors
    // active. Every codebase-wide syntax ban belongs in this list.
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': [
        'error',
        {
          // `.range()` on a PostgREST builder asks for a WINDOW; it does not
          // lift the server's db-max-rows clamp, and treating it as a fetch-all
          // shipped two silent-truncation bugs (see lib/fetchAllRows.ts's
          // header). Exhaustive reads go through fetchAllRows.
          selector: "CallExpression[callee.property.name='range']",
          message:
            'Do not fetch-all with .range() — it does not lift PostgREST\'s db-max-rows clamp. Use fetchAllRows (lib/fetchAllRows.ts) for exhaustive reads, or an explicit .limit() for bounded ones.',
        },
        {
          // Code-splitting goes through lazyChunk (lib/lazyChunk.ts), never
          // React's bare `lazy`. After a deploy a stale chunk 404s; the page has
          // to reload WITHOUT letting React observe the failed import, or the
          // user gets a full-page crash screen in front of a reload that was
          // already going to fix it. That was the 2026-08-19 incident.
          selector: "CallExpression[callee.name='lazy']",
          message:
            'Use lazyChunk (lib/lazyChunk.ts) instead of React.lazy so a stale chunk after a deploy self-heals instead of painting an error page.',
        },
        {
          selector:
            "CallExpression[callee.object.name='React'][callee.property.name='lazy']",
          message:
            'Use lazyChunk (lib/lazyChunk.ts) instead of React.lazy so a stale chunk after a deploy self-heals instead of painting an error page.',
        },
      ],
    },
  },
  {
    // The two files that IMPLEMENT the bans above are the only ones allowed to
    // use the banned syntax. Listing them together is safe: neither file
    // contains the other's construct.
    files: ['src/lib/fetchAllRows.ts', 'src/lib/lazyChunk.ts'],
    rules: { 'no-restricted-syntax': 'off' },
  },
];
