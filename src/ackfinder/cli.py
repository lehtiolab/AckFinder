"""Command line: `ackfinder init | check | classify | run` (also `python -m ackfinder`)."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import shutil
import sys
from pathlib import Path

import yaml

from . import config as C
from .core import EPMC, Http, Verifier, __version__

log = logging.getLogger("ackfinder")

EPILOG = """\
Typical use:
  ackfinder init --interactive      # answer questions -> config.yaml
  ackfinder check --online          # validate config.yaml, count hits per search phrase
  ackfinder run --years 2025 2026   # search, verify and write the reports

The config is ./config.yaml unless you pass --config or set ACKFINDER_CONFIG.
API keys (all optional) are read from the environment or from a .env file next to the config."""


# --------------------------------------------------------------------------- helpers
def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def _attach_run_log(outdir: Path, run_date: str) -> Path:
    """Also write this run's log to <outdir>/run_log_<date>.txt (a rerun on the same day replaces it)."""
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"run_log_{run_date}.txt"
    h = logging.FileHandler(path, mode="w", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    h.setLevel(root.level)
    root.addHandler(h)
    return path


def _load(args, need_valid: bool = True) -> dict | None:
    path = C.find_config(args.config)
    if not path.is_file():
        here = " (from $ACKFINDER_CONFIG)" if not args.config and os.environ.get(C.CONFIG_ENV_VAR) else ""
        print(f"No config file at {path}{here}.\n"
              "Create one with `ackfinder init --interactive` (questions) or `ackfinder init` "
              "(copies the commented template), or point to one with --config.", file=sys.stderr)
        return None
    C.load_dotenv(path.parent, Path.cwd())
    try:
        cfg = C.load_config(path)
    except C.ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return None
    if need_valid:
        errors, warnings = C.validate(cfg)
        for w in warnings:
            log.warning("config: %s", w)
        if errors:
            print(f"{path} has {len(errors)} error(s); fix them and try again "
                  "(`ackfinder check` lists them):", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            return None
    return cfg


def _ask(prompt: str, default: str = "", required: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        v = input(f"{prompt}{hint}: ").strip()
        if v:
            return v
        if default or not required:
            return default
        print("  (required)")


def _yes(prompt: str, default: bool = False) -> bool:
    v = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not v else v.startswith("y")


def interview() -> dict:
    """Terminal questions -> answers dict for config.build_config (no regular expressions needed)."""
    print("Answer in plain text. Lists: separate items with ';'. Press Enter to accept [defaults] or skip "
          "optional questions.\n")
    a: dict = {}
    a["facility_name"] = _ask("1. Full name of the facility as it appears in papers "
                              "(e.g. Clinical Proteomics Mass Spectrometry)", required=True)
    a["short_name"] = _ask("2. Short name or abbreviation (used in file names, e.g. CPMS)")
    a["host_institution"] = _ask("3. Host institution(s) (e.g. Karolinska Institutet / SciLifeLab)")
    a["name_variants"] = _ask("4. Other ways papers write the facility name (e.g. Clinical Proteomics MS)")
    a["acknowledgement_statement"] = _ask("5. The acknowledgement sentence you ask users to include", required=True)
    a["funding_body"] = _ask("6. A funder named in that sentence whose name alone could hint at the facility "
                             "(e.g. Infrastructure Board at Karolinska Institutet); sentences naming it without "
                             "the facility go to 'Needs review'")
    if a["funding_body"]:
        a["field_terms"] = _ask("6b. Words that show such a sentence is about your field (e.g. proteom; mass "
                                "spectrometry)")
        a["other_facilities"] = _ask("6c. Other facilities funded by the same body (their sentences are "
                                     "excluded, not reviewed)")
    if _yes("7. Check whether each paper's work is in scope for your facility (e.g. proteomics vs "
            "metabolomics done elsewhere)?"):
        a["scope"] = {
            "column": _ask("   Report column name", "Service type"),
            "in_scope_label": _ask("   Label for in-scope work (e.g. Proteomics)", required=True),
            "in_scope_terms": _ask("   Words showing in-scope work (e.g. proteom; peptide; trypsin; TMT)",
                                   required=True),
            "out_of_scope_label": _ask("   Label for out-of-scope work (e.g. Small-molecule MS)", required=True),
            "out_of_scope_terms": _ask("   Words showing out-of-scope work (e.g. metabolom; lipidom)", required=True),
            "out_of_scope_note": _ask("   Short note on who does that work (optional)"),
            "out_of_scope_facilities": _ask("   Names of the facilities that do that work (optional)"),
        }
    a["affiliation_search"] = _yes("8. Also search author affiliations for the facility name? Papers with such an "
                                   "author are listed even without a facility statement")
    a["facility_staff"] = _ask("8b. Names of facility staff, separated by ';' (e.g. Jane Doe; Doe, John "
                               "(0000-0002-1825-0097)). Their papers are listed even without a facility statement")
    a["requesters"] = _ask("8c. Names of people who request your services, separated by ';'. Their papers count "
                           "only with a facility statement (long lists are easier to paste into config.yaml later)")
    short = a["short_name"] or C.slugify(a["facility_name"]).split("_")[0]
    a["output_dir"] = _ask("9. Folder for the reports (relative to the config file)",
                           f"reports/{short.lower()}_acknowledgements")
    a["years"] = _ask("10. Years to report by default (e.g. 2025 2026)", str(dt.date.today().year))
    a["jif_table"] = _ask("11. Path to a JCR journal impact factor export (CSV/XLSX), if you have one")
    return a


def _write_env_example(folder: Path) -> Path | None:
    if (folder / ".env").exists() or (folder / C.ENV_TEMPLATE_NAME).exists():
        return None
    p = folder / C.ENV_TEMPLATE_NAME
    p.write_text(C.template_text(C.ENV_TEMPLATE_NAME), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- commands
def cmd_init(args) -> int:
    path = Path(args.config or C.find_config(None)).expanduser()
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to replace it (a .bak copy is kept), or edit it directly.",
              file=sys.stderr)
        return 1
    if args.interactive and args.answers:
        print("Use either --interactive or --answers, not both.", file=sys.stderr)
        return 1
    template = C.template_text()
    if args.interactive or args.answers:
        if args.answers:
            answers = yaml.safe_load(Path(args.answers).read_text(encoding="utf-8")) or {}
        else:
            answers = interview()
        try:
            text = C.render_config(C.build_config(answers), template)
        except C.ConfigError as e:
            print(f"Could not build a config: {e}", file=sys.stderr)
            return 1
    else:
        text = template
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
        print(f"Previous config kept as {path.name}.bak")
    path.write_text(text, encoding="utf-8")
    env = _write_env_example(path.parent)
    print(f"Wrote {path}")
    if env:
        print(f"Wrote {env} (API keys are optional; copy it to .env to use them)")
    cfg = C.load_config(path)
    errors, warnings = C.validate(cfg)
    if errors:
        print("\nStill to fill in before the first run:")
        for e in errors:
            print(f"  - {e}")
        print("Open the file, replace every CHANGE_ME, then run `ackfinder check`.")
    else:
        for w in warnings:
            print(f"  warning: {w}")
        print("\nNext: `ackfinder check --online`, then `ackfinder run`.")
    return 0


def _epmc_hits(phrase: str, y0: int, y1: int) -> int | None:
    http = Http(None)
    r = http.get(f"{EPMC}/search", params=dict(
        query=f'"{phrase}" AND (FIRST_PDATE:[{y0 - 1}-01-01 TO {y1}-12-31])', format="json",
        resultType="idlist", pageSize=1))
    if r is None or r.status_code != 200:
        return None
    return int(r.json().get("hitCount", 0))


def cmd_check(args) -> int:
    path = C.find_config(args.config)
    if not path.is_file():
        _load(args)
        return 2
    env_files = C.load_dotenv(path.parent, Path.cwd())
    try:
        cfg = C.load_config(path)
    except C.ConfigError as e:
        print(f"Config error: {e}")
        return 1
    errors, warnings = C.validate(cfg)
    sc = cfg.get("scope_check") or {}
    pdfs = len(list(cfg["pdf_dir"].glob("*.pdf"))) if cfg.get("pdf_dir") and cfg["pdf_dir"].is_dir() else 0

    def state(p: Path | None, kind: str = "dir") -> str:
        if not p:
            return "not set"
        if kind == "file":
            return f"{p}" + ("" if p.is_file() else "  (missing)")
        return f"{p}" + ("" if p.exists() else "  (will be created)")

    years = cfg.get("years") or [dt.date.today().year]
    print(f"Config:            {path}")
    print(f"Facility:          {cfg.get('facility_label')}")
    print(f"Report file names: {cfg.get('report_slug')}_<year>.md/.csv/.xlsx")
    print(f"Output folder:     {state(cfg.get('output_dir'))}")
    print(f"Cache folder:      {state(cfg.get('cache_dir'))}")
    print(f"PDF folder:        {state(cfg.get('pdf_dir'))}" + (f"  ({pdfs} PDFs)" if pdfs else ""))
    print(f"JIF table:         {state(cfg.get('jif_table'), 'file')}")
    md = cfg.get("manual_decisions")
    print("Manual decisions:  " + (f"{md}" + ("" if md.is_file() else "  (no file yet: no decisions)")
                                   if md else "not set"))
    print(f"Default years:     {' '.join(map(str, years))}" + ("" if cfg.get("years") else "  (current year)"))
    print(f"Year basis:        {cfg.get('year_basis')}")
    print("Needs review:      " + ("on (funding-body sentences)" if cfg.get("funding_regex") else "off"))
    print("Scope check:       " + (f"on ({sc.get('column')}: {sc.get('in_scope_label')} vs "
                                    f"{sc.get('out_of_scope_label')})" if sc.get("enabled") else "off"))
    def people(k: str) -> str:
        ps = C.parse_people(cfg.get(k), k)[0]
        return f"{len(ps)} people ({sum(bool(x.orcid) for x in ps)} with ORCID)" if ps else "off"
    aff = cfg.get("affiliation_phrases") or []
    print("Author searches:   affiliation " + (f"{len(aff)} phrase(s)" if aff else "off")
          + f"; facility staff {people('facility_staff')}; requesters {people('requesters')}")
    print("Environment:       " + (", ".join(str(p) for p in env_files) if env_files else "no .env file found"))
    for k in C.API_ENV_VARS:
        print(f"  {k:<17}{'set' if os.environ.get(k) else 'not set'}")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")
    if args.online and not errors:
        y0, y1 = min(years), max(years)
        print(f"\nEurope PMC full-text hits per search phrase ({y0 - 1}-{y1}; the search window starts a year early "
              "to catch papers first posted then):")
        for ph in cfg["search_phrases"]:
            n = _epmc_hits(ph, y0, y1)
            flag = ""
            if n is None:
                flag = "  (query failed)"
            elif n == 0:
                flag = "  (no hits: check the spelling)"
            elif n > 500:
                flag = "  (many hits: the phrase may be too generic; every hit is downloaded and checked)"
            print(f"  {n if n is not None else '-':>6}  {ph}{flag}")
    print("\nOK" if not errors else "\nNot ready: fix the errors above.")
    return 1 if errors else 0


def cmd_classify(args) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    v = Verifier(None, cfg, None, None)
    text = " ".join(args.sentence)
    matches = v.classify([(text, "command line", args.section)])
    if not matches:
        print("No match: the text names neither the facility (facility_regex) nor the funding body "
              "(funding_regex).")
        print(f"Similarity to the reference statement: {v.score(text):.3f}")
        return 0
    for m in matches:
        print(f"{m.klass:<20} similarity {m.score:.3f}  {m.sentence}")
    if v.scope:
        n_in, t_in = v._terms(v.scope.in_rx, v.fac.sub(" ", text))
        n_out, t_out = v._terms(v.scope.out_rx, text)
        print(f"Scope terms in this text: {v.scope.in_label} {n_in} {t_in[:5] or ''}; "
              f"{v.scope.out_label} {n_out} {t_out[:5] or ''}")
    print(f"(Canonical needs similarity >= {cfg['canonical_threshold']}; the facility name must match "
          "facility_regex.)")
    return 0


def cmd_run(args) -> int:
    _setup_logging(args.verbose)
    cfg = _load(args)
    if cfg is None:
        return 1
    from .pipeline import RunSettings, run   # imported here so `init`/`check` start fast
    over = dict(years=args.years, output_dir=args.outdir, cache_dir=args.cache, pdf_dir=args.pdf_dir,
                jif_table=args.jif_table, year_basis=args.year_basis,
                use_openalex=False if args.no_openalex else None, use_pmc=False if args.no_pmc else None,
                manual_decisions=args.decisions)
    over = {k: (Path(v).expanduser().resolve() if isinstance(v, (str, Path)) and k not in ("year_basis",) else v)
            for k, v in over.items()}
    for k in ("jif_table", "manual_decisions"):
        if over.get(k) and not over[k].is_file():
            print(f"File not found: {over[k]}", file=sys.stderr)
            return 1
    settings = RunSettings.from_config(cfg, **over)
    log_path = _attach_run_log(settings.output_dir, settings.run_date)
    log.info("AckFinder %s; config %s; years %s; run log %s", __version__, cfg["_config_path"],
             " ".join(map(str, settings.years)), log_path)
    if not os.environ.get("OPENALEX_API_KEY") and settings.use_openalex:
        log.info("No OPENALEX_API_KEY: OpenAlex runs keyless (first page of results only, may hit rate limits).")
    try:
        result = run(cfg, settings)
    except Exception:
        log.exception("Run failed")
        raise
    lines = ["", "Year  Papers  Published  Preprints  Needs review  Unverified  Report"]
    for y, r in result["years"].items():
        lines.append(f"{y}  {r['total']:>6}  {r['published']:>9}  {r['preprints']:>9}  {r['needs_review']:>12}  "
                     f"{r['unverified']:>10}  {r['report']}")
    lines += [f"Overview: {result['overview']}", f"Audit trail: {result['audit']}", f"Run log: {log_path}"]
    print("\n".join(lines))
    with log_path.open("a", encoding="utf-8") as f:       # the results table goes in the log too
        f.write("\n".join(lines) + "\n")
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", type=Path,
                        help="Config file (default: $ACKFINDER_CONFIG, else ./config.yaml)")
    ap = argparse.ArgumentParser(
        prog="ackfinder", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Find publications that acknowledge a core facility and write yearly reports.")
    ap.add_argument("--version", action="version", version=f"AckFinder {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("init", parents=[common], help="Create config.yaml (template, questions, or answers file)",
                       description="Create config.yaml. Without options: copy the commented template, then edit "
                                   "every CHANGE_ME. --interactive asks plain questions and writes the patterns "
                                   "for you. --answers reads the same answers from a YAML file.")
    p.add_argument("-i", "--interactive", action="store_true", help="Ask questions in the terminal")
    p.add_argument("--answers", type=Path, help="YAML file with answers (see README: 'Answers file')")
    p.add_argument("--force", action="store_true", help="Replace an existing config (keeps a .bak copy)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("check", parents=[common], help="Validate the config and show what a run would use")
    p.add_argument("--online", action="store_true", help="Also count Europe PMC hits for each search phrase")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("classify", parents=[common], help="Show how one sentence would be classified",
                       description="Test the patterns on a sentence, e.g. "
                                   "ackfinder classify \"MS analysis was done at the X facility.\"")
    p.add_argument("sentence", nargs="+", help="The sentence (quote it)")
    p.add_argument("--section", default="Acknowledgements",
                   help="Section category the sentence comes from (default: Acknowledgements)")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("run", parents=[common], help="Search, verify and write the reports",
                       description="Run the search. Options override the config for this run only.")
    p.add_argument("--years", nargs="+", type=int, help="Years to report (default: `years` in the config, "
                                                        "else the current year)")
    p.add_argument("--outdir", type=Path, help="Report folder (overrides output_dir)")
    p.add_argument("--cache", type=Path, help="Download cache folder (overrides cache_dir)")
    p.add_argument("--pdf-dir", type=Path, help="Folder with PDFs named by DOI (overrides pdf_dir)")
    p.add_argument("--jif-table", type=Path, help="JCR export with ISSN and JIF columns (overrides jif_table)")
    p.add_argument("--decisions", type=Path, help="Manual decisions file (overrides manual_decisions)")
    p.add_argument("--year-basis", choices=["first", "issue"], help="Overrides year_basis")
    p.add_argument("--no-openalex", action="store_true", help="Skip OpenAlex")
    p.add_argument("--no-pmc", action="store_true", help="Skip the NCBI PMC search")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.set_defaults(func=cmd_run)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "command", None):
        ap.print_help()
        return 0
    if args.command != "run":
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    C.load_dotenv(Path.cwd())          # may set ACKFINDER_CONFIG; the config folder's .env is read later
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
