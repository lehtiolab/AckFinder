import re

import yaml

from ackfinder import config as C


def test_template_flags_placeholders():
    cfg = yaml.safe_load(C.template_text())
    assert "facility_regex" in cfg
    errors, _ = C.validate(C.load_config(_write(C.template_text())))
    assert any("facility_label" in e and "placeholder" in e for e in errors)
    assert any("facility_regex" in e for e in errors)
    assert not any("openalex_phrases" in e for e in errors)   # null in the template: not the user's fault


def test_cpms_example_is_valid(cpms_cfg):
    errors, warnings = C.validate(cpms_cfg)
    assert errors == [] and warnings == []
    assert cpms_cfg["report_slug"] == "CPMS_acknowledgements"
    assert cpms_cfg["scope_check"]["enabled"] is True
    assert cpms_cfg["output_dir"].is_absolute()


def test_answers_build_render_roundtrip(answers, tmp_path):
    values = C.build_config(answers)
    text = C.render_config(values)
    p = tmp_path / "config.yaml"
    p.write_text(text)
    cfg = C.load_config(p)
    errors, warnings = C.validate(cfg)
    assert errors == [], errors
    assert warnings == [], warnings
    assert "# 1. Your facility (REQUIRED)" in text        # template comments are kept
    assert "How to use this file" not in text
    fac = re.compile(cfg["facility_regex"], re.I)
    assert fac.search("thanks to the Example Proteomics Facility for MS")
    assert fac.search("the Example  Proteomics-Core helped")
    assert cfg["report_slug"] == "EPF_acknowledgements"
    assert cfg["output_dir"] == (tmp_path / "reports/epf_acknowledgements").resolve()
    assert cfg["scope_check"]["column"] == "MS type"


def test_disabled_scope_keeps_template_example(answers, tmp_path):
    answers = dict(answers)
    answers.pop("scope")
    text = C.render_config(C.build_config(answers))
    sc = yaml.safe_load(text)["scope_check"]
    assert sc["enabled"] is False and sc["in_scope_label"]       # example still there as a guide


def test_name_regex_tolerates_hyphens_and_linebreaks():
    rx = re.compile(C.name_regex("Clinical Proteomics Mass Spectrometry"), re.I)
    assert rx.search("clinical proteomics mass-spectrometry")
    assert rx.search("Clinical Proteomics Mass\nSpectrometry")
    assert not rx.search("clinical proteomics")


def test_bad_regex_and_years_are_errors(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("facility_label: X\ncanonical_statement: Done at the X facility.\nsearch_phrases: [X facility]\n"
                 "facility_regex: '(unclosed'\nyears: [25]\n")
    errors, _ = C.validate(C.load_config(p))
    assert any("facility_regex" in e and "regular expression" in e for e in errors)
    assert any("years" in e for e in errors)


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FAK_TEST_A=from_file\nFAK_TEST_B='quoted'  \n# comment\nexport FAK_TEST_C=x # note\n")
    monkeypatch.setenv("FAK_TEST_A", "from_shell")
    monkeypatch.delenv("FAK_TEST_B", raising=False)
    monkeypatch.delenv("FAK_TEST_C", raising=False)
    C.load_dotenv(tmp_path)
    import os
    assert os.environ["FAK_TEST_A"] == "from_shell"
    assert os.environ["FAK_TEST_B"] == "quoted"
    assert os.environ["FAK_TEST_C"] == "x"


def _write(text):
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    (d / "config.yaml").write_text(text)
    return d / "config.yaml"
