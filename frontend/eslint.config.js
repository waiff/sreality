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
];
