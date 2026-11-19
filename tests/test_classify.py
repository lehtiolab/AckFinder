from ackfinder.core import Match, Record, Verifier

CANON = ("Mass spectrometry analysis was performed by the Clinical Proteomics Mass Spectrometry facility "
         "financed by the Infrastructure Board at Karolinska Institutet, SciLifeLab and the Swedish Research Council.")


def klass(cfg, text, cat="Acknowledgements"):
    ms = Verifier(None, cfg, None, None).classify([(text, "test", cat)])
    return [m.klass for m in ms]


def test_canonical(cpms_cfg):
    assert klass(cpms_cfg, CANON) == ["Canonical statement"]
    assert Verifier(None, cpms_cfg, None, None).score(CANON) == 1.0


def test_variant(cpms_cfg):
    assert klass(cpms_cfg, "We thank the Clinical Proteomics MS facility at Karolinska Institutet for help.") == \
        ["Variant wording"]


def test_funder_only_goes_to_review(cpms_cfg):
    s = "Proteomics was supported by the Infrastructure Board at Karolinska Institutet."
    assert klass(cpms_cfg, s) == ["Review"]


def test_funder_only_other_facility_excluded(cpms_cfg):
    s = "Flow cytometry was supported by the Infrastructure Board at Karolinska Institutet and mass spectrometry."
    assert klass(cpms_cfg, s) == ["Excluded"]


def test_reference_list_excluded(cpms_cfg):
    assert klass(cpms_cfg, CANON, cat="References") == ["Excluded"]


def test_unrelated_text(cpms_cfg):
    assert klass(cpms_cfg, "Samples were analysed by flow cytometry.") == []


def _rec_with(cfg, statement, body):
    v = Verifier(None, cfg, None, None)
    blocks = [(statement, "Acknowledgements", "Acknowledgements"), (body, "Methods", "Methods")]
    rec = Record(matches=v.classify(blocks))
    return v, rec, blocks


def test_scope_in_scope(cpms_cfg):
    v, rec, blocks = _rec_with(cpms_cfg, CANON, "Proteins were digested with trypsin; peptides were labelled "
                               "with TMT and searched in MaxQuant.")
    assert v.ms_type(rec, blocks) is None
    assert rec.ms_type == "Proteomics"


def test_scope_out_of_scope_generic_sentence_goes_to_review(cpms_cfg):
    v, rec, blocks = _rec_with(cpms_cfg, CANON, "Lipidomics and metabolomics profiling; metabolite extraction "
                               "with methanol; GC-MS of bile acids.")
    verdict = v.ms_type(rec, blocks)
    assert verdict and verdict[0] == "Review"
    assert rec.ms_type == "Small-molecule MS?"


def test_scope_sentence_names_out_of_scope_work_excluded(cpms_cfg):
    s = ("Lipidomics analysis was performed by the Clinical Proteomics Mass Spectrometry facility financed by "
         "the Infrastructure Board at Karolinska Institutet.")
    v, rec, blocks = _rec_with(cpms_cfg, s, "")
    verdict = v.ms_type(rec, blocks)
    assert verdict and verdict[0] == "Excluded" and rec.out_of_scope


def test_scope_disabled(cpms_cfg):
    cfg = dict(cpms_cfg, scope_check={"enabled": False})
    v = Verifier(None, cfg, None, None)
    assert v.ms_type(Record(matches=[Match(CANON, "a", "Acknowledgements", "Canonical statement", 1.0)]), []) is None
