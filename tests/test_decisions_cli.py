import shutil

from ackfinder import cli
from ackfinder.core import BASIS_MANUAL, Record, Verifier
from ackfinder.pipeline import apply_decision, load_decisions

from conftest import ROOT


def test_load_decisions_yaml_and_csv(tmp_path):
    d = load_decisions(ROOT / "examples" / "manual_decisions.example.yaml")
    assert d["10.1234/example.2025.001"]["decision"] == "include"
    assert d["10.1234/example.2025.002"]["decision"] == "exclude"
    p = tmp_path / "d.csv"
    p.write_text("DOI,Decision,Reason\nhttps://doi.org/10.1/ABC,Include,ok\n10.1/x,maybe,bad\n")
    d = load_decisions(p)
    assert list(d) == ["10.1/abc"]
    assert load_decisions(tmp_path / "missing.yaml") == {}


def test_apply_decision(cpms_cfg):
    v = Verifier(None, cpms_cfg, None, None)
    rec = Record(status="Review", reason="Funding-body sentence ...")
    apply_decision(rec, {"doi": "10.1/a", "decision": "include", "reason": "checked", "decided_by": "AB",
                         "date": "2026-01-01"}, v)
    assert rec.status == "Confirmed" and rec.basis == BASIS_MANUAL and rec.reason.startswith("Manual decision (AB")
    rec = Record(status="Confirmed", reason="")
    apply_decision(rec, {"doi": "10.1/b", "decision": "exclude", "reason": "imaging", "decided_by": "",
                         "date": ""}, v)
    assert rec.status == "Excluded" and "imaging" in rec.reason and "Confirmed" in rec.reason


def test_cli_init_check_classify(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ACKFINDER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    shutil.copy(ROOT / "examples" / "setup_answers.example.yaml", tmp_path / "answers.yaml")
    cfgp = tmp_path / "config.yaml"
    assert cli.main(["init", "--answers", "answers.yaml"]) == 0
    assert cfgp.is_file() and (tmp_path / ".env.example").is_file()
    assert cli.main(["init", "--answers", "answers.yaml"]) == 1               # no overwrite without --force
    assert cli.main(["init", "--answers", "answers.yaml", "--force"]) == 0
    assert (tmp_path / "config.yaml.bak").is_file()
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "OPENALEX_API_KEY" in out
    assert cli.main(["classify", "MS was done by the Example Proteomics Facility."]) == 0
    assert "Variant wording" in capsys.readouterr().out


def test_cli_template_init_needs_editing(tmp_path, capsys):
    p = tmp_path / "sub" / "config.yaml"
    assert cli.main(["init", "-c", str(p)]) == 0
    assert "CHANGE_ME" in capsys.readouterr().out
    assert cli.main(["check", "-c", str(p)]) == 1
    assert cli.main(["run", "-c", str(tmp_path / "nope.yaml")]) == 1
