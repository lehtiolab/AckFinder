# AckFinder

Finds publications that credit a core facility in their acknowledgements, funding statement or methods, checks
each one against the paper's full text, and writes one report per year (Markdown, Excel, CSV).

It was built for the Clinical Proteomics Mass Spectrometry (CPMS) facility at Karolinska Institutet / SciLifeLab.
Everything facility-specific lives in one config file, so any facility can use it.

The tool is deterministic: regular expressions, fixed rules and Python's `difflib`, with no language model. The
same config and the same database contents give the same report.

**Contents**

1. [Three ways to use it](#1-three-ways-to-use-it)
2. [Install](#2-install)
3. [Quick start](#3-quick-start)
4. [Set up the config for your facility](#4-set-up-the-config-for-your-facility)
5. [API keys and the `.env` file](#5-api-keys-and-the-env-file)
6. [Run and read the reports](#6-run-and-read-the-reports)
7. [Review uncertain papers and record decisions](#7-review-uncertain-papers-and-record-decisions)
8. [How it works](#8-how-it-works)
9. [Use from Python, schedule, share](#9-use-from-python-schedule-share)
10. [Known limits](#10-known-limits)

---

## 1. Three ways to use it

| You are… | Do this |
|---|---|
| **Not into coding** | Open this folder in an AI coding agent (Claude Code, Codex, Cursor, Copilot agent, …) and say *"Set up and run the facility acknowledgement search."* The agent follows [`AGENTS.md`](AGENTS.md): it installs the tool, asks you about your facility, writes the config, runs the search and, if you want, helps you review the uncertain papers. |
| **Comfortable with a terminal** | Install (section 2), run `ackfinder init --interactive`, answer the questions, then run `ackfinder run`. No code, no regular expressions. |
| **A programmer** | Edit `config.yaml` directly, call the CLI from scripts or cron, or import the package (section 9). |

All three use the same Python code and give the same results. The agent only adds the conversation and an
optional second look at uncertain papers.

## 2. Install

You need Python 3.10 or newer (tested on 3.12) and internet access to Europe PMC, NCBI, OpenAlex and bioRxiv.
The dependencies are installed for you: pandas, requests, lxml, pypdf, PyYAML, XlsxWriter, openpyxl.

Choose **one** of these:

**pip** (any Python environment, venv or conda env)

```bash
cd AckFinder
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install .                                          # or: pip install -e ".[dev]" to edit the code
ackfinder --version
```

**uv** ([docs.astral.sh/uv](https://docs.astral.sh/uv/)) uses the pinned versions in `uv.lock`:

```bash
cd AckFinder
uv sync                       # creates .venv with the locked dependencies
uv run ackfinder --version    # prefix every command with `uv run`
```

To get `ackfinder` as a command available everywhere: `uv tool install /path/to/AckFinder`.

**pixi** ([pixi.sh](https://pixi.sh)) installs from conda-forge and needs no Python beforehand:

```bash
cd AckFinder
pixi install
pixi run ackfinder --version
pixi run setup                         # = ackfinder init --interactive
pixi run check                         # = ackfinder check --online
pixi run report --years 2025 2026      # = ackfinder run --years 2025 2026
```

After any of these, `ackfinder` and `python -m ackfinder` do the same thing.

## 3. Quick start

Work in a folder that will hold your config and reports. This can be this folder or any other.

```bash
ackfinder init --interactive        # answers -> config.yaml (+ .env.example)
ackfinder check --online            # validates config.yaml, counts hits per search phrase
ackfinder run --years 2025 2026     # search, verify, write reports
```

The reports are written to the `output_dir` set in the config (default `reports/`). To try the tool before
setting up your own facility, copy the worked example: `cp examples/config.cpms.yaml config.yaml`.

## 4. Set up the config for your facility

The config works like a `.env` file:

| File | What it is | Share it? |
|---|---|---|
| `config.example.yaml` | Commented template with `CHANGE_ME` placeholders. Do not edit it. | Yes (part of the package) |
| `config.yaml` | Your facility's settings, made from the template. | Your choice (ignored by git by default) |
| `examples/config.cpms.yaml` | A complete real config (CPMS) to copy from. | Yes |

There are three ways to create `config.yaml`:

1. **Questions:** `ackfinder init --interactive` asks 11 plain-language questions and writes all the
   patterns for you.
2. **Answers file:** fill in a copy of [`examples/setup_answers.example.yaml`](examples/setup_answers.example.yaml),
   then run `ackfinder init --answers setup_answers.yaml`. The AI agent uses this route.
3. **By hand:** `ackfinder init` (or `cp config.example.yaml config.yaml`), then replace every `CHANGE_ME`.

All three keep the template's comments, so the file explains itself later.

### What you must change

Only section 1 of the config is required. `ackfinder check` and `ackfinder run` refuse to work while any of
these still says `CHANGE_ME`.

| Key | What to put there | CPMS example |
|---|---|---|
| `facility_label` | Name used in report titles | `Clinical Proteomics Mass Spectrometry facility (Karolinska Institutet / SciLifeLab)` |
| `canonical_statement` | The acknowledgement sentence you ask users to include, word for word | `Mass spectrometry analysis was performed by the Clinical Proteomics Mass Spectrometry facility …` |
| `facility_regex` | Pattern that recognises your facility's name in a sentence (`init` writes it from the names you give) | `'clinical[\s\-]+proteomics[\s\-]+(?:mass[\s\-]*spectrometry\|ms)\b'` |
| `search_phrases` | Exact phrases sent to the full-text indexes: the full name and common variants | `Clinical Proteomics Mass Spectrometry`, `Clinical Proteomics MS` |

### What you will probably want to change

| Key | Default | Purpose |
|---|---|---|
| `output_dir` | `reports` | Where reports go. Relative paths are relative to the config file. |
| `years` | current year | Years reported when you run without `--years` |
| `report_slug` | from the label | File names: `<report_slug>_<year>.md` |
| `jif_table` | none | Clarivate JCR export, for official impact factors (section 6) |

### Optional features

| Config section | Keys | Turn it on when |
|---|---|---|
| 3. Funder-only sentences | `funding_regex`, `funding_label`, `context_regex`, `context_label`, `other_facility_regex` | Papers sometimes thank your funder ("… supported by the Infrastructure Board at KI") without naming the facility. Such papers go to *Needs review* and are never counted automatically. |
| 4. Scope check | `scope_check:` block | Your field overlaps another facility's (e.g. proteomics vs metabolomics). Adds a column such as *MS type*, and excludes or sends to review papers whose credited work is out of scope. |
| 5. Facility co-authors | `include_facility_authors`, `affiliation_phrases` | You also want papers where facility staff are co-authors but there is no facility statement. |
| 7. Fine-tuning | `canonical_threshold`, `word_expansions`, `other_doi_prefixes` | Rarely needed. |

Every key is explained in a comment in [`config.example.yaml`](config.example.yaml).

### Test your patterns

```bash
ackfinder check                    # errors (must fix), warnings (advice), resolved folders, keys set or not
ackfinder check --online           # + Europe PMC hit count per search phrase (0 = typo? >500 = too generic?)
ackfinder classify "We thank the Example Proteomics Core for MS analysis."
```

`classify` prints the class (*Canonical statement*, *Variant wording*, *Review*, *Excluded*) and the similarity
score. Your own `canonical_statement` should come out as *Canonical statement*.

### Config file location

The tool looks for the config in this order: `--config PATH`, then the environment variable
`ACKFINDER_CONFIG`, then `./config.yaml`. To keep several facilities apart, give each its own folder or config
file.

## 5. API keys and the `.env` file

No key is required. Keys are read from environment variables or from a `.env` file; they never go in the config
or the code. Copy the template and fill in what you have:

```bash
cp .env.example .env      # `ackfinder init` also writes .env.example next to config.yaml
```

| Variable | Needed? | What it adds |
|---|---|---|
| `OPENALEX_API_KEY` | No, but recommended | Free personal key ([OpenAlex docs](https://help.openalex.org/how-to-use-the-api/rate-limits-and-authentication)). Without one, OpenAlex returns only the first 100 results per query (the report says so when this happens) and refuses requests with HTTP 429 once the shared daily budget is used up. |
| `NCBI_API_KEY` | No | 10 instead of 3 requests per second to PubMed/PMC |
| `NCBI_EMAIL` | No | Contact address that NCBI asks API users to send |
| `ACKFINDER_CONFIG` | No | Config path, if not `./config.yaml` |

`.env` is read from the folder you run the command in and from the config file's folder. Variables already set
in your shell take precedence. `ackfinder check` shows whether each key is set but never prints a value.
Never share or commit `.env`.

### Getting an OpenAlex key

OpenAlex is the source most likely to limit a run without a key. Signs are `rate-limited (HTTP 429)` lines in the
log and "skipped" or "cut off" for OpenAlex in the report's *Method and coverage* table. A free key fixes most of
this:

| | Without a key | Free key |
|---|---|---|
| Daily budget | about $0.10 | $1 (10×) |
| Requests per second | 10 | 30 |
| Results per search phrase | first page only (100) | all pages |

1. Sign up at [openalex.org](https://openalex.org) (free), confirm your e-mail address, and copy the key from
   **Settings → API key**. The key only works once the e-mail address is confirmed.
2. Put it in `.env`: `OPENALEX_API_KEY=your-key`.
3. Run `ackfinder check`; `OPENALEX_API_KEY` should show as *set*.

Without a key, the daily budget is shared by everyone on the same network address. On a university network, other
people's OpenAlex use counts against it, so it can be used up before your run starts.

New accounts (created after 3 November 2026) spend their first $1 at no more than $0.10 every 5 minutes, about 50
minutes of steady use, so the first run with a new key can still get some HTTP 429 responses. The tool waits and
retries; waiting costs nothing. Budgets reset at midnight UTC. Keys are personal: each user creates their own.

When OpenAlex answers that the daily budget is used up, the tool does not wait for the reset. It skips OpenAlex for
the rest of the run, takes journal names from Europe PMC and PubMed, and leaves the IF proxy empty for papers OpenAlex
had not answered for. The report's *Method and coverage* table says so. Papers are still found and checked through
Europe PMC and PMC.

Figures from the [OpenAlex rate-limit page](https://help.openalex.org/how-to-use-the-api/rate-limits-and-authentication)
and OpenAlex's own HTTP 429 message, checked 2026-11-19. OpenAlex changes them from time to time; check that page if a run behaves differently.

## 6. Run and read the reports

```bash
ackfinder run                              # years from the config (or the current year)
ackfinder run --years 2024 2025 2026
ackfinder run --outdir /some/other/folder  # override for this run only
```

| Option | Overrides config key | Purpose |
|---|---|---|
| `--years Y [Y …]` | `years` | One report per year |
| `--outdir DIR` | `output_dir` | Report folder |
| `--year-basis first\|issue` | `year_basis` | `first` = earliest public date (online first or preprint posting); `issue` = journal issue year |
| `--pdf-dir DIR` | `pdf_dir` | PDFs you supply for paywalled papers |
| `--jif-table FILE` | `jif_table` | JCR export for official impact factors |
| `--decisions FILE` | `manual_decisions` | Your include/exclude decisions (section 7) |
| `--include-facility-authors` | `include_facility_authors` | Also list papers with a facility co-author but no statement |
| `--no-openalex`, `--no-pmc` | – | Skip a source |
| `--cache DIR` | `cache_dir` | Download cache; delete it to force fresh downloads |
| `-v` | – | Debug logging |

A first run takes a few minutes, mostly downloading full texts. Reruns are faster because full texts are cached.

**Output** (in `output_dir`):

| File | Content |
|---|---|
| `README.md` | Overview, one row per year |
| `<year>/<slug>_<year>.md` | The report: summary, paper table, the statement as found in each paper, *Needs review*, *Unverified* and *Excluded* lists, sources searched with hit counts, method notes |
| `<year>/<slug>_<year>.xlsx` | Same content as spreadsheet sheets |
| `<year>/<slug>_<year>.csv` | Paper table only |
| `candidates_audit_<date>.json` | Every query with its hit count, and every candidate with the reason it was kept or rejected |

**Statuses.** Only *Confirmed* papers are counted.

| Status | Meaning |
|---|---|
| Confirmed | The full text names the facility outside the reference list: *Canonical statement* (close to yours) or *Variant wording* |
| Needs review | A funder sentence without the facility name, or a generic statement in a paper whose work looks out of scope. Not counted. |
| Unverified | Found by a search index, but no full text could be retrieved. Not counted. |
| Excluded | The phrase appears only in references, or the sentence is about another facility or out-of-scope work |

**Impact factor.** Clarivate JIFs are licensed and cannot be fetched openly. Every report includes an open proxy:
the journal's OpenAlex *2-year mean citedness*. It is computed like the JIF but from OpenAlex citation data, so it
is usually higher than the JIF. For official values, export the journal list from JCR (your library provides
access) and set `jif_table`. The file needs ISSN/eISSN columns and a JIF column.

## 7. Review uncertain papers and record decisions

1. **Unverified papers:** download the PDF if you have access. Save it in `pdf_dir`, named after the DOI with `/`
   replaced by `_` (e.g. `10.1002_cam4.72386.pdf`), and run again. The tool then checks the PDF itself.
2. **Needs review, or a call you disagree with:** add an entry to the manual decisions file (`manual_decisions`
   in the config, default `manual_decisions.yaml`; format in
   [`examples/manual_decisions.example.yaml`](examples/manual_decisions.example.yaml)):

   ```yaml
   - doi: "10.1234/example.2025.001"
     decision: include          # or: exclude
     reason: Acknowledges the facility under its old name; confirmed with the PI.
     decided_by: AB
     date: "2026-01-15"
   ```

   Run again. The code applies the decision and shows it in the report (Inclusion basis *Manual decision*, with
   your reason) and in the audit file. Do not edit the reports by hand: the next run overwrites them. A CSV file
   with the columns `doi,decision,reason,decided_by,date` works too.

An AI agent can do steps 1 and 2 with you: it reads each uncertain paper, proposes a decision with the quoted
evidence, and writes the decisions you approve to this file.

## 8. How it works

1. **Search.** Exact-phrase full-text search for each `search_phrases` entry in Europe PMC and NCBI PMC, and in
   OpenAlex (`openalex_phrases`). The window starts one year before the first requested year, so papers whose
   dates differ between databases are not missed.
2. **Merge** hits on DOI, PMCID, PMID, preprint ID and title.
3. **Metadata** from OpenAlex, Europe PMC and bioRxiv: year, journal, authors, preprint or article, the published
   version of preprints, impact factor.
4. **Verify.** The full text comes from the first source that works: Europe PMC JATS, NCBI PMC JATS,
   bioRxiv/medRxiv JATS, open-access PDF, then your local PDF. The text is split into sentences tagged with their
   section, and every sentence that matches `facility_regex` (or `funding_regex`) is classified:
   * **Canonical statement:** names the facility, and its similarity to `canonical_statement` is at least
     `canonical_threshold` (default 0.85). Similarity is the larger of (a) the share of the reference statement's
     content words found in the sentence and (b) Python `difflib`'s ratio between the two word sequences.
   * **Variant wording:** names the facility, with lower similarity.
   * **Review:** a funder sentence that matches `context_regex`, does not name the facility, and names no other
     facility.
   * **Excluded:** reference-list matches, or sentences about other facilities or out-of-scope work.

   If the scope check is on, each paper with a facility sentence is then labelled by term counts in its text.
5. **Report** per year, plus the overview and the audit file.

[`ALGORITHM.md`](ALGORITHM.md) shows the call graph and where to change behaviour in the code.

## 9. Use from Python, schedule, share

**Python**

```python
from ackfinder import load_config, validate, RunSettings, run

cfg = load_config("config.yaml")
errors, warnings = validate(cfg)
result = run(cfg, RunSettings.from_config(cfg, years=[2025, 2026]))
print(result["years"][2025]["total"], result["overview"])
```

**Scheduling** (cron, monthly):

```cron
17 6 1 * *  cd /path/to/workfolder && /path/to/.venv/bin/ackfinder run --years $(date +\%Y) >> ackfinder.log 2>&1
```

**Sharing.** Share the package folder: code, `config.example.yaml`, `.env.example`, `examples/`, the docs and
`skills/`. Do **not** share `.env` (keys), `pdfs/` (copies of paywalled papers) or `cache/`; all three are in
`.gitignore`. `config.yaml` holds no secrets, so you may share it.

**AI agents.** [`AGENTS.md`](AGENTS.md) holds the agent instructions. Codex, Cursor, Copilot and most other coding
agents read it automatically; Claude Code reads it through `CLAUDE.md`. The agent always runs the Python tool.
Only when no Python environment exists at all (e.g. a chat-only assistant) does it fall back to the skill in
[`skills/ackfinder-no-code/`](skills/ackfinder-no-code/SKILL.md), which repeats the method by hand and gives
less complete, non-reproducible results. To make the skill available to Claude Code, copy the folder to
`.claude/skills/` in your own project or to `~/.claude/skills/`.

**Development.** `pip install -e ".[dev]"`, then `pytest` (offline tests) and `pyflakes src tests`.

## 10. Known limits

* **Coverage.** Full-text indexes cover open-access papers, PMC author manuscripts, preprints, and the paywalled
  papers OpenAlex has indexed. A paywalled paper that none of them hold is not found, so counts are a lower bound.
* **Indexing lag.** Europe PMC and PMC index new papers days to weeks after publication, so counts for the current
  year keep rising.
* **Names, not paraphrases.** A paper is counted only if it names the facility in a form that `facility_regex`
  matches. Informal names ("the proteomics core") are missed unless you add them. Funder-only sentences go to
  *Needs review* if config section 3 is set up.
* **Scope check.** The scope check counts terms; it does not read the methods. When a paper has both kinds of
  work it cannot tell which facility ran which experiment, so such papers are labelled for you to check.
* **OpenAlex without a key.** OpenAlex returns only the first page of results per query, and refuses all requests
  once the daily budget, shared by everyone on your network address, is used up. The tool then uses the last stored
  search answer, or skips OpenAlex for the rest of the run, which leaves the IF proxy empty for some papers. The
  report's *Method and coverage* table shows what happened. A free key fixes most of this (section 5, *Getting an OpenAlex key*).

## License

MIT; see [`LICENSE`](LICENSE).
