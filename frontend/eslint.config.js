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
    // `.range()` on a PostgREST builder asks for a WINDOW; it does not lift the
    // server's db-max-rows clamp, and treating it as a fetch-all shipped two
    // silent-truncation bugs (see lib/fetchAllRows.ts's header). Exhaustive
    // reads go through fetchAllRows — the one file allowed to call .range().
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/lib/fetchAllRows.ts'],
    rules: {
      'no-restricted-syntax': [
        'error',
        {
          selector: "CallExpression[callee.property.name='range']",
          message:
            'Do not fetch-all with .range() — it does not lift PostgREST\'s db-max-rows clamp. Use fetchAllRows (lib/fetchAllRows.ts) for exhaustive reads, or an explicit .limit() for bounded ones.',
        },
      ],
    },
  },
];
