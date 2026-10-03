# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.
- **The numbered Architectural rules in `CLAUDE.md`**, with their rationale in `docs/architecture.md` § Architectural rules, and the program plans under `docs/design/`. This repo recorded its standing decisions there before `docs/adr/` existed, and they bind exactly as ADRs do.

If `GLOSSARY.md` or `docs/adr/` doesn't exist, **proceed silently**. Don't flag the absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

This is a single-context repo:

```
/
├── CLAUDE.md                 ← the numbered architectural rules
├── GLOSSARY.md
└── docs/
    ├── adr/
    │   └── 0001-<decision>.md
    ├── architecture.md       ← rationale behind each numbered rule
    └── design/               ← program plans and their decision ledgers
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR or a numbered rule, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
>
> _Contradicts rule #15 (merges go through the one chokepoint), but worth reopening because…_
