---
description: "Ingest and score target-company roles from public Greenhouse and Ashby boards, then write them to the Jobs tab."
---

Run the job scout to refresh matched roles for the Jobs tab.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/dashboard/job_scout.py
```

Two files drive this, and they do different jobs:

- **Which boards** — `${RECRUIT_HOME:-$HOME/.recruit-copilot}/state/target_companies.json`, rows of
  `{"name", "ats": "greenhouse"|"ashby", "token"}`. If the file is absent, a small built-in list of
  AI companies is used in memory; nothing is written, so the user still has to create the file to
  choose their own targets.
- **How roles score** — `state/goals.json` (from `/recruit:goals`): title keywords, seniority, comp
  floor, locations, `min_match`, and the `profile` block (years of experience, level ceiling). If it
  does not exist yet, the scout writes a starter file and **stops**, rather than scoring the user's
  career against somebody else's defaults. If that happens, walk them through `/recruit:goals`
  before re-running.

It pulls roles from the public Greenhouse boards-api and Ashby posting-api (both no-auth and
ToS-clean), scores them on title, level, location and pay, then reads the full posting for the
leading candidates per board and runs the **qualification pass** (`dashboard/qualification.py`):
the years the posting asks for against the years in the profile, a title above the user's level, a
required credential the experience bank does not show, and how much of the requirements section the
bank can actually answer. Roles are re-ranked on the result and each carries a **`fit`** tag —
`fit`, `stretch`, `unqualified`, or **`unverified`** when the posting could not be fetched or
stated nothing checkable — plus the plain-English reasons, in `why`. Then it writes
`state/jobs.json`, which is what the Jobs tab reads. Only `--per-company` rows per board are
written, the same ones the console prints; the scout reads twice that many to have something to
promote when the qualification pass demotes a role.

After it runs, summarize for the user: how many roles matched, the fit/stretch/unqualified split,
the top handful (company, title, match score, `fit`, comp), and which boards failed if any (the
`sources` list carries a plain-language reason per board). **Lead with the `fit` roles** — a
`stretch` is worth an application if they want it, an `unqualified` is there for transparency, not
as a suggestion, and an `unverified` means the tool has no opinion — say so rather than letting its
match score speak for it. To change what counts as a match, send them to `/recruit:goals` — not to
`target_companies.json`, which only picks boards. For a stricter list, re-run with `--min 65`; for
a slow or flaky board, `--timeout 60` (every request is already retried once).
