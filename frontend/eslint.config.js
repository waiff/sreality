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
        {
          // Route paths come from lib/routes.ts. A hand-typed one drifts
          // silently: rename the route and the literal falls through to
          // routes.tsx's `path: '*'`, rendering NotFound with an HTTP 200 — the
          // host serves the SPA at every depth, so there is no server 404, no
          // type error and no failing test. Scoped to POSITION (a `to`/`href`
          // JSX attribute, or navigate()'s first argument), never to content:
          // lib/api.ts and lib/brokers.ts hold ~120 byte-identical API endpoint
          // strings (`/brokers/${id}` is both an SPA route and an API path) and
          // a content-matching selector would fire on every one of them.
          //
          // This rail is ergonomics, not the gate. It cannot see a route string
          // passed through a prop — the bug that started this program was
          // `onClick={() => onOpen(id)}` with the path two levels away. Only a
          // test asserting the rendered role proves anchor-ness.
          selector:
            "JSXAttribute[name.name=/^(to|href)$/] > Literal[value=/^\\/(?!\\/)/]",
          message:
            'Build route paths from ROUTES (lib/routes.ts) instead of typing them: ROUTES.brokerDetail.build({ id }). A hand-typed path drifts into the 404 catch-all silently.',
        },
        {
          selector:
            "JSXAttribute[name.name=/^(to|href)$/] > JSXExpressionContainer > TemplateLiteral > TemplateElement[value.raw=/^\\/(?!\\/)/]",
          message:
            'Build route paths from ROUTES (lib/routes.ts) instead of interpolating them: ROUTES.brokerDetail.build({ id }). A hand-typed path drifts into the 404 catch-all silently.',
        },
        {
          selector: "CallExpression[callee.name='navigate'] > Literal:first-child[value=/^\\/(?!\\/)/]",
          message:
            'Build route paths from ROUTES (lib/routes.ts) instead of typing them into navigate().',
        },
        {
          selector:
            "CallExpression[callee.name='navigate'] > TemplateLiteral:first-child > TemplateElement[value.raw=/^\\/(?!\\/)/]",
          message:
            'Build route paths from ROUTES (lib/routes.ts) instead of interpolating them into navigate().',
        },
        {
          // A <label> wrapping a BUTTON is the "caption over a group" bug: the
          // first pill inherits the whole caption as its accessible name, the
          // rest get no association, and clicking the caption activates the
          // first pill (HTML label activation — clicking the word "Nabídka"
          // selected "Prodej"). Field as="group" (components/controls.tsx) is
          // the primitive; as="control" is the sanctioned single-child wrap and
          // carries the one per-line exemption. Position-scoped via :has so a
          // legitimate <label><input/></label> is untouched.
          selector:
            "JSXElement[openingElement.name.name='label']:has(JSXElement[openingElement.name.name='button'])",
          message:
            'A <label> must not wrap buttons — it names only the first and makes the caption click it. Use <Field label=…> (components/controls.tsx), which renders role="group".',
        },
        {
          // Three near-identical YmPicker copies existed (components/YmPicker,
          // ListingMap's PsgYmPicker, pages/Datasets' page-local); all three
          // shipped two selects with an accessible name of "". The primitive
          // lives in components/YmPicker.tsx and is exempted there per line.
          selector: "FunctionDeclaration[id.name=/YmPicker$/]",
          message:
            'Do not redefine YmPicker — import it from components/YmPicker.tsx. The shared one takes a required `label` and names both selects.',
        },
        {
          // A form control with no accessible name of its own: no aria-label,
          // no aria-labelledby, no id (for a <label htmlFor>), and not wrapped
          // in a <label> or in <Field as="control">. A placeholder is a hint,
          // not a name; a role="group" caption above it is ancestor context
          // that never becomes the control's name. Probed against the four
          // shapes before shipping: fires on a bare control and on one inside
          // a GROUP Field, silent on the two sanctioned wraps.
          //
          // Cannot see across a component boundary — a <Switch> or a widget
          // that renders its own <input> is invisible here; the widget's own
          // required `label` prop and the role+name tests are the gate.
          selector:
            "JSXOpeningElement[name.name=/^(input|textarea|select)$/]:not(:has(JSXAttribute[name.name=/^(aria-label|aria-labelledby|id)$/])):not(JSXElement[openingElement.name.name='label'] JSXOpeningElement[name.name=/^(input|textarea|select)$/]:not(:has(JSXAttribute[name.name=/^(aria-label|aria-labelledby|id)$/]))):not(JSXElement[openingElement.name.name='Field']:has(JSXAttribute[name.name='as'][value.value='control']) JSXOpeningElement[name.name=/^(input|textarea|select)$/]:not(:has(JSXAttribute[name.name=/^(aria-label|aria-labelledby|id)$/])))",
          message:
            'This control has no accessible name. Wrap exactly one control in <Field label=… as="control">, or pass the visible caption as aria-label / aria-labelledby (never a placeholder).',
        },
        {
          // The design system paints ONE focus indicator: the global
          // :focus-visible ring in globals.css. 65 sites had opted out with
          // focus:outline-none, 33 of them for a border swap that computes to
          // 1.18:1 — measurably worse than nothing. There is nothing to
          // allowlist: an element that needs a different indicator adds a
          // focus-visible: utility ON TOP of the ring, never instead of it.
          selector: "Literal[value=/focus:outline-none/]",
          message:
            'Do not opt out of the global :focus-visible ring. Add a focus-visible: utility on top of it if the element needs more, never focus:outline-none.',
        },
        {
          selector: "TemplateElement[value.raw=/focus:outline-none/]",
          message:
            'Do not opt out of the global :focus-visible ring. Add a focus-visible: utility on top of it if the element needs more, never focus:outline-none.',
        },
        {
          // Six local `function Field` copies existed before the shared one;
          // a seventh must not appear. The primitive lives in controls.tsx and
          // is exempted there per line.
          selector: "FunctionDeclaration[id.name='Field']",
          message:
            'Do not redefine Field — import it from components/controls.tsx. The shared one names groups correctly (role="group" + aria-labelledby).',
        },
      ],
    },
  },
  {
    // The files that IMPLEMENT the bans above are the only ones allowed to use
    // the banned syntax. Safe to list together: no file contains another's
    // construct. routes.ts owns every route pattern; listingUrl/runLinks/
    // browseState are the domain resolvers that build on it and are allowed to
    // compose paths; routes.tsx is the router's own table, which reads
    // ROUTES.*.childPath but still declares the structural '/' and '*'.
    //
    // THIS LIST MUST NOT GROW. It switches no-restricted-syntax OFF wholesale
    // for a file, so adding a primitive here would silently void every OTHER
    // ban (.range(), lazy, route literals) inside it. A new ban exempts its
    // own implementing primitive with a per-line `eslint-disable-next-line
    // no-restricted-syntax` at the one site that needs it, never an entry here.
    files: [
      'src/lib/fetchAllRows.ts',
      'src/lib/lazyChunk.ts',
      'src/lib/routes.ts',
      'src/lib/listingUrl.ts',
      'src/lib/runLinks.ts',
      'src/lib/browseState.ts',
      'src/routes.tsx',
    ],
    rules: { 'no-restricted-syntax': 'off' },
  },
];
