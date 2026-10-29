# skill-inspector (vendored)

`SKILL.md` and `LICENSE` in this directory are copied unmodified from
[NVIDIA/SkillSpector](https://github.com/NVIDIA/SkillSpector) under the Apache License 2.0.

| Field | Value |
|---|---|
| Upstream path | `skills/skill-inspector/SKILL.md` |
| Upstream commit | `b5987a332c9a118ec4714a5c79eb76641b546251` |
| SKILL.md sha256 | `1acc220016936ba68f7b9083c19575a8866c9cf74c251d689285a0d9fbe2a067` |
| SkillSpector version | 2.12.0 |

Verify the vendored copy has not drifted from upstream:

```bash
curl -sL https://raw.githubusercontent.com/NVIDIA/SkillSpector/b5987a332c9a118ec4714a5c79eb76641b546251/skills/skill-inspector/SKILL.md | sha256sum
sha256sum .claude/skills/skill-inspector/SKILL.md
```

## How it is wired into this repo

| Layer | What runs | Depends on an AI model |
|---|---|---|
| CI gate | `.github/workflows/skill-inspector.yml` runs `scripts/skill_inspector_scan.py` on every PR and push to `main` | No. Static analysis only, `--no-llm` |
| History audit | Same workflow, weekly and on demand, scans every version of every agent asset ever committed | No |
| Local | `make skill-scan` and `make skill-scan-history` | No |
| Agent review | This skill, for a semantic second opinion on a specific finding | Yes |

The CI gate is the source of truth. The skill is an optional second line of review and never
overrides a failing scanner result.

## What gets scanned

SkillSpector requires a `SKILL.md`. The runner scans real skills as-is and stages everything
else that steers an agent with a neutral stub `SKILL.md` so the same analyzers apply:

- every directory containing `SKILL.md`
- every `.claude/` directory: hooks, commands, subagents, settings, output styles
- every `CLAUDE.md` and `AGENTS.md`
- every `.mcp.json` and `mcp.json`

Findings on a staged target's `SKILL.md:1` with rule `LP3` refer to the stub, not repo content.

## Install locally

Requires Python 3.12 or newer for SkillSpector itself.

```bash
uv tool install --python 3.12 "git+https://github.com/NVIDIA/SkillSpector.git@b5987a332c9a118ec4714a5c79eb76641b546251"
make skill-scan
```

## Accepting a finding

No finding is suppressed by default. A target fails the gate until a human triages it. To accept
reviewed findings for one target, generate an exact-fingerprint baseline and commit it with a
reason per entry:

```bash
skillspector baseline <staged-or-skill-dir> --no-llm -o .skillspector/baselines/<slug>.yaml
```

`<slug>` is the report filename in `skillspector-reports/` without `.json`. Fingerprints are
invalidated by any change to the scanned content or a SkillSpector upgrade, so an edited file
is always re-reviewed.
