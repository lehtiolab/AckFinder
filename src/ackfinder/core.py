"""Search, merge, full-text verification and metadata.

Pipeline stages (run by :func:`ackfinder.pipeline.run`):

1. Candidate search in full-text indexes: Europe PMC, NCBI PMC, OpenAlex
   (``fulltext.search``); author-affiliation search only when facility authors are included.
2. Merge candidates across sources (DOI / PMCID / PMID / preprint id / title).
3. Metadata: title, DOI, first and last author, journal, article vs preprint, published
   version of preprints, and an impact-factor value (Clarivate JIF from a user-supplied
   table if given, otherwise the OpenAlex 2-year mean citedness of the journal as an open proxy).
4. Verification: retrieve the full text (Europe PMC JATS -> NCBI PMC JATS -> bioRxiv/medRxiv
   JATS -> open-access PDF -> local PDF folder), split it into sentences with their section,
   and classify each sentence that names the facility.

Nothing here is facility-specific: all names, phrases and patterns come from the config file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import pandas as pd
import requests
from lxml import etree

log = logging.getLogger("ackfinder")

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX = "https://api.openalex.org"
BIORXIV_API = "https://api.biorxiv.org"
OPENALEX_PER_PAGE = 100     # documented maximum per page
try:
    from importlib.metadata import version as _version
    __version__ = _version("ackfinder")
except Exception:  # noqa: BLE001  (running from a source checkout without installing)
    __version__ = "1.0.0"
USER_AGENT = f"AckFinder/{__version__} (python-requests; literature acknowledgement audit)"

# DOI prefixes of preprint servers.
PREPRINT_PREFIXES = {
    "10.1101": "bioRxiv/medRxiv",
    "10.64898": "bioRxiv/medRxiv",
    "10.21203": "Research Square",
    "10.20944": "Preprints.org",
    "10.2139": "SSRN",
    "10.48550": "arXiv",
    "10.31234": "PsyArXiv",
    "10.31219": "OSF Preprints",
    "10.22541": "Authorea",
    "10.26434": "ChemRxiv",
    "10.36227": "TechRxiv",
}
# DOI prefixes of institutional repositories (theses etc.) come from the config
# (`other_doi_prefixes`); their records are typed "Other".

STOPWORDS = set(
    "a an and the of by at in on for to was were is are be been with from as this that "
    "which who whom we our us it its".split()
)

CATEGORY_ORDER = ["Acknowledgements", "Funding", "Methods", "Main text", "Declarations",
                  "Supplementary", "Unknown (PDF)", "Affiliation", "References"]

# Why a paper is in the report ("Inclusion basis" column).
BASIS_STATEMENT = "Facility statement"
BASIS_BOTH = "Facility statement + core facility member listed as author"
BASIS_MEMBER = "Core facility member listed as author"
BASIS_MANUAL = "Manual decision"

# A sentence that credits someone uses one of these words; an affiliation line does not.
STATEMENT_VERB = re.compile(
    r"perform|carried\s+out|conduct|analy[sz]|acknowledg|thank|grateful|support|fund|financ|provid|"
    r"assist|help|service|measur|acquir|process|run\b|courtesy", re.I)


# --------------------------------------------------------------------------- HTTP
class Http:
    """Small polite HTTP client: per-host pacing, retries, optional disk cache."""

    PACING = {"www.ebi.ac.uk": 0.12, "eutils.ncbi.nlm.nih.gov": 0.35,
              "api.openalex.org": 0.12, "api.biorxiv.org": 0.4,
              "www.biorxiv.org": 0.6, "www.medrxiv.org": 0.6}
    # An HTTP 429 whose Retry-After is longer than this (seconds) is not retried: the server
    # means hours (e.g. OpenAlex's used-up daily budget), and waiting a minute cannot help.
    GIVE_UP_AFTER = 600

    def __init__(self, cache_dir: Path | None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._last: dict[str, float] = {}
        self.calls = 0

    def _pace(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = self.PACING.get(host, 0.3) - (time.time() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.time()

    @staticmethod
    def _from_file(p: Path, url: str) -> requests.Response:
        r = requests.Response()
        r.status_code, r._content, r.url = 200, p.read_bytes(), url
        return r

    def get(self, url: str, params: dict | None = None, cache: bool = False,
            tries: int = 4, timeout: int = 60, fallback: bool = False,
            max_wait: int = 60) -> requests.Response | None:
        """GET with retries.

        cache=True    : serve from the disk cache when present (full texts).
        fallback=True : always query live, store successful answers, and fall back to the
                        last stored answer if the live query fails (search results). The
                        returned response then has `.stale = <date of the stored answer>`.
        """
        key = None
        if (cache or fallback) and self.cache_dir:
            key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
            p = self.cache_dir / key
            if cache and p.exists():
                return self._from_file(p, url)
        last = None
        for attempt in range(tries):
            self._pace(url)
            try:
                self.calls += 1
                r = self.s.get(url, params=params, timeout=timeout, allow_redirects=True)
            except requests.RequestException as e:
                log.debug("GET %s failed (%s), retry %d", url, e, attempt)
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                # Europe PMC answers 500 for "no full text" too; do not hammer it.
                if r.status_code == 500 and "europepmc" in url and "fullText" in url:
                    return r
                last = r
                wait = 2 ** attempt * 1.5
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After", "")
                    if ra.isdigit():
                        r.retry_after = int(ra)
                        if r.retry_after > self.GIVE_UP_AFTER:
                            log.info("%s: rate-limited (HTTP 429) and asked to wait %.1f h; not retrying",
                                     urlparse(url).netloc, r.retry_after / 3600)
                            break
                        wait = min(int(ra) + 1, max_wait)
                    log.info("%s: rate-limited (HTTP 429), attempt %d/%d, waiting %d s",
                             urlparse(url).netloc, attempt + 1, tries, wait)
                if attempt < tries - 1:
                    time.sleep(wait)
                continue
            if r.status_code == 200 and key:
                (self.cache_dir / key).write_bytes(r.content)
            return r
        if fallback and key and (self.cache_dir / key).exists():
            p = self.cache_dir / key
            r = self._from_file(p, url)
            r.stale = dt.datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()
            log.warning("%s unavailable; using the stored answer from %s", urlparse(url).netloc, r.stale)
            return r
        return last


# ------------------------------------------------------------------------ helpers
def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d or None


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = re.sub(r"<[^>]+>", "", s)
    return " ".join(s.split()).rstrip(".")


def norm_title(t: str | None) -> str | None:
    if not t:
        return None
    t = re.sub(r"[^a-z0-9]+", "", clean_text(t).lower())
    return t[:150] or None


def norm_words(s: str, expansions: dict[str, str] | None = None) -> list[str]:
    """Lower-case content words. `expansions` maps a short name to its long form
    (config `word_expansions`, e.g. scilifelab -> scilifelab science life laboratory)."""
    s = s.lower()
    for short, long in (expansions or {}).items():
        s = s.replace(short.lower(), long.lower())
    return [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in STOPWORDS]


def lcfirst(s: str) -> str:
    """'Small-molecule MS' -> 'small-molecule MS'; leaves acronyms such as 'LC-MS' alone."""
    return s[:1].lower() + s[1:] if len(s) > 1 and s[1].islower() else s


def compile_rx(pattern: str | None) -> re.Pattern | None:
    """Case-insensitive regex, or None for an empty pattern (an empty regex would match everything)."""
    return re.compile(pattern, re.I) if pattern and pattern.strip() else None


@dataclass
class Scope:
    """Optional service-scope check (config `scope_check`), e.g. proteomics vs small-molecule MS.

    For every paper with a facility statement it decides whether the credited work is the
    facility's service ("in scope") or a neighbouring service run by someone else ("out of scope").
    """
    column: str                       # report column, e.g. "MS type"
    in_label: str                     # e.g. "Proteomics"
    out_label: str                    # e.g. "Small-molecule MS"
    out_note: str                     # e.g. "metabolomics, lipidomics; run by another facility"
    in_rx: re.Pattern
    out_rx: re.Pattern
    out_facility_rx: re.Pattern | None
    in_min: int = 3
    out_min: int = 3
    in_examples: str = ""

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Scope | None":
        sc = cfg.get("scope_check") or {}
        if not sc.get("enabled"):
            return None
        return cls(column=sc["column"], in_label=sc["in_scope_label"], out_label=sc["out_of_scope_label"],
                   out_note=sc.get("out_of_scope_note", ""), in_rx=re.compile(sc["in_scope_regex"], re.I),
                   out_rx=re.compile(sc["out_of_scope_regex"], re.I),
                   out_facility_rx=compile_rx(sc.get("out_of_scope_facility_regex")),
                   in_min=int(sc.get("in_scope_min_terms", 3)), out_min=int(sc.get("out_of_scope_min_terms", 3)),
                   in_examples=sc.get("in_scope_examples", ""))

    @property
    def mixed(self) -> str:
        return f"{self.in_label} + {lcfirst(self.out_label)} in paper"

    unknown = "Not determined"

    @property
    def review_label(self) -> str:
        return f"{self.out_label}?"

    @property
    def evidence_column(self) -> str:
        return f"{self.column.replace(' ', '-')} evidence"

    @property
    def out_text(self) -> str:
        """'small-molecule MS (metabolomics, lipidomics; ...)' for running text."""
        return lcfirst(self.out_label) + (f" ({self.out_note})" if self.out_note else "")


def doi_prefix(doi: str | None) -> str:
    return doi.split("/")[0] if doi else ""


def chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- data classes
@dataclass
class Item:
    """One hit from one source, before merging."""
    source: str                     # EuropePMC | PMC | OpenAlex
    phrase: str
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    ppr: str | None = None
    title: str | None = None
    kind_hint: str | None = None    # preprint / article / other
    epmc: dict | None = None
    openalex: dict | None = None
    pmc_summary: dict | None = None


@dataclass
class Match:
    sentence: str
    section: str
    category: str
    klass: str          # Canonical statement | Variant wording | Affiliation only | Review | Excluded
    score: float


@dataclass
class Record:
    items: list[Item] = field(default_factory=list)
    epmc: list[dict] = field(default_factory=list)
    openalex: dict | None = None
    biorxiv: dict | None = None
    pmc_summary: dict | None = None
    matches: list[Match] = field(default_factory=list)
    fulltext_source: str | None = None
    jats: bytes | None = None                                     # full-text XML, if retrieved
    facility_authors: list[str] = field(default_factory=list)     # authors with a facility affiliation
    facility_affs: list[str] = field(default_factory=list)        # the affiliation strings themselves
    facility_aff_source: str = ""                                 # where the affiliation was seen
    basis: str = ""                                               # inclusion basis (BASIS_*)
    ms_type: str = ""                                             # scope-check call (Scope labels)
    ms_evidence: str = ""                                         # what the scope call was based on
    out_of_scope: bool = False                                    # left out by the scope check
    blocks: list = field(default_factory=list)                    # retrieved full-text blocks
    manual: dict | None = None                                    # manual decision applied, if any
    status: str = "pending"
    reason: str = ""
    meta: dict = field(default_factory=dict)

    # identifiers -------------------------------------------------------
    @property
    def dois(self) -> list[str]:
        out = []
        for it in self.items:
            if it.doi and it.doi not in out:
                out.append(it.doi)
        for e in self.epmc:
            d = norm_doi(e.get("doi"))
            if d and d not in out:
                out.append(d)
        return out

    def first(self, attr: str) -> str | None:
        for it in self.items:
            v = getattr(it, attr)
            if v:
                return v
        for e in self.epmc:
            key = {"pmcid": "pmcid", "pmid": "pmid"}.get(attr)
            if key and e.get(key):
                return e[key]
            if attr == "ppr" and e.get("source") == "PPR":
                return e.get("id")
        return None

    @property
    def found_by(self) -> list[str]:
        return sorted({f"{it.source}: {it.phrase}" for it in self.items})


# ------------------------------------------------------------- candidate search
class Searcher:
    def __init__(self, http: Http, cfg: dict, oa_key: str | None, ncbi_key: str | None):
        self.http, self.cfg = http, cfg
        self.oa_key, self.ncbi_key = oa_key, ncbi_key
        self.status: list[dict] = []        # one entry per query, shown in the report
        self.oa_search_down = False         # set after OpenAlex search refuses us; skip further searches
        self.oa_down = ""                   # why OpenAlex refused us for good; then skip all OpenAlex calls
        self._oa_meta_noted = False
        self._last_note = ""

    def _note(self, source: str, where: str, phrase: str, hits: int | None, note: str = "") -> None:
        self.status.append(dict(source=source, field=where, phrase=phrase, hits=hits, note=note))
        n = f"{hits:4d} hits" if hits is not None else "   - hits"
        log.info("%-11s %-11s %-46s %s%s", source, where, f'"{phrase}"', n, f"  ({note})" if note else "")

    def _ncbi_base(self, db: str) -> dict:
        base = {"db": db, "retmode": "json", "tool": "ackfinder"}
        if self.ncbi_key:
            base["api_key"] = self.ncbi_key
        if os.environ.get("NCBI_EMAIL"):
            base["email"] = os.environ["NCBI_EMAIL"]
        return base

    # Europe PMC ---------------------------------------------------------
    def epmc_query(self, query: str, max_records: int = 5000, fallback: bool = False) -> list[dict]:
        out, cursor = [], "*"
        self._last_note = ""
        while True:
            r = self.http.get(f"{EPMC}/search", params=dict(
                query=query, format="json", resultType="core", pageSize=1000, cursorMark=cursor),
                fallback=fallback)
            if r is None or r.status_code != 200:
                log.warning("Europe PMC query failed: %s", query)
                self._last_note = f"failed (HTTP {getattr(r, 'status_code', 'no response')})"
                break
            if getattr(r, "stale", None):
                self._last_note = f"live query failed; stored result from {r.stale}"
            j = r.json()
            res = j.get("resultList", {}).get("result", [])
            out.extend(res)
            nxt = j.get("nextCursorMark")
            if not res or not nxt or nxt == cursor or len(out) >= max_records:
                break
            cursor = nxt
        return out

    def epmc_search(self, y0: int, y1: int, affiliations: bool = True) -> list[Item]:
        """Full-text phrase search, plus author-affiliation search (AFF:) if requested."""
        items = []
        date = f"(FIRST_PDATE:[{y0 - 1}-01-01 TO {y1}-12-31])"
        queries = [(f'"{ph}"', ph, "EuropePMC", "full text") for ph in self.cfg["search_phrases"]]
        if affiliations:
            queries += [(f'AFF:"{ph}"', ph, "EuropePMC (affiliation)", "affiliation")
                        for ph in self.cfg.get("affiliation_phrases", [])]
        for q, ph, source, where in queries:
            res = self.epmc_query(f"{q} AND {date}", fallback=True)
            self._note("Europe PMC", where, ph, len(res), self._last_note)
            items += [self._item_from_epmc(e, source, ph) for e in res]
        return items

    @staticmethod
    def _item_from_epmc(e: dict, source: str, phrase: str) -> Item:
        is_pp = e.get("source") == "PPR" or "Preprint" in (e.get("pubTypeList", {}) or {}).get("pubType", [])
        return Item(source=source, phrase=phrase, doi=norm_doi(e.get("doi")), pmid=e.get("pmid"),
                    pmcid=e.get("pmcid"), ppr=e.get("id") if e.get("source") == "PPR" else None,
                    title=e.get("title"), kind_hint="preprint" if is_pp else "article", epmc=e)

    def epmc_lookup(self, field_name: str, values: list[str]) -> dict[str, list[dict]]:
        """Batch lookup of Europe PMC records by DOI / PMCID / PMID."""
        found: dict[str, list[dict]] = {}
        for batch in chunks(sorted(set(values)), 40):
            if field_name == "PMID":
                q = " OR ".join(f"(EXT_ID:{v} AND SRC:MED)" for v in batch)
            else:
                q = " OR ".join(f'{field_name}:"{v}"' for v in batch)
            for e in self.epmc_query(q, fallback=True):
                k = {"DOI": norm_doi(e.get("doi")), "PMCID": e.get("pmcid"), "PMID": e.get("pmid")}.get(field_name)
                if k:
                    found.setdefault(k.lower() if field_name == "DOI" else k, []).append(e)
        return found

    def _esearch(self, db: str, term: str) -> tuple[list[str], str]:
        r = self.http.get(f"{EUTILS}/esearch.fcgi",
                          params={**self._ncbi_base(db), "term": term, "retmax": 10000}, fallback=True)
        if r is None or r.status_code != 200:
            return [], f"failed (HTTP {getattr(r, 'status_code', 'no response')})"
        j = r.json().get("esearchresult", {})
        note = f"live query failed; stored result from {r.stale}" if getattr(r, "stale", None) else ""
        if j.get("warninglist", {}).get("phrasesignored") or j.get("errorlist", {}).get("phrasesnotfound"):
            note = (note + "; " if note else "") + "phrase not in NCBI phrase index"
        return j.get("idlist", []), note

    def _esummary(self, db: str, ids: list[str]) -> dict[str, dict]:
        out = {}
        for batch in chunks(ids, 100):
            r = self.http.get(f"{EUTILS}/esummary.fcgi", params={**self._ncbi_base(db), "id": ",".join(batch)})
            if r is not None and r.status_code == 200:
                res = r.json().get("result", {})
                for uid in res.get("uids", []):
                    out[uid] = res[uid]
        return out

    # PubMed (author affiliations) ---------------------------------------
    def pubmed_aff_search(self, y0: int, y1: int) -> list[Item]:
        hits: list[tuple[str, str]] = []
        for ph in self.cfg.get("affiliation_phrases", []):
            ids, note = self._esearch("pubmed", f'"{ph}"[ad] AND {y0 - 1}:{y1}[dp]')
            self._note("PubMed", "affiliation", ph, len(ids) if not note.startswith("failed") else None, note)
            hits += [(ph, i) for i in ids]
        pmids = sorted({i for _, i in hits})
        epmc = self.epmc_lookup("PMID", pmids) if pmids else {}
        summ = self._esummary("pubmed", [p for p in pmids if p not in epmc])
        items = []
        for ph, pmid in hits:
            if pmid in epmc:
                items.append(self._item_from_epmc(epmc[pmid][0], "PubMed (affiliation)", ph))
                continue
            s = summ.get(pmid, {})
            doi = next((norm_doi(a.get("value")) for a in s.get("articleids", []) if a.get("idtype") == "doi"), None)
            items.append(Item(source="PubMed (affiliation)", phrase=ph, pmid=pmid, doi=doi, title=s.get("title"),
                              kind_hint="article", pmc_summary=s or None))
        return items

    # NCBI PMC -----------------------------------------------------------
    def pmc_search(self, y0: int, y1: int) -> list[Item]:
        items = []
        base = self._ncbi_base("pmc")
        for ph in self.cfg["search_phrases"]:
            ids, note = self._esearch("pmc", f'"{ph}" AND {y0 - 1}:{y1}[pdat]')
            self._note("NCBI PMC", "full text", ph, len(ids) if not note.startswith("failed") else None, note)
            for i in ids:
                items.append(Item(source="PMC", phrase=ph, pmcid=f"PMC{i}", kind_hint="article"))
        # attach Europe PMC metadata; fall back to NCBI esummary for PMCIDs Europe PMC lacks
        pmcids = sorted({it.pmcid for it in items})
        epmc = self.epmc_lookup("PMCID", pmcids) if pmcids else {}
        missing = [p for p in pmcids if p not in epmc]
        summaries: dict[str, dict] = {}
        for batch in chunks(missing, 100):
            r = self.http.get(f"{EUTILS}/esummary.fcgi",
                              params={**base, "id": ",".join(p[3:] for p in batch)})
            if r is not None and r.status_code == 200:
                res = r.json().get("result", {})
                for uid in res.get("uids", []):
                    summaries[f"PMC{uid}"] = res[uid]
        for it in items:
            if it.pmcid in epmc:
                e = epmc[it.pmcid][0]
                it.epmc, it.doi, it.pmid, it.title = e, norm_doi(e.get("doi")), e.get("pmid"), e.get("title")
            elif it.pmcid in summaries:
                s = summaries[it.pmcid]
                it.pmc_summary = s
                it.title = s.get("title")
                for aid in s.get("articleids", []):
                    if aid.get("idtype") == "doi":
                        it.doi = norm_doi(aid.get("value"))
                    if aid.get("idtype") == "pmid":
                        it.pmid = aid.get("value")
        return items

    # OpenAlex -------------------------------------------------------------
    def _oa_params(self, extra: dict) -> dict:
        p = dict(extra)
        if self.oa_key:
            p["api_key"] = self.oa_key
        return p

    @staticmethod
    def _oa_budget_gone(r: requests.Response | None) -> bool:
        """HTTP 429 with a Retry-After of hours: the daily budget is used up until midnight UTC."""
        return getattr(r, "retry_after", 0) > Http.GIVE_UP_AFTER

    def _oa_stop(self, why: str) -> None:
        """Make no more OpenAlex calls (searches or metadata lookups) in this run."""
        if not self.oa_down:
            self.oa_down = why
            log.warning("OpenAlex refused requests (%s): no more OpenAlex calls in this run. Journal names come "
                        "from Europe PMC/PubMed; the IF proxy is missing for papers OpenAlex had not answered for.%s",
                        why, "" if self.oa_key else " A free OPENALEX_API_KEY has its own budget.")

    def _oa_lookup(self, path: str, params: dict) -> requests.Response | None:
        """OpenAlex metadata lookup. Once OpenAlex refuses one (HTTP 429 after Http's retries), the
        rest of the run skips OpenAlex instead of waiting minutes for every paper."""
        if not self.oa_down:
            r = self.http.get(f"{OPENALEX}/{path}", params=self._oa_params(params))
            if r is None or r.status_code != 429:
                return r
            self._oa_stop("HTTP 429, daily budget used up" if self._oa_budget_gone(r) else "HTTP 429")
        if not self._oa_meta_noted:
            self._oa_meta_noted = True
            self._note("OpenAlex", "metadata", "journal details, IF proxy", None,
                       f"skipped: {self.oa_down}" + ("" if self.oa_key else "; set OPENALEX_API_KEY"))
        return None

    def openalex_search(self, y0: int, y1: int, affiliations: bool = True) -> list[Item]:
        """OpenAlex full-text search, plus raw-affiliation search if requested.

        Anonymous OpenAlex *search* is rate-limited when their cluster is busy (HTTP 429).
        We wait as told by Retry-After a few times, then fall back to the stored answer of
        the last successful run, and otherwise skip OpenAlex search for the rest of the run.
        A Retry-After of hours means the daily budget is used up: no waiting, and no further
        OpenAlex calls at all in this run (searches or metadata lookups).
        """
        queries = [("fulltext.search", ph, "OpenAlex", "full text") for ph in self.cfg.get("openalex_phrases", [])]
        if affiliations:
            queries += [("raw_affiliation_strings.search", ph, "OpenAlex (affiliation)", "affiliation")
                        for ph in self.cfg.get("openalex_affiliation_phrases",
                                               self.cfg.get("affiliation_phrases", [])[:1])]
        items = []
        for flt, ph, source, where in queries:
            if self.oa_search_down or self.oa_down:
                self._note("OpenAlex", where, ph, None, "skipped: OpenAlex search unavailable in this run")
                continue
            cursor, n, note, total = "*", 0, "", None
            while cursor:
                r = self.http.get(f"{OPENALEX}/works", params=self._oa_params({
                    "filter": f'{flt}:"{ph}",publication_year:{y0 - 1}-{y1}',
                    "per-page": OPENALEX_PER_PAGE, "cursor": cursor}), fallback=True, tries=4, max_wait=75)
                if r is None or r.status_code != 200:
                    code = getattr(r, "status_code", "no response")
                    budget = self._oa_budget_gone(r)
                    note = f"failed (HTTP {code}{', daily budget used up' if budget else ''})" + (
                        "; partial result" if n else "")
                    log.warning("OpenAlex search failed (%s): %s", code, getattr(r, "text", "")[:160])
                    if code == 429:
                        self.oa_search_down = True
                    if budget:
                        self._oa_stop("HTTP 429, daily budget used up")
                    break
                if getattr(r, "stale", None):
                    note = f"live query failed; stored result from {r.stale}"
                j = r.json()
                if total is None:
                    total = (j.get("meta") or {}).get("count")
                for w in j.get("results", []):
                    n += 1
                    t = w.get("type")
                    doi = norm_doi(w.get("doi"))
                    kind = "preprint" if (t == "preprint" or doi_prefix(doi) in PREPRINT_PREFIXES) else (
                        "article" if t in ("article", "review", "letter") else "other")
                    ids = w.get("ids") or {}
                    pmcid = (ids.get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1].upper() or None
                    if pmcid and not pmcid.startswith("PMC"):
                        pmcid = "PMC" + pmcid
                    items.append(Item(source=source, phrase=ph, doi=doi,
                                      pmid=(ids.get("pmid") or "").rstrip("/").rsplit("/", 1)[-1] or None,
                                      pmcid=pmcid, title=w.get("title"), kind_hint=kind, openalex=w))
                cursor = j.get("meta", {}).get("next_cursor")
            if total and n < total:
                cut = f"truncated: {n} of {total} results" + ("" if self.oa_key else
                                                              " (set OPENALEX_API_KEY to page further)")
                log.warning("OpenAlex %s \"%s\": %s", where, ph, cut)
                note = f"{note}; {cut}" if note else cut
            self._note("OpenAlex", where, ph, None if note.startswith("failed") and not n else n, note)
        # attach Europe PMC records by DOI so PMCIDs / preprint IDs are known
        dois = [it.doi for it in items if it.doi]
        epmc = self.epmc_lookup("DOI", dois) if dois else {}
        for it in items:
            if it.doi and it.doi in epmc:
                e = epmc[it.doi][0]
                it.epmc = e
                it.pmcid = it.pmcid or e.get("pmcid")
                it.pmid = it.pmid or e.get("pmid")
                if e.get("source") == "PPR":
                    it.ppr = e.get("id")
        return items

    def openalex_works(self, dois: list[str]) -> dict[str, dict]:
        out = {}
        for batch in chunks(sorted(set(dois)), 50):
            r = self._oa_lookup("works", {"filter": "doi:" + "|".join(batch), "per-page": 50})
            if r is None or r.status_code != 200:
                continue
            for w in r.json().get("results", []):
                d = norm_doi(w.get("doi"))
                if d:
                    out[d] = w
        return out

    def openalex_sources(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        short = sorted({i.rsplit("/", 1)[-1] for i in ids if i})
        for batch in chunks(short, 50):
            r = self._oa_lookup("sources", {"filter": "openalex:" + "|".join(batch), "per-page": 50})
            if r is None or r.status_code != 200:
                continue
            for s in r.json().get("results", []):
                out[s["id"].rsplit("/", 1)[-1]] = s
        return out

    def openalex_source_by_issn(self, issn: str) -> dict | None:
        r = self._oa_lookup("sources", {"filter": f"issn:{issn}"})
        if r is not None and r.status_code == 200 and r.json().get("results"):
            return r.json()["results"][0]
        return None

    def biorxiv_details(self, doi: str) -> dict | None:
        for server in ("biorxiv", "medrxiv"):
            r = self.http.get(f"{BIORXIV_API}/details/{server}/{doi}", cache=True)
            if r is not None and r.status_code == 200:
                try:
                    coll = r.json().get("collection") or []
                except ValueError:
                    continue
                if coll:
                    rec = dict(coll[-1])           # latest version
                    rec["_first_date"] = coll[0].get("date")
                    rec["_server"] = server
                    return rec
        return None


# ----------------------------------------------------------------- merge
def merge_items(items: list[Item]) -> list[Record]:
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    seen: dict[str, int] = {}
    for i, it in enumerate(items):
        keys = []
        if it.doi:
            keys.append("doi:" + it.doi)
        if it.pmcid:
            keys.append("pmcid:" + it.pmcid.upper())
        if it.pmid:
            keys.append("pmid:" + str(it.pmid))
        if it.ppr:
            keys.append("ppr:" + it.ppr)
        t = norm_title(it.title)
        if t and len(t) > 25:
            keys.append(f"title:{it.kind_hint}:{t}")
        for k in keys:
            if k in seen:
                union(seen[k], i)
            else:
                seen[k] = i
    groups: dict[int, Record] = {}
    for i, it in enumerate(items):
        rec = groups.setdefault(find(i), Record())
        rec.items.append(it)
        if it.epmc and all(e.get("id") != it.epmc.get("id") for e in rec.epmc):
            rec.epmc.append(it.epmc)
        if it.openalex and not rec.openalex:
            rec.openalex = it.openalex
        if it.pmc_summary and not rec.pmc_summary:
            rec.pmc_summary = it.pmc_summary
    return list(groups.values())


# ------------------------------------------------------------- full text
JATS_BLOCKS = {"p", "funding-statement", "td", "th", "aff", "title", "label", "list-item",
               "award-group", "institution", "attrib", "def"}


def _title_of(el) -> str:
    t = el.find("title")
    if t is None:
        t = el.find("label")
    return " ".join("".join(t.itertext()).split()) if t is not None else ""


def jats_section(el) -> tuple[str, str]:
    """Return (path, category) for a JATS element."""
    path, cat = [], None
    tags = [a.tag for a in el.iterancestors() if isinstance(a.tag, str)]
    if "ref-list" in tags:
        return "References", "References"
    if el.tag == "aff" or "aff" in tags or "contrib-group" in tags or "aff-alternatives" in tags:
        return "Author affiliations", "Affiliation"
    for a in el.iterancestors():
        if not isinstance(a.tag, str):
            continue
        if a.tag == "ack":
            path.append(_title_of(a) or "Acknowledgements")
            cat = cat or "Acknowledgements"
        elif a.tag == "funding-group":
            path.append("Funding")
            cat = cat or "Funding"
        elif a.tag in ("sec", "app", "notes", "fn-group", "fn", "boxed-text", "supplementary-material"):
            t = _title_of(a) or a.get("sec-type") or a.get("fn-type") or a.get("notes-type") or ""
            if t:
                path.append(t)
        elif a.tag == "abstract":
            path.append("Abstract")
        elif a.tag in ("body", "back", "front", "sub-article"):
            path.append({"body": "Main text", "back": "Back matter", "front": "Front matter"}.get(a.tag, a.tag))
            break
    path = [p for p in reversed(path) if p]
    joined = " / ".join(path).lower()
    if cat is None:
        if re.search(r"acknowledg", joined):
            cat = "Acknowledgements"
        elif re.search(r"funding|financial|financing|grant support", joined):
            cat = "Funding"
        elif re.search(r"method|materials|experimental procedures|experimental section|"
                       r"experimental design|protocol|mass spectrometry|proteomic", joined):
            cat = "Methods"
        elif re.search(r"declaration|conflict|competing|contribution|data availability|ethic", joined):
            cat = "Declarations"
        elif re.search(r"supplement", joined):
            cat = "Supplementary"
        elif "front matter" in joined and "abstract" not in joined:
            cat = "Affiliation" if el.tag in ("aff", "institution") else "Main text"
        else:
            cat = "Main text"
    return " › ".join(path) or "Main text", cat


def _jats_root(xml: bytes):
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError:
        return None
    if root is None:
        return None
    # strip namespaces so tag names are simple
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _txt(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def jats_facility_authors(xml: bytes, rx: re.Pattern) -> tuple[list[str], list[str]]:
    """Authors whose affiliation matches `rx`, and the matching affiliation strings."""
    root = _jats_root(xml)
    if root is None:
        return [], []

    def outside_refs(el) -> bool:
        return not any(isinstance(a.tag, str) and a.tag in ("ref-list", "sub-article") for a in el.iterancestors())

    aff_by_id, fac_affs = {}, []
    for a in root.iter("aff"):
        if not outside_refs(a):
            continue
        t, lab = _txt(a), _txt(a.find("label"))
        if lab and t.startswith(lab):           # drop the footnote label ("1", "a", ...)
            t = t[len(lab):].strip()
        if a.get("id"):
            aff_by_id[a.get("id")] = t
        if rx.search(t) and t not in fac_affs:
            fac_affs.append(t)
    fac_ids = {i for i, t in aff_by_id.items() if rx.search(t)}
    names = []
    for c in root.iter("contrib"):
        if c.get("contrib-type", "author") != "author" or not outside_refs(c):
            continue
        rids = set()
        for x in c.iter("xref"):
            if x.get("ref-type") == "aff":
                rids.update((x.get("rid") or "").split())
        inline = [_txt(a) for a in c.iter("aff")]
        if not (rids & fac_ids or any(rx.search(t) for t in inline)):
            continue
        nm = c.find(".//name")
        if nm is not None:
            name = f"{_txt(nm.find('given-names'))} {_txt(nm.find('surname'))}".strip()
        else:
            name = _txt(c.find(".//collab")) or _txt(c.find(".//string-name"))
        if name and name not in names:
            names.append(name)
    return names, fac_affs


def jats_blocks(xml: bytes) -> list[tuple[str, str, str]]:
    """Yield (text, section path, category) for every text block of a JATS document."""
    root = _jats_root(xml)
    if root is None:
        return []
    out = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        is_block = el.tag in JATS_BLOCKS
        # text placed directly in ack / sec / fn without <p>
        if el.tag in ("ack", "fn", "notes") and el.find(".//p") is None:
            is_block = True
        if not is_block:
            continue
        if any(isinstance(a.tag, str) and (a.tag in JATS_BLOCKS) for a in el.iterancestors()):
            continue  # nested inside another block already collected
        text = " ".join("".join(el.itertext()).split())
        if text:
            sec, cat = jats_section(el)
            out.append((text, sec, cat))
    return out


PDF_HEADINGS = [
    (r"acknowledge?ments?", "Acknowledgements"), (r"funding|financial support|funding information", "Funding"),
    (r"(materials? and |experimental |online |star ?)?methods?|experimental procedures|experimental section",
     "Methods"), (r"references|bibliography|literature cited", "References"),
    (r"author contributions?|competing interests?|conflicts? of interest|declarations?|data availability", "Declarations"),
    (r"results?( and discussion)?|discussion|introduction|abstract|conclusions?", "Main text"),
]


def pdf_blocks(pdf_bytes: bytes) -> list[tuple[str, str, str]]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # noqa: BLE001
        log.debug("PDF parse failed: %s", e)
        return []
    out, cur, buf = [], ("Unknown (PDF)", "Unknown (PDF)"), []
    inline = re.compile(r"(?:\d+\.?\s*)?((?i:acknowledge?ments?)|ACKNOWLEDGE?MENTS?|Funding|FUNDING)\b\s*[:.]?\s+\S")
    for line in text.splitlines():
        s = line.strip()
        head, keep = None, False
        if 2 < len(s) < 60:
            for rx, cat in PDF_HEADINGS:
                if re.fullmatch(r"(\d+(\.\d+)*\.?\s*)?(" + rx + r")\s*[:.]?", s, re.I):
                    head = (s.rstrip(":."), cat)
                    break
        if not head:
            m = inline.match(s)
            if m:
                head = (m.group(1).title(), "Acknowledgements" if m.group(1).lower().startswith("ack") else "Funding")
                keep = True
        if head:
            if buf:
                out.append((" ".join(buf), cur[0], cur[1]))
                buf = []
            cur = head
            if keep:
                buf.append(s)
            continue
        buf.append(s)
        if len(" ".join(buf)) > 3000:
            out.append((" ".join(buf), cur[0], cur[1]))
            buf = []
    if buf:
        out.append((" ".join(buf), cur[0], cur[1]))
    # PDF line breaks hyphenate words; repair the common case
    return [(re.sub(r"(\w)- (\w)", r"\1\2", t), s, c) for t, s, c in out]


class Verifier:
    """Retrieves full texts and classifies sentences. `http` and `searcher` may be None when
    only `classify` / `score` are used (e.g. `ackfinder classify`)."""

    def __init__(self, http: Http | None, cfg: dict, pdf_dir: Path | None, searcher: Searcher | None):
        self.http, self.cfg, self.pdf_dir, self.searcher = http, cfg, pdf_dir, searcher
        self.fac = re.compile(cfg["facility_regex"], re.I)
        self.fund = compile_rx(cfg.get("funding_regex"))            # optional
        self.ctx = compile_rx(cfg.get("context_regex"))             # optional
        self.other = compile_rx(cfg.get("other_facility_regex"))    # optional
        self.scope = Scope.from_cfg(cfg)                            # optional
        self.expansions = cfg.get("word_expansions") or {}
        canon = " ".join(cfg["canonical_statement"].split())
        self.canon = canon
        self.canon_words = set(norm_words(canon, self.expansions))
        self.canon_norm = " ".join(norm_words(canon, self.expansions))

    def _fund(self, s: str) -> bool:
        return bool(self.fund and self.fund.search(s))

    def _out_of_scope(self, s: str) -> bool:
        return bool(self.scope and self.scope.out_rx.search(s))

    # retrieval ----------------------------------------------------------
    def fetch(self, rec: Record) -> tuple[list[tuple[str, str, str]], str | None]:
        pmcids = [rec.first("pmcid")] + [e.get("pmcid") for e in rec.epmc]
        for pmcid in dict.fromkeys(p for p in pmcids if p):
            r = self.http.get(f"{EPMC}/{pmcid}/fullTextXML", cache=True)
            if r is not None and r.status_code == 200 and r.content.lstrip().startswith(b"<"):
                b = jats_blocks(r.content)
                if b:
                    rec.jats = r.content
                    return b, f"Europe PMC JATS ({pmcid})"
            r = self.http.get(f"{EUTILS}/efetch.fcgi", params={"db": "pmc", "id": pmcid[3:], "retmode": "xml",
                                                               "tool": "ackfinder"}, cache=True)
            if r is not None and r.status_code == 200 and b"<body" in r.content:
                b = jats_blocks(r.content)
                if b:
                    rec.jats = r.content
                    return b, f"NCBI PMC JATS ({pmcid})"
        for doi in rec.dois:
            if doi_prefix(doi) in ("10.1101", "10.64898"):
                det = rec.biorxiv or self.searcher.biorxiv_details(doi)
                if det:
                    rec.biorxiv = det
                    url = det.get("jatsxml")
                    if url:
                        r = self.http.get(url, cache=True)
                        if r is not None and r.status_code == 200 and r.content.lstrip().startswith(b"<"):
                            b = jats_blocks(r.content)
                            if b:
                                rec.jats = r.content
                                return b, f"{det.get('_server', 'bioRxiv')} JATS"
        # open-access PDF reported by OpenAlex
        w = rec.openalex or {}
        pdf_urls = []
        for loc in [w.get("best_oa_location")] + (w.get("locations") or []):
            if loc and loc.get("pdf_url"):
                pdf_urls.append(loc["pdf_url"])
        for url in dict.fromkeys(pdf_urls):
            r = self.http.get(url, cache=True, tries=2)
            if r is not None and r.status_code == 200 and r.content[:4] == b"%PDF":
                b = pdf_blocks(r.content)
                if b:
                    return b, f"open-access PDF ({urlparse(url).netloc})"
        # local PDFs supplied by the user (named by DOI, '/' replaced by '_')
        if self.pdf_dir and self.pdf_dir.exists():
            for doi in rec.dois:
                for cand in (self.pdf_dir / f"{doi.replace('/', '_')}.pdf",
                             self.pdf_dir / f"{doi.replace('/', '_').replace(':', '_')}.pdf"):
                    if cand.exists():
                        b = pdf_blocks(cand.read_bytes())
                        if b:
                            return b, f"local PDF ({cand.name})"
        return [], None

    # classification -----------------------------------------------------
    def score(self, sentence: str) -> float:
        """Similarity to the reference statement: the larger of (a) the share of the reference
        statement's content words found in the sentence and (b) difflib's SequenceMatcher ratio
        between the two normalised word sequences."""
        words = norm_words(sentence, self.expansions)
        recall = len(set(words) & self.canon_words) / max(1, len(self.canon_words))
        ratio = SequenceMatcher(None, " ".join(words), self.canon_norm).ratio()
        return round(max(recall, ratio), 3)

    @staticmethod
    def sentences(text: str) -> list[str]:
        parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"“])", text)
        return [p.strip() for p in parts if p.strip()]

    def classify(self, blocks: list[tuple[str, str, str]]) -> list[Match]:
        found, seen = [], set()
        for text, sec, cat in blocks:
            if not (self.fac.search(text) or self._fund(text)):
                continue
            sents = [text] if cat == "Affiliation" else self.sentences(text)
            for s in sents:
                has_fac, has_fund = bool(self.fac.search(s)), self._fund(s)
                if not (has_fac or has_fund):
                    continue
                key = (re.sub(r"\W+", "", s.lower())[:200], cat)
                if key in seen:
                    continue
                seen.add(key)
                sc = self.score(s)
                if cat == "References":
                    klass = "Excluded"
                elif cat == "Affiliation":
                    klass = "Affiliation only" if has_fac else "Excluded"
                elif has_fac and cat == "Unknown (PDF)" and not STATEMENT_VERB.search(s):
                    # PDF text before the first heading: an author-affiliation line, not a statement
                    klass = "Affiliation only"
                elif has_fac:
                    klass = "Canonical statement" if sc >= self.cfg["canonical_threshold"] else "Variant wording"
                elif ((self.ctx is None or self.ctx.search(s)) and not (self.other and self.other.search(s))
                      and not self._out_of_scope(s)):
                    klass = "Review"
                else:
                    klass = "Excluded"
                found.append(Match(sentence=s[:1200], section=sec, category=cat, klass=klass, score=sc))
        return found

    def facility_affiliations(self, rec: Record) -> tuple[list[str], list[str], str]:
        """Authors who list the facility as an affiliation: (names, affiliation strings, where seen).

        Sources: the full-text author list (JATS), Europe PMC / PubMed author affiliations,
        OpenAlex raw affiliation strings. Names are taken from the first source that has them.
        """
        found: list[tuple[str, list[str], list[str]]] = []
        if rec.jats:
            n, a = jats_facility_authors(rec.jats, self.fac)
            if a:
                found.append(("full-text author list", n, a))
        n, a = [], []
        for e in rec.epmc:
            for au in ((e.get("authorList") or {}).get("author") or []):
                texts = [x.get("affiliation") or "" for x in
                         ((au.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or [])]
                texts.append(au.get("affiliation") or "")
                hit = [t for t in texts if t and self.fac.search(t)]
                if hit:
                    n.append(_person_epmc(au) or "")
                    a += hit
        if a:
            found.append(("Europe PMC/PubMed metadata", n, a))
        n, a = [], []
        for au in ((rec.openalex or {}).get("authorships") or []):
            hit = [t for t in (au.get("raw_affiliation_strings") or []) if self.fac.search(t)]
            if hit:
                n.append(au.get("raw_author_name") or (au.get("author") or {}).get("display_name") or "")
                a += hit
        if a:
            found.append(("OpenAlex metadata", n, a))
        names = next((x[1] for x in found if any(x[1])), [])
        affs = list(dict.fromkeys(t for x in found for t in x[2]))
        return list(dict.fromkeys(x for x in names if x)), affs, "; ".join(x[0] for x in found)

    @staticmethod
    def _terms(rx: re.Pattern, text: str) -> tuple[int, list[str]]:
        hits = [m.group(0).lower() for m in rx.finditer(text)]
        return len(hits), sorted(set(hits), key=lambda t: -hits.count(t))

    def ms_type(self, rec: Record, blocks: list[tuple[str, str, str]]) -> tuple[str, str] | None:
        """Scope check: is the credited work in scope (e.g. proteomics) or out of scope
        (e.g. small-molecule MS)? Does nothing when `scope_check` is disabled.

        Sets rec.ms_type / rec.ms_evidence. Returns (status, reason) when the paper should leave the
        report: ("Excluded", ...) when the facility sentence itself is about out-of-scope work, or
        ("Review", ...) when the sentence is generic and the paper describes only out-of-scope work.
        """
        sc = self.scope
        if sc is None:
            return None
        stmts = [m.sentence for m in rec.matches if m.klass in ("Canonical statement", "Variant wording")]
        # the facility's own name may contain an in-scope word ("proteomics"), so remove it first
        bare = [self.fac.sub(" ", s) for s in stmts]
        body = self.fac.sub(" ", " ".join(t for t, _, c in blocks if c not in ("References", "Affiliation")))
        n_in, t_in = self._terms(sc.in_rx, body)
        n_out, t_out = self._terms(sc.out_rx, body)
        out_fac = sorted({m.group(0) for m in sc.out_facility_rx.finditer(body)}) if sc.out_facility_rx else []
        lin, lout = lcfirst(sc.in_label), lcfirst(sc.out_label)
        ev = [f"{lin} terms in text: {n_in}" + (f" ({', '.join(t_in[:5])})" if t_in else ""),
              f"{lout} terms: {n_out}" + (f" ({', '.join(t_out[:5])})" if t_out else "")]
        if out_fac:
            ev.append(f"{lout} facility named: " + ", ".join(out_fac[:3]))

        def names_out_facility(text: str) -> bool:
            return bool(sc.out_facility_rx and sc.out_facility_rx.search(text))

        if any(sc.in_rx.search(b) for b in bare):
            rec.ms_type, how = sc.in_label, f"facility sentence names {lin}"
        elif any(sc.out_rx.search(b) and not names_out_facility(b) for b in bare):
            rec.ms_type, rec.ms_evidence = sc.out_label, "; ".join([f"facility sentence names {lout}"] + ev)
            rec.out_of_scope = True
            return "Excluded", f"{sc.out_label}" + (f" ({sc.out_note})" if sc.out_note else "") + \
                ": the facility sentence describes it."
        elif n_in >= sc.in_min or (n_in and n_out < sc.out_min and not out_fac):
            rec.ms_type, how = sc.in_label, f"generic facility sentence; {lin} workflow in the text"
        elif n_out >= sc.out_min or out_fac:
            rec.ms_type, rec.ms_evidence = sc.review_label, "; ".join(["generic facility sentence"] + ev)
            return "Review", (f"Generic facility sentence, and the paper describes {lout} but no {lin} "
                              "workflow: possibly another facility's work.")
        else:
            rec.ms_type, how = sc.unknown, f"no {lin} or {lout} terms found in the retrieved text"
        if rec.ms_type == sc.in_label and (n_out >= sc.out_min or out_fac):
            rec.ms_type = sc.mixed
        rec.ms_evidence = "; ".join([how] + ev)
        return None

    def verify(self, rec: Record) -> None:
        blocks, src = self.fetch(rec)
        rec.blocks = blocks
        rec.fulltext_source = src
        rec.matches = self.classify(blocks) if blocks else []
        rec.facility_authors, rec.facility_affs, rec.facility_aff_source = self.facility_affiliations(rec)
        classes = {m.klass for m in rec.matches}
        has_member = bool(rec.facility_affs) or "Affiliation only" in classes
        if rec.facility_affs and "Affiliation only" not in classes:
            # seen only in database metadata: record it so the report can quote it
            rec.matches.append(Match(sentence=rec.facility_affs[0][:1200],
                                     section=f"Author affiliations ({rec.facility_aff_source})",
                                     category="Affiliation", klass="Affiliation only", score=0.0))
        if classes & {"Canonical statement", "Variant wording"}:
            rec.status = "Confirmed"
            rec.basis = BASIS_BOTH if has_member else BASIS_STATEMENT
            verdict = self.ms_type(rec, blocks)
            if verdict:                                   # small-molecule MS: not this facility's work
                rec.status, rec.reason = verdict
        elif has_member:
            rec.status = "Affiliation only"
            rec.basis = BASIS_MEMBER
            rec.reason = ("No facility statement in the full text; included because a core facility member is "
                          "listed as author." if blocks else
                          "Full text not retrievable, so acknowledgements were not checked; included because a "
                          "core facility member is listed as author (database metadata).")
        elif not blocks:
            rec.status = "Unverified"
            rec.reason = "Full text not retrievable (paywalled or not indexed); matched only in a search index."
        elif "Review" in classes:
            rec.status = "Review"
            rec.reason = (f"Funding-body sentence mentions {self.cfg.get('context_label') or 'the field'} "
                          "but does not name the facility.")
        else:
            rec.status = "Excluded"
            if any(m.category == "References" for m in rec.matches) and not any(
                    m.category != "References" for m in rec.matches):
                rec.reason = "Phrase occurs only in the reference list."
            elif any(self._out_of_scope(m.sentence) for m in rec.matches if m.klass == "Excluded"):
                rec.reason = f"Funding-body sentence concerns {self.scope.out_text}."
            elif rec.matches:
                rec.reason = "Funding-body sentence concerns another facility."
            else:
                rec.reason = "Phrase not found in the retrieved full text."


# -------------------------------------------------------------- metadata
def _epmc_primary(rec: Record) -> dict | None:
    if not rec.epmc:
        return None
    pref = {"MED": 0, "PMC": 1, "PPR": 2}
    return sorted(rec.epmc, key=lambda e: (0 if e.get("doi") else 1, pref.get(e.get("source"), 3)))[0]


def _person_epmc(a: dict) -> str | None:
    if a.get("lastName"):
        return " ".join(x for x in (a.get("firstName"), a.get("lastName")) if x)
    return a.get("collectiveName") or a.get("fullName")


def build_meta(rec: Record, year_basis: str, oa_sources: dict, jif: "JifTable | None",
               searcher: Searcher, other_prefixes: dict[str, str] | None = None) -> dict:
    other_prefixes = other_prefixes or {}
    e = _epmc_primary(rec)
    w = rec.openalex or {}
    b = rec.biorxiv
    doi = None
    # prefer the DOI of the version that carries the statement / the EPMC primary record
    for cand in ([norm_doi(e.get("doi")) if e else None, norm_doi(w.get("doi"))] + rec.dois):
        if cand:
            doi = cand
            break
    title = clean_text((e or {}).get("title") or w.get("title") or (b or {}).get("title")
                       or (rec.pmc_summary or {}).get("title"))

    # kind ---------------------------------------------------------------
    pubtypes = [p for x in rec.epmc for p in (x.get("pubTypeList", {}) or {}).get("pubType", [])]
    pfx = doi_prefix(doi)
    if (e and e.get("source") == "PPR") or "Preprint" in pubtypes or pfx in PREPRINT_PREFIXES \
            or w.get("type") == "preprint":
        kind = "Preprint"
    elif pfx in other_prefixes or (w and w.get("type") not in (None, "article", "review", "letter", "editorial")
                                  and not e):
        kind = "Other"
    else:
        kind = "Published article"

    # authors -------------------------------------------------------------
    first_author = last_author = None
    auths = [a for a in ((e or {}).get("authorList", {}) or {}).get("author", []) if a.get("lastName")]
    oa_auths = w.get("authorships") or []
    if oa_auths:
        def nm(a):
            return a.get("raw_author_name") or (a.get("author") or {}).get("display_name")
        first_author, last_author = nm(oa_auths[0]), nm(oa_auths[-1])
    if auths and (not first_author or len(auths) >= len(oa_auths)):
        first_author, last_author = _person_epmc(auths[0]), _person_epmc(auths[-1])
    if not first_author and b and b.get("authors"):
        al = [x.strip() for x in b["authors"].split(";") if x.strip()]
        first_author, last_author = al[0], al[-1]
    if not first_author and rec.pmc_summary:
        al = [a.get("name") for a in rec.pmc_summary.get("authors", []) if a.get("name")]
        if al:
            first_author, last_author = al[0], al[-1]
    n_auth = max(len(auths), len(oa_auths))

    # journal -------------------------------------------------------------
    src = (w.get("primary_location") or {}).get("source") or {}
    oa_src = oa_sources.get((src.get("id") or "").rsplit("/", 1)[-1]) if src else None
    ji = ((e or {}).get("journalInfo") or {}).get("journal") or {}
    issns = [x for x in [ji.get("issn"), ji.get("essn")] + (src.get("issn") or []) if x]
    if kind == "Preprint":
        journal = (b or {}).get("_server", "").replace("biorxiv", "bioRxiv").replace("medrxiv", "medRxiv") \
                  or ((e or {}).get("bookOrReportDetails") or {}).get("publisher") \
                  or PREPRINT_PREFIXES.get(pfx) or src.get("display_name") or "Preprint server"
    elif kind == "Other":
        journal = other_prefixes.get(pfx) or src.get("display_name") or \
                  (w.get("primary_location") or {}).get("raw_source_name") or ""
    else:
        journal = src.get("display_name") or ji.get("title") or (rec.pmc_summary or {}).get("fulljournalname") or ""
        if not oa_src and issns:
            oa_src = searcher.openalex_source_by_issn(issns[0])
            if oa_src and not src.get("display_name"):
                journal = oa_src.get("display_name") or journal

    # impact factor -------------------------------------------------------
    if_proxy = jif_val = jif_year = None
    if kind == "Published article":
        if oa_src:
            if_proxy = (oa_src.get("summary_stats") or {}).get("2yr_mean_citedness")
            issns = list(dict.fromkeys(issns + (oa_src.get("issn") or [])))
        if jif:
            jif_val, jif_year = jif.lookup(issns, journal)

    # dates ------------------------------------------------------------------
    first_date = (e or {}).get("firstPublicationDate") or w.get("publication_date") \
        or (b or {}).get("_first_date") or (rec.pmc_summary or {}).get("epubdate") or ""
    issue_year = (((e or {}).get("journalInfo") or {}).get("yearOfPublication")
                  or (e or {}).get("pubYear") or w.get("publication_year")
                  or (first_date[:4] if first_date else None))
    first_year = int(first_date[:4]) if re.match(r"\d{4}", str(first_date)) else None
    issue_year = int(issue_year) if issue_year else None
    year = first_year if year_basis == "first" else (issue_year or first_year)

    # preprint -> published version ------------------------------------------
    published_version = ""
    if kind == "Preprint":
        if b and b.get("published") and b["published"] not in ("NA", ""):
            published_version = norm_doi(b["published"])
        else:
            for x in rec.epmc:
                for cc in ((x.get("commentCorrectionList") or {}).get("commentCorrection") or []):
                    if cc.get("type") == "Preprint of":
                        published_version = f"PMID:{cc.get('id')} ({cc.get('reference', '')})"
    return dict(
        year=year, first_pub_date=first_date, issue_year=issue_year, title=title, doi=doi or "",
        first_author=first_author or "", last_author=last_author or "", n_authors=n_auth or "",
        journal=journal, kind=kind, if_proxy=if_proxy, jif=jif_val, jif_year=jif_year,
        pmid=rec.first("pmid") or "", pmcid=rec.first("pmcid") or "",
        published_version=published_version,
    )


class JifTable:
    """Optional Clarivate JIF table exported from JCR (CSV/XLSX).

    Needs a JIF column (name containing 'JIF' or 'impact factor') and at least one of
    ISSN / eISSN / journal name.
    """

    def __init__(self, path: Path):
        df = pd.read_excel(path) if path.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(path)
        cols = {str(c).lower().strip(): c for c in df.columns}

        def pick(*names, exclude=()):
            for n in names:
                for lc, c in cols.items():
                    if n in lc and not any(x in lc for x in exclude):
                        return c
            return None
        self.c_jif = pick("jif", "impact factor", exclude=("quartile", "percentile", "rank", "without"))
        self.c_eissn = pick("eissn", "e-issn", "electronic issn")
        self.c_issn = pick("issn", exclude=("eissn", "e-issn", "electronic"))
        self.c_name = pick("journal name", "full journal title", "journal", "title", exclude=("abbrev",))
        self.c_year = pick("year")
        if not self.c_jif:
            raise ValueError(f"No JIF column found in {path}")
        self.by_issn, self.by_name = {}, {}
        for _, row in df.iterrows():
            try:
                val = float(str(row[self.c_jif]).replace(",", "."))
            except ValueError:
                continue
            yr = row[self.c_year] if self.c_year else None
            for c in (self.c_issn, self.c_eissn):
                if c and isinstance(row[c], str) and row[c].strip():
                    self.by_issn[row[c].strip().upper()] = (val, yr)
            if self.c_name and isinstance(row[self.c_name], str):
                self.by_name[norm_title(row[self.c_name])] = (val, yr)

    def lookup(self, issns: list[str], name: str) -> tuple[float | None, Any]:
        for i in issns:
            if i and i.upper() in self.by_issn:
                return self.by_issn[i.upper()]
        return self.by_name.get(norm_title(name), (None, None))


def best_match(rec: Record) -> Match | None:
    order = {"Canonical statement": 0, "Variant wording": 1, "Review": 2, "Affiliation only": 3, "Excluded": 4}
    cat_order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    ms = sorted(rec.matches, key=lambda m: (order[m.klass], cat_order.get(m.category, 99), -m.score))
    return ms[0] if ms else None


def row_for(rec: Record) -> dict:
    def order(c):
        return CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99
    counted = [x for x in rec.matches if x.klass in ("Canonical statement", "Variant wording")]
    bm = best_match(rec)
    cats = sorted({x.category for x in counted}, key=order)
    if len(cats) > 1 and "Unknown (PDF)" in cats:
        cats.remove("Unknown (PDF)")          # a known heading is more informative
    paths = []
    for x in sorted(counted, key=lambda x: order(x.category)):
        if x.category in cats and x.section not in paths:
            paths.append(x.section)
    aff = next((x for x in rec.matches if x.klass == "Affiliation only"), None)
    stmt = bm if bm and bm.klass != "Affiliation only" else None
    manual_include = bool(rec.manual and rec.manual["decision"] == "include" and rec.basis == BASIS_MANUAL)
    return {**rec.meta,
            "inclusion_basis": rec.basis,
            "ms_type": rec.ms_type, "ms_evidence": rec.ms_evidence,
            "facility_authors": "; ".join(rec.facility_authors),
            "facility_affiliation": rec.facility_affs[0] if rec.facility_affs else (aff.sentence if aff else ""),
            "facility_aff_source": rec.facility_aff_source or ("full text" if aff else ""),
            "statement_type": BASIS_MANUAL if manual_include else (stmt.klass if stmt else ""),
            "similarity": stmt.score if stmt else None,
            "sections": "; ".join(cats),
            "section_paths": "; ".join(paths) or (stmt.section if stmt else (aff.section if aff else "")),
            "statement_text": stmt.sentence if stmt else "",
            "fulltext_source": rec.fulltext_source or "",
            "found_by": "; ".join(rec.found_by),
            "reason": rec.reason,
            "out_of_scope": rec.out_of_scope}

