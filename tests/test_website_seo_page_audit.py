"""website-seo-page-audit's script audits raw HTML deterministically, against a local site."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent / "plugins" / "ai-search-suite" / "skills" / "website-seo-page-audit"
WORDS = " ".join(["Invoices for agencies that bill by the hour and by the project."] * 40)


def _page(title: str, body: str, head: str = "") -> str:
    return f'<!doctype html><html lang="en"><head><title>{title}</title>{head}</head><body>{body}</body></html>'


def _site(origin: str) -> dict[str, tuple[int, str, str]]:
    canonical = lambda path: f'<link rel="canonical" href="{origin}{path}">'  # noqa: E731
    org = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Example"}</script>'
    nav = '<nav><a href="/pricing">Pricing</a><a href="/about">About</a><a href="/blog/post">Blog</a></nav>'
    return {
        "/": (200, "text/html", _page(
            "Example Invoicing: invoice software for agencies, freelancers and studios worldwide",
            f"{nav}<h1>Invoices that get paid</h1><p>{WORDS}</p><img src=a.png>",
            canonical("/") + org + '<meta property="og:title" content="Example"><meta property="og:image" content="/og.png">',
        )),
        "/pricing": (200, "text/html", _page(
            "Pricing",
            f"{nav}<h1>Pricing</h1><p>{WORDS}</p>",
            canonical("/pricing") + '<meta name="description" content="Plans for agencies of every size, from one person to fifty, billed monthly or yearly."><meta name="robots" content="noindex">',
        )),
        "/about": (200, "text/html", _page(
            "About Example Invoicing",
            f"{nav}<h1>About us</h1><p>We started in 2019.</p>",
            canonical("/about") + '<script type="application/ld+json">{not json</script>',
        )),
        "/blog/post": (200, "text/html", _page(
            "About Example Invoicing",
            f"{nav}<h1>How to invoice</h1><h1>Second H1</h1><p>{WORDS}</p><img src=b.png><img src=c.png alt=Chart>",
            canonical("/blog/post"),
        )),
        "/app": (200, "text/html", _page("Example App", '<div id="root"></div><script src="/app.js"></script>', canonical("/app"))),
        "/private/report": (200, "text/html", _page("Private report page", f"<h1>Private</h1><p>{WORDS}</p>")),
        "/robots.txt": (200, "text/plain", f"User-agent: *\nDisallow: /private\n\nSitemap: {origin}/sitemap.xml\n"),
        "/sitemap.xml": (200, "application/xml", (
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{origin}{p}</loc></url>" for p in ("/", "/pricing", "/about", "/blog/post", "/app", "/private/report", "/gone"))
            + "</urlset>"
        )),
    }


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    pages: dict[str, tuple[int, str, str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, kind, body = pages.get(self.path, (404, "text/html", _page("Not found", "<h1>404</h1>")))
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    pages.update(_site(url))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield url
    server.shutdown()


def _run(tmp_path: Path, inputs: dict) -> tuple[dict, int]:
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "audit.py"), "--input", str(path), "--out", str(tmp_path / "o.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    output = json.loads(done.stdout)
    schema = json.loads((SKILL / "assets" / "output.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(output, schema)
    return output, done.returncode


@pytest.fixture(scope="module")
def audit(origin: str, tmp_path_factory: pytest.TempPathFactory) -> dict:
    output, code = _run(tmp_path_factory.mktemp("audit"), {"site_url": origin, "focus_keywords": "invoice software, time tracking"})
    assert code == 0
    return output


def _finding(audit: dict, fid: str) -> dict:
    return next(f for f in audit["findings"] if f["id"] == fid)


def test_discovers_pages_from_the_sitemap_robots_declares(audit: dict, origin: str) -> None:
    assert audit["robots"]["sitemaps"] == [f"{origin}/sitemap.xml"]
    assert audit["sitemap"]["url_count"] == 7
    assert {p["final_url"] for p in audit["pages"]} >= {f"{origin}/", f"{origin}/pricing", f"{origin}/about", f"{origin}/blog/post"}


def test_noindex_on_a_sitemap_page_is_critical(audit: dict, origin: str) -> None:
    found = _finding(audit, "noindex")
    assert found["severity"] == "critical"
    assert found["pages"] == [f"{origin}/pricing"]


def test_an_error_page_and_a_blocked_sitemap_page_are_reported(audit: dict, origin: str) -> None:
    assert f"{origin}/gone" in _finding(audit, "broken-pages")["pages"]
    assert _finding(audit, "sitemap-blocked")["pages"] == [f"{origin}/private/report"]
    assert audit["outcome"] == "partial"


def test_titles_metas_and_headings(audit: dict, origin: str) -> None:
    assert set(_finding(audit, "title-duplicate")["pages"]) == {f"{origin}/about", f"{origin}/blog/post"}
    assert f"{origin}/" in _finding(audit, "title-long")["pages"]
    assert f"{origin}/pricing" in _finding(audit, "title-short")["pages"]
    assert _finding(audit, "description-missing")["severity"] == "high"  # the homepage has none
    assert _finding(audit, "h1-multiple")["pages"] == [f"{origin}/blog/post"]
    assert f"{origin}/about" in _finding(audit, "thin-content")["pages"]


def test_javascript_only_pages_and_broken_schema(audit: dict, origin: str) -> None:
    assert _finding(audit, "no-html-text")["pages"] == [f"{origin}/app"]
    assert _finding(audit, "no-html-text")["severity"] == "critical"
    assert _finding(audit, "schema-invalid")["pages"] == [f"{origin}/about"]


def test_focus_keywords_and_alt_text(audit: dict) -> None:
    ids = {f["id"] for f in audit["findings"]}
    assert "keyword-untargeted:time tracking" in ids
    assert "keyword-untargeted:invoice software" not in ids  # it is in the homepage title
    assert _finding(audit, "images-no-alt")["evidence"].startswith("2 images")


def test_scores_follow_the_published_rule(audit: dict) -> None:
    points = {"critical": 4, "high": 2, "medium": 1, "low": 0.5}
    for category in audit["categories"]:
        lost = sum(points[f["severity"]] for f in audit["findings"] if f["category"] == category["id"])
        assert category["score"] == max(0, round(10 - lost))
    assert audit["overall_score"] == round(sum(c["score"] for c in audit["categories"]) / 6 * 10)
    assert audit["findings"][0]["severity"] == "critical"


def test_page_text_cannot_change_the_findings(origin: str, tmp_path: Path, audit: dict) -> None:
    again, _ = _run(tmp_path, {"site_url": origin, "focus_keywords": "invoice software, time tracking"})
    strip = lambda out: [(f["id"], f["severity"], f["pages"]) for f in out["findings"]]  # noqa: E731
    assert strip(again) == strip(audit)


def test_an_unreachable_site_is_an_error(tmp_path: Path) -> None:
    output, code = _run(tmp_path, {"site_url": "http://127.0.0.1:9"})
    assert code == 1
    assert output["outcome"] == "error"
    assert output["pages"] == []
