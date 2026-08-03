#!/usr/bin/env python3
"""Print the parts of the round-2 probe result that decide the design.

Plain ASCII on purpose: a heredoc with box-drawing characters got mangled by the
terminal, so this is shipped as a file instead.

Usage:
    cd ~/Mkwlsoso
    .venv/bin/python show_probe2.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PATH = Path("/tmp/photo_probe2_result.json")


def line(ch="-", n=68):
    print(ch * n)


def main() -> int:
    if not PATH.is_file():
        print(f"NOT FOUND: {PATH}")
        print("Run the probe first:  .venv/bin/python probe_photo_download.py 989124089268")
        return 1

    data = json.loads(PATH.read_text(encoding="utf-8"))
    probes = data.get("probes") or {}

    line("=")
    print("STEP 3 -- upload.getFile (the best download path)")
    line("=")
    s3 = probes.get("step3_getFile") or {}
    print(f"  overall ok: {s3.get('ok')}")
    for t in s3.get("tries") or []:
        print("   " + json.dumps(t, ensure_ascii=False, default=str)[:260])

    print()
    line("=")
    print("STEP 4 -- what tweb's managers really expose")
    line("=")
    s4 = probes.get("step4_managers") or {}
    for k, v in s4.items():
        if k == "ok":
            continue
        print(f"  {k}:")
        if isinstance(v, list):
            if not v:
                print("     (none matched the filter)")
            for m in v:
                print(f"     - {m}")
        else:
            print(f"     {v}")

    print()
    line("=")
    print("STEP 5 -- tweb's own downloader")
    line("=")
    s5 = probes.get("step5_tweb_download") or {}
    print(f"  overall ok: {s5.get('ok')}")
    for t in s5.get("tries") or []:
        print("   " + json.dumps(t, ensure_ascii=False, default=str)[:260])

    print()
    line("=")
    print("STEP 6 -- DOM canvas read")
    line("=")
    print("   " + json.dumps(probes.get("step6_dom"), ensure_ascii=False,
                             default=str)[:400])

    print()
    line("=")
    print("STEP 2 -- size ladder (max achievable quality)")
    line("=")
    s2 = probes.get("step2_structure") or {}
    print(f"  dc_id: {s2.get('dc_id')}   "
          f"file_reference: {s2.get('file_reference_type')} "
          f"len={s2.get('file_reference_len')}")
    for sz in s2.get("sizes") or []:
        print(f"     type={sz.get('type')!r:5} {sz.get('w')}x{sz.get('h')}  "
              f"bytes={sz.get('size')}  ctor={sz.get('_')}")

    print()
    line("=")
    print("TIMINGS / ERRORS")
    line("=")
    print(f"  timings: {data.get('timings')}")
    errs = data.get("errors") or []
    if not errs:
        print("  errors: none")
    for e in errs:
        print(f"  error: {e}")

    print()
    line("=")
    print("SUMMARY")
    line("=")
    print(f"  getFile works        : {bool(s3.get('ok'))}")
    print(f"  tweb downloader works: {bool(s5.get('ok'))}")
    print(f"  DOM canvas works     : {bool((probes.get('step6_dom') or {}).get('ok'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
