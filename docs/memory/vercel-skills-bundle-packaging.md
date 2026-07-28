---
name: vercel-skills-bundle-packaging
description: Post-merge follow-up — package the vidushi-oa skill + its scripts as a deployable vercel/skills bundle
metadata: 
  node_type: memory
  type: project
  originSessionId: 99afe2dc-d6bb-44f2-8156-92ea1d4df250
  modified: 2026-07-25T16:04:41.880Z
---

**RESOLVED by the 1.0.0 ship (2026-07-27):** the Vercel Agent Skills ecosystem doesn't bundle a pip
package into a skill — they are two standard channels, which is exactly what shipped: the **engine**
installs from **PyPI** (`uv tool install vidushi-oa`) and the **skill** installs from the **public GitHub
repo** (`npx skills add anthill-tec/office_assistant/skills/vidushi-oa`, auto-discovered — no submission).
`npx skills` installs `SKILL.md` markdown (+ an optional `scripts/` subdir), not Python wheels, so a single
skill+engine deployable isn't how the ecosystem works. See [[cicd-release-convention]]. Original note kept
for context:

The user wanted to package the `skills/vidushi-oa/` skill **together with its associated
scripts/engine** into a single deployable **vercel/skills** bundle. Flagged 2026-07-25 (during
CR-OA-016) as a real requirement that the current CRs may **not** have fully captured.

**Why:** CR-OA-016 made the skill install-ready and documented two install paths (local `npx skills
add ./skills/vidushi-oa` + `pip install -e .`; public `uv tool install vidushi-oa` + `npx skills add
anthill-tec/office_assistant/skills/vidushi-oa`), but treats the skill (npx/skills) and the
`voa` engine (pip) as **two separate installs**. The user's goal is a **single bundle** carrying
both the skill and the scripts.

**How to apply:** Discuss scope **after CR-OA-016 merged** (it is merged to `develop`). Likely a new
CR. Open question: how the flat-layout vercel/skills packaging (which travels `SKILL.md` +
`references/`) should also carry the Python engine/scripts — the vercel `skills` CLI installs skill
markdown, not pip packages. Note `skills add` supports a `scripts/` subdir inside a skill bundle.
Validation tool is real: `agentskills validate` via `pip install skills-ref` (Python, not npm),
already wrapped by the repo's release gate (`.skill-release.toml` + `~/.claude/scripts/skill-release-gate.py`).
