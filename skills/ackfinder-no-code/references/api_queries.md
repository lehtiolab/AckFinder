# API requests used by AckFinder

These are the requests the Python tool sends (see `src/ackfinder/core.py`). None needs a key. Pace them:
at most about 3 requests per second to NCBI, and a short pause between requests elsewhere. URL-encode query
values. Do not send personal data (names, e-mail addresses) in requests unless the user asked you to.

Placeholders: `PHRASE` = one search phrase; `Y0` = first requested year; `Y1` = last requested year.
The search window starts one year early, at `Y0-1`, to catch papers whose dates differ between databases.

## 1. Europe PMC full-text search (main source)

```
GET https://www.ebi.ac.uk/europepmc/webservices/rest/search
    ?query="PHRASE" AND (FIRST_PDATE:[{Y0-1}-01-01 TO {Y1}-12-31])
    &format=json&resultType=core&pageSize=1000&cursorMark=*
```

* Paging: repeat with `cursorMark=<nextCursorMark>` until the result list is empty or `nextCursorMark` stops
  changing.
* Record `hitCount` for the report's coverage table.
* Useful fields in `resultList.result[]`: `id`, `source`, `pmid`, `pmcid`, `doi`, `title`, `authorString`,
  `authorList.author[].fullName`, `journalInfo.journal.title`, `journalInfo.yearOfPublication`,
  `firstPublicationDate`, `pubTypeList.pubType`, `bookOrReportDetails.publisher`.
* Look up known DOIs in one request: `query=DOI:"10.x/a" OR DOI:"10.x/b"` (same parameters).

## 2. Full text

| Source | Request | Notes |
|---|---|---|
| Europe PMC | `GET https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML` | JATS XML; 404 = not open access |
| NCBI PMC | `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={PMCID without "PMC"}&retmode=xml` | JATS XML; sometimes works when Europe PMC does not |
| bioRxiv / medRxiv | `GET https://api.biorxiv.org/details/{biorxiv or medrxiv}/{DOI}` → `collection[-1].jatsxml` | Preprints (DOI prefix 10.1101 or 10.64898) |
| Open-access PDF | OpenAlex work → `best_oa_location.pdf_url` / `locations[].pdf_url` | Read headings to find sections |

## 3. OpenAlex (optional second source)

```
GET https://api.openalex.org/works
    ?filter=fulltext.search:"PHRASE",publication_year:{Y0-1}-{Y1}
    &per-page=100
```

* `meta.count` is the total. Without a key only the first page (100 results) is returned; say so in the report
  if `meta.count` is larger.
* OpenAlex often refuses keyless searches with HTTP 429. Try once more after a pause; if it fails again, skip
  OpenAlex and note it in the coverage table.
* Look up works by DOI (up to 50 per request): `GET https://api.openalex.org/works?filter=doi:10.x/a|10.x/b&per-page=50`.
  Useful fields: `doi`, `title`, `publication_date`, `type`, `authorships[].author.display_name`,
  `primary_location.source.{id, display_name, issn}`, `ids.pmid`, `ids.pmcid`.

## 4. Impact-factor proxy

```
GET https://api.openalex.org/sources?filter=openalex:S123|S456&per-page=50
GET https://api.openalex.org/sources?filter=issn:1234-5678
```

Use `summary_stats.2yr_mean_citedness`. Label it "IF proxy (OpenAlex 2-year mean citedness)", never "JIF".

## 5. Preprints

`GET https://api.biorxiv.org/details/{biorxiv|medrxiv}/{DOI}` → `collection[-1]`: `date` (posting date),
`published` (DOI of the published version, or `NA`), `jatsxml`.

## 6. NCBI PMC search (the tool also uses this; optional by hand)

```
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi
    ?db=pmc&term="PHRASE" AND {Y0-1}:{Y1}[pdat]&retmax=10000&retmode=json
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pmc&id=ID1,ID2,…&retmode=json
```
