"""Author lists (facility_staff, requesters): parsing, matching, searches, verify statuses, report."""
from types import SimpleNamespace

from ackfinder import config as C
from ackfinder.core import (BASIS_BOTH, BASIS_MEMBER, BASIS_STATEMENT, Author, Item, Record, Searcher, Verifier,
                            match_people, match_person, record_authors, row_for)
from ackfinder.report import _search_rows, write_reports

from test_classify import CANON

ORCID = "0000-0002-1825-0097"      # ORCID's fictional test researcher
OTHER = "0000-0001-0000-0009"      # made up
BODY = "Proteins were digested with trypsin; peptides were labelled with TMT and searched in MaxQuant."
STMT = [(CANON, "Acknowledgements", "Acknowledgements"), (BODY, "Methods", "Methods")]
NO_STMT = [(BODY, "Methods", "Methods")]
FUNDER_ONLY = [("Proteomics was supported by the Infrastructure Board at Karolinska Institutet.",
                "Acknowledgements", "Acknowledgements")]


def au(last, first, orcid=None, aff=None):
    a = {"lastName": last, "firstName": first}
    if orcid:
        a["authorId"] = {"type": "ORCID", "value": orcid}
    if aff:
        a["authorAffiliationDetailsList"] = {"authorAffiliation": [{"affiliation": aff}]}
    return a


def epmc(*authors):
    return {"id": "1", "source": "MED", "authorList": {"author": list(authors)}}


# ------------------------------------------------------------------ config
def test_parse_person_forms():
    p = C.parse_person("Jane Doe")
    assert (p.surname, p.given, p.orcid, p.query_name) == ("Doe", "Jane", None, "Doe Jane")
    assert C.parse_person("Doe JA").query_name == "Doe J"
    assert C.parse_person("Nazlı Ezgi Özkan").query_name == "Özkan Nazlı"
    assert C.parse_person("Doe, Jane").key == ("doe", "jane")
    assert C.parse_person("Doe JA").key == ("doe", "j")
    assert C.parse_person("Ludwig van Beethoven").surname == "van Beethoven"
    assert C.parse_person("Åsa Ödegård").key == ("odegard", "asa")
    p = C.parse_person(f"Jane Doe ({ORCID})")
    assert p.name == "Jane Doe" and p.orcid == ORCID
    assert C.parse_person({"name": "Jane Doe", "orcid": f"https://orcid.org/{ORCID}"}).orcid == ORCID


def test_parse_person_affiliations():
    p = C.parse_person({"name": "Jane Doe", "affiliations": ["Karolinska Institutet", "SciLifeLab"]})
    assert p.affiliations == ("Karolinska Institutet", "SciLifeLab") and p.orcid is None
    assert C.parse_person({"name": "Jane Doe", "affiliation": "Karolinska Institutet; SciLifeLab"}).affiliations == \
        ("Karolinska Institutet", "SciLifeLab")
    assert C.affiliation_matches("Dept. of Oncology-Pathology, Karolinska Institutet, Solna", p.affiliations) == \
        "Karolinska Institutet"
    assert C.affiliation_matches("Science for Life Laboratory (SCILIFELAB), Stockholm", p.affiliations) == "SciLifeLab"
    assert C.affiliation_matches("Lund University, Sweden", p.affiliations) is None


def test_parse_people_errors():
    people, errors = C.parse_people(["Jane Doe", "Madonna", {"name": "Jane Doe", "orcid": "1234"},
                                     {"name": "Jane Doe", "afiliations": ["X"]}], "requesters")
    assert len(people) == 1
    assert len(errors) == 3 and errors[0].startswith("`requesters` entry 2") and "afiliations" in errors[2]


def test_validate_people_and_removed_key(tmp_path):
    base = ("facility_label: X\ncanonical_statement: Done at the X facility.\nsearch_phrases: [X facility]\n"
            "facility_regex: 'X facility'\n")
    p = tmp_path / "a.yaml"
    p.write_text(base + "requesters: [Madonna]\n")
    errors, _ = C.validate(C.load_config(p))
    assert any("`requesters` entry 1" in e for e in errors)
    p.write_text(base + f"include_facility_authors: true\nfacility_staff: [Jane Doe, 'Doe, John ({ORCID})']\n")
    errors, warnings = C.validate(C.load_config(p))
    assert errors == []
    assert len(warnings) == 1 and "include_facility_authors" in warnings[0] and "no longer used" in warnings[0]


def test_answers_with_people_roundtrip(answers, tmp_path):
    answers = dict(answers, facility_staff=f"Jane Doe; Doe, John ({ORCID})",
                   requesters=[{"name": "Anna Svensson", "orcid": ORCID}, "Li Wei",
                               {"name": "Åsa Ödegård", "affiliations": "Karolinska Institutet; SciLifeLab"}])
    p = tmp_path / "config.yaml"
    p.write_text(C.render_config(C.build_config(answers)))
    cfg = C.load_config(p)
    assert C.validate(cfg) == ([], [])
    assert cfg["facility_staff"] == ["Jane Doe", f"Doe, John ({ORCID})"]
    req = C.parse_people(cfg["requesters"])[0]
    assert [x.orcid for x in req] == [ORCID, None, None]
    assert req[2].affiliations == ("Karolinska Institutet", "SciLifeLab")


# ------------------------------------------------------------------ matching
def test_match_by_name_ignores_accents_and_checks_first_name():
    authors = record_authors(Record(epmc=[epmc(au("Ödegård", "Åsa"), au("Smith", "Anna"), au("Doe", "John"),
                                               {"lastName": "Lee", "initials": "C"})]))
    people = C.parse_people(["Asa Odegard", "John Smith", "Jane Doe", "Chris Lee"])[0]
    assert match_people(authors, people) == ["Asa Odegard", "Chris Lee"]   # Jane is not John; C = Chris
    assert C.given_matches("chris", "christopher") and not C.given_matches("jane", "john")


def test_orcid_refines_the_name_match():
    namesake = record_authors(Record(epmc=[epmc(au("Doe", "Jane", OTHER))]))
    assert match_people(namesake, [C.parse_person(f"Jane Doe ({ORCID})")]) == []
    assert match_people(namesake, [C.parse_person("Jane Doe")]) == ["Jane Doe"]          # the name is the base
    renamed = record_authors(Record(epmc=[epmc(au("Doe-Smith", "Jane", ORCID))]))
    assert match_people(renamed, [C.parse_person(f"Jane Doe ({ORCID})")]) == ["Jane Doe"]


def test_affiliation_refines_the_name_match():
    p = C.parse_person({"name": "Jane Doe", "affiliations": ["Karolinska Institutet", "SciLifeLab"]})
    ki = record_authors(Record(epmc=[epmc(au("Doe", "Jane", aff="Dept of Medicine, Karolinska Institutet"))]))
    assert match_person(ki, p) == (True, "name + affiliation Karolinska Institutet")
    elsewhere = Record(epmc=[epmc(au("Doe", "Jane", aff="Lund University, Sweden"))],
                       biorxiv={"authors": "Doe, J."})          # a source with less to say does not overrule
    ok, how = match_person(record_authors(elsewhere), p)
    assert not ok and "Lund University" in how
    unknown = record_authors(Record(epmc=[epmc(au("Doe", "Jane"))]))
    assert match_person(unknown, p) == (True, "name")            # nothing to compare: the name decides
    both = C.parse_person({"name": "Jane Doe", "orcid": ORCID, "affiliations": ["Karolinska Institutet"]})
    other_ki = record_authors(Record(epmc=[epmc(au("Doe", "Jane", OTHER, "Karolinska Institutet"))]))
    assert match_person(other_ki, both) == (False, "same name, different ORCID")    # ORCID comes first
    oa = record_authors(Record(openalex={"authorships": [{"raw_author_name": "Jane Doe",
                                                          "raw_affiliation_strings": ["SciLifeLab, Solna"]}]}))
    assert match_person(oa, p) == (True, "name + affiliation SciLifeLab")


def test_openalex_and_biorxiv_give_names_but_openalex_orcids_are_not_trusted():
    rec = Record(openalex={"authorships": [{"raw_author_name": "Jane A. Doe",
                                            "author": {"display_name": "Jane Doe",
                                                       "orcid": f"https://orcid.org/{OTHER}"}}]},
                 biorxiv={"authors": "Smith, J.; Ödegård, Å."})
    authors = record_authors(rec)
    assert {Author("doe", "jane"), Author("smith", "j"), Author("odegard", "a")} <= set(authors)   # nothing else known
    assert match_people(authors, [C.parse_person(f"Jane Doe ({ORCID})")]) == ["Jane Doe"]


# ------------------------------------------------------------------ searches
def test_epmc_author_queries(monkeypatch):
    s = Searcher(None, {"facility_staff": [f"Jane Doe ({ORCID})"], "requesters": ["Doe, John"]}, None, None)
    seen = []
    monkeypatch.setattr(s, "epmc_query", lambda q, fallback=False: seen.append(q) or [{"id": "1", "source": "MED"}])
    items = s.epmc_author_search(2025, 2026)
    assert seen[0].startswith(f'(AUTH:"Doe Jane" OR AUTHORID:"{ORCID}") AND (FIRST_PDATE:[2024-01-01')
    assert seen[1].startswith('(AUTH:"Doe John") AND')
    assert [(i.source, i.phrase) for i in items] == [("EuropePMC (staff)", "Jane Doe"),
                                                    ("EuropePMC (requester)", "Doe, John")]


def test_openalex_author_search_uses_orcids_only(monkeypatch):
    s = Searcher(None, {"facility_staff": [f"Jane Doe ({ORCID})", "John Doe"]}, None, None)
    seen = []
    monkeypatch.setattr(s, "_openalex_query", lambda flt, ph, source, where: seen.append((flt, source)) or [])
    s.openalex_author_search(2025, 2026)
    assert seen == [(f"author.orcid:{ORCID},publication_year:2024-2026", "OpenAlex (staff)")]


# ------------------------------------------------------------------ verify
def _cfg(cpms_cfg):
    return dict(cpms_cfg, facility_staff=["Jane Doe"], requesters=["Anna Svensson"])


def _verify(cfg, blocks, authors, source="EuropePMC"):
    v = Verifier(None, cfg, None, None)
    v.fetch = lambda rec: (blocks, "test source" if blocks else None)
    rec = Record(items=[Item(source=source, phrase="x", doi="10.1/x")], epmc=[epmc(*authors)])
    v.verify(rec)
    return rec


def test_statement_with_and_without_staff_author(cpms_cfg):
    rec = _verify(_cfg(cpms_cfg), STMT, [au("Doe", "Jane")])
    assert (rec.status, rec.basis, rec.staff_authors) == ("Confirmed", BASIS_BOTH, ["Jane Doe"])
    assert row_for(rec)["has_statement"] == "Yes"
    rec = _verify(_cfg(cpms_cfg), STMT, [au("Other", "Person")])
    assert (rec.status, rec.basis) == ("Confirmed", BASIS_STATEMENT)


def test_staff_author_without_statement_is_listed(cpms_cfg):
    rec = _verify(_cfg(cpms_cfg), NO_STMT, [au("Doe", "Jane")], source="EuropePMC (staff)")
    assert (rec.status, rec.basis) == ("Confirmed", BASIS_MEMBER)
    row = row_for(rec)
    assert row["has_statement"] == "No" and row["author_search_only"] == "staff"
    assert "Jane Doe" in row["facility_authors"]
    rec = _verify(_cfg(cpms_cfg), [], [au("Doe", "Jane")], source="EuropePMC (staff)")
    assert rec.status == "Confirmed" and row_for(rec)["has_statement"] == "Not checked"


def test_requester_counts_only_with_statement(cpms_cfg):
    cfg = _cfg(cpms_cfg)
    rec = _verify(cfg, STMT, [au("Svensson", "Anna")], source="EuropePMC (requester)")
    assert rec.status == "Confirmed" and row_for(rec)["requester_authors"] == "Anna Svensson"
    rec = _verify(cfg, NO_STMT, [au("Svensson", "Anna")], source="EuropePMC (requester)")
    assert rec.status == "Excluded" and "Anna Svensson" in rec.reason
    rec = _verify(cfg, FUNDER_ONLY, [au("Svensson", "Anna")], source="EuropePMC (requester)")
    assert rec.status == "Review"                                     # ambiguous
    rec = _verify(cfg, [], [au("Svensson", "Anna")], source="EuropePMC (requester)")
    assert rec.status == "Unverified" and "PDF" in rec.reason


def test_name_search_namesake_is_excluded(cpms_cfg):
    rec = _verify(_cfg(cpms_cfg), [], [au("Doe", "John")], source="EuropePMC (staff)")
    assert rec.status == "Excluded" and "namesake" in rec.reason


def test_staff_affiliation_decides(cpms_cfg):
    cfg = dict(cpms_cfg, facility_staff=[{"name": "Jane Doe", "affiliations": ["Karolinska Institutet"]}])
    rec = _verify(cfg, NO_STMT, [au("Doe", "Jane", aff="Karolinska Institutet, Stockholm")], source="EuropePMC (staff)")
    assert (rec.status, rec.basis) == ("Confirmed", BASIS_MEMBER)
    assert "matched by name + affiliation Karolinska Institutet" in row_for(rec)["facility_affiliation"]
    rec = _verify(cfg, NO_STMT, [au("Doe", "Jane", aff="University of Porto")], source="EuropePMC (staff)")
    assert rec.status == "Excluded" and "Jane Doe (same name, affiliation \"University of Porto\")" in rec.reason


# ------------------------------------------------------------------ report
def test_search_rows_sum_people():
    st = [dict(source="Europe PMC", field="full text", phrase="X", hits=3, note=""),
          dict(source="Europe PMC", field="author (staff)", phrase="Jane Doe", hits=2, note=""),
          dict(source="Europe PMC", field="author (staff)", phrase="John Doe", hits=0, note="")]
    rows = _search_rows(st)
    assert len(rows) == 2
    assert rows[1]["hits"] == 2 and rows[1]["phrase"].startswith("2 people") and "John Doe" in rows[1]["note"]


def test_report_shows_statement_yes_no(cpms_cfg, tmp_path):
    cfg = _cfg(cpms_cfg)
    meta = dict(year=2025, first_pub_date="2025-01-01", issue_year=2025, title="T", first_author="", last_author="",
                n_authors="", journal="J", kind="Published article", if_proxy=None, jif=None, jif_year=None,
                pmid="", pmcid="", published_version="")
    a = _verify(cfg, STMT, [au("Svensson", "Anna")])
    b = _verify(cfg, NO_STMT, [au("Doe", "Jane")], source="EuropePMC (staff)")
    a.meta, b.meta = dict(meta, doi="10.1/a"), dict(meta, doi="10.1/b")
    S = write_reports(2025, [row_for(a), row_for(b)], {"review": [], "unverified": [], "excluded": []}, cfg,
                      SimpleNamespace(year_basis="first", jif_table=None), tmp_path,
                      {"date": "2026-01-01", "n_candidates": 2, "search_status": [], "n_manual": 0})
    assert (S["with_statement"], S["member_only"], S["found_only_by_staff"], S["with_requester"]) == (1, 1, 1, 1)
    md = (tmp_path / f"{cfg['report_slug']}_2025.md").read_text()
    assert "| Yes, canonical |" in md and "| No |" in md
    assert "Facility listed as author, **no** facility statement | 1" in md
