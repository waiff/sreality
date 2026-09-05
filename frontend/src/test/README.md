# Interactive-element assertions

The idiom every component test uses for the interactive-semantics program. It
extends what the suite already does (24 of 44 test files query by role) rather
than introducing a new style.

**Query by role and name, never by text or class.**
`getByRole('button', { name: 'Domy' })` fails the moment the control stops being
a button or loses its name — which is exactly what the program guards.

**Assert names with `toHaveAccessibleName`.** It is the same algorithm a screen
reader runs (dom-accessibility-api, wired via jest-dom in `vitest.setup.ts`).
When the defect is the *name* — a caption stealing a pill's label, a card link
announcing every control inside it — assert the name, not the markup.

**Assert structure only where nesting is the defect.**
`expectNoNestedInteractive(container)` from `./a11y` — hand-written because
axe-core's `nested-interactive` rule cannot see an `<a>` wrapping controls (see
the header of `a11y.ts` for the measurement).

**Assert keyboard contracts with `expectRovingGroup`.** A `role="menu"` that
does not move focus on ArrowDown is a lie to assistive technology; the helper
makes the lie fail.

**Assert the NEGATIVE where an HTML behaviour is the bug.** A `<label>` wrapping
a group of pills makes a click on the caption activate the first pill. The rail
for that is a test that clicks the caption and asserts *nothing changed*.

**What jsdom cannot prove, say so.** No CSS is loaded: `:focus-visible`,
contrast, and Tailwind-hidden panels are all invisible. Focus *management*
(`document.activeElement`) is testable; focus *visibility* is a browser check.
