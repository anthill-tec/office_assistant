# Project memory — office_assistant (Vidushi OA)

Durable, **project-specific** operating notes for agents working on **this repo** (the parent project
that *builds* Vidushi OA). Kept **in-repo and git-tracked** — not in any harness's private store
(`~/.claude/`, `~/.codex/`, …) — so they are portable across harnesses and reviewable in history.
Referenced from [`AGENTS.md`](../../AGENTS.md) (which `CLAUDE.md` symlinks to).

> These notes are about **developing this project**. They are **not** part of the shipped
> `skills/vidushi-oa/` bundle — that skill is harness-agnostic and carries no project memory.

Each file is one note with YAML frontmatter (`name`, `description`, `metadata.type` ∈
`user | feedback | project | reference`). Link between notes with `[[name]]`.

## Index

- [cicd-release-convention](cicd-release-convention.md) — **1.0.0 SHIPPED**: automated PyPI publish from
  `main` (git-flow + **hatch-vcs**, version from the tag), TestPyPI via **`workflow_dispatch`**; skill ships
  as a **public GitHub repo** (`npx skills add anthill-tec/office_assistant/skills/vidushi-oa`); no
  manual-approval gate; GPL-3.0; no-mistakes + AXI on the release branch; `act` + `ci-monitor`.
- [vercel-skills-bundle-packaging](vercel-skills-bundle-packaging.md) — **RESOLVED**: engine → PyPI, skill →
  public GitHub repo (`npx skills`); the ecosystem doesn't bundle a pip package into a skill.
- [mongo-preexisting-data-migration](mongo-preexisting-data-migration.md) — this machine's OA keeps its store on
  Mongo `vidushi_oa`@27017 (no forced SQLite migration); set `VIDUSHI_BACKEND=mongo` for CLI + SessionStart hook.
- [external-data-sources-decisions](external-data-sources-decisions.md) — no consumer marketplace API exists;
  **schema.org email extraction approved** (CR deferred) + **carrier-tracking aggregator** an opt-in option
  (inclusion **decision deferred**). See `DN-external-data-sources.md`.
