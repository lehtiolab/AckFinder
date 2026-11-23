# How AckFinder works

Map of the code for people who want to change it. Usage is in [README.md](README.md).

## Modules

| Module | Role |
|---|---|
| `cli.py` | `ackfinder` command: `init`, `check`, `classify`, `run`; argument parsing, terminal interview |
| `config.py` | Find, load, validate and write configs; `.env` loading; builds regular expressions from plain names (`build_config`) |
| `pipeline.py` | `RunSettings` (config + command-line overrides), manual decisions, `run()` = the five stages below |
| `core.py` | HTTP client, searchers, merging, full-text parsing, `Verifier`, metadata, impact factors, `row_for` |
| `report.py` | Markdown / CSV / XLSX reports, overview `README.md`, audit JSON |
| `templates/` | `config.example.yaml` and `.env.example` (copied into the wheel at build time) |

## From the command line to a config

```
ackfinder init --interactive | --answers FILE         (cli.cmd_init)
│   cli.interview()  or  yaml.safe_load(answers file)  ─► answers dict (plain names, word lists)
│   config.build_config(answers)                      ─► config dict
│      ├─ names_regex(names) ─► name_regex(name)      "Clinical Proteomics MS" → 'clinical[\s\-]+proteomics[\s\-]+ms\b'
│      └─ terms_regex(words)                          word stems → '(?:proteom|peptide|…)'
│   config.render_config(values, template_text())     fills the commented template key by key (_fill), keeps comments
└─ writes config.yaml (+ .env.example), then load_config + validate

ackfinder check [--online]                            (cli.cmd_check)
    load_dotenv(config folder, cwd) → load_config → validate → prints settings, key status (set / not set)
    --online: _epmc_hits(phrase) per search phrase

load_config(path)
    YAML → defaults for missing keys → paths resolved against the config folder → report_slug from the label
    → _set_keys (which keys the file actually sets; placeholders are only flagged there)
validate(cfg) → (errors, warnings)
    errors: required key empty, CHANGE_ME left, invalid regex, scope_check incomplete,
            bad years / year_basis / canonical_threshold, missing jif_table file
    warnings: facility_regex misses canonical_statement, a search phrase neither regex matches,
              funding_label empty, removed keys (include_facility_authors), unknown keys
    people:   parse_people(facility_staff / requesters) ─► Person(name, surname, given, orcid, affiliations);
              a malformed entry, ORCID or unknown key is an error
```

## A run

`cli.cmd_run` loads the config, turns options into `RunSettings.from_config(cfg, **overrides)`, attaches a log
file (`_attach_run_log` → `<output_dir>/run_log_<date>.txt`, which also gets the final results table) and calls
`pipeline.run(cfg, settings)`. Every network call goes through `Http.get`, which paces requests per host, retries
on HTTP 429 and 5xx (honouring Retry-After), caches downloads in `cache_dir`, and for search queries falls back to
the last stored answer if the live query fails. A 429 whose Retry-After exceeds `Http.GIVE_UP_AFTER` (10 min) is not
retried: for OpenAlex it means the daily budget is used up, and `Searcher._oa_stop()` then ends all OpenAlex calls
for the run.

```
pipeline.run(cfg, settings)
│
├─ 0  load_decisions(settings.manual_decisions)    YAML list or CSV/XLSX → {doi: decision}
│
├─ 1  SEARCH ── Searcher ─────────────────────────────────────────────── returns Item objects (one per hit)
│     ├─ epmc_search(y0, y1)          Europe PMC full text, each search_phrase
│     │    └─ epmc_query()            cursorMark paging ─► Http.get
│     ├─ pmc_search(y0, y1)           NCBI PMC full text (skipped with --no-pmc)
│     │    ├─ _esearch() / _esummary()
│     │    └─ epmc_lookup("PMCID")    adds the Europe PMC record (authors, journal, dates)
│     ├─ openalex_search(y0, y1)      OpenAlex fulltext.search, 100 per page; cursor paging only with a key;
│     │    └─ epmc_lookup("DOI")      notes truncation; stops for the run on repeated 429
│     ├─ [affiliation_phrases]        pubmed_aff_search() + AFF: queries + OpenAlex raw affiliation strings
│     ├─ [facility_staff / requesters] epmc_author_search(): AUTH:"Surname Firstname" OR AUTHORID:"<orcid>", per person
│     │                                openalex_author_search(): author.orcid:<a>|<b>…, ORCID entries only
│     ├─ manual "include" DOIs        epmc_lookup("DOI") or a bare Item, so they are always candidates
│     └─ _note()                      every query and its hit count (→ "Method and coverage" table, audit)
│
├─ 2  MERGE ── merge_items(items) ─────────────────────────────────────── returns Record objects
│        union-find on DOI, PMCID, PMID, preprint id and normalised title (norm_doi, norm_title)
│
├─ 3  METADATA
│     ├─ Searcher.openalex_works(dois)      authors, journal, dates, source id
│     ├─ Searcher.biorxiv_details(doi)      preprints: posting date, published version
│     ├─ Searcher.openalex_sources(ids)     journal 2-year mean citedness (IF proxy)
│     │    (all OpenAlex lookups go through _oa_lookup(): after one refused lookup (429) the run skips OpenAlex,
│     │     with a "metadata skipped" row in the Method and coverage table)
│     └─ build_meta(rec, year_basis, …) ─► rec.meta   year, title, authors, journal, Type, IF
│            ├─ JifTable.lookup()            only with jif_table
│            ├─ Searcher.openalex_source_by_issn()   when the source id is missing
│            └─ other_doi_prefixes           repositories reported as "Other"
│
│     keep records whose rec.meta["year"] is in settings.years
│
├─ 4  VERIFY ── for each record: Verifier.verify(rec), then apply_decision(rec, decision) if one exists
│     ├─ fetch(rec)            first source that works:
│     │     Europe PMC JATS → NCBI PMC JATS → bioRxiv/medRxiv JATS → open-access PDF → local PDF (pdf_dir)
│     │     ├─ jats_blocks(xml) ─► jats_section(el)   (text, section path, category) blocks
│     │     └─ pdf_blocks(pdf)                         same, from PDF headings
│     ├─ classify(blocks)      one Match per sentence matching facility_regex or funding_regex
│     │     ├─ sentences(text)
│     │     ├─ score(sentence)  max(content-word recall, difflib ratio) vs canonical_statement
│     │     │                   ≥ canonical_threshold → Canonical statement, else Variant wording
│     │     ├─ References → Excluded;  Affiliation → Affiliation only
│     │     └─ funder sentence without the facility name:
│     │           names other_facility_regex or out-of-scope work → Excluded
│     │           matches context_regex (or none set)             → Review
│     ├─ facility_affiliations(rec) ─► jats_facility_authors()   facility in an author affiliation
│     ├─ record_authors(rec) ─► match_person(authors, p)         staff_authors, requester_authors
│     │     same surname + first name (initial if that is all there is), refined in this order:
│     │       1. ORCID (Europe PMC author records): same → match; different → namesake
│     │       2. affiliations (Europe PMC/PubMed, OpenAlex raw strings): one listed → match;
│     │          known but none listed → namesake
│     │       3. nothing to compare → match by name
│     ├─ status:
│     │     Canonical/Variant found                       → "Confirmed", then ms_type()
│     │     facility listed as author (affiliation/staff) → "Confirmed", basis "Facility listed as author, no statement"
│     │     found only by a name search, nobody matches   → "Excluded" (namesake)
│     │     no full text                                  → "Unverified"
│     │     Review sentence                               → "Review"
│     │     otherwise (incl. requester without statement) → "Excluded" (with reason)
│     ├─ ms_type(rec, blocks)  scope check (only if scope_check.enabled; Scope.from_cfg)
│     │     ├─ _terms(in_scope_regex), _terms(out_of_scope_regex), out_of_scope_facility_regex
│     │     ├─ facility sentence names in-scope work            → in_scope_label
│     │     ├─ facility sentence names only out-of-scope work   → "Excluded"
│     │     ├─ generic sentence + ≥ in_scope_min_terms           → in_scope_label
│     │     ├─ generic sentence + out-of-scope work only         → "Review"
│     │     └─ in scope and the paper also has out-of-scope work → "<in> + <out> in paper"
│     └─ apply_decision(rec, d)  include → Confirmed, basis "Manual decision"; exclude → Excluded;
│                                the automatic call is kept in the reason
│
└─ 5  REPORT (per year)
      ├─ row_for(rec) ─► best_match(rec)      one flat dict per paper
      │     rows       = Confirmed (Statement column: Yes / No / Not checked)
      │     side lists = Review, Unverified, Excluded
      ├─ report.write_reports(year, rows, side, cfg, settings, …)
      │     ├─ summarise(rows)                 totals, published/preprints, mean IF, scope counts
      │     ├─ <slug>_<year>.md                summary, papers, statements, side lists, method and coverage
      │     ├─ <slug>_<year>.csv               papers table
      │     └─ <slug>_<year>.xlsx              Summary, Papers, Needs review, Unverified, Excluded
      ├─ report.write_overview()  → <output_dir>/README.md            one row per year
      └─ report.write_audit()     → candidates_audit_<date>.json      every query + every candidate
```

`run()` returns `{"years": {year: counts + report path}, "overview": path, "audit": path, "http_requests": n}`.

## Data objects

| Object | Created by | Holds |
|---|---|---|
| config `dict` | `config.load_config` | the YAML with defaults filled in and paths resolved |
| `RunSettings` | `RunSettings.from_config` | years, folders, year basis, sources on/off, decisions file |
| `Item` | the `Searcher.*_search` methods | one search hit: source, phrase, DOI/PMID/PMCID, raw records |
| `Record` | `merge_items` | all Items for one paper, plus everything later stages add (`meta`, `matches`, `status`, `basis`, `ms_type`, `manual`, …) |
| `Match` | `Verifier.classify` | one matching sentence: text, section, category, class, similarity score |
| `Scope` | `Scope.from_cfg` | the compiled scope_check settings, or `None` when off |
| row `dict` | `row_for` | the flattened Record that `write_reports` turns into table rows |

## Where to change behaviour

| To change | Edit |
|---|---|
| Search phrases | `search_phrases`, `openalex_phrases` in `config.yaml` |
| What counts as naming the facility | `facility_regex` (test with `ackfinder classify`) |
| Canonical vs Variant | `canonical_statement`, `canonical_threshold`, `word_expansions`; scoring in `Verifier.score` |
| Funder-only sentences → Needs review | `funding_regex`, `context_regex`, `other_facility_regex` |
| Scope rules | `scope_check.*` in `config.yaml`; logic in `Verifier.ms_type` |
| A single paper's outcome | the manual decisions file, never the code |
| Section detection | `jats_section` (JATS), `pdf_blocks` (PDF headings) in `core.py` |
| Report columns | `paper_cols`, `side_cols` and `write_reports` in `report.py` |
| Interview questions | `cli.interview` and `config.build_config` (keep `examples/setup_answers.example.yaml` and AGENTS.md in step) |
| New config key | default in `config.load_config`, check in `validate`, comment in `config.example.yaml`, README section 4 |
