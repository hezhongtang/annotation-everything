# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Layout

This is a single-context repo. Domain terms live in the root `GLOSSARY.md`; architecture decisions live in `docs/adr/`.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If any of these files don't exist, proceed silently. The `/domain-modeling` skill creates them lazily when terms or decisions actually get resolved.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, or a test name), use the term as defined in `GLOSSARY.md`. Avoid synonyms the glossary explicitly excludes.

If the concept you need isn't in the glossary yet, reconsider whether it belongs to the project or note the gap for `/domain-modeling`.

## Flag ADR conflicts

If your output contradicts an existing ADR, surface the conflict and explain why the decision may need to be reopened.
