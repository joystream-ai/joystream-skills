"""website-seo-performance's script turns Lighthouse and Chrome UX Report data into findings
deterministically. A fake Lighthouse and a fake PageSpeed API stand in for the real ones."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent / "plugins" / "ai-search-suite" / "skills" / "website-seo-performance"


def _lhr(perf: float, lcp: float, cls: float, tbt: float, server: float = 200, render_ms: float = 0, unused_ms: float = 0, unused_bytes: float = 0, images: tuple = ()) -> dict:
    audits = {
        "largest-contentful-paint": {"title": "Largest Contentful Paint", "numericValue": lcp},
        "cumulative-layout-shift": {"title": "Cumulative Layout Shift", "numericValue": cls},
        "total-blocking-time": {"title": "Total Blocking Time", "numericValue": tbt},
        "first-contentful-paint": {"title": "First Contentful Paint", "numericValue": lcp * 0.5},
        "speed-index": {"title": "Speed Index", "numericValue": lcp * 0.8},
        "server-response-time": {"title": "Initial server response time", "numericValue": server},
        "render-blocking-resources": {"title": "Eliminate render-blocking resources", "score": 0.5 if render_ms else 1, "details": {"type": "opportunity", "overallSavingsMs": render_ms}},
        "unused-javascript": {"title": "Reduce unused JavaScript", "score": 0.5 if unused_ms else 1, "details": {"type": "opportunity", "overallSavingsMs": unused_ms, "overallSavingsBytes": unused_bytes}},
    }
    for audit_id, saved_ms, saved_bytes in images:
        audits[audit_id] = {"title": audit_id.replace("-", " ").capitalize(), "score": 0.4, "details": {"type": "opportunity", "overallSavingsMs": saved_ms, "overallSavingsBytes": saved_bytes}}
    categories = {"performance": perf, "seo": 0.92, "accessibility": 0.88, "best-practices": 0.96}
    return {"categories": {k: {"score": v} for k, v in categories.items()}, "audits": audits}


FIXTURES = {
    "/": _lhr(0.42, 4600, 0.05, 350, server=720, render_ms=780, unused_ms=620, unused_bytes=180_000,
              images=(("uses-optimized-images", 300, 150_000), ("modern-image-formats", 250, 120_000))),
    "/pricing": _lhr(0.71, 3100, 0.18, 120, render_ms=300, unused_ms=100, unused_bytes=20_000),
    "/blog/post": _lhr(0.93, 1900, 0.31, 80, server=150),
    "/fast": _lhr(0.99, 1200, 0.01, 20, server=90),
    "/blocked": {"runtimeError": {"code": "ERRORED_DOCUMENT_REQUEST", "message": "Lighthouse was unable to reliably load the page (Status code: 403)"}, "categories": {"performance": {"score": None}}, "audits": {}},
}

FAKE_LIGHTHOUSE = f"""#!{sys.executable}
import json, os, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
with open(os.environ["FAKE_LH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
url = next(a for a in args if not a.startswith("--"))
out = next(a.split("=", 1)[1] for a in args if a.startswith("--output-path="))
fixtures = json.load(open(os.environ["FAKE_LH_FIXTURES"]))
path = urlsplit(url).path or "/"
if path not in fixtures:
    print("Runtime error encountered: net::ERR_CONNECTION_REFUSED", file=sys.stderr)
    sys.exit(1)
json.dump(fixtures[path], open(out, "w"))
"""

PSI = {
    "/psi-slow": {"originLoadingExperience": {"id": "https://www.example.com", "overall_category": "SLOW", "metrics": {
        "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4300, "category": "SLOW"},
        "INTERACTION_TO_NEXT_PAINT": {"percentile": 240, "category": "AVERAGE"},
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 8, "category": "FAST"}}}},
    "/psi-average-url": {"originLoadingExperience": {"id": "https://www.example.com"}, "loadingExperience": {"overall_category": "AVERAGE", "metrics": {
        "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2900, "category": "AVERAGE"},
        "INTERACTION_TO_NEXT_PAINT": {"percentile": 150, "category": "FAST"},
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 3, "category": "FAST"}}}},
    "/psi-empty": {"lighthouseResult": {}},
}


@pytest.fixture(scope="module")
def psi() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            status, body = (200, PSI[path]) if path in PSI else (429, {"error": {"code": 429, "message": "Quota exceeded"}})
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def lighthouse(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("lighthouse")
    binary = root / "lighthouse"
    binary.write_text(FAKE_LIGHTHOUSE, encoding="utf-8")
    binary.chmod(0o755)
    fixtures = root / "fixtures.json"
    fixtures.write_text(json.dumps(FIXTURES), encoding="utf-8")
    return {"LIGHTHOUSE_BIN": str(binary), "FAKE_LH_FIXTURES": str(fixtures), "FAKE_LH_LOG": str(root / "calls.log")}


def _run(tmp_path: Path, inputs: dict, env: dict[str, str]) -> tuple[dict, int]:
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "measure.py"), "--input", str(path), "--out", str(tmp_path / "o.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "PSI_API_KEY": "", **env},
    )
    output = json.loads(done.stdout)
    schema = json.loads((SKILL / "assets" / "output.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(output, schema)
    return output, done.returncode


def _finding(output: dict, fid: str) -> dict:
    return next(f for f in output["findings"] if f["id"] == fid)


SITE = "https://www.example.com"


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory, lighthouse: dict, psi: str) -> dict:
    output, code = _run(tmp_path_factory.mktemp("m"), {"site_url": SITE, "pages": [f"{SITE}/pricing", f"{SITE}/blog/post", "https://other.example.net/x"]}, {**lighthouse, "PSI_ENDPOINT": f"{psi}/psi-slow"})
    assert code == 0
    return output


def test_measures_the_homepage_first_and_only_same_site_pages(measured: dict) -> None:
    assert [p["url"] for p in measured["pages"]] == [f"{SITE}/", f"{SITE}/pricing", f"{SITE}/blog/post"]
    assert measured["outcome"] == "ok"
    home = measured["pages"][0]
    assert (home["performance_score"], home["lcp_ms"], home["tbt_ms"], home["cls"]) == (42, 4600, 350, 0.05)
    assert home["ratings"] == {"lcp": "poor", "cls": "good", "tbt": "needs_improvement"}
    assert [o["id"] for o in home["opportunities"]][:2] == ["render-blocking-resources", "unused-javascript"]
    assert measured["score"] == 69  # (42 + 71 + 93) / 3


def test_vitals_follow_googles_thresholds(measured: dict) -> None:
    lcp = _finding(measured, "lcp-slow")
    assert (lcp["severity"], lcp["pages"]) == ("high", [f"{SITE}/", f"{SITE}/pricing"])
    cls = _finding(measured, "cls-unstable")
    assert (cls["severity"], cls["pages"]) == ("high", [f"{SITE}/blog/post", f"{SITE}/pricing"])
    tbt = _finding(measured, "tbt-blocking")
    assert (tbt["severity"], tbt["pages"]) == ("medium", [f"{SITE}/"])
    assert _finding(measured, "perf-score-low")["severity"] == "high"


def test_savings_findings_and_their_effort(measured: dict) -> None:
    ids = {f["id"]: f for f in measured["findings"]}
    assert ids["render-blocking"]["pages"] == [f"{SITE}/"]  # /pricing's 300 ms is under the bar
    assert ids["images-heavy"]["effort"] == "quick fix"  # 550 ms and 264 KB combined
    assert ids["unused-js"]["pages"] == [f"{SITE}/"]
    assert ids["slow-server"]["evidence"] == "/: 720 ms"
    assert all(f["category"] == "performance" for f in measured["findings"])


def test_slow_real_users_are_critical_and_first(measured: dict) -> None:
    assert measured["field"] == {"source": "crux_origin", "lcp_ms": 4300, "inp_ms": 240, "cls": 0.08, "overall": "SLOW",
                                 "ratings": {"lcp": "poor", "inp": "needs_improvement", "cls": "good"}}
    first = measured["findings"][0]
    assert (first["id"], first["severity"]) == ("field-cwv-failing", "critical")
    assert measured["sentence"] == "Mobile performance 69/100 across 3 pages: LCP 4.6 s on /. Real users: slow."


def test_url_level_field_data_when_the_origin_has_none(tmp_path: Path, lighthouse: dict, psi: str) -> None:
    output, _ = _run(tmp_path, {"site_url": SITE, "max_pages": 1}, {**lighthouse, "PSI_ENDPOINT": f"{psi}/psi-average-url"})
    assert output["field"]["source"] == "crux_url"
    assert _finding(output, "field-cwv-failing")["severity"] == "high"


@pytest.mark.parametrize("endpoint", ["/psi-refuses", "/psi-empty"])
def test_no_field_data_is_named_not_guessed(tmp_path: Path, lighthouse: dict, psi: str, endpoint: str) -> None:
    output, code = _run(tmp_path, {"site_url": SITE, "max_pages": 1}, {**lighthouse, "PSI_ENDPOINT": f"{psi}{endpoint}"})
    assert code == 0
    assert output["field"] is None
    assert "field-cwv-failing" not in {f["id"] for f in output["findings"]}
    assert any(u.startswith("No real-user data") and "lab data only" in u for u in output["unknowns"])


def test_a_page_lighthouse_cannot_load_makes_the_run_partial(tmp_path: Path, lighthouse: dict) -> None:
    output, code = _run(tmp_path, {"site_url": SITE, "pages": [f"{SITE}/blocked", f"{SITE}/gone"], "field_data": False}, lighthouse)
    assert code == 0
    assert output["outcome"] == "partial"
    blocked, gone = output["pages"][1], output["pages"][2]
    assert blocked["status"] == "error" and blocked["error"].startswith("ERRORED_DOCUMENT_REQUEST")
    assert gone["status"] == "error" and "ERR_CONNECTION_REFUSED" in gone["error"]
    assert output["score"] == 42
    assert output["sentence"].endswith("2 page(s) could not be measured.")


def test_desktop_uses_the_desktop_preset(tmp_path: Path, lighthouse: dict) -> None:
    log = Path(lighthouse["FAKE_LH_LOG"])
    log.write_text("", encoding="utf-8")
    output, _ = _run(tmp_path, {"site_url": f"{SITE}/fast", "strategy": "desktop", "field_data": False}, lighthouse)
    assert output["strategy"] == "desktop"
    assert "--preset=desktop" in json.loads(log.read_text(encoding="utf-8").splitlines()[0])


def test_a_fast_site_has_no_findings(tmp_path: Path, lighthouse: dict) -> None:
    fixtures = json.loads(Path(lighthouse["FAKE_LH_FIXTURES"]).read_text(encoding="utf-8"))
    fixtures["/"] = fixtures["/fast"]
    path = tmp_path / "fast.json"
    path.write_text(json.dumps(fixtures), encoding="utf-8")
    output, _ = _run(tmp_path, {"site_url": SITE, "max_pages": 1, "field_data": False}, {**lighthouse, "FAKE_LH_FIXTURES": str(path)})
    assert output["findings"] == []
    assert output["sentence"] == "Mobile performance 99/100 across 1 page: every lab Core Web Vital is in the good range."


def test_missing_lighthouse_is_an_error(tmp_path: Path) -> None:
    output, code = _run(tmp_path, {"site_url": SITE}, {"LIGHTHOUSE_BIN": str(tmp_path / "nope")})
    assert code == 1
    assert output["outcome"] == "error"
    assert output["score"] is None and output["pages"] == []


def test_every_page_failing_is_an_error(tmp_path: Path, lighthouse: dict) -> None:
    output, code = _run(tmp_path, {"site_url": "https://down.example.org", "field_data": False}, {**lighthouse, "FAKE_LH_FIXTURES": str(tmp_path / "none.json")})
    assert code == 1
    assert output["outcome"] == "error"
    assert output["pages"][0]["status"] == "error"


def test_every_example_is_what_the_script_prints(tmp_path: Path, lighthouse: dict, psi: str) -> None:
    for case in sorted((SKILL / "assets" / "examples").glob("*.input.json")):
        expected = json.loads(case.with_name(case.name.replace(".input.", ".output.")).read_text(encoding="utf-8"))
        output, _ = _run(tmp_path, json.loads(case.read_text(encoding="utf-8")), {**lighthouse, "PSI_ENDPOINT": f"{psi}/psi-slow"})
        assert output == expected, case.name
