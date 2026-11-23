# Agent instructions: AckFinder

Instructions for AI assistants: coding agents (Claude Code, Codex, Cursor, Copilot, …) and chat apps that were
given a link to this repository. Humans: see [README.md](README.md).

This package finds publications that acknowledge a core facility and writes yearly reports. **The Python tool does
the work. Your job is to set it up, run it, explain the results and, if asked, help review the uncertain papers.**
The user may not be a programmer: run the commands for them, explain in plain words, and do not ask them to edit
code.

The README describes four routes. You serve route 3 (run the tool for the user) and, only when you cannot run the
tool, route 4 (the no-code skill).

## Rules

1. **Use the code.** Search, verification, counts and reports come from `ackfinder`. Do not count papers, build
   tables or write reports yourself while the tool can run. The no-code fallback (end of this file) is only for
   when no Python environment exists at all.
2. **Never edit the files the tool writes** in `output_dir` (reports, CSV, XLSX, overview, audit, run log). Corrections
   go in the manual decisions file (Step 4), then rerun. That keeps every change visible and repeatable.
3. **Keys stay secret.** API keys live only in `.env` or environment variables. Never write them into
   `config.yaml`, code or chat output, and never print `.env`. `ackfinder check` reports keys only as set/not set.
4. **Ask before overwriting** `config.yaml`, `.env` or the manual decisions file. `init --force` keeps a `.bak`
   copy, but ask anyway.
5. **Do not share or upload** `.env`, `pdf_dir` (copies of paywalled papers) or `cache_dir`.
6. **Report what happened.** If a command fails, show the relevant error lines and say what you will try next.
   Uncertain papers go to the user as *Needs review*; never count them on your own judgement.

## Step 0: Make sure the tool runs

| Where you are | Go to |
|---|---|
| In a copy of this repository on the user's computer, with a shell (coding agent) | 0a |
| A chat app with a code sandbox; the user gave you the repository link | 0b |
| You cannot run code at all | No-code fallback (end of this file) |

### 0a: Coding agent

```bash
ackfinder --version          # or: uv run ackfinder --version / pixi run ackfinder --version
```

If that fails, install it from this folder with whatever is available, in this order:

| Available | Install | Then prefix commands with |
|---|---|---|
| `pixi` | `pixi install` | `pixi run` |
| `uv` | `uv sync` | `uv run` |
| Python ≥ 3.10 | `python -m venv .venv && .venv/bin/pip install -e .` (Windows: `.venv\Scripts\pip`) | `.venv/bin/` (or activate the venv) |

Check again with `ackfinder --version`. If Python is older than 3.10, or installs fail for lack of network, tell
the user what is missing.

### 0b: Chat app sandbox

The sandbox is temporary: files are gone when the chat ends, and network access may be limited. Do these steps
before you ask the user any setup question.

1. **Get the repository** and work inside it for the whole chat:

   ```bash
   git clone https://github.com/lehtiolab/AckFinder.git && cd AckFinder
   ```

   Without `git`, download and unzip `https://github.com/lehtiolab/AckFinder/archive/refs/heads/main.zip`.
   Do not `pip install` straight from the link: you also need `examples/` and this file.

2. **Install:** `pip install .`, then `ackfinder --version`.

3. **Test the sources.** The tool needs these sites; a reply below 400 means reachable:

   ```bash
   python -c "
   import requests
   for u in ['https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=proteomics&format=json&pageSize=1',
             'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi',
             'https://api.openalex.org/works?per-page=1',
             'https://api.biorxiv.org/details/biorxiv/10.1101/339747',
             'https://www.biorxiv.org/', 'https://www.medrxiv.org/']:
       try: print(requests.get(u, timeout=20, allow_redirects=False).status_code, u.split('/')[2])
       except Exception as e: print('BLOCKED', u.split('/')[2], type(e).__name__)
   "
   ```

   * **All reachable:** go on. Open-access PDFs come from publishers' own sites; if the sandbox allows only
     listed sites, some of them will fail and those papers end up *Unverified*. Mention this in the summary.
   * **Some blocked:** tell the user which sites. Explain that the chat app's code settings must allow them (in
     the Claude app: Settings → Capabilities, network access for code execution; on Team and Enterprise plans an
     admin sets it), and that they must start a new chat after changing it. Europe PMC (`www.ebi.ac.uk`) is
     required. If only others are blocked, the user may choose a run with lower coverage (`--no-openalex`,
     `--no-pmc`); say so in the summary.
   * **The clone or install failed for lack of network, or nothing is reachable:** the tool cannot run here. Say
     so, and offer the no-code fallback (if you can fetch web pages) or a coding agent on the user's computer.

4. **Restore earlier work.** Ask whether the user has an `ackfinder_work.zip` from an earlier chat (Step 6). If
   they upload it, unpack it into the repository folder; it holds `config.yaml`, `setup_answers.yaml`,
   `manual_decisions.yaml` and earlier reports. Then go to Step 1.

5. **Keys.** There is no `.env` here. Without an OpenAlex key, a shared cloud address often runs out of OpenAlex
   budget. If the user wants to use a key, they can upload their `.env` file or paste the key; write it to `.env`
   without repeating it back. Never put `.env` in the zip of Step 6.

6. **Long commands.** If the sandbox stops `ackfinder run` before it ends, run the same command again: full texts
   already downloaded are cached in `cache_dir`, so it continues where it stopped.

## Step 1: Is there a config?

The tool looks for `--config PATH`, then `$ACKFINDER_CONFIG`, then `./config.yaml`.

* **`config.yaml` exists** → run `ackfinder check`.
  * `OK` → go to Step 3.
  * Errors that mention `CHANGE_ME` (an unfilled template) → tell the user, and with their permission go to Step 2
    (you will need `init --force`).
  * Other errors → show them, fix them with the user (edit `config.yaml`), check again.
* **Only `config.example.yaml` exists** → Step 2. Do not copy the example and fill it in by hand; the interview
  route writes the patterns for you.

## Step 2: Interview the user and create the config

Ask the questions below in a few short rounds, not all at once. Give the example with each question, accept
"skip" for optional ones, and do not ask the user for regular expressions: the tool writes them from plain names
and words. If they already have a facility config elsewhere, ask for it and use it instead.

| # | Ask | Answer key | Required |
|---|---|---|---|
| 1 | Full name of the facility as it appears in papers (e.g. *Clinical Proteomics Mass Spectrometry*) | `facility_name` | yes |
| 2 | Short name or abbreviation, used in file names (e.g. *CPMS*) | `short_name` | no |
| 3 | Host institution(s) (e.g. *Karolinska Institutet / SciLifeLab*) | `host_institution` | no |
| 4 | Other ways papers write the name: old names, abbreviations, spelling variants | `name_variants` (list) | no |
| 5 | The acknowledgement sentence you ask users to include, word for word | `acknowledgement_statement` | yes |
| 6 | Is a funder named in that sentence whose name alone could hint at the facility? (e.g. *Infrastructure Board at Karolinska Institutet*) | `funding_body` | no |
| 6b | If yes: words showing a sentence is about your field (e.g. *proteom; mass spectromet*) | `field_terms` (list) | no |
| 6c | If yes: other facilities paid by the same funder (e.g. *flow cytometry*) | `other_facilities` (list) | no |
| 7 | Does your field overlap another facility's, so papers should be checked for which kind of work they credit (e.g. proteomics vs metabolomics)? If yes: column name, in-scope label and words, out-of-scope label and words, who does the out-of-scope work | `scope:` block (see `examples/setup_answers.example.yaml`) | no |
| 8 | Also search author affiliations for the facility name? Such papers are listed even without a statement (default no) | `affiliation_search` | no |
| 8b | Names of facility staff (e.g. *Jane Doe; Doe, John (0000-0002-1825-0097)*); their papers are listed even without a statement | `facility_staff` (list) | no |
| 8c | Names of people who request the facility's services; their papers count only with a statement. Long lists can be pasted into `config.yaml` later | `requesters` (list) | no |
| 9 | **Where should the reports be saved?** Suggest `reports/<short>_acknowledgements`. Relative paths are relative to the config file. | `output_dir` | yes (confirm) |
| 10 | Which years should a run report by default? | `years` (list) | no |
| 11 | Do you have a Clarivate JCR export for official impact factors? Path? | `jif_table` | no |

Then:

1. Write the answers to `setup_answers.yaml` next to where `config.yaml` will be (format:
   `examples/setup_answers.example.yaml`; keep `cache_dir: cache`, `pdf_dir: pdfs`,
   `manual_decisions: manual_decisions.yaml` unless the user wants otherwise).
2. `ackfinder init --answers setup_answers.yaml` (add `--force` only if the user agreed to replace a config).
3. `ackfinder check --online`. Show the user the hit count per search phrase. 0 hits usually means a typo or a
   name nobody uses; more than 500 means the phrase is too generic (every hit is downloaded and checked).
4. `ackfinder classify "<their acknowledgement sentence>"` must print *Canonical statement*. Also try one or two
   variants the user expects to see, e.g. `ackfinder classify "Samples were analysed at the <short name>."`.
5. Show the user the key settings (facility label, search phrases, output folder, years, optional features) and
   ask them to confirm. To change something, edit `setup_answers.yaml` and rerun `init --answers … --force`, or
   edit `config.yaml` directly for fine-tuning (every key has a comment).
6. API keys (optional): if `.env` does not exist, explain that an OpenAlex key (free) makes the search more complete
   and that NCBI keys are optional. Ask the user to copy `.env.example` to `.env` and paste their keys there
   themselves. If they give you a key in chat, write it to `.env` without repeating it back.
7. `setup_answers.yaml` can stay; it is a readable record of the setup and is ignored by git.

## Step 3: Run the search

Before running, ask the user three things in one message:

1. **Which years?** Default: `years` in the config, else the current year.
2. **Output folder:** show the resolved folder from `ackfinder check` ("Output folder: …") and ask whether that
   is where they want the reports. If not, change `output_dir` in `config.yaml` (permanent) or pass
   `--outdir` (this run only).
3. **Do you want me to verify the results after the run?** The tool already verifies every paper against its full
   text; your check is a second reading of the uncertain and borderline cases (Step 4). Say where you will put
   your notes (default `<output_dir>/<year>/verification_<date>.md`) and let them choose another place.

Then:

```bash
ackfinder check
ackfinder run --years 2025 2026
```

A first run takes several minutes (it downloads every candidate's full text); give the command a long timeout or
run it in the background, and do not stop it early. The run ends with a table: papers, published, preprints,
Needs review and Unverified per year, and the report paths.

## Step 4 (optional): Help verify the results

Do this only if the user said yes in Step 3. You propose; the user decides; the code applies the decision.

Sources, for each year: the report `<output_dir>/<year>/<slug>_<year>.md` and the audit file
`<output_dir>/candidates_audit_<date>.json`. The audit lists every candidate with `status`, `reason`, `doi`,
`title`, `fulltext_source` and `matches` (each matched sentence with its class, similarity score and section).
Start from the matched sentences; open the full text only when they are not enough (Europe PMC:
`https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML`, or the DOI landing page).

1. **Needs review:** for each paper, decide whether it credits *this* facility's work. Quote the sentence and its
   section. Propose include or exclude.
2. **Unverified:** list them with DOI links. Ask the user to download the PDFs they can access into `pdf_dir`,
   named after the DOI with `/` replaced by `_` (e.g. `10.1002_cam4.72386.pdf`). In a chat app, the user uploads
   them into the chat and you copy them into `pdf_dir` with those names. Do not try to get around paywalls. The
   next run checks the PDFs.
3. **Confirmed (spot check):** if there are 10 or fewer, check all; otherwise check every *Variant wording* paper,
   every paper whose scope label says "… in paper" (mixed work), every paper whose *Statement* column says *No* or
   *Not checked* (facility listed as author: is the named author really facility staff, not a namesake or a
   research group at the same address?), and 3 *Canonical statement* papers. Check that
   the sentence names this facility (not a namesake elsewhere), sits in acknowledgements, methods or funding
   (not references), and that a preprint and its published version are not both counted.
4. **Excluded:** skim the reasons and flag any that look wrong.
5. Write your notes file with one row per checked paper: DOI, short title, tool status, your finding, quoted
   evidence (section), proposed decision (none / include / exclude). This file is yours; the tool never reads it.
6. Show the user the proposed changes and ask which to accept. Ask for their initials for `decided_by`.
7. Add each accepted decision to the manual decisions file (`manual_decisions` in the config, default
   `manual_decisions.yaml`; format in `examples/manual_decisions.example.yaml`):

   ```yaml
   - doi: "10.xxxx/…"
     decision: include            # or exclude
     reason: "<one sentence with the evidence>; agent-assisted review"
     decided_by: "<initials>"
     date: "<YYYY-MM-DD>"
   ```

8. Rerun `ackfinder run` with the same years and confirm that the counts changed as expected. The report then
   shows *Manual decision* as the inclusion basis, with the reason.

## Step 5: Summarise

Give the user, per year: papers counted (published / preprints), Needs review and Unverified still open, the mean
impact-factor proxy, and the report paths. Add the caveats that apply:

* Counts are a lower bound: paywalled papers that no index holds cannot be found.
* The current year is incomplete; databases index papers weeks after publication.
* "IF proxy" is OpenAlex's 2-year mean citedness, not the Clarivate JIF (set `jif_table` for official values).
* If the report's *Method and coverage* table says OpenAlex was skipped or cut off, coverage is lower; an
  OpenAlex key helps.

## Step 6 (chat apps only): Hand over the files

The sandbox is deleted when the chat ends. Before you finish:

1. Pack these into `ackfinder_work.zip`, with their folders: `config.yaml`, `setup_answers.yaml`,
   `manual_decisions.yaml` (if present) and the output folder (reports, audit, your verification notes).
   **Never include** `.env`, `cache_dir` or `pdf_dir`.
2. Give the zip to the user as a download. Tell them to keep it and upload it at the start of their next chat
   (Step 0b, item 4), and to keep their own copies of any PDFs they uploaded, since those are not in the zip.

## Reference

| Command | Purpose |
|---|---|
| `ackfinder init --answers FILE [--force]` | Config from an answers file (agent route) |
| `ackfinder init --interactive` | Config from terminal questions (human route) |
| `ackfinder check [--online]` | Validate the config; show folders, years, features, key status; `--online` adds hit counts |
| `ackfinder classify "SENTENCE"` | How one sentence is classified, with its similarity score |
| `ackfinder run [--years …] [--outdir …]` | Search, verify, write reports (`ackfinder run -h` for all options) |

| File | Role | Who edits it |
|---|---|---|
| `config.example.yaml` | Commented template | Nobody |
| `config.yaml` | The facility's settings | `init`, then user/agent with permission |
| `setup_answers.yaml` | Interview answers that produced the config | Agent, during Step 2 |
| `.env` | API keys | The user |
| `manual_decisions.yaml` | Include/exclude decisions per DOI | Agent with user approval, or the user |
| `<output_dir>/…` | Reports, overview, audit, run log | Only the tool |
| `ackfinder_work.zip` | Chat apps: the user's files between chats (Step 6) | Agent packs it; the user keeps it |

**Troubleshooting**

| Problem | What to do |
|---|---|
| `ackfinder: command not found` | Step 0; or `python -m ackfinder …` inside the environment |
| `No config file at …` | Step 1 |
| OpenAlex HTTP 429, or "OpenAlex skipped" in the report | Add `OPENALEX_API_KEY` to `.env`, or rerun later. `--no-openalex` skips it (lower coverage). |
| Chat app: `BLOCKED` sites, or connection errors during the run | The sandbox's network settings block a source: Step 0b, item 3 |
| Chat app: the run stopped before the end | Run the same command again; it reuses the downloaded full texts (Step 0b, item 6) |
| A search phrase has 0 hits | Check the spelling with the user; remove the phrase if nobody uses it |
| A paper the user knows about is missing | Look it up in the audit file. If it is there, its `reason` says why it was left out. If not, no index found it: add a manual `include` decision for its DOI (the tool then adds it as a candidate). |
| A Confirmed paper is wrong | Manual `exclude` decision; if the pattern is the cause, discuss changing `facility_regex` and check with `classify` |

## No-code fallback

Use `skills/ackfinder-no-code/SKILL.md` **only** when Step 0 fails: you cannot run code at all, or your chat app's
sandbox cannot install the tool or reach Europe PMC (Step 0b). Tell the user first that the result will be less
complete and not reproducible. The skill repeats the method with web requests and your own reading, and its
output must be labelled as not produced by the tool. If the tool can run later, rerun with it and use that result
instead.

## Changing the package itself

Developers: `pip install -e ".[dev]"`, `pytest`, `pyflakes src tests`. [ALGORITHM.md](ALGORITHM.md) maps the
code. Keep README.md, this file and the CLI help in step when commands or config keys change.
