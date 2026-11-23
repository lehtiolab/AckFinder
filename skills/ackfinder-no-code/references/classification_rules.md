# Classification rules (mirror of `Verifier` in core.py)

Apply these exactly. When the code and this file disagree, the code is right.

## 1. Sections

Tag every text block with a section category, taken from its position in the JATS XML:

| JATS position | Category |
|---|---|
| inside `<ref-list>` | References |
| `<aff>`, inside `<contrib-group>` or `<aff-alternatives>` | Affiliation |
| inside `<ack>` | Acknowledgements |
| inside `<funding-group>` | Funding |
| `<sec>` whose title contains "acknowledg" | Acknowledgements |
| `<sec>` title contains funding / financial / financing / grant support | Funding |
| `<sec>` title contains method / materials / experimental procedures / experimental section / experimental design / protocol / mass spectrometry / proteomic | Methods |
| `<sec>` title contains declaration / conflict / competing / contribution / data availability / ethic | Declarations |
| `<sec>` title contains supplement | Supplementary |
| anything else | Main text |

PDF text: the heading above the text decides (Acknowledgements, Funding, Methods, References, Declarations, Main
text); text before the first heading is "Unknown (PDF)".

Ranking when a paper has several matching sentences: Acknowledgements, Funding, Methods, Main text,
Declarations, Supplementary, Unknown (PDF), Affiliation, References.

## 2. Sentences

Split each block after `.`, `!`, `?` or `;` when followed by whitespace and then a capital letter, digit, `(` or
a quotation mark. Affiliation blocks are not split. Ignore a sentence you have already seen in the same category.

A sentence **names the facility** if `facility_regex` matches it (case-insensitive). It **names the funder** if
`funding_regex` is set and matches. Skip sentences that do neither.

## 3. Similarity to the reference statement

1. Lower-case both the sentence and `canonical_statement`. Replace each key of `word_expansions` by its value.
2. Split into words on anything that is not a–z or 0–9, and drop these stopwords:
   `a an and the of by at in on for to was were is are be been with from as this that which who whom we our us it its`.
3. **Recall** = (distinct reference words that also occur in the sentence) ÷ (distinct reference words).
4. **Ratio** = Python `difflib.SequenceMatcher(None, sentence_words_joined_by_spaces, reference_words_joined).ratio()`.
   You cannot compute this exactly by hand; use recall alone and mark the score as approximate ("≈").
5. Similarity = max(recall, ratio), rounded to 3 decimals.

## 4. Class of each sentence

Check in this order; the first rule that applies wins.

| # | Condition | Class |
|---|---|---|
| 1 | Category is References | Excluded |
| 2 | Category is Affiliation | Affiliation only if it names the facility, else Excluded |
| 3 | Names the facility, category is Unknown (PDF), and has no statement verb* | Affiliation only |
| 4 | Names the facility, similarity ≥ `canonical_threshold` (default 0.85) | **Canonical statement** |
| 5 | Names the facility, lower similarity | **Variant wording** |
| 6 | Funder only: matches `context_regex` (or `context_regex` is empty), does not match `other_facility_regex`, and does not name out-of-scope work (scope check) | Review |
| 7 | Anything else | Excluded |

\* Statement verbs (regular expression, case-insensitive):
`perform|carried\s+out|conduct|analy[sz]|acknowledg|thank|grateful|support|fund|financ|provid|assist|help|service|measur|acquir|process|run\b|courtesy`

## 5. Status of the paper

| Condition (first that applies) | Status | Counted? |
|---|---|---|
| At least one Canonical statement or Variant wording | **Confirmed** (then the scope check, below) | yes |
| The facility is listed as author: an author's affiliation names the facility, or an author is in `facility_staff` (same surname and first name) | Confirmed, basis "Facility listed as author, no statement" | yes, marked as without a statement |
| No full text could be retrieved | Unverified | no |
| At least one Review sentence | Needs review | no |
| Otherwise | Excluded, with a reason: "Phrase occurs only in the reference list", "Funding-body sentence concerns another facility", or "Phrase not found in the retrieved full text" | no |

## 6. Scope check (only if `scope_check.enabled` is true)

For Confirmed papers. Let *bare sentences* be the facility sentences with the facility name removed (the name
itself may contain an in-scope word such as "proteomics"). Let *body* be all text except References and
Affiliation, also with the facility name removed. Count matches of `in_scope_regex` (n_in) and
`out_of_scope_regex` (n_out) in the body, and note any `out_of_scope_facility_regex` matches.

| Condition (first that applies) | Result |
|---|---|
| A bare sentence matches `in_scope_regex` | in_scope_label (counted) |
| A bare sentence matches `out_of_scope_regex` and names no out-of-scope facility | **Excluded**: "the facility sentence describes out-of-scope work" |
| n_in ≥ `in_scope_min_terms`, or (n_in > 0 and n_out < `out_of_scope_min_terms` and no out-of-scope facility named) | in_scope_label (counted) |
| n_out ≥ `out_of_scope_min_terms`, or an out-of-scope facility is named | **Needs review**: generic sentence, paper describes only out-of-scope work |
| none of the above | "Not determined" (counted) |

A counted paper that also has n_out ≥ `out_of_scope_min_terms` (or names an out-of-scope facility) is labelled
"<in_scope_label> + <out_of_scope_label, first letter in lower case> in paper".

## 7. Type and year

* **Preprint** if the DOI starts with 10.1101 or 10.64898 (bioRxiv/medRxiv), 10.21203 (Research Square),
  10.20944 (Preprints.org), 10.2139 (SSRN), 10.48550 (arXiv), 10.31234 (PsyArXiv), 10.31219 (OSF Preprints),
  10.22541 (Authorea), 10.26434 (ChemRxiv) or 10.36227 (TechRxiv), or if the record's type is preprint.
  **Other** if the DOI prefix is listed in `other_doi_prefixes`. Otherwise **Published article**.
* **Year:** `year_basis: first` → year of the earliest public date (online, print or preprint posting);
  `issue` → the journal issue year (fall back to the first date if missing).
