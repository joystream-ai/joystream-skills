#!/usr/bin/env python3
"""Save text an earlier step produced as a downloadable file in the run's output folder.

Run inside JoyStream's sandbox by the content-file-save skill. The model writes the
content to a scratch file with sandbox_write_file, then runs:

    python3 scripts/save_file.py --source /tmp/content.md --name "Account report" --format md

Standard library only. Prints exactly one JSON object on stdout (the skill's output
fields) and exits 0, or prints {"saved": false, "error": ..., "detail": ...} and exits 1.
Progress and warnings go to stderr.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

DEFAULT_OUT_DIR = "/workspace/out"
MAX_BYTES = 20 * 1024 * 1024
MAX_STEM = 100

FORMATS: dict[str, tuple[str, str]] = {
    "md": (".md", "text/markdown"),
    "txt": (".txt", "text/plain"),
    "csv": (".csv", "text/csv"),
    "json": (".json", "application/json"),
    "html": (".html", "text/html"),
}
ALIASES = {"markdown": "md", "text": "txt", "htm": "html"}

UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]+')


class SaveError(Exception):
    def __init__(self, error: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


def normalize_format(value: str) -> str:
    fmt = (value or "md").strip().lower().lstrip(".")
    fmt = ALIASES.get(fmt, fmt)
    if fmt not in FORMATS:
        raise SaveError("unsupported_format", f"format {value!r} is not one of {', '.join(FORMATS)}")
    return fmt


def safe_stem(name: str, fallback: str = "document") -> str:
    """A file name stem with no path parts, no shell-hostile characters, at most 100 chars."""
    stem = unicodedata.normalize("NFKC", name or "").strip()
    for ext, _ in FORMATS.values():
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    stem = UNSAFE.sub("-", stem)
    stem = re.sub(r"\s+", "-", stem)
    stem = re.sub(r"-{2,}", "-", stem).strip("-. ")
    stem = stem[:MAX_STEM].rstrip("-. ")
    return stem or fallback


def check_content(text: str, fmt: str) -> str:
    """Validate the content for its format; return it with one trailing newline."""
    if not text.strip():
        raise SaveError("empty_content", "the content is empty; nothing to save")
    if fmt == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise SaveError("invalid_json", f"content is not valid JSON: {exc}") from exc
    if fmt == "csv":
        rows = list(csv.reader(io.StringIO(text)))
        widths = {len(r) for r in rows if r}
        if len(widths) > 1:
            raise SaveError(
                "invalid_csv", f"rows have different column counts: {sorted(widths)}"
            )
    return text.rstrip("\n") + "\n"


def unique_path(out_dir: Path, stem: str, ext: str, overwrite: bool) -> Path:
    path = out_dir / f"{stem}{ext}"
    if overwrite or not path.exists():
        return path
    n = 2
    while (out_dir / f"{stem}-{n}{ext}").exists():
        n += 1
    return out_dir / f"{stem}-{n}{ext}"


def save(source: Path, name: str, fmt: str, out_dir: Path, overwrite: bool) -> dict:
    fmt = normalize_format(fmt)
    ext, media_type = FORMATS[fmt]
    if not source.is_file():
        raise SaveError("source_missing", f"no file at {source}")
    raw = source.read_bytes()
    if len(raw) > MAX_BYTES:
        raise SaveError("too_large", f"content is {len(raw)} bytes; the limit is {MAX_BYTES}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SaveError("not_utf8", f"content is not UTF-8 text: {exc}") from exc
    text = check_content(text.replace("\r\n", "\n"), fmt)
    data = text.encode("utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(out_dir, safe_stem(name), ext, overwrite)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".save-", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {
        "saved": True,
        "path": str(path),
        "file_name": path.name,
        "format": fmt,
        "media_type": media_type,
        "bytes": len(data),
        "lines": text.count("\n"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="scratch file holding the content")
    parser.add_argument("--name", default="document", help="file name, without folder")
    parser.add_argument("--format", default="md", help=", ".join(FORMATS))
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--overwrite", action="store_true", help="replace a same-named file")
    args = parser.parse_args(argv)
    try:
        result = save(Path(args.source), args.name, args.format, Path(args.out_dir), args.overwrite)
    except SaveError as exc:
        print(json.dumps({"saved": False, "error": exc.error, "detail": exc.detail}))
        return 1
    print(f"save_file: wrote {result['path']} ({result['bytes']} bytes)", file=sys.stderr)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
