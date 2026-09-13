#!/usr/bin/env python3
"""Smoke test: prove a fresh clone actually works, end to end, with no network and
no API key. Run it after cloning, or before opening a PR.

    python3 smoke_test.py

It builds a throwaway workspace in a temp dir, drives the real shipped scripts the
same way the /recruit: commands do, and checks the artifacts that result. It never
touches your own workspace and never talks to the network.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python3"

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([PY] + args, capture_output=True, text=True, timeout=120, **kw)


def main() -> int:
    print("recruit-copilot smoke test\n")
    ws = tempfile.mkdtemp(prefix="recruit-smoke-")
    os.makedirs(os.path.join(ws, "resumes"), exist_ok=True)
    os.makedirs(os.path.join(ws, "state"), exist_ok=True)
    try:
        # 1. Every shipped script imports and responds to --help (stdlib only).
        print("1. shipped scripts run (stdlib only)")
        scripts = [
            "skills/resume-builder/scripts/build.py",
            "skills/resume-builder/scripts/format_qa.py",
            "skills/resume-builder/scripts/parse_check.py",
            "skills/resume-grader/scripts/aggregate.py",
            "skills/resume-intake/scripts/validate_bank.py",
            "skills/resume-intake/scripts/extract_text.py",
            "dashboard/job_scout.py",
            "dashboard/search_goals.py",
            "dashboard/qualification.py",
        ]
        for s in scripts:
            p = run([os.path.join(ROOT, s), "--help"])
            check(f"{s} --help", p.returncode == 0, (p.stderr or "").strip().splitlines()[-1] if p.returncode else "")

        # 2. The shipped example bank validates against the schema.
        print("\n2. example experience bank is valid")
        bank_src = os.path.join(ROOT, "workspace", "master-experience.example.json")
        p = run([os.path.join(ROOT, "skills/resume-intake/scripts/validate_bank.py"), bank_src])
        check("example bank validates", p.returncode == 0, (p.stdout or p.stderr)[-160:].strip())

        # 3. Tailor + build: the example bank -> a real PDF, both machine gates green.
        print("\n3. build a resume (render + layout gate + round-trip gate)")
        bank = json.load(open(bank_src))
        c = bank["contact"]
        job = bank["jobs"][0]
        tailored = {
            "name": bank["name"],
            "contact": " | ".join([c["email"], c["phone"], c["location"], *c.get("links", [])]),
            "summary": list(bank["summaries"].values())[0],
            "jobs": [{"title": j["title"], "company": j["company"], "location": j["location"],
                      "dates": j["dates"], "bullets": list(j["bullets"].values())[:3]}
                     for j in bank["jobs"]],
            "education": bank.get("education", []),
            "skills": bank["skills_pool"],
            "additional": "",
        }
        tpath = os.path.join(ws, "tailored.json")
        json.dump(tailored, open(tpath, "w"))
        pdf = os.path.join(ws, "resumes", "smoke-test.pdf")
        p = run([os.path.join(ROOT, "skills/resume-builder/scripts/build.py"), tpath, "--out", pdf, "--pages", "1"])
        check("build.py exits 0 (both gates passed)", p.returncode == 0,
              f"exit={p.returncode} {(p.stdout or p.stderr)[-200:].strip()}")
        check("PDF exists and is non-trivial", os.path.exists(pdf) and os.path.getsize(pdf) > 1000,
              f"{os.path.getsize(pdf) if os.path.exists(pdf) else 0} bytes")
        if os.path.exists(pdf):
            with open(pdf, "rb") as fh:
                check("PDF has a valid header", fh.read(5) == b"%PDF-", "")

        # 3b. The layout gate has to catch what only the typeset page shows. These
        # are regressions we actually shipped once: a long employer name printed on
        # top of its own date range, and a right-aligned date measured before the
        # renderer folded its en dash to a hyphen, which pushed it off the margin.
        print("\n3b. layout gate catches page-only defects")
        sys.path.insert(0, os.path.join(ROOT, "skills/resume-builder/scripts"))
        import format_qa, render_resume, pdfwrite  # noqa: E402

        def page(lines, pages=1):
            return {"pages": pages, "page_width": 612.0, "page_height": 792.0,
                    "text_right_edge": 558.0, "notes": [],
                    "content": [{"page": i + 1, "lines": lines if i == 0 else []}
                                for i in range(pages)]}

        def run_line(text, x, w, kind="body", y=100.0):
            return {"text": text, "kind": kind, "size": 9.2, "style": "regular",
                    "bbox": [x, 792 - y - 9.2, x + w, 792 - y]}

        r = format_qa.analyze(page([run_line("A Very Long Employer Name Indeed", 54, 400, "employer"),
                                    run_line("January 2018 to December 2024", 420, 138, "dates")]), 1)
        check("gate fails overlapping text on one baseline",
              not r["passed"] and any(i["code"] == "text_collision" for i in r["issues"]),
              f"issues={[i['code'] for i in r['issues']] or 'none'}")

        r = format_qa.analyze(page([run_line("an unbreakable identifier off the page", 54, 530, "bullet")]), 1)
        check("gate fails text well past the right margin",
              not r["passed"] and any(i["code"] == "margin_overflow" for i in r["issues"]), "")

        r = format_qa.analyze(page([run_line("Led a team \u2014 shipped fast", 54, 140, "summary")]), 1)
        check("gate fails a typographic dash that reached the page",
              not r["passed"] and any(i["code"] == "banned_glyph" for i in r["issues"]), "")

        dash = os.path.join(ws, "resumes", "dash-align.pdf")
        lay = render_resume.render(
            {"name": "Dana Reyes", "contact": "dana@example.org | 555-0100",
             "jobs": [{"company": "Acme", "title": "Engineer", "dates": "2018\u20132024",
                       "bullets": ["Shipped a thing that mattered to real users."]}]}, dash)
        dates = [l for pg in lay["content"] for l in pg["lines"] if l["kind"] == "dates"]
        check("a right-aligned date stays inside the margin after folding",
              bool(dates) and all(l["bbox"][2] <= lay["text_right_edge"] + 0.01 for l in dates),
              f"{[round(l['bbox'][2], 1) for l in dates]} vs edge {lay['text_right_edge']}")

        wide = render_resume.render(
            {"name": "Dana Reyes", "contact": "dana@example.org",
             "jobs": [{"company": "Metropolitan Interoperability and Data Platform Services "
                                  "Group of the Greater Region", "title": "Engineer",
                       "dates": "January 2018 to December 2024",
                       "bullets": ["Shipped a thing that mattered to real users."]}]},
            os.path.join(ws, "resumes", "wide-employer.pdf"))
        check("the renderer never overlaps a long employer with its dates",
              format_qa.analyze(wide, 2)["stats"]["collisions"] == 0,
              f"{format_qa.analyze(wide, 2)['stats']['collisions']} collision(s)")

        # A one-page resume that stops short is wasting the most valuable space the
        # candidate has. The gate has to say so, and say how much room is left in
        # bullets, because "76% full" is not an instruction.
        sparse = render_resume.render(
            {"name": "Dana Reyes", "contact": "dana@example.org | 555-0100",
             "summary": "Engineer.",
             "jobs": [{"company": "Acme", "title": "Engineer", "dates": "2022 to 2024",
                       "bullets": ["Shipped one thing that mattered to real users."]}]},
            os.path.join(ws, "resumes", "sparse.pdf"))
        r = format_qa.analyze(sparse, 1)
        uf = [i for i in r["issues"] if i["code"] == "page_underfill"]
        check("gate flags a one-pager that leaves the page half empty",
              bool(uf) and "bullet" in uf[0]["detail"],
              uf[0]["detail"][:70] if uf else f"fill={r['stats']['fill_pct']}%")
        check("body text is in the normal 10-11pt resume range, not shrunk to fit",
              10.0 <= render_resume.BODY_SIZE <= 11.0, f"{render_resume.BODY_SIZE}pt")

        # 3c. Every module has to agree on where the workspace is. They did not:
        # the scout defaulted to the plugin's own workspace/ while the dashboard
        # defaulted to ~/.recruit-copilot, so a default user's scouted jobs landed
        # in a directory the Jobs tab never reads -- and got wiped on the next
        # plugin version, which is the whole reason paths.py exists.
        print("\n3c. one workspace, agreed on by every module")
        sys.path.insert(0, os.path.join(ROOT, "dashboard"))
        import paths  # noqa: E402
        import job_scout  # noqa: E402
        import server as dash_server  # noqa: E402
        check("scout and dashboard resolve the same workspace",
              job_scout.HOME == paths.home() == dash_server.HOME,
              f"scout={job_scout.HOME} paths={paths.home()} server={dash_server.HOME}")
        check("the workspace is outside the plugin tree",
              not os.path.abspath(paths.home()).startswith(os.path.abspath(ROOT) + os.sep),
              f"{paths.home()} vs plugin {ROOT}")

        docs = []
        for sub in ("commands", "skills"):
            for dirpath, _dn, fns in os.walk(os.path.join(ROOT, sub)):
                docs += [os.path.join(dirpath, f) for f in fns if f.endswith(".md")]
        defaults = set()
        for d in docs:
            for m in re.finditer(r"RECRUIT_HOME:-([^}]*)", open(d, encoding="utf-8").read()):
                defaults.add(m.group(1))
        check("every command documents the same fallback path",
              len(defaults) <= 1, f"found {sorted(defaults)}")

        # 3d. Two scouting bugs that only showed up against live postings.
        print("\n3d. job scout reads postings the way a person would")
        import search_goals  # noqa: E402
        esc = ("&lt;h2&gt;&lt;strong&gt;Responsibilities:&lt;/strong&gt;&lt;/h2&gt;"
               "&lt;li&gt;Build production applications with Claude.&lt;/li&gt;")
        plain = job_scout._plain(esc)
        check("an HTML-escaped posting body becomes prose, not markup",
              "<" not in plain and "Responsibilities:" in plain and "Claude" in plain, repr(plain[:60]))

        band = search_goals.pay_band("The base pay range for this role is $228,600 - $343,000 USD.")
        _sc, why = search_goals.score(
            "Forward Deployed Engineer", "Remote",
            {"titles": {"strong": ["forward deployed engineer"], "medium": []},
             "seniority": {"prefer": [], "avoid": []}, "comp_min": 300000,
             "locations": ["remote"], "keywords_bonus": [], "min_match": 55},
            "The base pay range for this role is $228,600 - $343,000 USD.")
        pay_why = " ".join(r for r in why if "pay" in r)
        check("the pay shown and the pay scored are the same number",
              band is not None and str(band[1]) and f"${band[1]:,}" in pay_why,
              f"shown={band[2] if band else None!r} why={pay_why!r}")
        check("a signing bonus does not become the bottom of the band",
              search_goals.pay_band("Pay: $180,000 to $260,000, plus a $25,000 signing bonus.")[0] == 180000, "")

        # 3e. The qualification pass: could this person actually GET the role?
        # Scoring title/level/location/pay alone put a Corporate Development LEAD
        # at $425-600k and a PRINCIPAL Product Manager at the top of a second-year
        # MBA's list. Both were the right shape and neither was reachable.
        print("\n3e. qualification pass (can you actually get it?)")
        import qualification  # noqa: E402

        years_cases = [
            ("5+ years of experience in product management", 5),
            ("7-10 years of relevant experience", 7),
            ("3–5 years of experience", 3),
            ("minimum of 8 years of experience", 8),
            ("at least 6 yrs of experience", 6),
            ("8+ yrs. of experience leading teams", 8),
            ("A minimum of five years of experience", 5),
            ("10 years' experience in a similar role", 10),
            ("Experience: 12+ years", 12),
            ("Bachelor's degree and 5 years of related experience", 5),
            # a preferred ceiling must never outrank the stated floor
            ("Preferred: 10+ years of experience. Required: 5+ years of experience.", 5),
            ("0-2 years of experience", 0),
            # and the three that must NOT be read as a requirement
            ("A 4-year degree is required, plus strong communication skills", None),
            ("We have served customers for 25 years", None),
            ("Compensation is $120,000 - $180,000 per year", None),
        ]
        wrong = [(t, qualification.years_required(t), want)
                 for t, want in years_cases if qualification.years_required(t) != want]
        check("years parser reads a dozen real phrasings, and refuses three lookalikes",
              not wrong, f"{len(years_cases) - len(wrong)}/{len(years_cases)}; wrong={wrong[:3]}")

        ceil = qualification.DEFAULT_LEVEL_CEILING
        lvl_cases = [("Principal Publishing Product Manager", True), ("Director of Product", True),
                     ("VP of Strategy", True), ("Head of Corporate Development", True),
                     ("Staff Software Engineer", True), ("General Partner", True),
                     ("GM, Payments", True), ("Distinguished Engineer", True),
                     ("Chief Financial Officer", True),
                     # the exceptions: a target title, a junior title, and a
                     # partner-facing title that is not a partnership
                     ("Chief of Staff", False), ("Chief of Staff, Product", False),
                     ("Staff Accountant", False), ("Partner Marketing Manager", False),
                     ("Senior Product Manager", False), ("Team Lead", False)]
        bad_lvl = [t for t, want in lvl_cases if bool(qualification.level_ceiling_hits(t, ceil)) != want]
        check("level ceiling fires on nine senior titles and spares Chief of Staff",
              not bad_lvl, f"misjudged={bad_lvl}")
        check("'Lead <role>' is a modifier, 'Team Lead' is not",
              qualification.lead_is_seniority_modifier("Lead Product Manager")
              and not qualification.lead_is_seniority_modifier("Technical Lead"), "")

        ex_bank = json.load(open(bank_src))          # the shipped backend-engineer bank
        prof5 = {"years_experience": 5}
        jd_reach = ("About the role\n\nQualifications\n\n- 12+ years of experience building "
                    "distributed systems\n- Deep Kubernetes and Kafka expertise\n"
                    "- Postgres, Redis and streaming pipelines at scale\n")
        a = qualification.assess("Director of Platform Engineering", jd_reach, ex_bank, prof5)
        check("12 years asked of a 5-year bank is unqualified, and says both numbers",
              a.fit == "unqualified" and a.delta <= -35
              and any("12" in r and "5" in r for r in a.reasons), f"{a.fit} {a.delta} {a.reasons}")

        jd_stretch = jd_reach.replace("12+ years", "8+ years")
        a = qualification.assess("Senior Software Engineer", jd_stretch, ex_bank, prof5)
        check("8 years asked of a 5-year bank is a stretch, not a knockout",
              a.fit == "stretch" and a.delta == qualification.YEARS_STRETCH_PENALTY,
              f"{a.fit} {a.delta}")

        jd_fit = jd_reach.replace("12+ years", "5+ years")
        a = qualification.assess("Senior Software Engineer", jd_fit, ex_bank, prof5)
        check("a role at your own level with your own stack is a clean fit",
              a.fit == "fit" and a.delta == 0 and a.cap is None, f"{a.fit} {a.delta} cap={a.cap}")

        # Overlap: same seniority, a field this bank has nothing to say about.
        jd_far = ("Requirements\n\n- Licensed clinical experience in inpatient oncology nursing\n"
                  "- Familiarity with Epic charting, HIPAA documentation and infusion protocols\n"
                  "- Phlebotomy certification and bedside patient triage\n"
                  "- Comfortable coordinating discharge planning with attending physicians\n")
        a = qualification.assess("Clinical Program Manager", jd_far, ex_bank, prof5)
        check("a posting whose requirements the bank cannot answer is capped at 60",
              a.cap == qualification.OVERLAP_CAP and any("matched" in r for r in a.reasons),
              f"cap={a.cap} {a.reasons}")
        check("the cap is a ceiling, not a subtraction",
              qualification.apply(99, a) == 60 and qualification.apply(42, a) == 42,
              f"99->{qualification.apply(99, a)} 42->{qualification.apply(42, a)}")

        a = qualification.assess("Senior Software Engineer",
                                 "Requirements\n\n- A PhD in computer science is required\n"
                                 "- 5+ years of experience with Kubernetes and Kafka\n",
                                 ex_bank, prof5)
        check("a stated credential the bank does not hold is a knockout",
              a.fit == "unqualified" and a.delta <= -40 and any("doctorate" in r for r in a.reasons),
              f"{a.fit} {a.delta} {a.reasons}")
        clearance = ("Requirements\n\n- Ability to obtain a security clearance\n"
                     "- 5+ years of experience with distributed systems, Kafka and Postgres\n")
        a = qualification.assess("Senior Software Engineer", clearance, ex_bank, prof5)
        check("'ability to obtain a clearance' is a hiring promise, not a knockout",
              a.fit == "fit", f"{a.fit} {a.reasons}")

        # No profile means the tool does not know who you are, and guessing is the
        # one thing goals.json exists to prevent. The bank-driven checks still run.
        a = qualification.assess("Director of Platform Engineering", jd_reach, ex_bank, None)
        check("with no profile the years and level checks stand down",
              a.fit == "fit" and a.delta == 0, f"{a.fit} {a.delta} {a.reasons}")
        a = qualification.assess("Senior Software Engineer", jd_far, {}, prof5)
        check("with no experience bank the overlap check stands down, and says so",
              a.cap is None and a.fit == "unverified", f"cap={a.cap} fit={a.fit} {a.reasons}")
        # Every other key in the search block falls back to the example. `profile`
        # must not: it is a claim about who the user IS, and inheriting the
        # example's "8 years" would tell a career changer they are qualified for
        # roles they are not, in the tool's own confident voice.
        gdir = os.path.join(ws, "goals-no-profile")
        os.makedirs(gdir, exist_ok=True)
        json.dump({"search": {"titles": {"strong": ["site reliability engineer"]}}},
                  open(os.path.join(gdir, "goals.json"), "w"))
        loaded = search_goals.load(gdir)
        check("a search block with no profile does not inherit the example's",
              loaded.get("profile") == {}
              and search_goals.EXAMPLE_SEARCH["profile"].get("years_experience"),
              f"profile={loaded.get('profile')!r}")
        json.dump({"search": {"titles": {"strong": ["site reliability engineer"]},
                              "profile": {"years_experience": 3}}},
                  open(os.path.join(gdir, "goals.json"), "w"))
        check("a profile the user did write survives the merge intact",
              search_goals.load(gdir).get("profile") == {"years_experience": 3}, "")

        check("the requirements section is found, not the whole posting",
              "Kubernetes" in qualification.requirements_section(jd_reach)
              and "About the role" not in qualification.requirements_section(jd_reach), "")

        # 3f. Four adversarial cases, one per bug found in review. Each of these
        # produced a confident, wrong answer before the fix, which is the only
        # kind of wrong answer that matters in a tool people act on.
        print("\n3f. adversarial: the four ways this pass lied")

        # (1) A company bragging about its own tenure is not a requirement. This
        # read as "12 years required" and sank the role by 35 points.
        brag = ("About us\n\nOur team has 12 years of experience serving customers, and we have "
                "been trusted for over 20 years of experience in the field.\n")
        check("a company's own tenure is not a years requirement",
              qualification.required_years(brag) is None
              and qualification.years_required(brag) == 12,   # the raw parser still sees it
              f"required={qualification.required_years(brag)} raw={qualification.years_required(brag)}")
        a = qualification.assess("Senior Software Engineer", brag, ex_bank, prof5)
        check("  ...so the About-us paragraph costs the role nothing",
              a.delta == 0 and a.fit == "unverified", f"{a.fit} {a.delta}")
        check("  ...but a requirement outside any section still counts, when anchored",
              qualification.required_years(
                  "We are looking for someone with at least 9 years of experience.") == 9, "")

        # (2) The posting's own concession that a doctorate is OPTIONAL became the
        # reason it was treated as mandatory, because the match crossed a `;`.
        # Both spellings matter: the long one is caught by the character budget
        # alone, the SHORT one is caught only by the clause split -- so without
        # the short case this test would pass against the unfixed code.
        preferred = ["A PhD is preferred; a master's degree in a related field is required.",
                     "PhD preferred; a BS is required.",
                     "PhD or equivalent, degree required.",
                     "CPA preferred; a bachelor degree is required."]
        still_knocked = [p for p in preferred if qualification.knockouts(p, set())]
        check("a credential the posting calls OPTIONAL never becomes a knockout",
              not still_knocked, f"leaked={still_knocked}")
        check("  ...while the same sentence without the concession still knocks out",
              qualification.knockouts("A PhD in statistics is required.", set()) == ["a doctorate"], "")
        check("  ...and a bank that holds the credential is never knocked out by it",
              qualification.knockouts("A PhD in statistics is required.", {"phd"}) == [], "")

        # (3) A Greenhouse detail fetch that fails sets jd_text="" -- and an empty
        # posting used to come back tagged 'fit', which is the tool at its most
        # confident exactly where it knows least.
        a = qualification.assess("Senior Software Engineer", "", ex_bank, prof5)
        check("an unreadable posting is 'unverified', never 'fit'",
              a.fit == "unverified" and a.delta == 0 and a.cap is None, f"{a.fit} {a.delta}")
        # "we could not read it" and "we read it and it said nothing" both end at
        # unverified, but they are different problems and the row has to say which.
        check("  ...and says the body could not be read, not that it said nothing",
              any("could not be read" in r for r in a.reasons), f"{a.reasons}")
        # ...and a posting that is all marketing gets no cap, because the
        # denominator would be company prose no resume would ever match.
        prose = ("About us\n\nWe are a mission-driven company reimagining the future of "
                 "hospitality through delightful guest journeys, artisanal sourcing and "
                 "regenerative agriculture across our boutique properties.\n")
        a = qualification.assess("Senior Software Engineer", prose, ex_bank, prof5)
        check("a posting with no requirements section is never capped on its marketing copy",
              a.cap is None and a.fit == "unverified", f"cap={a.cap} fit={a.fit} {a.reasons}")
        check("'unverified' is a real state the dashboard knows how to draw",
              "unverified" in qualification.FITS
              and "unverified:" in open(os.path.join(ROOT, "dashboard/index.html")).read(), "")

        # (4) --per-company is a promise about the table, not just the console.
        # jobs.json was being written from the whole 2xN candidate pool.
        src = open(os.path.join(ROOT, "dashboard/job_scout.py")).read()
        check("jobs.json is written from the reported top N, not the candidate pool",
              "flat.extend(top)" in src and "flat.extend(candidates)" not in src, "")

        # 4. Grading math: a known panel must produce the documented numbers.
        print("\n4. panel aggregation math")
        def persona(score, vote):
            keys = ["quantified_impact_credibility", "keyword_requirement_coverage",
                    "experience_domain_relevance", "target_employer_convention_fit",
                    "structure_clarity_execution"]
            return {"dimensions": {k: {"score": score} for k in keys},
                    "would_interview": vote, "reason": "smoke"}
        panel = {"company": "Example Co",
                 "personas": {"hiring_manager": persona(80, True),
                              "recruiter": persona(80, True),
                              "ai_systems_rep": persona(80, True)}}
        ppath = os.path.join(ws, "panel.json")
        json.dump(panel, open(ppath, "w"))
        p = run([os.path.join(ROOT, "skills/resume-grader/scripts/aggregate.py"), ppath])
        ok = p.returncode == 0
        check("aggregate.py runs", ok, (p.stderr or "")[-160:].strip())
        if ok:
            try:
                agg = json.loads(p.stdout)
                # vote-coupling: a would-interview persona floors at 85
                check("vote-coupling floors a yes-vote persona at 85",
                      agg.get("panel_avg", 0) >= 85, f"panel_avg={agg.get('panel_avg')}")
                check("counts all three interview votes",
                      agg.get("interview_votes") == 3, f"votes={agg.get('interview_votes')}")
                check("non-reach employer passes at >= 70",
                      agg.get("overall_pass") is True, f"pass={agg.get('overall_pass')} thr={agg.get('threshold')}")
            except json.JSONDecodeError as e:
                check("aggregate.py emits valid JSON", False, str(e))

        # 5. The dashboard serves, and the Start Here walkthrough tracks progress.
        print("\n5. dashboard + Start Here walkthrough")
        import urllib.request
        env = dict(os.environ, RECRUIT_HOME=ws, RECRUIT_DASH_PORT="8894")
        srv = subprocess.Popen([PY, os.path.join(ROOT, "dashboard/server.py")],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import time
            time.sleep(1.5)
            def get(path):
                with urllib.request.urlopen(f"http://127.0.0.1:8894{path}", timeout=10) as r:
                    return json.loads(r.read())
            setup = get("/api/setup")
            check("/api/setup responds", isinstance(setup.get("steps"), list),
                  f"{setup.get('done')}/{setup.get('total')} steps done")
            check("walkthrough sees the built resume",
                  any(s["n"] == 4 and s["done"] for s in setup.get("steps", [])), "")
            alld = get("/api/all")
            check("/api/all responds with every tab's data", len(alld) >= 5, f"{len(alld)} sections")
            for tab in ("setup", "jobs", "goals", "resumes", "system"):
                check(f"  tab data: {tab}", tab in alld, "")
            with urllib.request.urlopen("http://127.0.0.1:8894/", timeout=10) as r:
                html = r.read().decode()
            check("index.html serves with Start Here", "Start Here" in html, "")

            # The dashboard is a single <script>. One undefined name in it is a
            # ReferenceError at evaluation time, so nav() and load() never run and
            # the page sits on "loading..." forever -- while every /api/* endpoint
            # still answers 200 and every check above still passes. That shipped.
            dispatch = re.search(r"const R=\{([^}]*)\}", html)
            refs = re.findall(r":\s*(render[A-Za-z]+)", dispatch.group(1)) if dispatch else []
            defined = set(re.findall(r"function\s+(render[A-Za-z]+)", html))
            missing = [r for r in refs if r not in defined]
            check("every tab renderer the dashboard dispatches actually exists",
                  bool(refs) and not missing, f"referenced={refs} missing={missing or 'none'}")

            calls = set(re.findall(r"\b([a-zA-Z_$][\w$]*)\s*\(", html))
            declared = (set(re.findall(r"function\s+([a-zA-Z_$][\w$]*)", html))
                        | set(re.findall(r"(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*=", html)))
            builtins = {"if", "for", "while", "switch", "catch", "return", "fetch", "String",
                        "Number", "Object", "Array", "Date", "JSON", "parseInt", "parseFloat",
                        "document", "console", "setTimeout", "Math", "isNaN", "encodeURIComponent"}
            undefined = sorted(c for c in calls
                               if c.startswith("render") and c not in declared and c not in builtins)
            check("no undefined render function is called anywhere in the page",
                  not undefined, f"undefined={undefined}")
        finally:
            srv.terminate()
            srv.wait(timeout=10)
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'=' * 56}")
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFAILED:")
        for _, name, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    print("All good. A fresh clone works end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
