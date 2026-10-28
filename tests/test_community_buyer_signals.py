"""community-buyer-signals' collector shapes HN/Reddit search results; no live network.

Run: uv run --with pytest --with pyyaml --with jsonschema pytest tests
"""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent / "skills" / "community-buyer-signals"
SCRIPT = SKILL / "scripts" / "search.py"
OUTPUT_SCHEMA = json.loads((SKILL / "assets" / "output.schema.json").read_text())
INPUT_SCHEMA = json.loads((SKILL / "assets" / "input.schema.json").read_text())
NOW = "2026-10-28T00:00:00Z"
NOW_EPOCH = 1793145600  # 2026-10-28T00:00:00Z


def load():
    spec = importlib.util.spec_from_file_location("community_search", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.time.sleep = lambda _s: None
    return module


def hn_hit(object_id: int, kind: str = "story", **fields) -> dict:
    hit = {"objectID": str(object_id), "_tags": [kind, f"author_u{object_id}"], "author": f"u{object_id}",
           "created_at_i": NOW_EPOCH - 86400, "created_at": "2026-10-27T00:00:00Z"}
    if kind == "comment":
        hit.update(story_title="Ask HN: GPU monitoring?", comment_text="<p>We need &amp; want <i>alerts</i></p>", story_id=1)
    else:
        hit.update(title=f"Story {object_id}", story_text=None, url="https://blog.example.com/post", points=10, num_comments=3)
    hit.update(fields)
    return hit


class FakeHttp:
    """Answers fetch_json by host; records every request."""

    def __init__(self, hn: dict | None = None, reddit: tuple[int, object] = (403, None), hn_status: int = 200) -> None:
        self.hn = hn or {}
        self.reddit = reddit
        self.hn_status = hn_status
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, headers: dict | None = None):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((url, params))
        if parsed.netloc == "hn.algolia.com":
            if self.hn_status != 200:
                return self.hn_status, None
            hits = self.hn.get((params["query"], "optionalWords" in params), self.hn.get(params["query"], []))
            return 200, {"hits": hits, "nbHits": len(hits)}
        if parsed.netloc == "www.reddit.com":
            return self.reddit
        raise AssertionError(f"unexpected host {parsed.netloc}")


def run(tmp_path: Path, inputs: dict, http: FakeHttp, monkeypatch, via: str = "env") -> tuple[int, dict]:
    module = load()
    monkeypatch.setattr(module, "fetch_json", http)
    for key in ("INPUT_JSON", "INPUT_JSON_PATH"):
        monkeypatch.delenv(key, raising=False)
    if via == "env":
        monkeypatch.setenv("INPUT_JSON", json.dumps(inputs))
    else:
        path = tmp_path / "input.json"
        path.write_text(json.dumps(inputs))
        monkeypatch.setenv("INPUT_JSON_PATH", str(path))
    code, summary = module.collect(module.load_inputs(None), tmp_path)
    return code, summary


def posts(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "posts.json").read_text())["posts"]


def test_queries_hn_by_relevance_with_tags_window_and_size(tmp_path, monkeypatch) -> None:
    http = FakeHttp({"gpu monitoring": [hn_hit(11)]})
    code, summary = run(tmp_path, {"queries": ["gpu monitoring"], "tags": ["ask_hn", "comment"],
                                   "since_days": 30, "max_posts": 12, "now": NOW}, http, monkeypatch)
    assert code == 0
    url, params = http.calls[0]
    assert url.startswith("https://hn.algolia.com/api/v1/search?")
    assert params["tags"] == "(ask_hn,comment)"
    assert params["numericFilters"] == f"created_at_i>{NOW_EPOCH - 30 * 86400}"
    assert params["hitsPerPage"] == "12"
    assert summary["kept"] == 1 and summary["since"] == "2026-09-28T00:00:00Z"


def test_shapes_hits_into_posts(tmp_path, monkeypatch) -> None:
    long_text = "<p>" + "word " * 3000 + "</p>"
    http = FakeHttp({"q": [hn_hit(21), hn_hit(22, "comment"), hn_hit(23, "story", story_text=long_text),
                           hn_hit(24, "story", _tags=["story", "ask_hn"], title="Ask HN: which tool?")]})
    run(tmp_path, {"queries": ["q"], "now": NOW}, http, monkeypatch)
    story, comment, long_post, ask = posts(tmp_path)
    assert story["url"] == "https://news.ycombinator.com/item?id=21"
    assert story["link_url"] == "https://blog.example.com/post"
    assert story["text"] == "Story 21" and story["kind"] == "story"
    assert comment["kind"] == "comment" and comment["title"] == "Ask HN: GPU monitoring?"
    assert comment["text"] == "We need & want alerts"
    assert comment["thread_url"] == "https://news.ycombinator.com/item?id=1"
    assert len(long_post["text"]) == 6000 and "<p>" not in long_post["text"]
    assert ask["kind"] == "ask_hn"
    assert [p["source_id"] for p in posts(tmp_path)] == ["S1", "S2", "S3", "S4"]
    assert story["published_at"] == "2026-10-27T00:00:00Z"


def test_dedupes_and_interleaves_across_queries_up_to_max_posts(tmp_path, monkeypatch) -> None:
    http = FakeHttp({"a": [hn_hit(1), hn_hit(2), hn_hit(3)], "b": [hn_hit(2), hn_hit(4), hn_hit(5)]})
    _, summary = run(tmp_path, {"queries": ["a", "b"], "max_posts": 4, "now": NOW}, http, monkeypatch)
    assert [p["post_id"] for p in posts(tmp_path)] == ["hn:1", "hn:2", "hn:4", "hn:3"]
    assert summary["kept"] == 4


def test_retries_with_optional_words_when_strict_query_finds_nothing(tmp_path, monkeypatch) -> None:
    http = FakeHttp({("gpu cluster outage", False): [], ("gpu cluster outage", True): [hn_hit(7)]})
    _, summary = run(tmp_path, {"queries": ["gpu cluster outage"], "now": NOW}, http, monkeypatch)
    assert [c[1].get("optionalWords") for c in http.calls] == [None, "gpu cluster outage"]
    assert summary["kept"] == 1
    assert any("closest matches" in u for u in summary["unknowns"])


def test_reddit_refusal_is_a_skipped_source(tmp_path, monkeypatch) -> None:
    http = FakeHttp({"q": [hn_hit(1)]}, reddit=(403, None))
    code, summary = run(tmp_path, {"queries": ["q"], "include_reddit": True, "now": NOW}, http, monkeypatch)
    assert code == 0
    assert summary["by_source"] == {"hn": 1, "reddit": 0}
    assert any("Reddit" in u and "HTTP 403" in u for u in summary["unknowns"])
    reddit_call = [c for c in http.calls if "reddit" in c[0]][0]
    assert reddit_call[1]["q"] == "q"


def test_reddit_results_are_shaped_and_windowed(tmp_path, monkeypatch) -> None:
    listing = {"data": {"children": [
        {"data": {"id": "abc", "permalink": "/r/mlops/comments/abc/x/", "title": "Need GPU alerts",
                  "selftext": "Looking for a tool", "created_utc": NOW_EPOCH - 3600, "subreddit": "mlops"}},
        {"data": {"id": "old", "permalink": "/r/mlops/comments/old/y/", "title": "Old",
                  "selftext": "", "created_utc": NOW_EPOCH - 400 * 86400}},
    ]}}
    http = FakeHttp({"q": []}, reddit=(200, listing))
    run(tmp_path, {"queries": ["q"], "include_reddit": True, "now": NOW}, http, monkeypatch)
    (post,) = posts(tmp_path)
    assert post["url"] == "https://www.reddit.com/r/mlops/comments/abc/x/"
    assert post["source"] == "reddit" and post["text"] == "Looking for a tool"


def test_every_hn_failure_exits_1(tmp_path, monkeypatch) -> None:
    code, summary = run(tmp_path, {"queries": ["q"], "now": NOW}, FakeHttp(hn_status=503), monkeypatch)
    assert code == 1 and summary["error"] == "hn_unavailable"


@pytest.mark.parametrize("bad", [{"queries": []}, {"queries": ["a"] * 1 + ["b", "c", "d", "e", "f"]},
                                 {"queries": ["a"], "tags": ["poll"]}, {"queries": ["a"], "max_posts": 61}])
def test_invalid_inputs_are_refused(tmp_path, monkeypatch, bad) -> None:
    module = load()
    monkeypatch.setenv("INPUT_JSON", json.dumps(bad))
    with pytest.raises(module.InputError):
        module.collect(module.load_inputs(None), tmp_path)


def test_reads_inputs_from_input_json_path(tmp_path, monkeypatch) -> None:
    http = FakeHttp({"q": [hn_hit(1)]})
    code, summary = run(tmp_path, {"queries": ["q"], "now": NOW}, http, monkeypatch, via="path")
    assert code == 0 and summary["kept"] == 1


def test_pages_are_small_enough_to_read_whole(tmp_path, monkeypatch) -> None:
    big = [hn_hit(i, "story", story_text="x " * 3500) for i in range(1, 41)]
    http = FakeHttp({"q": big})
    _, summary = run(tmp_path, {"queries": ["q"], "max_posts": 40, "now": NOW}, http, monkeypatch)
    assert len(summary["pages"]) > 1
    seen = []
    for page in summary["pages"]:
        assert Path(page).stat().st_size < 64 * 1024
        seen += [p["post_id"] for p in json.loads(Path(page).read_text())["posts"]]
    assert seen == [p["post_id"] for p in posts(tmp_path)]


def test_never_sends_credentials(tmp_path, monkeypatch) -> None:
    """The real fetch sends only a User-Agent and Accept header."""
    module = load()
    sent = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"hits": []}'

    def fake_urlopen(request, timeout):
        sent.update(dict(request.header_items()))
        return Response()

    monkeypatch.setenv("GITHUB_TOKEN", "secret-value")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    status, body = module.fetch_json("https://hn.algolia.com/api/v1/search?query=x")
    assert (status, body) == (200, {"hits": []})
    assert {k.lower() for k in sent} == {"user-agent", "accept"}
    assert "secret-value" not in json.dumps(sent)


def test_examples_match_the_schemas() -> None:
    for path in sorted((SKILL / "assets" / "examples").glob("*.input.json")):
        jsonschema.validate(json.loads(path.read_text()), INPUT_SCHEMA)
    for path in sorted((SKILL / "assets" / "examples").glob("*.output.json")):
        result = json.loads(path.read_text())
        jsonschema.validate(result, OUTPUT_SCHEMA)
        ids = {s["id"] for s in result["report"]["sources"]}
        assert {p["source_id"] for p in result["posts"]} == ids
        assert all(b in result["posts"] for b in result["buyers"])
        assert all(b["buying"]["p"] >= 0.6 and b["vendor"]["p"] < 0.5 for b in result["buyers"])


def test_must_include_keeps_the_company_required_in_the_retry(tmp_path, monkeypatch) -> None:
    http = FakeHttp({("Lambda GPU outage complaints", False): [], ("Lambda GPU outage complaints", True): [hn_hit(8)]})
    inputs = {"queries": ["Lambda GPU outage complaints"], "must_include": "Lambda", "now": NOW}
    _, summary = run(tmp_path, inputs, http, monkeypatch)
    assert [c[1].get("optionalWords") for c in http.calls] == [None, "GPU outage complaints"]
    assert summary["kept"] == 1
    assert any("still requiring 'lambda'" in u for u in summary["unknowns"])


def test_must_include_missing_from_the_query_is_added_to_it(tmp_path, monkeypatch) -> None:
    http = FakeHttp()
    run(tmp_path, {"queries": ["GPU outage"], "must_include": "Together AI", "now": NOW}, http, monkeypatch)
    assert http.calls[0][1]["query"] == "together ai GPU outage"
    assert http.calls[1][1]["optionalWords"] == "GPU outage"


def test_no_retry_when_only_required_words_remain(tmp_path, monkeypatch) -> None:
    http = FakeHttp()
    run(tmp_path, {"queries": ["Lambda"], "must_include": "Lambda", "now": NOW}, http, monkeypatch)
    assert len(http.calls) == 1


# --------------------------------------------------------------------------- finalize

FINALIZE = SKILL / "scripts" / "finalize.py"


def load_finalize():
    spec = importlib.util.spec_from_file_location("community_finalize", FINALIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finalize(tmp_path: Path, inputs: dict, scores: dict, monkeypatch) -> dict:
    monkeypatch.setenv("INPUT_JSON", json.dumps(inputs))
    monkeypatch.delenv("INPUT_JSON_PATH", raising=False)
    (tmp_path / "scores.json").write_text(json.dumps(scores))
    module = load_finalize()
    assert module.main(["--scores", str(tmp_path / "scores.json"), "--dir", str(tmp_path), "--out", str(tmp_path / "out.json")]) == 0
    result = json.loads((tmp_path / "out.json").read_text())
    jsonschema.validate(result, OUTPUT_SCHEMA)
    return result


def collected(tmp_path: Path, monkeypatch, hits: list[dict], **inputs) -> dict:
    inputs = {"queries": ["gpu monitoring"], "now": NOW, **inputs}
    run(tmp_path, inputs, FakeHttp({"gpu monitoring": hits}), monkeypatch)
    return inputs


def test_finalize_builds_a_valid_output_from_scores(tmp_path, monkeypatch) -> None:
    inputs = collected(tmp_path, monkeypatch, [hn_hit(1, "comment"), hn_hit(2), hn_hit(3, created_at_i=None, created_at=None)],
                       product="GPU monitoring")
    scores = {
        "S1": {"buying": {"p": 0.9, "quote": "We need & want alerts"}, "pain": {"p": 0.7, "quote": "want alerts"}, "vendor": {"p": 0, "quote": ""}},
        "S2": {"buying": {"p": 0.1, "quote": "ignored below 0.2"}, "pain": {"p": 0.65, "quote": "Story 2"}, "vendor": {"p": 0.2, "quote": ""}},
        "S3": {"buying": {"p": 1.7, "quote": ""}, "pain": {"p": 0, "quote": ""}, "vendor": {"p": 0.9, "quote": "Story 3"}},
    }
    result = finalize(tmp_path, inputs, scores, monkeypatch)
    assert [b["source_id"] for b in result["buyers"]] == ["S1"]
    assert result["posts"][1]["buying"] == {"p": 0.1, "quote": ""}
    assert result["posts"][2]["buying"]["p"] == 1.0  # clamped; a vendor is never a buyer
    assert result["report"]["summary"] == "Scored 3 Hacker News posts from the last 365 days: 1 look like buyers, 2 describe the pain, 1 are vendors."
    assert [s["heading"] for s in result["report"]["sections"]] == ["Buyers", "Pain"]
    assert result["report"]["severity"] == "attention"
    assert "published_at" not in result["report"]["sources"][2]
    assert result["report"]["meta"]["generated_at"] == NOW


def test_finalize_drops_quotes_not_in_the_post_and_names_unscored_posts(tmp_path, monkeypatch) -> None:
    inputs = collected(tmp_path, monkeypatch, [hn_hit(1), hn_hit(2)])
    result = finalize(tmp_path, inputs, {"S1": {"buying": {"p": 0.8, "quote": "words the post never said"}}}, monkeypatch)
    assert result["posts"][0]["buying"] == {"p": 0.8, "quote": ""}
    assert any("S2" in u for u in result["report"]["unknowns"])
    assert any("1 quote(s)" in u for u in result["report"]["unknowns"])


def test_finalize_cuts_a_quote_before_a_contact_detail(tmp_path, monkeypatch) -> None:
    inputs = collected(tmp_path, monkeypatch, [hn_hit(1, story_text="Looking for a tool, mail me at a@b.co today")])
    result = finalize(tmp_path, inputs, {"S1": {"buying": {"p": 0.7, "quote": "Looking for a tool, mail me at a@b.co today"}}}, monkeypatch)
    assert result["posts"][0]["buying"]["quote"] == "Looking for a tool, mail me at"


def test_finalize_reports_no_posts_and_search_errors(tmp_path, monkeypatch) -> None:
    inputs = collected(tmp_path, monkeypatch, [])
    result = finalize(tmp_path, inputs, {}, monkeypatch)
    assert result["scored_count"] == 0
    assert result["report"]["summary"] == "No Hacker News posts matched gpu monitoring in the last 365 days."

    failed = tmp_path / "failed"
    module = load()
    monkeypatch.setattr(module, "fetch_json", FakeHttp(hn_status=500))
    monkeypatch.setenv("INPUT_JSON", json.dumps({"queries": ["x y"], "now": NOW}))
    assert module.main(["--out-dir", str(failed)]) == 1
    result = finalize(failed, {"queries": ["x y"], "now": NOW}, {}, monkeypatch)
    assert result["report"]["summary"] == "Hacker News search was not reachable; nothing was scored."
    assert result["report"]["severity"] == "attention"
