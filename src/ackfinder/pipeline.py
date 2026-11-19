"""Run the whole search → merge → metadata → verify → report pipeline for a config."""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from .core import (BASIS_MANUAL, Http, Item, JifTable, Searcher, Verifier, build_meta, doi_prefix,
                   merge_items, norm_doi, row_for)
from .report import fmt_num, write_audit, write_overview, write_reports

log = logging.getLogger("ackfinder")


@dataclass
class RunSettings:
    """Everything a run needs besides the facility patterns. Built from the config, then CLI overrides."""
    years: list[int]
    output_dir: Path
    cache_dir: Path | None
    pdf_dir: Path | None
    jif_table: Path | None = None
    year_basis: str = "first"
    include_facility_authors: bool = False
    use_openalex: bool = True
    use_pmc: bool = True
    manual_decisions: Path | None = None
    run_date: str = field(default_factory=lambda: dt.date.today().isoformat())

    @property
    def statement_only(self) -> bool:
        return not self.include_facility_authors

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "RunSettings":
        years = overrides.pop("years", None) or cfg.get("years") or [dt.date.today().year]
        s = cls(years=sorted({int(y) for y in years}), output_dir=cfg["output_dir"], cache_dir=cfg["cache_dir"],
                pdf_dir=cfg["pdf_dir"], jif_table=cfg.get("jif_table"), year_basis=cfg["year_basis"],
                include_facility_authors=bool(cfg["include_facility_authors"]),
                manual_decisions=cfg.get("manual_decisions"))
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, v)
        return s


# ------------------------------------------------------------------ manual decisions
def load_decisions(path: Path | None) -> dict[str, dict]:
    """Manual decisions per DOI from a YAML list or a CSV/XLSX table.

    Each entry: doi, decision (include | exclude), reason; optional decided_by, date.
    """
    if not path or not Path(path).is_file():
        return {}
    path = Path(path)
    if path.suffix.lower() in (".csv", ".xlsx", ".xls"):
        df = pd.read_excel(path) if path.suffix.lower() != ".csv" else pd.read_csv(path)
        rows = df.fillna("").to_dict("records")
    else:
        rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(rows, dict):
            rows = rows.get("decisions") or []
    out = {}
    for i, r in enumerate(rows, 1):
        r = {str(k).strip().lower(): v for k, v in dict(r).items()}
        doi = norm_doi(str(r.get("doi") or ""))
        decision = str(r.get("decision") or "").strip().lower()
        if not doi or decision not in ("include", "exclude"):
            log.warning("manual decision %d in %s ignored: needs a DOI and decision include/exclude", i, path.name)
            continue
        out[doi] = {"doi": doi, "decision": decision, "reason": str(r.get("reason") or "").strip(),
                    "decided_by": str(r.get("decided_by") or "").strip(), "date": str(r.get("date") or "").strip()}
    return out


def apply_decision(rec, d: dict, verifier: Verifier) -> None:
    rec.manual = d
    who = f" ({d['decided_by']}{', ' + d['date'] if d['date'] else ''})" if d["decided_by"] else ""
    note = f"Manual decision{who}: {d['reason'] or d['decision']}"
    if d["decision"] == "include":
        if rec.status != "Confirmed" or rec.out_of_scope:
            rec.status, rec.basis, rec.out_of_scope = "Confirmed", BASIS_MANUAL, False
            verifier.ms_type(rec, rec.blocks)     # record the scope call; the decision stands either way
            rec.out_of_scope = False
        rec.reason = note if not rec.reason else f"{note}. Automatic call: {rec.reason}"
    else:
        auto = (rec.status or "") + (f" ({rec.reason})" if rec.reason else "")
        rec.status, rec.reason = "Excluded", note + (f". Automatic call: {auto}" if auto else "")


# ------------------------------------------------------------------ run
def run(cfg: dict, settings: RunSettings) -> dict:
    """Run the pipeline and write the reports.

    Returns {"years": {year: counts and report path}, "overview": path, "audit": path, "http_requests": n}.
    """
    s = settings
    http = Http(s.cache_dir)
    searcher = Searcher(http, cfg, os.environ.get("OPENALEX_API_KEY"), os.environ.get("NCBI_API_KEY"))
    y0, y1 = min(s.years), max(s.years)
    jif = JifTable(s.jif_table) if s.jif_table else None
    use_aff = not s.statement_only
    decisions = load_decisions(s.manual_decisions)
    if decisions:
        log.info("%d manual decisions loaded from %s", len(decisions), s.manual_decisions)

    # 1. search -------------------------------------------------------------
    items = searcher.epmc_search(y0, y1, affiliations=use_aff)
    if s.use_pmc:
        items += searcher.pmc_search(y0, y1)
    if use_aff:
        items += searcher.pubmed_aff_search(y0, y1)
    if s.use_openalex:
        items += searcher.openalex_search(y0, y1, affiliations=use_aff)
    # papers named in manual "include" decisions are always candidates, even if no index found them
    manual_dois = [d for d, x in decisions.items() if x["decision"] == "include"]
    if manual_dois:
        epmc = searcher.epmc_lookup("DOI", manual_dois)
        for d in manual_dois:
            e = (epmc.get(d) or [None])[0]
            items.append(Searcher._item_from_epmc(e, "Manual decision", "manual decision") if e else
                         Item(source="Manual decision", phrase="manual decision", doi=d))
    recs = merge_items(items)
    log.info("%d search hits -> %d unique candidate records", len(items), len(recs))

    # 2. metadata (OpenAlex works + sources, bioRxiv) -----------------------
    all_dois = sorted({d for r in recs for d in r.dois})
    works = searcher.openalex_works(all_dois) if s.use_openalex else {}
    for r in recs:
        if not r.openalex:
            r.openalex = next((works[d] for d in r.dois if d in works), None)
        for d in r.dois:
            if doi_prefix(d) in ("10.1101", "10.64898") and not r.biorxiv:
                r.biorxiv = searcher.biorxiv_details(d)
    src_ids = [((r.openalex or {}).get("primary_location") or {}).get("source", {}) or {} for r in recs]
    oa_sources = searcher.openalex_sources([x.get("id") for x in src_ids if x]) if s.use_openalex else {}
    for r in recs:
        r.meta = build_meta(r, s.year_basis, oa_sources, jif, searcher, cfg.get("other_doi_prefixes"))

    # 3. verify candidates that fall in the requested years ------------------
    in_range = [r for r in recs if r.meta["year"] in s.years]
    log.info("%d candidates fall in %s; verifying full texts …", len(in_range), s.years)
    verifier = Verifier(http, cfg, s.pdf_dir, searcher)
    for i, r in enumerate(in_range, 1):
        verifier.verify(r)
        d = next((decisions[x] for x in r.dois if x in decisions), None)
        if d:
            apply_decision(r, d, verifier)
        log.info("[%3d/%d] %-16s %-34s %s", i, len(in_range), r.status,
                 (r.meta['doi'] or r.first('pmcid') or '')[:34], (r.fulltext_source or "")[:40])
    unused = set(decisions) - {d for r in in_range for d in r.dois}
    for d in sorted(unused):
        log.warning("manual decision for %s not applied: not a candidate published in %s", d, s.years)

    # 4. reports -------------------------------------------------------------
    run_info = {"date": s.run_date, "n_candidates": len(recs), "search_status": searcher.status,
                "n_manual": len(decisions)}
    overview, result = [], {"years": {}}
    for y in s.years:
        sel = [r for r in in_range if r.meta["year"] == y]
        member_ok = not s.statement_only
        if not member_ok:
            for r in sel:
                if r.status == "Affiliation only":
                    r.reason = ("No facility statement found; a facility member is listed as author (listed only "
                                "with include_facility_authors).")
        rows = [row_for(r) for r in sel if r.status == "Confirmed" or (r.status == "Affiliation only" and member_ok)]
        side = {"review": [row_for(r) for r in sel if r.status == "Review"],
                "unverified": [row_for(r) for r in sel if r.status == "Unverified"],
                "excluded": [row_for(r) for r in sel if r.status == "Excluded"
                             or (r.status == "Affiliation only" and not member_ok)]}
        S = write_reports(y, rows, side, cfg, s, s.output_dir / str(y), run_info)
        n = {k: len(v) for k, v in side.items()}
        overview.append((y, S, n))
        result["years"][y] = {**{k: S[k] for k in ("total", "published", "preprints", "other", "with_statement",
                                                    "member_only", "manual", "mean_if_proxy", "left_out_scope")},
                              "needs_review": n["review"], "unverified": n["unverified"], "excluded": n["excluded"],
                              "report": str(s.output_dir / str(y) / f"{cfg['report_slug']}_{y}.md")}
        log.info("%d: %d papers (%d with facility statement, %d facility member only, %d manual; %d published, "
                 "%d preprints, %d other); mean IF proxy %s; needs review %d, unverified %d", y, S["total"],
                 S["with_statement"], S["member_only"], S["manual"], S["published"], S["preprints"], S["other"],
                 fmt_num(S["mean_if_proxy"], 2), n["review"], n["unverified"])

    # overview + audit trail -------------------------------------------------
    result["overview"] = str(write_overview(s.output_dir, cfg, overview, s.statement_only, s.run_date))
    result["audit"] = str(write_audit(s.output_dir, in_range, searcher.status, s.run_date))
    result["http_requests"] = http.calls
    log.info("Done. %d HTTP requests. Reports in %s", http.calls, s.output_dir)
    return result
