---
description: Set the search goals that drive job matching and the dashboard.
---

Set up or revise the user's search goals in
`${RECRUIT_HOME:-$HOME/.recruit-copilot}/state/goals.json`.

This file does two jobs: the `goals` list is what the Goals tab tracks, and the
`search` block is what every job in the Jobs tab is scored against. Getting it right
is the difference between a search and a feed.

Ask for, and write into `search`:
- `titles.strong` - the titles they actually want. Required; without it every job
  scores the same.
- `titles.medium` - adjacent titles worth seeing.
- `seniority.prefer` / `seniority.avoid` - level words that should lift or sink a role.
- `comp_min` - a number, or 0 to ignore pay. Postings that state no pay are never
  penalized for it, because an unstated salary is not a failed one.
- `locations` - cities, "remote", "united states".
- `keywords_bonus` - domain words that make a role more interesting.
- `min_match` - the score below which a role is not worth showing. 55 is a sane start.

Then ask for, and write into `search.profile` — this is who they ARE, as opposed to
what they want, and it is what stops the scout ranking a VP job they cannot land
above the manager job they can:
- `years_experience` - **ask for this explicitly**: how many years of relevant
  full-time experience they have since finishing school. A number. The scout compares
  it against the years each posting states; within a year of the ask is a fit, two or
  three over is a stretch, four or more sinks the role.
- `education` - highest degree, in progress or finished (e.g. "MBA (expected 2027)").
- `level_ceiling` - title words that sit above them today. Omit it to take the default
  (director, vp, vice president, head of, principal, staff, distinguished, chief,
  partner, gm, general manager), or set it to `[]` if none of those are a reach. Note
  a word can mean two things: "Principal" is a reach in product and a mid-level title
  in venture capital, so if they are searching VC, take it out.

If they leave `profile` out entirely, the years and level checks are switched off
rather than guessed — an inherited example profile would tell them they are qualified
for roles they are not, in the tool's own confident voice.

Also ask what they are actually trying to achieve and write those as `goals` rows
(`goal`, `status`, optional `note`) so the dashboard tracks the search, not just the
listings.

Then run `/recruit:scout` and show them the top matches with the `why` for each, so
they can see whether the scoring reflects what they said. Tune and re-run if not.
