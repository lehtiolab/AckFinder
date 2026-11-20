---
name: ackfinder-no-code
description: Fallback for finding publications that acknowledge a core facility and writing a yearly report WITHOUT running the AckFinder Python package. Use only when the ackfinder command cannot be run: there is no shell or Python environment (e.g. a chat assistant with web access but no code execution), or the chat app's code sandbox cannot install the package or reach Europe PMC. If Python 3.10+ with internet access is available, including in a chat app's sandbox, install and run the package instead (see AGENTS.md Step 0); never use this skill as the default.
---

# Facility acknowledgement search without code (fallback)

This skill repeats the method of the AckFinder package by hand: web requests to the same public APIs
and your own reading of the full texts. It is **not the default**. The Python tool is deterministic, complete and
repeatable; this procedure is slower, covers fewer sources, and two runs may differ.

## Before you start

1. **Confirm the tool really cannot run.** If you can run code at all, including in a chat app's sandbox, follow
   `AGENTS.md` Step 0 first. Use this skill only if Step 0 sends you here: no code execution, or a sandbox that
   cannot install the tool or reach Europe PMC.
2. **Tell the user** that this is the no-code fallback, that the report will be labelled as such, and that the
   counts should be confirmed with the Python tool when possible.
3. **Ask** which years to report, where to save the report, and whether they want a full or a quick search (see
   "Scale" below).

## Inputs

* **`config.yaml`** in the package folder. Read these keys: `facility_label`, `report_slug`,
  `canonical_statement`, `facility_regex`, `search_phrases`, `openalex_phrases`, `funding_regex`,
  `funding_label`, `context_regex`, `other_facility_regex`, `scope_check`, `output_dir`, `years`, `year_basis`,
  `canonical_threshold`, `word_expansions`.
* **No `config.yaml`:** interview the user with the questions in `AGENTS.md` Step 2. Write the answers to
  `setup_answers.yaml` (format: `examples/setup_answers.example.yaml`) if you can write files, so the Python
  tool can later build the same config with `ackfinder init --answers setup_answers.yaml`. For this run, use
  the facility name and its variants as search phrases, and treat a sentence as naming the facility if it contains
  any of those names, ignoring case and allowing spaces or hyphens between words.

## Procedure

Exact request formats are in [references/api_queries.md](references/api_queries.md); the classification rules
in [references/classification_rules.md](references/classification_rules.md); the report layout in
[references/report_template.md](references/report_template.md).

1. **Search.** For every phrase in `search_phrases`, query Europe PMC full text for the window from 1 January of
   (first year − 1) to 31 December of the last year. Follow `nextCursorMark` until no new results. Record each
   query with its `hitCount`. If you can, repeat with OpenAlex `fulltext.search` for `openalex_phrases` (or
   `search_phrases`); note in the report if OpenAlex refused (HTTP 429) or you skipped it.
2. **Merge** hits that share a DOI, PMCID or PMID, or have the same title ignoring case and punctuation.
3. **Assign a year.** `year_basis: first` → the year of `firstPublicationDate` (for preprints, the posting date);
   `issue` → `journalInfo.yearOfPublication`. Keep only the requested years.
4. **Scale.** If more than 100 candidates remain, tell the user the number and ask whether to continue, check a
   subset (say which), or wait until the Python tool can run.
5. **Verify each candidate.**
   * Fetch the Europe PMC full text (`/{PMCID}/fullTextXML`). For bioRxiv/medRxiv preprints without a PMCID, use
     the bioRxiv API's `jatsxml` link. If no full text is available, the paper is **Unverified**: list it, and if
     the user has access they can give you the PDF. Never try to get around a paywall.
   * Find every sentence that names the facility (or the funder in `funding_regex`). Note the section it sits in,
     using the JATS element: `<ack>` = Acknowledgements, `<funding-group>` = Funding, `<ref-list>` = References,
     `<aff>` = author affiliation, otherwise the title of the enclosing `<sec>`.
   * Classify each sentence and set the paper's status with
     [classification_rules.md](references/classification_rules.md). Copy the sentence verbatim.
   * If `scope_check.enabled` is true, apply the scope rules in the same file.
6. **Metadata** for each Confirmed paper: title, first and last author, journal, publication date, DOI, PMID,
   PMCID, Published article or Preprint (DOI prefixes listed in the rules file), the published version of a
   preprint (bioRxiv API `published` field), and optionally the impact-factor proxy (OpenAlex source
   `summary_stats.2yr_mean_citedness`).
7. **Write the report** following [report_template.md](references/report_template.md) to
   `<output_dir>/<year>/<report_slug>_<year>_no-code.md`. The `_no-code` suffix keeps it apart from the tool's
   reports, which must never be overwritten by hand. If you cannot write files, give the report in the chat.
8. **Summarise** for the user: counts per year (published / preprints), Needs review, Unverified, sources searched
   and any that failed or were skipped, and the caveats below.

## Rules

* Only **Confirmed** papers are counted. Uncertain cases go to **Needs review**; never count them on your own
  judgement. The user can decide, and decisions go in the manual decisions file (`manual_decisions.yaml`) so
  the Python tool applies them later.
* Quote sentences exactly as they appear. Do not paraphrase evidence.
* API keys: never ask the user to paste one into the chat, and never write one into a file other than `.env`.
  This procedure works without keys.
* Do not store or share PDFs of paywalled papers.

## Caveats to state in every no-code report

* Produced by hand following the AckFinder method; not by the tool. Not reproducible in the strict
  sense; confirm with `ackfinder run` when Python is available.
* Similarity scores are estimates (see the rules file); the tool computes them exactly.
* Sources: Europe PMC (and OpenAlex if used). The tool also searches NCBI PMC and reads PDFs, so its counts may be
  higher.
* Counts are a lower bound: paywalled papers that no index holds cannot be found.
