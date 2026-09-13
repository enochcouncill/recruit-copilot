#!/usr/bin/env python3
"""Can you actually get this job?

The title/seniority/location/pay scorer answers "is this the kind of role I want?"
It has no idea who you are, so it will happily hand a second-year MBA with five
years of experience a 99 on a Corporate Development *Lead* at $425-600k, and a 93
on a *Principal* Product Manager. Both are the right shape and neither is a job
he can get. A ranked list whose top rows are unreachable is worse than no list:
it spends the scarce thing, attention, on the applications least likely to land.

So this module reads the posting against the experience bank the user already
confirmed at intake, and returns a score adjustment plus the plain-English reason
for it. Four checks, in order of how confidently a machine can judge them:

  1. Years required   - the posting states a number; you have a number. Arithmetic.
  2. Level ceiling    - a title word (Director, VP, Principal, Head of) that sits
                        above where you are. Read off the title, no JD needed.
  3. Hard knockouts   - a credential the posting says is required and the bank
                        does not contain (PhD, CPA, bar admission, clearance).
  4. Must-have overlap- how much of what the requirements section actually asks
                        for appears anywhere in your bank. Weakest signal of the
                        four, so it caps the score rather than subtracting.

Everything here is deterministic string work on the standard library. No model is
called, nothing leaves the machine, and every number it produces comes with the
sentence that explains it.

Deliberately NOT here: anything that guesses. If the posting does not state a
years requirement, no years penalty is applied. If it has no parseable
requirements, no overlap penalty is applied. An unstated requirement is not a
failed one -- the same rule the pay scorer already follows.

And when a posting cannot be read at all, or reads as nothing checkable, the
answer is `unverified` rather than `fit`. "I found no problem" and "I could not
look" are different sentences, and only one of them should be reassuring.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

# UNVERIFIED is not a fourth severity, it is the absence of one: the posting body
# could not be read, or could be read and said nothing checkable. It exists
# because the alternative is worse -- a failed Greenhouse fetch used to come back
# tagged "fit", so the label the user trusts most was strongest exactly where the
# tool knew least. Silence has to look like silence.
FIT, STRETCH, UNQUALIFIED, UNVERIFIED = "fit", "stretch", "unqualified", "unverified"
FITS = (FIT, STRETCH, UNQUALIFIED, UNVERIFIED)
_RANK = {UNVERIFIED: -1, FIT: 0, STRETCH: 1, UNQUALIFIED: 2}


class Assessment(NamedTuple):
    """What the qualification pass decided.

    NOTE ON THE SHAPE: this is a 4-field tuple, not the (delta, reasons, fit)
    triple the brief sketched. The overlap check produces a CEILING, not a
    subtraction -- "cap this at 60" and "subtract 39" are different claims, and
    only the first one survives a base score that moves. Folding the cap into
    delta would have made the number depend on a base score `assess` does not
    see. The first three fields are in the order the brief named them.
    """
    delta: int
    reasons: List[str]
    fit: str
    cap: Optional[int] = None


def apply(base_score: int, a: "Assessment") -> int:
    """The final 0-100 score: base + delta, then the cap, then clamped."""
    s = base_score + a.delta
    if a.cap is not None:
        s = min(s, a.cap)
    return max(0, min(100, int(s)))


# --------------------------------------------------------------------------
# 1. Years of experience the posting asks for
# --------------------------------------------------------------------------
# Word forms show up often enough to matter ("a minimum of five years").
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "fifteen": 15, "twenty": 20}
_NUM = r"(\d{1,2}|" + "|".join(_WORD_NUM) + r")"
_DASH = r"(?:-|–|—|to|through)"
# "years" / "yrs" / "yrs." / "year's" / "years'"
_YEARS = r"(?:years?|yrs?\.?)['’]?"

# Ranges first ("7-10 years", "3 to 5 yrs") -- the MINIMUM is the first number.
_RE_RANGE = re.compile(rf"(?<![\d.$]){_NUM}\s*{_DASH}\s*{_NUM}\s*\+?\s*{_YEARS}", re.I)
# Explicit floors ("minimum of 8 years", "at least 6 yrs", "no fewer than 4 years").
_RE_FLOOR = re.compile(
    rf"(?:minimum(?:\s+of)?|min\.?(?:\s+of)?|at\s+least|no\s+less\s+than|no\s+fewer\s+than|"
    rf"a\s+minimum\s+of|over|more\s+than)\s+(?:approximately\s+)?{_NUM}\s*\+?\s*{_YEARS}", re.I)
# Bare statements ("5+ years", "8 years of experience", "5 or more years").
_RE_PLAIN = re.compile(rf"(?<![\d.$]){_NUM}\s*(?:\+|or\s+more|or\s+greater|plus)?\s*{_YEARS}", re.I)
# The one singular form worth taking, so "4-year degree" can never be read as a
# four-year experience requirement.
_RE_SINGULAR = re.compile(rf"(?<![\d.$-]){_NUM}\s+year\s+of\s+(?:\w+\s+){{0,3}}experience", re.I)

_EXPERIENCE_NEAR = re.compile(r"experien", re.I)
# How far from the number the word "experience" may sit and still be about it.
_BEFORE, _AFTER = 60, 90


def _num(tok: str) -> Optional[int]:
    t = tok.strip().lower()
    if t.isdigit():
        return int(t)
    return _WORD_NUM.get(t)


def _is_about_experience(text: str, start: int, end: int) -> bool:
    """A number of years only counts when the posting is talking about experience.

    Without this, "trusted by teams for 10 years" and "a 4-year degree" both read
    as requirements. Requiring the word nearby is strict on purpose: the failure
    direction of a missed requirement is a job that stays scored as-is, and the
    failure direction of a false one is a job the user never sees.
    """
    return bool(_EXPERIENCE_NEAR.search(text[max(0, start - _BEFORE):end + _AFTER]))


def _year_matches(text: str) -> List[Tuple[int, int, int]]:
    """Every (years, start, end) the text states about experience."""
    out: List[Tuple[int, int, int]] = []
    for rx in (_RE_RANGE, _RE_FLOOR, _RE_SINGULAR, _RE_PLAIN):
        for m in rx.finditer(text or ""):
            v = _num(m.group(1))
            if v is None or not (0 <= v <= 40):
                continue
            if _is_about_experience(text, m.start(), m.end()):
                out.append((v, m.start(), m.end()))
    return out


def years_required(jd_text: str) -> Optional[int]:
    """The smallest number of years of experience the TEXT GIVEN states, or None.

    Smallest, not largest, on purpose: postings routinely carry both a required
    floor and a higher "preferred" one, and the user is entitled to be judged
    against the floor.

    This is the parser, not the decision. It reads any experience-years phrase in
    whatever you hand it, so handing it a whole posting will read the company's
    own tenure ("our team has 12 years of experience serving customers") as a
    requirement. `required_years` is the function that decides; call that.
    """
    found = [v for v, _s, _e in _year_matches(jd_text or "")]
    return min(found) if found else None


# Requirement language. A years phrase outside the requirements section only
# counts when one of these sits beside it -- otherwise the About-us paragraph
# bragging about a decade in business becomes a ten-year requirement, and the
# tool tells the user they are unqualified on the strength of a marketing line.
_REQ_ANCHOR = re.compile(
    r"require|must\s+have|must\s+possess|minimum|min\.|at\s+least|qualificat"
    r"|you\s+(?:have|bring|are)|you(?:'|’)?(?:ll|\s+will)\s+(?:have|bring|need)"
    r"|looking\s+for|preferred|proven\s+track", re.I)
_ANCHOR_WINDOW = 120


def required_years(jd_text: str, requirements_text: Optional[str] = None) -> Optional[int]:
    """The years the posting REQUIRES, as opposed to any years it mentions.

    Inside an extracted requirements section, every stated number is a
    requirement by construction, so the parser runs unguarded. With no section to
    read, each candidate has to be anchored by requirement language nearby or it
    does not count -- a posting that only talks about itself must produce no
    requirement at all, not a made-up one.
    """
    if requirements_text and requirements_text.strip():
        return years_required(requirements_text)
    text = jd_text or ""
    found = [v for v, s, e in _year_matches(text)
             if _REQ_ANCHOR.search(text[max(0, s - _ANCHOR_WINDOW):e + _ANCHOR_WINDOW])]
    return min(found) if found else None


# --------------------------------------------------------------------------
# 2. Level ceiling
# --------------------------------------------------------------------------
DEFAULT_LEVEL_CEILING = [
    "director", "vp", "vice president", "head of", "principal", "staff",
    "distinguished", "chief", "partner", "gm", "general manager",
]

# Phrases where a ceiling word is part of a title that is NOT above the user.
# Blanked out of the title before matching, so "Chief of Staff" loses both its
# "chief" and its "staff". Kept short and explicit -- a long list here is a
# rulebook nobody can reason about.
_CEILING_EXCEPTIONS = (
    "chief of staff",        # the flagship case: a target title, not a ceiling
    "staff accountant",      # "staff X" is junior in finance, senior in engineering
    "staff assistant",
    "partner manager", "partner marketing", "partner operations",
    "partner success", "partnership", "partnerships",
)

_LEAD_MODIFIER = re.compile(r"(?:^|[-–,(/:]\s*)lead\s+\w", re.I)


def _strip_exceptions(title_lc: str) -> str:
    out = title_lc
    for phrase in _CEILING_EXCEPTIONS:
        if phrase in out:
            out = out.replace(phrase, " " * len(phrase))
    return out


def level_ceiling_hits(title: str, ceiling: Sequence[str]) -> List[str]:
    """Ceiling words present in the title, after the exceptions are removed."""
    t = _strip_exceptions((title or "").lower())
    hits = []
    for word in ceiling:
        w = (word or "").strip().lower()
        if not w:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", t):
            hits.append(w)
    return hits


def lead_is_seniority_modifier(title: str) -> bool:
    """True for "Lead Product Manager", false for "Team Lead" / "Technical Lead".

    "Lead" in front of a role noun is usually a senior-IC band a couple of steps
    up. "Lead" as the final noun is often just who runs a squad. The difference
    is whether it modifies something, so that is what we test -- and even then it
    only costs points when the posting ALSO asks for years the user does not have.
    """
    return bool(_LEAD_MODIFIER.search(title or ""))


# --------------------------------------------------------------------------
# 3. Hard knockouts
# --------------------------------------------------------------------------
# (label, requirement pattern, proofs the bank holds the credential)
# Small and explicit on purpose. Every entry here can end a job's chances, so
# each one has to be a phrase a posting only writes when it means it.
#
# Each pattern is matched against ONE CLAUSE at a time, never across a sentence.
# The gap-tolerant version ("PhD" ... within 40 characters ... "required") read
# "PhD preferred; a master's degree is required" as a doctorate requirement --
# the posting's own concession that a doctorate is optional became the reason it
# was treated as mandatory.
#
# A proof is an ALL-OF group of bank terms: ("juris", "doctor") matches a bank
# containing both tokens. Set membership, not substring search on a joined blob,
# because "cpa" is a substring of a dozen innocent words.
_KNOCKOUTS: Tuple[Tuple[str, "re.Pattern[str]", Tuple[Tuple[str, ...], ...]], ...] = (
    ("a doctorate",
     re.compile(r"(?:phd|doctoral degree|doctorate).{0,40}\b(?:is\s+)?(?:required|mandatory)\b"
                r"|(?:must have|requires?|require)\s+(?:an?\s+)?(?:phd|doctorate)", re.I),
     (("phd",), ("doctorate",), ("doctoral",))),
    ("a professional license",
     re.compile(r"must be (?:a\s+)?(?:currently\s+)?licensed|licensure is required|"
                r"(?:current|active|valid)\s+(?:state\s+)?licens(?:e|ure)\s+(?:is\s+)?required", re.I),
     (("licensed",), ("licensure",), ("license",))),
    ("an active security clearance",
     re.compile(r"(?:active|current)\s+(?:ts\s*/\s*sci|top\s+secret|secret|dod|security)\s*"
                r"clearance\b.{0,40}\b(?:required|mandatory)\b"
                r"|(?:security|ts\s*/\s*sci)\s*clearance\s+(?:is\s+)?(?:required|mandatory)"
                r"|must (?:have|possess|hold)\s+(?:an?\s+)?(?:active|current)?\s*"
                r"(?:ts\s*/\s*sci|security\s+clearance|clearance)", re.I),
     (("clearance",), ("sci",), ("secret",))),
    ("a CPA",
     re.compile(r"\bcpa\b.{0,30}\b(?:required|mandatory)\b|must be a (?:licensed )?cpa"
                r"|(?:active|current)\s+cpa\b", re.I),
     (("cpa",),)),
    ("a law degree",
     re.compile(r"\b(?:jd|juris doctor|law degree)\b.{0,30}\b(?:required|mandatory)\b"
                r"|(?:member(?:ship)? (?:in good standing )?of|admitted to) the\s+\w*\s*bar\b"
                r"|active bar (?:membership|admission)", re.I),
     (("juris", "doctor"), ("law", "degree"), ("bar", "admission"), ("attorney",))),
)

# "Ability to obtain a security clearance" is a hiring promise, not a knockout;
# so is "eligible to obtain". Anduril writes one of these on nearly every
# posting, and reading it as a hard requirement would delete an entire board.
_OBTAINABLE = re.compile(r"(?:able|ability|eligib\w*|willing\w*)\s+to\s+(?:obtain|acquire|receive)", re.I)

# The credentials whose own names contain the character we split clauses on.
# Normalising them first is what lets the splitter be as blunt as it should be.
_ABBREV = ((re.compile(r"\bph\.?\s*d\.?", re.I), "phd"),
           (re.compile(r"\bj\.\s*d\.", re.I), "jd"),
           (re.compile(r"\bu\.\s*s\.", re.I), "us"))
_CLAUSE_SPLIT = re.compile(r"[.;,\n]")


def _clauses(text: str) -> List[str]:
    """The posting, one clause at a time. A requirement and its credential have to
    live in the same breath to mean each other."""
    t = text or ""
    for rx, repl in _ABBREV:
        t = rx.sub(repl, t)
    return [c for c in (part.strip() for part in _CLAUSE_SPLIT.split(t)) if c]


def knockouts(jd_text: str, bank_terms: Iterable[str]) -> List[str]:
    """Credentials the posting says it requires that the bank does not show."""
    have = set(bank_terms)
    clauses = _clauses(jd_text)
    out = []
    for label, rx, proofs in _KNOCKOUTS:
        if any(all(tok in have for tok in group) for group in proofs):
            continue                                  # the bank holds it
        for clause in clauses:
            if rx.search(clause) and not _OBTAINABLE.search(clause):
                out.append(label)
                break
    return out


# --------------------------------------------------------------------------
# 4. Must-have overlap
# --------------------------------------------------------------------------
_REQ_HEADINGS = re.compile(
    r"^(?:"
    r"(?:minimum|basic|required|preferred|desired|key|core|ideal|essential|additional)?\s*"
    r"(?:qualifications?|requirements?|characteristics?|competencies|skills?(?:\s+and\s+experience)?)"
    r"|what\s+(?:you(?:'?ll|\s+will|\s+would)?\s+(?:bring|need|have)|we(?:'?re|\s+are)\s+looking\s+for|"
    r"we\s+(?:want|need|look\s+for)|makes\s+you\s+a\s+(?:great|strong)\s+fit)"
    r"|who\s+you\s+are|about\s+you|your\s+(?:background|experience|profile)"
    r"|you(?:'?ll)?\s+(?:have|bring|need)|must[\s-]?haves?|you\s+are"
    r"|nice[\s-]to[\s-]haves?|bonus\s+\w+|even\s+better|experience\s+(?:and|&)\s+skills"
    r"|the\s+ideal\s+candidate"
    r"|ideally,?\s+you(?:'?d)?\s+(?:have|would\s+have|will\s+have)"
    # "You might thrive in this role if you:", "You are likely to succeed if you have:"
    r"|.{0,60}\bif\s+you\b.{0,30}"
    r")$", re.I)

_BULLET = re.compile(r"^\s*(?:[-–—•*●▪‣·]|\(?\d{1,2}[.)])\s")


def _is_heading(line: str) -> bool:
    """Does this line announce a section, rather than belong to one?

    This is the hinge of the whole overlap check, and the naive version -- "short
    line" -- silently broke it: a requirements bullet is also a short line, so the
    section closed on its own first item and every posting looked structureless.
    A heading is short, is not a bullet, does not end in sentence punctuation, and
    either ends in a colon or is cased like a title.
    """
    s = (line or "").strip()
    if not s or len(s) > 80 or _BULLET.match(s):
        return False
    words = s.split()
    if len(words) > 10:
        return False
    if s.endswith(":"):
        return True
    if s.endswith((".", ",", ";", "!", "?")):
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    if sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:   # ALL CAPS
        return True
    caps = sum(1 for w in words if w[:1].isupper())
    return len(words) <= 8 and caps >= max(1, len(words) - 3)          # Title Case

_STOP_HEADINGS = re.compile(
    r"^(?:about\b.*|benefits?\b.*|compensation\b.*|salary\b.*|pay\b.*|perks?\b.*|"
    r"what\s+we\s+offer.*|why\s+(?:join|work).*|our\s+(?:values|culture|mission|team).*|"
    r"equal\s+.*|eeo\b.*|e-?verify.*|privacy.*|legal.*|how\s+we\s+hire.*|"
    r"interview\s+process.*|application\s+process.*|life\s+at\b.*|diversity.*|"
    r"accommodations?.*|please\s+.*|note\s*:?.*|disclaimer.*|location\b.*|"
    r"annual\s+salary.*|the\s+expected\s+.*)$", re.I)


def _normalize_heading(line: str) -> str:
    s = line.strip().replace("’", "'").replace("‘", "'")
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)          # "Core Competencies (Who You Are)"
    s = re.sub(r"^[^A-Za-z]+|[^A-Za-z']+$", "", s)  # bullets, colons, dashes
    return re.sub(r"\s+", " ", s).strip().lower()


def requirements_section(jd_text: str) -> str:
    """The part of the posting that states what it wants, or "" if it says nothing
    a machine can find. Falling back to the whole JD is the caller's decision, so
    that the caller can tell the two cases apart."""
    kept: List[str] = []
    inside = False
    for line in (jd_text or "").split("\n"):
        if _is_heading(line):
            norm = _normalize_heading(line)
            if norm and _REQ_HEADINGS.match(norm):
                # Postings routinely run Minimum then Preferred back to back.
                inside = True
                continue
            if inside and (_STOP_HEADINGS.match(norm) or not line.strip().endswith(":")):
                # A new section ends this one. A sub-header inside it
                # ("Technical skills:") does not.
                inside = False
                continue
        if inside:
            kept.append(line)
    return "\n".join(kept).strip()


# Words that appear in every posting and in every resume, so their overlap says
# nothing. Removing them from BOTH sides keeps the ratio honest; leaving them in
# makes every job look like a 40% match.
_STOPWORDS = set("""
a an and are as at be been being but by can could did do does doing done for from had has
have having he her hers him his how if in into is it its itself me more most my myself no
nor not of off on once only or other our ours out over own same she should so some such
than that the their theirs them themselves then there these they this those through to too
under until up very was we were what when where which while who whom why will with would
you your yours yourself
able about across after again against all along already also always among amount another any
anyone around because become becomes before behind below best better between both bring
during each either else enough etc even ever every everyone few first following further get
gets give given going great help here high highly however include includes including intoa
just keep know large last less like look made make makes many may might much must near need
needs never new next non none often one ongoing others overall part per perhaps plus quite
rather really right said same see seen several since small still strong take taken team teams
teamwork thing things think those three throughout thus together toward towards true two upon
use used uses using usual usually want ways well what whether within without work working works
year years yearly yrs
ability applicant applicants apply candidate candidates career careers company companies
compensation employee employees employer employment equal experience experienced hire hiring
job jobs offer opportunity position positions qualification qualifications qualified
requirement requirements required requires responsibility responsibilities role roles salary
skill skills benefits bonus equity insurance paid leave vacation holiday health dental vision
level senior junior lead staff principal director manager management
please note also thank thanks welcome join joining looking forward excited exciting passion
passionate mission vision values culture environment fast paced dynamic growing world class
""".split())

_ALLCAPS = re.compile(r"\b([A-Z][A-Z0-9/+.#]{1,7})\b")
_CAP_PHRASE = re.compile(r"\b([A-Z][a-zA-Z0-9+.#]*(?:\s+[A-Z][a-zA-Z0-9+.#]*){1,2})\b")
_TOKEN = re.compile(r"[a-z][a-z0-9+#.]{2,}")


def _tokens(text: str) -> List[str]:
    return [t.strip(".") for t in _TOKEN.findall((text or "").lower())]


def _notable(text: str, min_len: int = 4) -> set:
    """Tokens worth comparing: long enough to mean something, not boilerplate."""
    return {t for t in _tokens(text) if len(t) >= min_len and t not in _STOPWORDS}


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


def bank_terms(bank: Optional[dict]) -> set:
    """Everything the user's confirmed experience actually says, as comparable terms.

    Skills are listed comma-separated, so each comma-separated value is kept whole
    as well as split -- otherwise "power bi" is only ever two useless tokens. The
    bullets, titles and summaries supply the rest of the vocabulary: a person who
    ran enterprise implementations has "implementation", "go-live" and "onboarding"
    in their bank whether or not they put them in a skills line.
    """
    bank = bank or {}
    terms: set = set()

    def add_phrase(p: str) -> None:
        p = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", p or "")).strip().lower()
        p = p.strip(" .,;:-")
        if not p:
            return
        if " " in p and len(p) <= 40:
            terms.add(p)
        terms.update(_notable(p, min_len=2 if " " in p else 4))

    for section in _walk_strings(bank.get("skills_pool") or {}):
        for chunk in re.split(r"[,;/]", section):
            add_phrase(chunk)

    for job in bank.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        add_phrase(job.get("title") or "")
        for variant in _walk_strings(job.get("title_variants") or []):
            add_phrase(variant)
        for bullet in _walk_strings(job.get("bullets") or {}):
            terms |= _notable(bullet)
    for s in _walk_strings(bank.get("summaries") or {}):
        terms |= _notable(s)
    for block in ("projects", "leadership", "education"):
        for s in _walk_strings(bank.get(block) or []):
            terms |= _notable(s)
    return terms


def jd_terms(requirements_text: str) -> set:
    """What the posting is asking for, as comparable terms."""
    text = requirements_text or ""
    terms = _notable(text)
    for m in _ALLCAPS.finditer(text):           # SQL, API, GTM, BI, SaaS
        tok = m.group(1).lower().strip(".")
        if tok not in _STOPWORDS and len(tok) >= 2:
            terms.add(tok)
    for m in _CAP_PHRASE.finditer(text):        # Power BI, Google Cloud, Salesforce
        phrase = re.sub(r"\s+", " ", m.group(1)).strip().lower()
        parts = phrase.split()
        if len(phrase) <= 40 and not all(p in _STOPWORDS for p in parts):
            terms.add(phrase)
    return terms


class Overlap(NamedTuple):
    ratio: Optional[float]
    matched: List[str]
    considered: int


def overlap(requirements_text: str, terms: set) -> Overlap:
    """How much of what the posting asks for the bank can answer.

    ratio is None when there is not enough of a requirements section to judge --
    which is a refusal to score, not a zero.
    """
    jd = jd_terms(requirements_text)
    if len(jd) < 5:
        return Overlap(None, [], len(jd))
    matched = sorted(t for t in jd if t in terms)
    return Overlap(len(matched) / len(jd), matched, len(jd))


# --------------------------------------------------------------------------
# The pass itself
# --------------------------------------------------------------------------
# Chosen so the checks compose to something a person would recognise. A 99 that
# is four years short of the stated floor lands at 64 -- below every genuine fit,
# still visible if the whole board is a reach. A stretch lands in the 70s-80s,
# which is what "worth an application if you want it" should look like.
YEARS_STRETCH_PENALTY = -15      # 2-3 years over your experience
YEARS_UNQUALIFIED_PENALTY = -35  # 4+ years over
LEVEL_PENALTY = -20              # Director / VP / Principal / Head of
LEAD_PENALTY = -10               # "Lead <role>", and only alongside a years gap
KNOCKOUT_PENALTY = -40           # a credential you do not have
OVERLAP_CAP = 60                 # few of the required skills appear in your bank
OVERLAP_MIN_RATIO = 0.15
OVERLAP_MIN_TERMS = 5


def _raise_fit(current: str, candidate: str) -> str:
    return candidate if _RANK[candidate] > _RANK[current] else current


def assess(title: str, jd_text: str, bank: Optional[dict],
           profile: Optional[dict]) -> Assessment:
    """Score adjustment, reasons, and a fit tag for one posting.

    `profile` is the user's own block from goals.json. A missing profile disables
    the two checks that need to know who they are (years, level) -- scoring a
    stranger's search against somebody else's seniority is the exact failure the
    goals file exists to prevent. The bank-driven checks still run, because those
    read only from files the user confirmed themselves.

    Returns fit=`unverified` when the posting body is empty (a failed fetch) or
    no check could reach any evidence in it.
    """
    profile = profile or {}
    reasons: List[str] = []
    delta = 0
    fit = FIT
    cap: Optional[int] = None
    jd_text = jd_text or ""
    # Did any check actually get to look at evidence? A clean "fit" has to mean
    # "I read the requirements and you meet them", never "I had nothing to read".
    checked = False

    section = requirements_section(jd_text)
    if not jd_text.strip():
        return Assessment(0, ["the posting body could not be read, so nothing was checked"],
                          UNVERIFIED, None)

    # -- 1. years ----------------------------------------------------------
    mine = profile.get("years_experience")
    years_gap = 0
    try:
        mine = int(mine) if mine is not None else None
    except (TypeError, ValueError):
        mine = None
    if mine is not None:
        asked = required_years(jd_text, section)
        if asked is not None:
            checked = True
            years_gap = asked - mine
            if years_gap >= 4:
                delta += YEARS_UNQUALIFIED_PENALTY
                fit = _raise_fit(fit, UNQUALIFIED)
                reasons.append(f"asks for {asked}+ years of experience; your bank shows {mine}")
            elif years_gap >= 2:
                delta += YEARS_STRETCH_PENALTY
                fit = _raise_fit(fit, STRETCH)
                reasons.append(f"asks for {asked}+ years of experience; your bank shows {mine} — a stretch")
            # within +1 is noise, not a gap, and says nothing worth printing

    # -- 2. level ceiling --------------------------------------------------
    ceiling = profile.get("level_ceiling")
    if profile and ceiling is None:
        ceiling = DEFAULT_LEVEL_CEILING
    if profile and ceiling:
        hits = level_ceiling_hits(title, ceiling)
        if hits:
            if fit != UNQUALIFIED:
                delta += LEVEL_PENALTY
                fit = _raise_fit(fit, STRETCH)
            reasons.append(f"title is a level above you ({', '.join(hits)})")
        elif years_gap >= 2 and lead_is_seniority_modifier(title):
            # "Lead" on its own is ambiguous. Paired with a years requirement the
            # user does not meet, it is the senior-IC reading.
            delta += LEAD_PENALTY
            fit = _raise_fit(fit, STRETCH)
            reasons.append("'Lead' here reads as a senior-IC band, not a team lead")

    # -- 3 and 4 both read the bank. With no bank there is nothing to compare
    # against, and an empty term set would read as "you have none of these
    # skills" for every posting on earth -- so the bank-driven checks stand down
    # rather than firing on an absence of evidence.
    terms = bank_terms(bank)

    # -- 3. hard knockouts -------------------------------------------------
    if terms:
        missing = knockouts(jd_text, terms)
        if missing:
            checked = True
            delta += KNOCKOUT_PENALTY
            fit = UNQUALIFIED
            reasons.append("requires " + " and ".join(missing) + ", which your bank does not show")

    # -- 4. must-have overlap ----------------------------------------------
    # Only ever against a REAL requirements section. Run against a whole posting
    # the denominator is company prose -- an About-us paragraph, a benefits list,
    # a scam warning -- none of which a resume would ever match, so a fit gets
    # capped for failing to resemble marketing copy. Ratios from the two sources
    # are not even on the same scale (0.12-0.22 whole-posting against 0.21-0.59
    # section-only across 54 real postings), so one threshold cannot serve both.
    if terms and section:
        ov = overlap(section, terms)
        if ov.ratio is not None:
            checked = True
            if ov.ratio < OVERLAP_MIN_RATIO and ov.considered >= OVERLAP_MIN_TERMS:
                cap = OVERLAP_CAP
                fit = _raise_fit(fit, STRETCH)
                shown = ", ".join(ov.matched[:6]) if ov.matched else "none"
                reasons.append(f"few required skills appear in your bank (matched: {shown})")

    if not checked and fit == FIT:
        # We read the posting and it told us nothing we could test against. That
        # is not the same as passing, and must not be dressed up as one.
        return Assessment(delta, reasons + [
            "no stated requirements could be read from this posting, so nothing was verified"],
            UNVERIFIED, cap)

    return Assessment(delta, reasons, fit, cap)


if __name__ == "__main__":  # pragma: no cover - a hand probe, not a test suite
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--title", required=True)
    ap.add_argument("--jd", help="path to a file holding the posting text")
    ap.add_argument("--bank", help="path to master-experience.json")
    ap.add_argument("--years", type=int, default=None)
    a = ap.parse_args()
    jd = open(a.jd).read() if a.jd else ""
    bank = json.load(open(a.bank)) if a.bank else {}
    prof = {"years_experience": a.years} if a.years is not None else {}
    res = assess(a.title, jd, bank, prof)
    section = requirements_section(jd)
    json.dump({"delta": res.delta, "fit": res.fit, "cap": res.cap,
               "reasons": res.reasons,
               "years_required": required_years(jd, section),
               "requirements_section_found": bool(section)},
              sys.stdout, indent=2)
    print()
