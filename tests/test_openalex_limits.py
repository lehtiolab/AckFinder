"""OpenAlex refusals (HTTP 429): no waiting for hours, and no further OpenAlex calls once refused."""
import requests

from ackfinder import core
from ackfinder.core import Http, Searcher


def _resp(code: int, retry_after: int | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code, r._content, r.url = code, b'{"results": []}', "https://api.openalex.org/works"
    if retry_after is not None:
        r.headers["Retry-After"] = str(retry_after)
    return r


class FakeSession:
    """Answers from a list; when the list is empty, every answer is 'daily budget used up'."""

    def __init__(self, answers=()):
        self.answers, self.calls, self.headers = list(answers), 0, {}

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.calls += 1
        return self.answers.pop(0) if self.answers else _resp(429, 30843)


def _searcher(monkeypatch, answers=(), cfg=None):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    http = Http(None)
    http.s = FakeSession(answers)
    return Searcher(http, cfg or {}, None, None), http.s


def test_long_retry_after_is_not_retried(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    http = Http(None)
    http.s = FakeSession()
    r = http.get("https://api.openalex.org/works")
    assert r.status_code == 429 and r.retry_after == 30843
    assert http.s.calls == 1


def test_short_retry_after_is_retried(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    http = Http(None)
    http.s = FakeSession([_resp(429, 5), _resp(200)])
    assert http.get("https://api.openalex.org/works").status_code == 200
    assert http.s.calls == 2


def test_lookups_stop_after_budget_refusal(monkeypatch):
    s, sess = _searcher(monkeypatch)
    assert s.openalex_works(["10.1/a", "10.1/b"]) == {}
    assert s.openalex_sources(["https://openalex.org/S1"]) == {}
    assert s.openalex_source_by_issn("1234-5678") is None
    assert sess.calls == 1
    assert "daily budget" in s.oa_down
    assert [(x["field"], x["note"].split(":")[0]) for x in s.status] == [("metadata", "skipped")]


def test_lookups_stop_after_short_429(monkeypatch):
    s, sess = _searcher(monkeypatch, [_resp(429, 1)] * 4)
    s.openalex_works(["10.1/a"])
    assert sess.calls == 4                      # Http's normal retries
    for issn in ("1111-1111", "2222-2222", "3333-3333"):
        assert s.openalex_source_by_issn(issn) is None
    assert sess.calls == 4                      # nothing more after the refusal


def test_search_budget_refusal_skips_all_openalex(monkeypatch):
    s, sess = _searcher(monkeypatch, cfg={"openalex_phrases": ["Phrase A", "Phrase B"]})
    assert s.openalex_search(2025, 2026, affiliations=False) == []
    s.openalex_works(["10.1/a"])
    assert sess.calls == 1
    notes = [x["note"] for x in s.status]
    assert "daily budget used up" in notes[0]
    assert notes[1].startswith("skipped") and notes[2].startswith("skipped")
