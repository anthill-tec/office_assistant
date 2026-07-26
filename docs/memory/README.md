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

- [cicd-release-convention](cicd-release-convention.md) — automated production PyPI release from master
  (git-flow), TestPyPI on `release/*`; **no** manual-approval gate; GPL-3.0 license rationale; the
  `release/*` branch also runs no-mistakes + AXI validation; gate-script-vendoring + `act` + `ci-monitor`.
- [vercel-skills-bundle-packaging](vercel-skills-bundle-packaging.md) — post-merge follow-up: bundle the
  skill + its scripts/engine as one deployable vercel/skills package (flagged during CR-016).
