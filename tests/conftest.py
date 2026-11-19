from pathlib import Path

import pytest

from ackfinder import config as C

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cpms_cfg():
    """The CPMS worked example (offline: no network needed to load or classify)."""
    return C.load_config(ROOT / "examples" / "config.cpms.yaml")


@pytest.fixture
def answers():
    import yaml
    return yaml.safe_load((ROOT / "examples" / "setup_answers.example.yaml").read_text())
