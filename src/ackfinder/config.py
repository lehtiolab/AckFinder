"""Config loading, defaults, validation, `.env` loading, and building a config from plain answers.

The config is one YAML file (normally `config.yaml`, made from `config.example.yaml`). Relative paths
in it are resolved against the folder that holds the config file, so the tool can be run from anywhere.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV_VAR = "ACKFINDER_CONFIG"
TEMPLATE_NAME = "config.example.yaml"
ENV_TEMPLATE_NAME = ".env.example"
API_ENV_VARS = ("OPENALEX_API_KEY", "NCBI_API_KEY", "NCBI_EMAIL")

REQUIRED = ("facility_label", "canonical_statement", "search_phrases", "facility_regex")
PATH_KEYS = ("output_dir", "cache_dir", "pdf_dir", "jif_table", "manual_decisions")
REGEX_KEYS = ("facility_regex", "funding_regex", "context_regex", "other_facility_regex")
SCOPE_REGEX_KEYS = ("in_scope_regex", "out_of_scope_regex", "out_of_scope_facility_regex")
SCOPE_REQUIRED = ("column", "in_scope_label", "out_of_scope_label", "in_scope_regex", "out_of_scope_regex")

DEFAULTS: dict[str, Any] = {
    "report_slug": None,                 # derived from facility_label when empty
    "openalex_phrases": None,            # default: search_phrases
    "funding_regex": "", "funding_label": "", "context_regex": "", "context_label": "",
    "other_facility_regex": "",
    "scope_check": {"enabled": False},
    "facility_staff": [], "requesters": [],
    "affiliation_phrases": [], "openalex_affiliation_phrases": None,
    "output_dir": "reports", "cache_dir": "cache", "pdf_dir": "pdfs",
    "years": [], "year_basis": "first", "jif_table": None, "manual_decisions": None,
    "canonical_threshold": 0.85, "word_expansions": {}, "other_doi_prefixes": {},
}

# Keys that older configs may still carry: ignored, with this explanation.
REMOVED_KEYS = {"include_facility_authors": "author searches now run whenever facility_staff, requesters or "
                                            "affiliation_phrases has entries"}
PEOPLE_KEYS = ("facility_staff", "requesters")

# Text that marks a value as not filled in yet (the template uses these).
PLACEHOLDER_RX = re.compile(r"CHANGE[_ ]ME|<[A-Z][A-Z _/-]+>")


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- locating files
def find_config(explicit: Path | None = None) -> Path:
    """--config argument > $ACKFINDER_CONFIG > ./config.yaml."""
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get(CONFIG_ENV_VAR):
        return Path(os.environ[CONFIG_ENV_VAR]).expanduser()
    return Path.cwd() / "config.yaml"


def template_text(name: str = TEMPLATE_NAME) -> str:
    """A bundled template (config.example.yaml or .env.example).

    Installed packages carry them in ackfinder/templates/; a source checkout (editable
    install) reads them from the repository root.
    """
    here = Path(__file__).resolve().parent
    for p in (here / "templates" / name, here.parents[1] / name):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(f"template {name} not found next to the package")


def load_dotenv(*folders: Path) -> list[Path]:
    """Read KEY=VALUE lines from `.env` in the given folders. Existing environment variables win.

    Returns the files read. Values are never logged.
    """
    read = []
    for folder in dict.fromkeys(Path(f).resolve() for f in folders if f):
        p = folder / ".env"
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            elif " #" in val:
                val = val.split(" #", 1)[0].strip()
            if key and val and key not in os.environ:
                os.environ[key] = val
        read.append(p)
    return read


# --------------------------------------------------------------------------- loading
def slugify(label: str) -> str:
    base = re.sub(r"\(.*?\)", "", label)
    words = re.findall(r"[A-Za-z0-9]+", base)
    caps = [w for w in words if w[0].isupper()]
    short = "".join(w[0] for w in caps) if len(caps) >= 2 else "_".join(words[:3])
    return f"{short or 'facility'}_acknowledgements"


def load_config(path: Path) -> dict:
    """Read the YAML file, fill in defaults and resolve paths. Raises ConfigError if unreadable."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain key: value pairs")
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if v is not None or k in PATH_KEYS}}
    cfg["_raw_keys"] = sorted(raw)
    cfg["_set_keys"] = sorted(k for k, v in raw.items() if v not in (None, "", [], {}))
    cfg["_config_path"] = path.resolve()
    base = path.resolve().parent
    cfg["_config_dir"] = base
    for k in ("search_phrases", "affiliation_phrases", "years") + PEOPLE_KEYS:
        if isinstance(cfg.get(k), str):
            cfg[k] = [cfg[k]]
        cfg[k] = list(cfg.get(k) or [])
    if not cfg.get("report_slug"):
        cfg["report_slug"] = slugify(str(cfg.get("facility_label") or "facility"))
    if cfg.get("openalex_phrases") is None:
        cfg["openalex_phrases"] = list(cfg["search_phrases"])
    if cfg.get("openalex_affiliation_phrases") is None:
        cfg["openalex_affiliation_phrases"] = cfg["affiliation_phrases"][:1]
    sc = cfg.get("scope_check") or {}
    cfg["scope_check"] = sc if isinstance(sc, dict) else {"enabled": False}
    for k in PATH_KEYS:
        v = cfg.get(k)
        cfg[k] = (base / Path(str(v)).expanduser()).resolve() if v not in (None, "") else None
    return cfg


def validate(cfg: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors stop a run; warnings are advice."""
    errors, warnings = [], []
    for k in REQUIRED:
        v = cfg.get(k)
        if not v or (isinstance(v, list) and not any(str(x).strip() for x in v)):
            errors.append(f"`{k}` is required but empty")
    for k, v in cfg.items():
        if k.startswith("_") or k not in cfg.get("_set_keys", [k]):
            continue
        texts = v if isinstance(v, list) else [v] if isinstance(v, str) else []
        if any(PLACEHOLDER_RX.search(str(t)) for t in texts):
            errors.append(f"`{k}` still contains a placeholder (CHANGE_ME or <...>)")
    compiled = {}
    for k in REGEX_KEYS:
        if cfg.get(k):
            try:
                compiled[k] = re.compile(cfg[k], re.I)
            except re.error as e:
                errors.append(f"`{k}` is not a valid regular expression: {e}")
    sc = cfg.get("scope_check") or {}
    if sc.get("enabled"):
        for k in SCOPE_REQUIRED:
            if not sc.get(k):
                errors.append(f"`scope_check.{k}` is required when scope_check is enabled")
        for k in SCOPE_REGEX_KEYS:
            if sc.get(k):
                try:
                    re.compile(sc[k], re.I)
                except re.error as e:
                    errors.append(f"`scope_check.{k}` is not a valid regular expression: {e}")
    if cfg.get("year_basis") not in ("first", "issue"):
        errors.append("`year_basis` must be 'first' or 'issue'")
    try:
        thr = float(cfg.get("canonical_threshold"))
        if not 0 < thr <= 1:
            errors.append("`canonical_threshold` must be between 0 and 1")
    except (TypeError, ValueError):
        errors.append("`canonical_threshold` must be a number between 0 and 1")
    for y in cfg.get("years") or []:
        if not (isinstance(y, int) and 1900 < y < 2200):
            errors.append(f"`years` must be a list of years such as [2025, 2026]; got {y!r}")
    if cfg.get("jif_table") and not Path(cfg["jif_table"]).is_file():
        errors.append(f"`jif_table` points to a missing file: {cfg['jif_table']}")
    for k in PEOPLE_KEYS:
        errors += parse_people(cfg.get(k), k)[1]
    # a missing manual_decisions file is fine: it means no decisions yet
    if errors:
        return errors, warnings

    fac = compiled["facility_regex"]
    if not fac.search(" ".join(cfg["canonical_statement"].split())):
        warnings.append("`facility_regex` does not match `canonical_statement`: the reference statement itself would "
                        "not count as naming the facility")
    fund = compiled.get("funding_regex")
    for ph in cfg["search_phrases"]:
        if not (fac.search(ph) or (fund and fund.search(ph))):
            warnings.append(f"search phrase {ph!r} matches neither `facility_regex` nor `funding_regex`: papers found "
                            "only by it can never be confirmed")
    if cfg.get("funding_regex") and not cfg.get("funding_label"):
        warnings.append("`funding_label` is empty; reports will say 'the funding body'")
    for k in REMOVED_KEYS:
        if k in cfg.get("_raw_keys", []):
            warnings.append(f"`{k}` is no longer used ({REMOVED_KEYS[k]}); remove it")
    unknown = sorted(set(cfg.get("_raw_keys", [])) - set(DEFAULTS) - set(REQUIRED) - set(REMOVED_KEYS))
    if unknown:
        warnings.append(f"unknown keys (ignored; check the spelling): {', '.join(unknown)}")
    return errors, warnings


# --------------------------------------------------------------------------- people (author lists)
ORCID_RX = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", re.I)
# lower-case words that belong to the surname: "Ludwig van Beethoven" -> surname "van Beethoven"
SURNAME_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "dos", "das", "le", "la",
                     "ten", "ter", "zu", "af"}
_FOLD = str.maketrans({"ø": "o", "æ": "ae", "ß": "ss", "ł": "l", "đ": "d", "œ": "oe", "ð": "d", "þ": "th",
                       "ı": "i"})


def fold(s: str) -> str:
    """Lower case without accents: 'Lehtiö' -> 'lehtio', 'Ørsted' -> 'orsted'."""
    s = unicodedata.normalize("NFKD", str(s).lower().translate(_FOLD))
    return "".join(c for c in s if not unicodedata.combining(c))


def name_key(s: str) -> str:
    """Surname key for matching: folded, letters only ('García-Márquez' -> 'garciamarquez')."""
    return re.sub(r"[^a-z]", "", fold(s))


def given_key(given: str) -> str:
    """First given name for matching ('jane'), or its initial when only initials are known ('JA' -> 'j')."""
    tok = next((t for t in re.split(r"[\s.\-‐]+", given.strip()) if t), "")
    if re.fullmatch(r"[A-Z]{1,3}", tok):
        tok = tok[0]
    return name_key(tok)


def given_matches(a: str, b: str) -> bool:
    """Two given_key values agree: same initial if either is only an initial, else one is a prefix of
    the other ('chris' / 'christopher')."""
    if not a or not b:
        return False
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return a.startswith(b) or b.startswith(a)


def split_name(name: str) -> tuple[str, str]:
    """(surname, given names) from 'Jane A. Doe', 'Doe, Jane' or 'Doe JA' (PubMed style)."""
    name = " ".join(name.replace(".", " ").split()).strip(" ,")
    if "," in name:
        last, given = name.split(",", 1)
        return last.strip(), given.strip()
    words = name.split()
    if len(words) < 2:
        return name, ""
    n = 0                                                   # 'Doe JA' / 'Doe J A': initials last
    while n < len(words) - 1 and re.fullmatch(r"[A-Z]{1,3}", words[-1 - n]):
        n += 1
    if n:
        return " ".join(words[:-n]), " ".join(words[-n:])
    i = len(words) - 1
    while i > 1 and words[i - 1].lower() in SURNAME_PARTICLES:
        i -= 1
    return " ".join(words[i:]), " ".join(words[:i])


@dataclass(frozen=True)
class Person:
    """One entry of `facility_staff` or `requesters`."""
    name: str                 # as written in the config (without the ORCID)
    surname: str              # as written, e.g. 'van Beethoven'
    given: str
    orcid: str | None = None
    affiliations: tuple[str, ...] = ()   # e.g. ('Karolinska Institutet', 'SciLifeLab')

    @property
    def key(self) -> tuple[str, str]:
        """(surname key, first given name or initial): compared with a paper's authors by `given_matches`."""
        return name_key(self.surname), given_key(self.given)

    @property
    def query_name(self) -> str:
        """Europe PMC author search form: surname + first given name ('Cao Xiaofang'), which is far more
        precise than surname + initial ('Cao X'); the initial only when the config gives no more."""
        first = next((t for t in re.split(r"[\s.\-‐]+", self.given) if t), "")
        if len(first) > 1 and not re.fullmatch(r"[A-Z]{1,3}", first):
            return f"{self.surname} {first}"
        return f"{self.surname} {first[:1].upper()}".strip()


def norm_orcid(v: Any) -> str | None:
    m = ORCID_RX.search(str(v or ""))
    return m.group(1).upper() if m else None


PERSON_KEYS = ("name", "orcid", "affiliations", "affiliation")


def parse_person(entry: Any) -> Person:
    """'Jane Doe', 'Doe, Jane', 'Jane Doe (0000-0002-1825-0097)' or
    {name: ..., orcid: ..., affiliations: [...]}. Raises ValueError."""
    affs: list[str] = []
    if isinstance(entry, dict):
        bad = [str(k) for k in entry if k not in PERSON_KEYS]
        if bad:
            raise ValueError(f"unknown key {bad[0]!r} (use name, orcid, affiliations)")
        name, orcid_text = str(entry.get("name") or "").strip(), str(entry.get("orcid") or "").strip()
        raw = entry.get("affiliations", entry.get("affiliation"))
        if isinstance(raw, str):
            raw = raw.split(";")
        if raw is not None and not isinstance(raw, list):
            raise ValueError("`affiliations` must be a list, e.g. [Karolinska Institutet, SciLifeLab]")
        affs = [" ".join(str(a).split()) for a in raw or [] if str(a or "").strip()]
        if any(not aff_key(a).strip() for a in affs):
            raise ValueError(f"affiliation without letters or digits in {affs}")
    else:
        text = str(entry or "")
        m = ORCID_RX.search(text)
        orcid_text = m.group(1) if m else ""
        name = re.sub(r"[()\[\]]", " ", ORCID_RX.sub(" ", text)).strip(" ,;")
        name = " ".join(name.split())
    orcid = norm_orcid(orcid_text)
    if orcid_text and not orcid:
        raise ValueError(f"{orcid_text!r} is not an ORCID (format 0000-0002-1825-0097)")
    if re.search(r"\d", name):
        raise ValueError(f"{name!r}: unexpected digits (write the ORCID as 0000-0002-1825-0097)")
    surname, given = split_name(name)
    if not name_key(surname) or not name_key(given):
        raise ValueError(f"{name!r}: write first and last name, e.g. 'Jane Doe' or 'Doe, Jane'")
    return Person(name=name, surname=surname, given=given, orcid=orcid, affiliations=tuple(affs))


def parse_people(entries: Any, key: str = "people") -> tuple[list[Person], list[str]]:
    """Parse a config list of people. Returns (people, error messages); bad entries are left out."""
    if isinstance(entries, (str, dict)):
        entries = [entries]
    people, errors = [], []
    for i, e in enumerate(entries or [], 1):
        try:
            people.append(parse_person(e))
        except ValueError as ex:
            errors.append(f"`{key}` entry {i}: {ex}")
    return people, errors


def aff_key(s: str) -> str:
    """Affiliation text for matching: folded, punctuation as spaces, padded ('SciLifeLab,' -> ' scilifelab ')."""
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", fold(s)).split()) + " "


def affiliation_matches(text: str, phrases: Any) -> str | None:
    """The first of `phrases` found in the affiliation `text`, starting at a word (case, accents and
    punctuation ignored): 'Karolinska Institutet' is found in 'Dept. of Oncology, Karolinska Institutet,
    Solna'. None if none is."""
    t = aff_key(text)
    return next((p for p in phrases if aff_key(p).rstrip() in t), None)


def _person_entry(entry: Any) -> str | dict:
    """An answers-file entry for the config: 'Jane Doe (0000-0002-1825-0097)', or a mapping when it
    gives affiliations ({name: Jane Doe, affiliations: [...]})."""
    if isinstance(entry, dict):
        name, orcid = str(entry.get("name") or "").strip(), str(entry.get("orcid") or "").strip()
        affs = entry.get("affiliations", entry.get("affiliation"))
        if affs:
            return {"name": name, **({"orcid": orcid} if orcid else {}),
                    "affiliations": [a.strip() for a in (affs.split(";") if isinstance(affs, str) else affs)
                                     if str(a).strip()]}
        return f"{name} ({orcid})" if orcid else name
    return " ".join(str(entry).split())


# --------------------------------------------------------------------------- building a config
def name_regex(name: str) -> str:
    """'Clinical Proteomics Mass Spectrometry' -> a case-insensitive pattern tolerant of spaces,
    hyphens and line-break hyphens between the words."""
    sep = r"[\s\-‐‑–]+"
    words = []
    for w in name.split():
        parts = [re.escape(p) for p in re.split(r"[-‐‑–]", w) if p]
        words.append(r"[\s\-‐‑–]*".join(parts))
    return r"\b" + sep.join(words) + r"\b"


def names_regex(names: list[str]) -> str:
    names = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    if not names:
        return ""
    return names_regex_join([name_regex(n) for n in names])


def names_regex_join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"


def terms_regex(terms: list[str]) -> str:
    """Words or word stems (matched at the start of a word): ['proteom', 'peptide'] -> \\b(?:proteom|peptide)."""
    terms = [t.strip() for t in terms if t and t.strip()]
    if not terms:
        return ""
    return r"\b(?:" + "|".join(re.escape(t).replace(r"\ ", r"[\s\-]+") for t in terms) + ")"


def _list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[;\n]", v) if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def build_config(answers: dict) -> dict:
    """Turn plain answers (names, statement, folders; no regular expressions) into a config dict.

    Keys in `answers` (only facility_name and acknowledgement_statement are required):
      facility_name, short_name, host_institution, facility_label, name_variants,
      acknowledgement_statement, funding_body, field_terms, other_facilities,
      scope (in_scope_label, in_scope_terms, out_of_scope_label, out_of_scope_terms,
             out_of_scope_note, out_of_scope_facilities, column),
      affiliation_search, facility_staff, requesters, output_dir, cache_dir, pdf_dir, years,
      year_basis, jif_table, manual_decisions, report_slug
    """
    name = str(answers.get("facility_name") or "").strip()
    statement = " ".join(str(answers.get("acknowledgement_statement") or "").split())
    if not name or not statement:
        raise ConfigError("answers need at least facility_name and acknowledgement_statement")
    variants = _list(answers.get("name_variants"))
    host = str(answers.get("host_institution") or "").strip()
    label = answers.get("facility_label") or (f"{name} facility ({host})" if host and "facilit" not in name.lower()
                                              else f"{name} ({host})" if host else name)
    short = str(answers.get("short_name") or "").strip()
    funding = str(answers.get("funding_body") or "").strip()
    field_terms = _list(answers.get("field_terms"))
    others = _list(answers.get("other_facilities"))
    names = [name] + variants
    search = list(dict.fromkeys(names + ([funding] if funding else [])))
    cfg: dict[str, Any] = {
        "facility_label": label,
        "report_slug": answers.get("report_slug") or (f"{short}_acknowledgements" if short else slugify(name)),
        "canonical_statement": statement,
        "search_phrases": search,
        "openalex_phrases": list(dict.fromkeys([name] + ([funding] if funding else []))),
        "facility_regex": names_regex(names),
        "funding_regex": names_regex([funding]) if funding else "",
        "funding_label": f"the {funding}" if funding and not funding.lower().startswith("the ") else funding,
        "context_regex": terms_regex(field_terms),
        "context_label": " or ".join(field_terms[:2]) if field_terms else "",
        "other_facility_regex": names_regex(others),
    }
    sc = answers.get("scope") or {}
    if sc and sc.get("in_scope_terms") and sc.get("out_of_scope_terms"):
        cfg["scope_check"] = {
            "enabled": True,
            "column": sc.get("column") or "Service type",
            "in_scope_label": sc.get("in_scope_label") or "In scope",
            "out_of_scope_label": sc.get("out_of_scope_label") or "Out of scope",
            "out_of_scope_note": sc.get("out_of_scope_note") or "",
            "in_scope_regex": terms_regex(_list(sc.get("in_scope_terms"))),
            "out_of_scope_regex": terms_regex(_list(sc.get("out_of_scope_terms"))),
            "out_of_scope_facility_regex": names_regex(_list(sc.get("out_of_scope_facilities"))),
            "in_scope_min_terms": int(sc.get("in_scope_min_terms", 3)),
            "out_of_scope_min_terms": int(sc.get("out_of_scope_min_terms", 3)),
            "in_scope_examples": ", ".join(_list(sc.get("in_scope_terms"))[:5]),
        }
    else:
        cfg["scope_check"] = {"enabled": False}
    # `include_facility_authors` is the old name of `affiliation_search`
    aff = bool(answers.get("affiliation_search", answers.get("include_facility_authors", False)))
    cfg["affiliation_phrases"] = names if aff else []
    cfg["openalex_affiliation_phrases"] = names[:1] if aff else []
    for k in PEOPLE_KEYS:
        cfg[k] = [_person_entry(x) for x in (_list(answers.get(k)) if not isinstance(answers.get(k), list)
                                            else answers[k]) if x]
    cfg["output_dir"] = answers.get("output_dir") or f"reports/{cfg['report_slug']}"
    cfg["cache_dir"] = answers.get("cache_dir") or "cache"
    cfg["pdf_dir"] = answers.get("pdf_dir") or "pdfs"
    years = answers.get("years") or []
    cfg["years"] = [int(y) for y in (years if isinstance(years, list) else re.findall(r"\d{4}", str(years)))]
    cfg["year_basis"] = answers.get("year_basis") or "first"
    cfg["jif_table"] = answers.get("jif_table") or None
    cfg["manual_decisions"] = answers.get("manual_decisions") or "manual_decisions.yaml"
    cfg["canonical_threshold"] = 0.85
    cfg["word_expansions"] = answers.get("word_expansions") or {}
    cfg["other_doi_prefixes"] = answers.get("other_doi_prefixes") or {}
    return cfg


# --------------------------------------------------------------------------- writing a config
_FOLD_KEYS = {"canonical_statement", "facility_label"}
_REGEX_SUFFIX = "_regex"


def _scalar(key: str, v: Any, indent: str) -> str:
    """One YAML value, formatted for people: regexes in single quotes, long prose folded."""
    if v is None:
        return "null"
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return yaml.safe_dump(v).splitlines()[0]
    if isinstance(v, (dt.date,)):
        return v.isoformat()
    if isinstance(v, (dict, list)):                   # e.g. a person with affiliations: JSON is YAML flow style
        return json.dumps(v, ensure_ascii=False)
    s = str(v)
    if key.endswith(_REGEX_SUFFIX):
        return "'" + s.replace("'", "''") + "'"
    if key in _FOLD_KEYS and len(s) > 80 and "\n" not in s and not s.startswith((" ", "#")):
        body = textwrap.fill(s, width=100, break_long_words=False, break_on_hyphens=False)
        return ">-\n" + "\n".join(indent + "  " + line for line in body.splitlines())
    return json.dumps(s, ensure_ascii=False)


def _value(key: str, v: Any, indent: str) -> str:
    if isinstance(v, list):
        if not v:
            return "[]"
        return "\n" + "\n".join(f"{indent}  - {_scalar(key, x, indent + '    ')}" for x in v)
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "\n" + "\n".join(f"{indent}  {_key(k)}: {_value(str(k), x, indent + '  ')}" for k, x in v.items())
    return _scalar(key, v, indent)


def _key(k: Any) -> str:
    k = str(k)
    return k if re.fullmatch(r"[A-Za-z_][\w\-]*", k) else json.dumps(k)


def render_config(values: dict, template: str | None = None) -> str:
    """Fill `values` into the commented template (config.example.yaml), keeping every comment.

    Each top-level key in the template is replaced by the matching value; nested keys of
    `scope_check` are replaced one by one so their comments stay too. Keys missing from the
    template are appended at the end. The result is checked by re-reading it.
    """
    text = template if template is not None else template_text()
    # the template's "how to use this file" block is replaced by a one-line note
    text = re.sub(r"(?ms)^# How to use this file\n.*?(?=^# ={10,})",
                  f"# Made from config.example.yaml on {dt.date.today().isoformat()}.\n"
                  "# Check it with `ackfinder check`; test patterns with `ackfinder classify \"...\"`.\n",
                  text, count=1)
    text = text.replace("configuration (template)", "configuration", 1)
    lines = text.splitlines()
    out = _fill(lines, values, indent="")
    rendered = "\n".join(out).rstrip() + "\n"
    missing = [k for k in values if not re.search(rf"(?m)^{re.escape(_key(k))}:", rendered)]
    for k in missing:
        rendered += f"{_key(k)}: {_value(k, values[k], '')}\n"
    check = yaml.safe_load(rendered)
    for k, v in values.items():
        if check.get(k) != v and not (isinstance(v, dict) and isinstance(check.get(k), dict)
                                      and all(check[k].get(a) == b for a, b in v.items())):
            raise ConfigError(f"internal error: `{k}` did not survive writing the config")
    return rendered


def _fill(lines: list[str], values: dict, indent: str) -> list[str]:
    out, i = [], 0
    key_rx = re.compile(rf"^{re.escape(indent)}([A-Za-z_][\w\-]*):(.*)$")
    while i < len(lines):
        m = key_rx.match(lines[i])
        if not m or m.group(1) not in values:
            out.append(lines[i])
            i += 1
            continue
        key, rest = m.group(1), m.group(2)
        # the value block: following lines indented deeper than this key (blank lines inside allowed)
        j = i + 1
        while j < len(lines) and (lines[j].startswith(indent + " ") or
                                  (not lines[j].strip() and j + 1 < len(lines)
                                   and lines[j + 1].startswith(indent + " "))):
            j += 1
        block = lines[i + 1:j]
        val = values[key]
        comment = ""
        cm = re.search(r"\s+(#\s.*)$", rest)
        if cm and not rest.strip().startswith(("'", '"', ">", "|")):
            comment = "  " + cm.group(1)
        if isinstance(val, dict) and val and any(re.match(rf"^{re.escape(indent)}  [A-Za-z_]", b) for b in block):
            filled = _fill(block, val, indent + "  ")
            present = {mm.group(1) for b in filled if (mm := re.match(rf"^{re.escape(indent)}  ([A-Za-z_][\w\-]*):", b))}
            extra = [f"{indent}  {_key(k)}: {_value(str(k), v, indent + '  ')}" for k, v in val.items()
                     if k not in present]
            # template keys without a new value keep their template value (e.g. a disabled
            # scope_check keeps its worked example as a guide)
            out.append(f"{indent}{key}:{comment}")
            out.extend(filled + extra)
        else:
            v = _value(key, val, indent)
            first, *more = v.split("\n")
            out.append(f"{indent}{key}: {first}{comment if not more else ''}".rstrip())
            out.extend(more)
            # keep indented comment lines that belonged to the old block
            out.extend(b for b in block if b.strip().startswith("#"))
        i = j
    return out
