#!/usr/bin/env python3
"""job_scout - ingest roles from public ATS JSON endpoints (Greenhouse boards-api +
Ashby posting-api, both public, no auth, ToS-clean), score them against your goals,
check whether you could actually GET them, select the top N per company, and capture
the JD text. Writes state/jobs.json, which the Jobs tab reads.

Two questions, two scorers. search_goals.py answers "is this the kind of role I
want?" from the title, level, location and pay. qualification.py answers "could I
get it?" from the posting's stated requirements and your own experience bank. The
first one alone put a Corporate Development LEAD at $425-600k and a PRINCIPAL
Product Manager at the top of a second-year MBA's list, which is a ranked list
that spends attention on the applications least likely to land.

Edit <workspace>/state/target_companies.json to set YOUR target boards (each row is
{name, ats: greenhouse|ashby, token}) and criteria. Run: python3 job_scout.py
"""
import argparse, html, json, os, re, sys, time, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import qualification
import search_goals

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
# One resolver for the whole product. This module used to default to the plugin's
# own workspace/ while the dashboard defaulted to ~/.recruit-copilot, so a user who
# never set RECRUIT_HOME had the scout write jobs into the versioned plugin
# directory and the Jobs tab read an empty one three feet away.
HOME = paths.home(create=True)
STATE = os.path.join(HOME, "state")
CFG = os.path.join(STATE, "target_companies.json")
JOBS_OUT = os.path.join(STATE, "jobs.json")
MASTER = os.path.join(HOME, "master-experience.json")

# One board that hangs should not cost the whole run, and one board that hangs
# ONCE is usually just a slow board. Anduril's answers in ~3s or not at all.
DEFAULT_TIMEOUT = 30
# How many roles per company get their posting body fetched and qualification-
# checked. The qualification pass needs the JD, and the JD costs a request, so
# this is the knob that trades run time for how deep the honest ranking goes.
CANDIDATE_MULTIPLE = 2
CANDIDATE_CAP = 12

DEFAULT_COMPANIES = [
    {"name": "Anthropic", "ats": "greenhouse", "token": "anthropic"},
    {"name": "OpenAI", "ats": "ashby", "token": "openai"},
    {"name": "Databricks", "ats": "greenhouse", "token": "databricks"},
    {"name": "Scale AI", "ats": "greenhouse", "token": "scaleai"},
    {"name": "xAI", "ats": "greenhouse", "token": "xai"},
    {"name": "Cohere", "ats": "ashby", "token": "cohere"},
    {"name": "Sierra", "ats": "ashby", "token": "sierra"},
]
# Scoring comes from YOUR goals file, not from constants in here. See
# dashboard/search_goals.py for why that matters.

def _get(url, timeout=DEFAULT_TIMEOUT, retries=1):
    """GET some JSON, with one retry.

    A board that times out is reported to the user as a failed source, which
    reads exactly like "this company is not hiring" -- so it is worth one more
    attempt before saying it. One retry, not a loop: if a board is down, telling
    the user quickly is better than hanging on it.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "recruit-copilot-jobscout/1.0"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:                      # noqa: BLE001 - reported, not swallowed
            last = e
            if attempt < retries:
                time.sleep(1.0)
    raise last


def score_job(title, location, search, jd_text=""):
    """Delegates to the goals-driven scorer so the Jobs tab reflects your search."""
    return search_goals.score(title, location, search, jd_text)


def _comp_from_text(text):
    """Display string for the Jobs tab, from the same parser the scorer uses."""
    band = search_goals.pay_band(text)
    return band[2] if band else None


# ---- ATS adapters: normalize to {title, location, url, id, raw_jd_fetch()} ----
def fetch_greenhouse(token, timeout=DEFAULT_TIMEOUT):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", timeout)
    out = []
    for j in data.get("jobs", []):
        out.append({"title": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
                    "url": j.get("absolute_url", ""), "id": j.get("id"), "ats": "greenhouse", "token": token,
                    "jd_text": None})
    return out


def fetch_ashby(org, timeout=DEFAULT_TIMEOUT):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", timeout)
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({"title": j.get("title", ""), "location": j.get("location", ""),
                    "url": j.get("jobUrl", ""), "id": j.get("id"), "ats": "ashby", "token": org,
                    "jd_text": j.get("descriptionPlain") or None})  # Ashby ships the JD inline
    return out


def _plain(content: str) -> str:
    """Greenhouse ships the posting body as HTML-ESCAPED HTML, so unescaping has to
    come first. Stripping tags before unescaping finds no tags to strip -- there are
    no literal angle brackets yet -- and the unescape then turns the entities INTO
    markup. Every Greenhouse posting was reaching the tailoring and grading steps as
    a wall of div, h2, li and strong, which is what those steps were matching the
    resume's keywords against."""
    text = content or ""
    for _ in range(3):                       # some entities arrive double escaped
        once = html.unescape(text)
        if once == text:
            break
        text = once
    text = re.sub(r"<[^>]+>", " ", text).replace("\u00a0", " ")
    return re.sub(r"[ \t]*\n\s*\n\s*", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def hydrate_jd(job, timeout=DEFAULT_TIMEOUT):
    """Ensure job['jd_text'] is populated (Ashby already has it; Greenhouse fetch content)."""
    if job.get("jd_text"):
        return job
    if job["ats"] == "greenhouse":
        try:
            d = _get(f"https://boards-api.greenhouse.io/v1/boards/{job['token']}/jobs/{job['id']}?content=true",
                     timeout)
            job["jd_text"] = _plain(d.get("content", ""))
        except Exception as e:                      # noqa: BLE001 - recorded, not swallowed
            # An empty body used to be indistinguishable from a posting that says
            # nothing, and both came out of the qualification pass tagged "fit".
            # Keep the reason, so the row can be honest about why it was not read.
            job["jd_text"] = ""
            job["jd_error"] = str(e)
    return job


def load_bank():
    """The experience bank the qualification pass reads. Missing is fine and
    common (the scout runs before intake for plenty of people); qualification
    stands its bank-driven checks down rather than failing everything.

    A CORRUPT bank is not fine, and must not look like a missing one -- silently
    scoring every role as if the user had no experience is the failure that would
    take longest to notice."""
    if not os.path.exists(MASTER):
        return {}
    try:
        with open(MASTER) as fh:
            data = json.load(fh)
    except Exception as e:                          # noqa: BLE001 - reported below
        print(f"WARNING: {MASTER} could not be read ({e}).\n"
              f"         The qualification pass will run without an experience bank, so the "
              f"skills and credential checks are OFF. Fix the file or re-run /recruit:intake.",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict) or not data.get("jobs"):
        print(f"WARNING: {MASTER} has no `jobs` list, so there is no experience to compare "
              f"postings against. The skills and credential checks are OFF.", file=sys.stderr)
        return {}
    return data


def scout(per_company=5, min_match=None, timeout=DEFAULT_TIMEOUT):
    """Three passes, on purpose.

    Titles and locations are free to score, but the two things that decide whether
    a role is worth your afternoon -- what it pays and what it requires -- are only
    in the posting body, which costs a fetch per role. So: score on title first,
    keep what survives, fetch the body for the leading CANDIDATE_MULTIPLE x N per
    company, then re-score those with the pay AND run the qualification pass, then
    re-rank and keep the best N.

    The candidate pool is deliberately wider than the N that get reported. The
    qualification pass exists to move roles DOWN, so if it only ever saw the top N
    it would hand back a shorter list rather than a better one -- the role that
    should have taken the demoted one's place was never looked at.
    """
    search = search_goals.load(STATE)          # raises NoGoals; the caller must stop
    profile = search.get("profile") or {}
    bank = load_bank()

    companies = DEFAULT_COMPANIES
    if os.path.exists(CFG):
        try:
            companies = json.load(open(CFG)).get("companies", DEFAULT_COMPANIES)
        except Exception:
            pass
    min_match = min_match if min_match is not None else int(search.get("min_match", 55))
    n_candidates = min(max(per_company * CANDIDATE_MULTIPLE, per_company), CANDIDATE_CAP)

    flat, review, sources = [], [], []
    for c in companies:
        # Be forgiving about the row shape: a hand-written config that omits "ats"
        # means Greenhouse (the common case), and a missing token is a config error
        # the user must be told about plainly, not a bare KeyError swallowed below.
        name = c.get("name") or c.get("token") or "(unnamed)"
        token = c.get("token")
        ats = (c.get("ats") or "greenhouse").strip().lower()
        if not token:
            sources.append({"company": name, "ok": False,
                            "error": 'missing "token" — add the board token, e.g. {"name": "Acme", "token": "acme"}'})
            continue
        if ats not in ("greenhouse", "ashby"):
            sources.append({"company": name, "ok": False,
                            "error": f'unknown ats "{ats}" — use "greenhouse" or "ashby"'})
            continue
        try:
            jobs = fetch_ashby(token, timeout) if ats == "ashby" else fetch_greenhouse(token, timeout)
        except Exception as e:
            sources.append({"company": name, "ok": False, "error": str(e)})
            continue
        for j in jobs:
            j["company"] = name
            j["match"], j["why"] = score_job(j["title"], j["location"], search)
        jobs.sort(key=lambda x: x["match"], reverse=True)
        kept = [j for j in jobs if j["match"] >= min_match]

        candidates = kept[:n_candidates]
        for j in candidates:
            hydrate_jd(j, timeout)
            jd = j.get("jd_text") or ""
            j["comp"] = _comp_from_text(jd)
            # second pass: the posting body is here now, so pay can count
            base, why = score_job(j["title"], j["location"], search, jd)
            # third pass: and so can whether you could get it
            a = qualification.assess(j["title"], jd, bank, profile)
            j["match"] = qualification.apply(base, a)
            j["why"] = why + a.reasons
            if j.get("jd_error"):
                j["why"].append(f"the posting would not load ({j['jd_error']})")
            j["fit"] = a.fit
        candidates.sort(key=lambda x: x["match"], reverse=True)
        top = candidates[:per_company]
        counts = {k: sum(1 for j in top if j.get("fit") == k) for k in qualification.FITS}
        sources.append({"company": name, "ok": True, "total": len(jobs), "matched": len(kept),
                        "assessed": len(candidates), "reported": len(top), "fit_counts": counts})

        # --per-company is a promise about the table, not just the console: the
        # rows written here are the same rows the summary line above prints.
        # Everything the qualification pass demoted stays out, and the wider
        # candidate pool exists so there is something to promote in its place --
        # but the pool itself is working memory, not output.
        flat.extend(top)
        review.append({"name": name, "ats": ats, "token": token, "fit_counts": counts,
                       "jobs": [{k: j.get(k) for k in ("title", "location", "url", "id", "ats", "token",
                                                       "match", "why", "comp", "fit", "jd_text")} for j in top]})

    flat.sort(key=lambda x: x["match"], reverse=True)
    now = datetime.now(timezone.utc).isoformat()
    total_matched = sum(s.get("matched", 0) for s in sources if s.get("ok"))
    total_assessed = sum(s.get("assessed", 0) for s in sources if s.get("ok"))
    run_counts = {k: sum(1 for j in flat if j.get("fit") == k) for k in qualification.FITS}
    unverified = run_counts[qualification.UNVERIFIED]
    json.dump({"generated": now, "search": search,
               "sources": sources, "total_matched": total_matched,
               "assessed": total_assessed, "reported": len(flat), "fit_counts": run_counts,
               "shown": min(60, len(flat)),
               "jobs": [dict({k: j.get(k) for k in ("company", "title", "location",
                                                     "match", "why", "comp", "fit", "url")},
                             # the posting body, for the roles deep enough to have been
                             # hydrated, so /recruit:tailor has something to tailor TO
                             # instead of asking the user to paste it back in
                             jd_text=(j.get("jd_text") or "")[:12000] or None)
                        for j in flat[:60]],
               "note": (f"{total_matched} roles matched across "
                        f"{len([s for s in sources if s.get('ok')])} boards (Greenhouse + Ashby, "
                        f"ToS-clean); {total_assessed} were read in full and checked against your "
                        f"experience bank, and the best {len(flat)} are shown — "
                        f"{run_counts[qualification.FIT]} fit, "
                        f"{run_counts[qualification.STRETCH]} stretch, "
                        f"{run_counts[qualification.UNQUALIFIED]} unqualified"
                        + (f", {unverified} unverified (the posting stated nothing checkable)"
                           if unverified else "")
                        + ("" if bank else ". No experience bank yet, so only the level checks ran")
                        + ".")},
              open(JOBS_OUT, "w"), indent=2, default=str)
    return {"sources": sources, "review": review, "flat": len(flat), "matched": total_matched,
            "fit_counts": run_counts}


def _fit_summary(counts):
    c = counts or {}
    s = (f"fit {c.get(qualification.FIT, 0)} / stretch {c.get(qualification.STRETCH, 0)} "
         f"/ unqualified {c.get(qualification.UNQUALIFIED, 0)}")
    # Only printed when it happened, so a clean run stays readable -- but never
    # hidden, because an unverified row is the one the user should not trust.
    if c.get(qualification.UNVERIFIED):
        s += f" / unverified {c[qualification.UNVERIFIED]}"
    return s


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scout open roles and score them against your goals "
                                             "AND your experience bank.")
    ap.add_argument("--per-company", type=int, default=5,
                    help="how many roles to report per board (default 5)")
    ap.add_argument("--min", type=int, default=None,
                    help="minimum title score to consider (default: min_match from goals.json)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"seconds to wait on each board request, retried once (default {DEFAULT_TIMEOUT})")
    a = ap.parse_args()
    try:
        r = scout(a.per_company, a.min, a.timeout)
    except search_goals.NoGoals as e:
        print(f"\n{e}\n", file=sys.stderr)
        sys.exit(2)
    for co in r["review"]:
        print(f"{co['name']:<12} {len(co['jobs'])} selected [{_fit_summary(co.get('fit_counts'))}]: " +
              ", ".join(f"{j['title'][:28]}({j['match']}/{j.get('fit', '?')})" for j in co["jobs"][:3]) + " ...")

    # A board that failed is not a board with no matches. Say so, loudly, or a typo'd
    # token looks exactly like "this company is not hiring".
    failed = [s_ for s_ in r["sources"] if not s_.get("ok")]
    ok = [s_ for s_ in r["sources"] if s_.get("ok")]
    for s_ in failed:
        print(f"FAILED {s_['company']}: {s_.get('error','')}", file=sys.stderr)
    if not ok:
        print(f"\nNo board could be read ({len(failed)} failed). Check the tokens in "
              f"{CFG}, or your network connection.", file=sys.stderr)
        sys.exit(1)
    if failed:
        print(f"({len(failed)} of {len(r['sources'])} boards failed - see above)", file=sys.stderr)
    print(f"wrote {JOBS_OUT}")
