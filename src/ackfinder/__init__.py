"""Find publications that acknowledge a core facility and write yearly reports.

Python use:
    from ackfinder import load_config, validate, RunSettings, run
    cfg = load_config("config.yaml")
    errors, warnings = validate(cfg)
    result = run(cfg, RunSettings.from_config(cfg, years=[2025]))
"""
from .config import ConfigError, build_config, load_config, load_dotenv, render_config, validate
from .core import __version__
from .pipeline import RunSettings, load_decisions, run

__all__ = ["__version__", "ConfigError", "RunSettings", "build_config", "load_config", "load_decisions",
           "load_dotenv", "render_config", "run", "validate"]
