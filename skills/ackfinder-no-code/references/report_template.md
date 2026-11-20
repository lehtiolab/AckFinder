# Report template (no-code fallback)

Same layout as the tool's Markdown report, with a fallback banner. File:
`<output_dir>/<year>/<report_slug>_<year>_no-code.md`. Leave out sections that are empty, except *Method and
coverage*. Replace everything in `<…>`.

````markdown
# <facility_label>: publications, <year> (no-code fallback)

> **Produced without the AckFinder tool**, by following its method by hand on <YYYY-MM-DD>.
> Sources: <Europe PMC [, OpenAlex]>. Similarity scores are approximate. Confirm with `ackfinder run`
> when Python is available; the tool's counts take precedence.

Year assigned by <first publication date (earliest of online/print/preprint posting) | journal issue year>.

A paper is included when its full text names the facility in the acknowledgements, funding statement or methods.
<If scope check on: Papers whose credited work is <out_of_scope_label> (<out_of_scope_note>) are left out.>

## Summary

| Metric | Value |
|---|---|
| **Total papers** | **<n>** |
| Published (peer-reviewed journal articles) | <n> |
| Preprints | <n> |
| Mean impact factor, open proxy: OpenAlex 2-yr mean citedness (n = <k> of <published>) | <mean> (median <median>) — or "not retrieved" |
| Statement wording: canonical / variant | <a> / <b> |
| <scope column>: <in> / <in + out in paper> / not determined | <a> / <b> / <c> (only with scope check) |
| Where the statement appears (a paper can count in more than one) | Acknowledgements: <n>, Methods: <n>, … |
| Candidates needing manual review (not counted above) | <n> |
| Index hits that could not be checked in full text (not counted above) | <n> |

## Papers

| # | Title | DOI | First author | Last author | Journal | Type | IF proxy | <scope column> | Statement | Section |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | <title> | [<doi>](https://doi.org/<doi>) | <name> | <name> | <journal> | Article / Preprint | <x.xx> | <label> | Canonical / Variant (≈<score>) | Acknowledgements |

## Statement as found in each paper

**1. <short title>** (<section path>): "<sentence copied verbatim>"

## Needs review (not counted)

| Title | DOI | Reason | Sentence found | Section |
|---|---|---|---|---|

## Index hits that could not be verified (not counted)

| Title | DOI | Journal | Found by | Note |
|---|---|---|---|---|
| <title> | <doi> | <journal> | Europe PMC "<phrase>" | Full text not retrievable (paywalled or not indexed) |

## Excluded candidates

| Title | DOI | Reason |
|---|---|---|

## Method and coverage

| Source | Searched in | Phrase | Hits | Note |
|---|---|---|---|---|
| Europe PMC | full text | <phrase> | <hitCount> | |
| OpenAlex | full text | <phrase> | <meta.count> | <first page only / HTTP 429, skipped / not used> |
| NCBI PMC | – | – | – | not searched in the no-code fallback |

* Search window: <Y0-1>-01-01 to <Y1>-12-31; candidates then assigned to years as above.
* Classification: rules of AckFinder (`classification_rules.md`). Canonical = similarity to the
  reference statement ≥ <canonical_threshold>; similarity estimated from content-word recall only.
* Limits: counts are a lower bound (paywalled papers that no index holds are not found); the current year is
  incomplete because of indexing lag; the IF proxy is not the Clarivate JIF.
````

Also give the user the same summary in the chat. If you cannot write files, put the whole report in the chat.
