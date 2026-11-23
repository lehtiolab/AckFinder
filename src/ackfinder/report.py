"""Report writers: one Markdown + CSV + Excel report per year, an overview README and an audit JSON.

All facility-specific wording comes from the config (facility_label, funding_label, context_label,
scope_check labels); nothing about a particular facility is hard-coded here.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import pandas as pd

from .core import (BASIS_BOTH, BASIS_MANUAL, BASIS_MEMBER, BASIS_STATEMENT, CATEGORY_ORDER, Scope,
                   __version__, lcfirst)


def fmt_num(x, nd=1):
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else f"{x:.{nd}f}"


def md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _mean(xs):
    return statistics.mean(xs) if xs else None


def _median(xs):
    return statistics.median(xs) if xs else None


def _lists(cfg: dict | None) -> tuple[bool, bool]:
    """(facility_staff set, requesters set)."""
    cfg = cfg or {}
    return bool(cfg.get("facility_staff")), bool(cfg.get("requesters"))


def paper_cols(scope: Scope | None, cfg: dict | None = None) -> list[tuple[str, str]]:
    """Column name in the row dicts -> header in the Excel / CSV files."""
    staff, req = _lists(cfg)
    cols = [
        ("title", "Title"), ("doi", "DOI"), ("first_author", "First author"), ("last_author", "Last author"),
        ("journal", "Journal"), ("kind", "Type"), ("ms_type", scope.column if scope else None),
        ("inclusion_basis", "Inclusion basis"), ("has_statement", "Facility statement"),
        ("facility_authors", "Facility author(s)"), ("requester_authors", "Requester author(s)" if req else None),
        ("if_proxy", "IF proxy (OpenAlex 2-yr mean citedness)"),
        ("jif", "JIF (Clarivate)"), ("jif_year", "JIF year"), ("first_pub_date", "First published"),
        ("issue_year", "Issue year"), ("statement_type", "Statement wording"),
        ("similarity", "Similarity to reference statement"), ("sections", "Statement section(s)"),
        ("section_paths", "Section path(s)"), ("statement_text", "Statement as found"),
        ("ms_evidence", scope.evidence_column if scope else None),
        ("facility_affiliation", "Facility affiliation as found"), ("facility_aff_source", "Affiliation seen in"),
        ("fulltext_source", "Full-text source"), ("found_by", "Found by"),
        ("author_search_only", "Found only by author search" if staff or req else None),
        ("pmid", "PMID"), ("pmcid", "PMCID"),
        ("published_version", "Published version (preprints)"), ("n_authors", "No. of authors"), ("reason", "Note"),
    ]
    return [(k, h) for k, h in cols if h]


def side_cols(scope: Scope | None, cfg: dict | None = None) -> list[tuple[str, str]]:
    cols = [
        ("title", "Title"), ("doi", "DOI"), ("journal", "Journal"), ("kind", "Type"),
        ("first_pub_date", "First published"), ("found_by", "Found by"), ("fulltext_source", "Full-text source"),
        ("reason", "Note"), ("statement_text", "Sentence found"), ("section_paths", "Section path(s)"),
    ]
    if _lists(cfg)[1]:
        cols.insert(4, ("requester_authors", "Requester author(s)"))
    return cols + ([("ms_evidence", scope.evidence_column)] if scope else [])


COL_WIDTHS = {"Title": 60, "Statement as found": 70, "Facility affiliation as found": 50, "Sentence found": 70,
              "Section path(s)": 30, "Found by": 40, "Note": 50, "Journal": 28, "DOI": 30, "First author": 22,
              "Last author": 22, "Inclusion basis": 30, "Facility author(s)": 30, "Affiliation seen in": 24,
              "Requester author(s)": 30, "Facility statement": 12}


def summarise(rows: list[dict], use_jif: bool, scope: Scope | None) -> dict:
    pub = [r for r in rows if r["kind"] == "Published article"]
    pre = [r for r in rows if r["kind"] == "Preprint"]
    oth = [r for r in rows if r["kind"] == "Other"]
    stmt = [r for r in rows if r["inclusion_basis"] in (BASIS_STATEMENT, BASIS_BOTH)]
    member = [r for r in rows if r["inclusion_basis"] == BASIS_MEMBER]
    manual = [r for r in rows if r["inclusion_basis"] == BASIS_MANUAL]
    ifs = [r["if_proxy"] for r in pub if r["if_proxy"] is not None]
    ifs_stmt = [r["if_proxy"] for r in pub if r["if_proxy"] is not None and r["inclusion_basis"] != BASIS_MEMBER]
    jifs = [r["jif"] for r in pub if r["jif"] is not None]
    scoped = stmt + manual

    def secs(r):
        return [s for s in r["sections"].split("; ") if s]
    return dict(
        total=len(rows), published=len(pub), preprints=len(pre), other=len(oth),
        preprints_with_published_version=sum(bool(r["published_version"]) for r in pre),
        with_statement=len(stmt), statement_and_member=sum(r["inclusion_basis"] == BASIS_BOTH for r in rows),
        member_only=len(member), manual=len(manual),
        member_only_published=sum(r["kind"] == "Published article" for r in member),
        member_only_preprints=sum(r["kind"] == "Preprint" for r in member),
        member_only_unchecked=sum(not r["fulltext_source"] for r in member),
        mean_if_proxy=_mean(ifs), median_if_proxy=_median(ifs), n_if_proxy=len(ifs),
        mean_if_proxy_statement=_mean(ifs_stmt), n_if_proxy_statement=len(ifs_stmt),
        mean_jif=_mean(jifs), median_jif=_median(jifs), n_jif=len(jifs),
        canonical=sum(r["statement_type"] == "Canonical statement" for r in stmt),
        variant=sum(r["statement_type"] == "Variant wording" for r in stmt),
        ms_prot=sum(r["ms_type"] == scope.in_label for r in scoped) if scope else 0,
        ms_mixed=sum(r["ms_type"] == scope.mixed for r in scoped) if scope else 0,
        ms_unknown=sum(r["ms_type"] == scope.unknown for r in scoped) if scope else 0,
        by_section={c: sum(c in secs(r) for r in stmt) for c in CATEGORY_ORDER if any(c in secs(r) for r in stmt)},
        found_only_by_staff=sum("staff" in r.get("author_search_only", "") for r in rows),
        found_only_by_requester=sum("requester" in r.get("author_search_only", "") for r in rows),
        with_requester=sum(bool(r.get("requester_authors")) for r in rows),
        use_jif=use_jif,
    )


def _statement_cell(r: dict) -> str:
    """'Yes, canonical' / 'Yes, variant' / 'No' / 'Not checked' for the papers table."""
    if r["has_statement"] != "Yes":
        return r["has_statement"]
    return "Yes, " + lcfirst(r["statement_type"].replace(" statement", "").replace(" wording", "") or "found")


def _search_rows(status: list[dict]) -> list[dict]:
    """The search table, with the per-person author queries summed into one row per list."""
    out: list[dict] = []
    groups: dict[tuple, dict] = {}
    for s in status:
        if not (s["source"] == "Europe PMC" and s["field"].startswith("author (")):
            out.append(s)
            continue
        g = groups.get((s["source"], s["field"]))
        if g is None:
            g = groups[(s["source"], s["field"])] = dict(source=s["source"], field=s["field"], n=0, hits=0,
                                                         zero=[], failed=0)
            out.append(g)
        g["n"] += 1
        g["hits"] += s["hits"] or 0
        g["zero"] += [s["phrase"]] if s["hits"] == 0 else []
        g["failed"] += s["hits"] is None or str(s["note"]).startswith("failed")
    for g in groups.values():
        notes = ([f"no hits: {', '.join(g['zero'][:10])}" + (" …" if len(g["zero"]) > 10 else "")] if g["zero"]
                 else []) + ([f"{g['failed']} queries failed"] if g["failed"] else [])
        g.update(phrase=f"{g.pop('n')} people (name, and ORCID when given)", note="; ".join(notes))
        del g["zero"], g["failed"]
    return out


def _doi_md(doi: str) -> str:
    return f"[{doi}](https://doi.org/{doi})" if doi else ""


def _review_note(cfg: dict, scope: Scope | None) -> str:
    parts = []
    if cfg.get("funding_regex"):
        parts.append(f"a sentence credits {cfg.get('funding_label') or 'the funding body'}"
                     + (f" and mentions {cfg['context_label']}" if cfg.get("context_label") else "")
                     + " but does not name the facility")
    if scope:
        parts.append(f"the facility is credited in generic terms and the paper describes only "
                     f"{lcfirst(scope.out_label)}")
    if not parts:
        return "These papers are not counted above."
    text = parts[0] if len(parts) == 1 else "Either " + ", or ".join(parts)
    return text[0].upper() + text[1:] + ". These papers are not counted above."


def write_reports(year: int, rows: list[dict], side: dict[str, list[dict]], cfg: dict, settings,
                  outdir: Path, run_info: dict) -> dict:
    """Write <slug>_<year>.md/.csv/.xlsx into `outdir` and return the summary numbers.

    `settings` needs: year_basis, jif_table.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    scope = Scope.from_cfg(cfg)
    staff, req = _lists(cfg)
    slug = cfg["report_slug"]
    S = summarise(rows, use_jif=bool(settings.jif_table), scope=scope)
    rows = sorted(rows, key=lambda r: (r["kind"] != "Published article", r["kind"], r["first_pub_date"] or "",
                                       r["title"]))
    basis = ("first publication date (earliest of online/print/preprint posting)" if settings.year_basis == "first"
             else "journal issue year")
    n_out = sum(bool(r.get("out_of_scope")) for r in side["excluded"])

    # ---------------- markdown
    intro = ("A paper is included when its full text names the facility in the acknowledgements, funding statement "
             f"or methods (*{BASIS_STATEMENT}*), or when the facility is listed as author: an author's affiliation "
             "names the facility" + (", or an author is on the facility staff list" if staff else "")
             + f" (*{BASIS_MEMBER}* when the paper has no facility statement). The *Statement* column says whether "
             "a facility statement was found: *Yes*, *No*, or *Not checked* (full text not available).")
    if req:
        intro += " Papers by the listed requesters were searched too; they count only with a facility statement."
    if scope:
        intro += (f" Papers whose credited work is {scope.out_text} are left out; the *{scope.column}* column gives "
                  "the call for each paper.")
    if S["manual"]:
        intro += f" {S['manual']} paper(s) were added by a manual decision (*{BASIS_MANUAL}*)."
    L = [f"# {cfg['facility_label']}: publications, {year}\n",
         f"Generated {run_info['date']} by AckFinder {__version__}. "
         f"Year assigned by {basis}.\n",
         intro + "\n",
         "## Summary\n", "| Metric | Value |\n|---|---|",
         f"| **Total papers** | **{S['total']}** |",
         f"| Published (peer-reviewed journal articles) | {S['published']} |",
         f"| Preprints | {S['preprints']} |"]
    if S["other"]:
        L.append(f"| Other (theses, reports) | {S['other']} |")
    if S["preprints"]:
        L.append(f"| Preprints that already have a published version | {S['preprints_with_published_version']} |")
    if S["use_jif"]:
        L.append(f"| Mean journal impact factor (Clarivate JIF; n = {S['n_jif']} of {S['published']}) | "
                 f"{fmt_num(S['mean_jif'], 2)} (median {fmt_num(S['median_jif'], 2)}) |")
    L.append(f"| Mean impact factor, open proxy: OpenAlex 2-yr mean citedness (n = {S['n_if_proxy']} of "
             f"{S['published']} published) | {fmt_num(S['mean_if_proxy'], 2)} "
             f"(median {fmt_num(S['median_if_proxy'], 2)}) |")
    L.append(f"| With a facility statement (acknowledgements/funding/methods) | {S['with_statement']} "
             f"(of which {S['statement_and_member']} also list the facility as author) |")
    extra = f"; {S['member_only_unchecked']} not checked: no full text" if S["member_only_unchecked"] else ""
    L.append(f"| Facility listed as author, **no** facility statement | "
             f"{S['member_only']} ({S['member_only_published']} published, {S['member_only_preprints']} "
             f"preprints{extra}) |")
    if S["member_only"]:
        L.append(f"| Mean IF proxy, papers with a facility statement only (n = {S['n_if_proxy_statement']}) | "
                 f"{fmt_num(S['mean_if_proxy_statement'], 2)} |")
    if staff or req:
        L.append("| Found only by the author searches (the phrase searches missed them) | "
                 + "; ".join(([f"staff list: {S['found_only_by_staff']}"] if staff else [])
                             + ([f"requester list: {S['found_only_by_requester']}"] if req else [])) + " |")
    if req:
        L.append(f"| With a listed requester as author | {S['with_requester']} |")
    if S["manual"]:
        L.append(f"| Included by manual decision | {S['manual']} |")
    L.append(f"| Statement wording: canonical / variant | {S['canonical']} / {S['variant']} |")
    if scope:
        lin, lout = lcfirst(scope.in_label), lcfirst(scope.out_label)
        L.append(f"| {scope.column}: {lin} / {lin} + {lout} in the same paper / not determined | "
                 f"{S['ms_prot']} / {S['ms_mixed']} / {S['ms_unknown']} |")
        L.append(f"| Left out as {lout} (other facility) | {n_out} |")
    if S["by_section"]:
        L.append("| Where the statement appears (a paper can count in more than one) | " +
                 ", ".join(f"{k}: {v}" for k, v in S["by_section"].items()) + " |")
    L.append(f"| Candidates needing manual review (not counted above) | {len(side['review'])} |")
    L.append(f"| Index hits that could not be checked in full text (not counted above) | {len(side['unverified'])} |")
    L.append("")

    L.append("## Papers\n")
    hdr = ["#", "Title", "DOI", "First author", "Last author", "Journal", "Type", "IF proxy"]
    if S["use_jif"]:
        hdr.append("JIF")
    hdr += ([scope.column] if scope else []) + ["Inclusion basis", "Facility author(s)"]
    hdr += (["Requester(s)"] if req else []) + ["Statement", "Section"]
    L.append("| " + " | ".join(hdr) + " |")
    L.append("|" + "---|" * len(hdr))
    for i, r in enumerate(rows, 1):
        cells = [str(i), md_escape(r["title"]), _doi_md(r["doi"]), md_escape(r["first_author"]),
                 md_escape(r["last_author"]), md_escape(r["journal"]),
                 r["kind"].replace("Published article", "Article"), fmt_num(r["if_proxy"], 1)]
        if S["use_jif"]:
            cells.append(fmt_num(r["jif"], 1))
        section = r["sections"] if r["inclusion_basis"] != BASIS_MEMBER else (r["facility_aff_source"] or "Authors")
        if scope:
            cells += [r["ms_type"] or "–"]
        cells += [r["inclusion_basis"], md_escape(r["facility_authors"])]
        cells += ([md_escape(r["requester_authors"])] if req else [])
        cells += [_statement_cell(r), md_escape(section) or "–"]
        L.append("| " + " | ".join(cells) + " |")
    L.append("")

    L.append("## Statement or affiliation as found in each paper\n")
    for i, r in enumerate(rows, 1):
        t = f"*{md_escape(r['title'][:90])}{'…' if len(r['title']) > 90 else ''}*"
        if r["inclusion_basis"] == BASIS_MEMBER:
            aff = md_escape(r["facility_affiliation"])
            if not aff.startswith("Author on the facility staff list"):
                aff = f"author affiliation: \"{aff}\""
            L.append(f"{i}. {t} (facility listed as author, no statement): {aff}")
        elif r["inclusion_basis"] == BASIS_MANUAL:
            L.append(f"{i}. {t} (manual decision): {md_escape(r['reason'])}")
        else:
            L.append(f"{i}. {t} ({r['section_paths'].split('; ')[0]}): \"{md_escape(r['statement_text'])}\"")
    L.append("")

    for key, head, note in [
        ("review", "Needs manual review", _review_note(cfg, scope)),
        ("unverified", "Index hits that could not be verified",
         "A search index matched the phrase (or a listed requester is an author), but the full text could not be "
         "downloaded (usually paywalled) and the facility is not listed as author. Put the PDFs in the folder set as "
         "`pdf_dir` in the config (file name = DOI with `/` replaced by `_`) and re-run to resolve them."),
    ]:
        if side[key]:
            L.append(f"## {head}\n\n{note}\n")
            L.append("| Title | DOI | Journal | Type | Found by | Note |\n|---|---|---|---|---|---|")
            for r in side[key]:
                L.append(f"| {md_escape(r['title'])} | {_doi_md(r['doi'])} | {md_escape(r['journal'])} | "
                         f"{r['kind']} | {md_escape(r['found_by'])} | "
                         f"{md_escape(' '.join(x for x in (r.get('reason', ''), r.get('statement_text') and chr(34) + r['statement_text'] + chr(34)) if x))[:400]} |")
            L.append("")

    thr = cfg["canonical_threshold"]
    L.append("## Method and coverage\n")
    L.append(f"Searches run {run_info['date']}; {run_info['n_candidates']} unique candidate records across all "
             "years searched.\n")
    L.append("| Source | Searched in | Phrase | Hits | Note |\n|---|---|---|---|---|")
    for s in _search_rows(run_info["search_status"]):
        L.append(f"| {s['source']} | {s['field']} | {md_escape(s['phrase'])} | "
                 f"{'' if s['hits'] is None else s['hits']} | {md_escape(s['note'])} |")
    L.append("")
    method = [
        "- **Verification**: each candidate's full text (Europe PMC / PMC JATS XML, bioRxiv JATS, or an open-access "
        "PDF) was split into sentences. A paper counts as *Facility statement* when a sentence outside the reference "
        "list and author affiliations names the facility (regex `" + cfg["facility_regex"] + "`). *Canonical* = the "
        f"sentence closely matches the reference statement: it contains at least {thr:.0%} of the reference "
        f"statement's content words, or the two word sequences have a similarity ratio (Python `difflib`) of at "
        f"least {thr:.2f}. *Variant* = the facility is named with other wording.",
    ]
    method.append(
        "- **Facility listed as author**: an author's affiliation names the facility, in the full-text author list "
        "or in the Europe PMC/PubMed or OpenAlex records"
        + (", or an author is on the facility staff list (`facility_staff`)" if staff else "")
        + f". These papers are included even without a facility statement (*{BASIS_MEMBER}*). The tool does not "
        "check whether an author with the facility affiliation is facility staff or a member of a research group "
        "that shares the address, and a staff-list match is by name, so check the *Facility author(s)* column. "
        "When the full text was not available the acknowledgements could not be read (*Not checked*).")
    if staff or req:
        method.append(
            "- **Author searches**: each person in the "
            + " and ".join(x for x, on in (("facility staff list", staff), ("requester list", req)) if on)
            + " was searched in Europe PMC by name (surname and first name), and also by ORCID (Europe PMC "
            "and OpenAlex) when one is given. A paper matches a person when an author has the same surname and first "
            "name, or first initial when only initials are known (accents ignored); an ORCID in Europe PMC's author record refines this (same ORCID: a match "
            "whatever the spelling; different ORCID: a namesake). "
            + ("Papers by requesters count only with a facility statement. " if req else "")
            + "Papers found only by a name search where no listed person is an author are excluded as namesakes.")
    if scope:
        lin, lout = lcfirst(scope.in_label), lcfirst(scope.out_label)
        ex = f" such as {scope.in_examples}" if scope.in_examples else ""
        method.append(
            f"- **{scope.in_label} vs {lout}**: {scope.out_text} is out of scope. A facility sentence that itself "
            f"names {lin} is kept (*{scope.in_label}*). A sentence that names only {lout} is left out (listed under "
            "*Excluded candidates* in the Excel file). For a generic facility sentence the paper's text decides: a "
            f"{lin} workflow (≥ {scope.in_min} terms{ex}) keeps it; {lout} only sends it to *Needs manual review*. "
            f"*{scope.mixed}* marks kept papers that also report {lout}, or name a {lout} facility; the "
            f"*{scope.evidence_column}* column in the Excel file gives the term counts.")
    if S["manual"] or run_info.get("n_manual"):
        method.append(
            "- **Manual decisions**: decisions listed in the manual-decisions file (`manual_decisions` in the "
            "config) override the automatic call for those DOIs; each one is shown with its reason in the *Note* "
            "column.")
    method += [
        "- **Impact factor**: Clarivate Journal Impact Factors are licensed and have no open API. The *IF proxy* "
        "column is OpenAlex's journal-level 2-year mean citedness (citations this year to items from the two previous "
        "years, per item). It is computed like the JIF but over OpenAlex's citation graph, so it runs higher than the "
        "JIF for most journals. Use it for ranking, not as the official JIF. To report official JIFs, set `jif_table` "
        "in the config to a JCR export.",
        "- **Coverage limits**: full-text indexes cover open-access papers, PMC author manuscripts, and the preprints "
        "and paywalled papers that OpenAlex has indexed. A paywalled paper whose full text is in none of these indexes "
        "is found only through the author or affiliation searches, and counts only if the facility is listed as "
        "author or its PDF is added, so the counts are a lower bound.",
        "- **Deduplication**: records are merged on DOI, PMCID, PMID and title. A preprint and its published version "
        "are listed separately; the *Published version* column in the Excel file links them.",
    ]
    L.append("\n".join(method) + "\n")
    (outdir / f"{slug}_{year}.md").write_text("\n".join(L), encoding="utf-8")

    # ---------------- excel + csv
    keep = [(k, h) for k, h in paper_cols(scope, cfg) if (S["use_jif"] or k not in ("jif", "jif_year"))]
    df = pd.DataFrame([{h: r.get(k, "") for k, h in keep} for r in rows], columns=[h for _, h in keep])
    df.insert(0, "#", range(1, len(df) + 1))
    df.to_csv(outdir / f"{slug}_{year}.csv", index=False)

    summary_rows = [
        ("Year", year), ("Year basis", basis), ("Generated", run_info["date"]),
        ("Total papers", S["total"]), ("Published (journal articles)", S["published"]),
        ("Preprints", S["preprints"]), ("Other (theses, reports)", S["other"]),
        ("Preprints with a published version", S["preprints_with_published_version"]),
        ("Mean IF proxy (OpenAlex 2-yr mean citedness)", S["mean_if_proxy"]),
        ("Median IF proxy", S["median_if_proxy"]), ("n journals with IF proxy", S["n_if_proxy"]),
    ]
    if S["use_jif"]:
        summary_rows += [("Mean JIF (Clarivate)", S["mean_jif"]), ("Median JIF", S["median_jif"]),
                         ("n with JIF", S["n_jif"])]
    summary_rows += [("With a facility statement", S["with_statement"]),
                     ("  of which also facility listed as author", S["statement_and_member"]),
                     (f"{BASIS_MEMBER}", S["member_only"]),
                     ("  published / preprints", f"{S['member_only_published']} / {S['member_only_preprints']}"),
                     ("  not checked: no full text", S["member_only_unchecked"]),
                     ("Mean IF proxy, facility-statement papers only", S["mean_if_proxy_statement"])]
    if staff:
        summary_rows += [("Found only by the staff-list search", S["found_only_by_staff"])]
    if req:
        summary_rows += [("Found only by the requester-list search", S["found_only_by_requester"]),
                         ("With a listed requester as author", S["with_requester"])]
    if S["manual"]:
        summary_rows += [("Included by manual decision", S["manual"])]
    summary_rows += [("Canonical statement", S["canonical"]), ("Variant wording", S["variant"])]
    if scope:
        summary_rows += [(f"{scope.column}: {lcfirst(scope.in_label)}", S["ms_prot"]),
                         (f"{scope.column}: {lcfirst(scope.mixed)}", S["ms_mixed"]),
                         (f"{scope.column}: not determined", S["ms_unknown"]),
                         (f"Left out as {lcfirst(scope.out_label)} (other facility)", n_out)]
    summary_rows += [(f"Statement in: {k}", v) for k, v in S["by_section"].items()]
    summary_rows += [("Needs review (not counted)", len(side["review"])),
                     ("Unverified index hits (not counted)", len(side["unverified"])),
                     ("Excluded candidates", len(side["excluded"]))]
    summary_rows = [(k, round(v, 2) if isinstance(v, float) else v) for k, v in summary_rows]

    from xlsxwriter.utility import xl_col_to_name
    widths = dict(COL_WIDTHS)
    if scope:
        widths.update({scope.column: 22, scope.evidence_column: 50})
    xlsx = outdir / f"{slug}_{year}.xlsx"
    scols = side_cols(scope, cfg)
    with pd.ExcelWriter(xlsx, engine="xlsxwriter") as xw:
        pd.DataFrame(summary_rows, columns=["Metric", "Value"]).to_excel(xw, sheet_name="Summary", index=False)
        df.to_excel(xw, sheet_name="Papers", index=False)
        frames = {"Summary": None, "Papers": df}
        for key, sheet in [("review", "Needs review"), ("unverified", "Unverified"),
                           ("excluded", "Excluded candidates")]:
            f = pd.DataFrame([{h: r.get(k, "") for k, h in scols} for r in side[key]],
                             columns=[h for _, h in scols])
            f.to_excel(xw, sheet_name=sheet, index=False)
            frames[sheet] = f
        wb = xw.book
        wrap = wb.add_format({"text_wrap": True, "valign": "top"})
        bold = wb.add_format({"bold": True})
        member_fill = wb.add_format({"bg_color": "#FFF2CC"})
        for name, ws in xw.sheets.items():
            if name == "Summary":
                ws.set_column(0, 0, 60, bold)
                ws.set_column(1, 1, 60)
                continue
            frame = frames[name]
            for j, c in enumerate(frame.columns):
                ws.set_column(j, j, widths.get(c, 14), wrap)
            ws.freeze_panes(1, 0)
            ws.autofilter(0, 0, max(1, len(frame)), len(frame.columns) - 1)
            if name == "Papers" and len(frame) and "Inclusion basis" in frame.columns:
                col = xl_col_to_name(list(frame.columns).index("Inclusion basis"))
                ws.conditional_format(1, 0, len(frame), len(frame.columns) - 1, {
                    "type": "formula", "criteria": f'=${col}2="{BASIS_MEMBER}"', "format": member_fill})
    S["left_out_scope"] = n_out
    return S


def write_overview(outdir: Path, cfg: dict, overview: list[tuple[int, dict, dict]], run_date: str) -> Path:
    """README.md in the output folder: one row per year."""
    scope = Scope.from_cfg(cfg)
    slug = cfg["report_slug"]
    hdr = (["Year", "Total", "With facility statement", "Facility listed as author, no statement"]
           + ["Published", "Preprints", "Other", "Mean IF proxy"]
           + ([f"Left out: {lcfirst(scope.out_label)}"] if scope else []) + ["Needs review", "Unverified", "Report"])
    lines = [f"# {cfg['facility_label']}: overview\n", f"Generated {run_date}.\n",
             "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for y, S, n in overview:
        cells = ([y, S["total"], S["with_statement"], S["member_only"]]
                 + [S["published"], S["preprints"], S["other"], fmt_num(S["mean_if_proxy"], 2)]
                 + ([S["left_out_scope"]] if scope else [])
                 + [n["review"], n["unverified"], f"[{y}]({y}/{slug}_{y}.md)"])
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / "README.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_audit(outdir: Path, records, search_status: list[dict], run_date: str) -> Path:
    """candidates_audit_<date>.json: every query with its hit count, every candidate with its status and reason."""
    audit = [{"status": r.status, "inclusion_basis": r.basis, "reason": r.reason, "ms_type": r.ms_type,
              "ms_evidence": r.ms_evidence, **r.meta,
              "facility_authors": r.facility_authors, "facility_affiliations": r.facility_affs,
              "facility_aff_source": r.facility_aff_source, "staff_authors": r.staff_authors,
              "requester_authors": r.requester_authors, "found_by": r.found_by,
              "fulltext_source": r.fulltext_source, "manual_decision": r.manual,
              "matches": [m.__dict__ for m in r.matches]} for r in records]
    p = outdir / f"candidates_audit_{run_date}.json"
    p.write_text(json.dumps({"search_status": search_status, "candidates": audit}, indent=1, ensure_ascii=False,
                            default=str), encoding="utf-8")
    return p
