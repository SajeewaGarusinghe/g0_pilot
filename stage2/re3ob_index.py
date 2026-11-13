"""G0 stage 2, step 11a, item C: RE3-OB's case index, and nothing else.

RE3-OB is test stratum S3. `AGENTS.md` section 6 forbids opening its cases before the G3
freeze, and the dataset was deliberately never downloaded. Proposal Appendix B.9 asks for
"S3 cluster count from RE3-OB's case index", measured on "RE3-OB index only".

A zip file's index is its central directory, which sits at the end of the file. This script
reads that, over HTTP range requests, and is built so that case content cannot be reached:

  * it asks for exactly two ranges -- the tail (to find the end-of-central-directory record)
    and the central directory extent that record points at;
  * a byte cap (default 8 MiB) aborts the run before any larger transfer;
  * a range that would start before the central directory's own offset is refused;
  * the fetched bytes are never written to a file and no member is ever decompressed;
  * every request, range, byte count and SHA-256 goes into `zenodo_fetch_log.json`, so a
    reader can check for themselves which bytes were transferred.

What comes back is entry names and sizes. From the names: distinct root-cause services,
fault types, repetitions and cases -- the S3 cluster count the G3 power rerun needs.

Usage:
  python stage2/re3ob_index.py --out stage2/out/step11a-<date>
  python stage2/re3ob_index.py --out /tmp/x --byte-cap 1024   # guard demo: must abort
"""
import argparse
import csv
import hashlib
import json
import os
import struct
import sys
from datetime import datetime, timezone

RECORD = "14590730"
URL = f"https://zenodo.org/records/{RECORD}/files/RE3-OB.zip"
API = f"https://zenodo.org/api/records/{RECORD}"
TAIL_BYTES = 64 * 1024
DEFAULT_CAP = 8 * 1024 * 1024
# a generic client name: the project's identity is not sent to an external service
UA = {"User-Agent": "zip-central-directory-reader/1.0"}

EOCD_SIG = b"PK\x05\x06"
EOCD64_LOC_SIG = b"PK\x06\x07"
EOCD64_SIG = b"PK\x06\x06"
CEN_SIG = b"PK\x01\x02"


class Budget:
    """Total bytes this run may transfer, and the log of what it did transfer."""

    def __init__(self, cap):
        self.cap = cap
        self.used = 0
        self.log = []

    def spend(self, n, entry):
        if self.used + n > self.cap:
            raise SystemExit(
                f"REFUSED (byte cap): this request would bring the run to {self.used + n} bytes, "
                f"over the {self.cap}-byte cap. No further bytes are transferred."
            )
        self.used += n
        self.log.append(entry)


def http_get_range(session, url, start, end, budget, why):
    """GET bytes [start, end] inclusive. Returns the body; never writes it to disk."""
    length = end - start + 1
    headers = dict(UA, Range=f"bytes={start}-{end}")
    # streamed, so the status line is checked before any body is read: a server that
    # ignored the Range header is hung up on rather than downloaded
    r = session.get(url, headers=headers, timeout=120, allow_redirects=True, stream=True)
    with r:
        if r.status_code == 200:
            r.close()
            raise SystemExit(
                "REFUSED (no range support): the server answered 200, so it ignored the Range "
                "header and would have sent the whole file. The body was not read; fall back to "
                "the Zenodo record metadata."
            )
        if r.status_code != 206:
            r.close()
            raise SystemExit(f"range request failed: HTTP {r.status_code} for bytes={start}-{end}")
        chunks, got = [], 0
        for chunk in r.iter_content(65536):
            got += len(chunk)
            if got > length:
                r.close()
                raise SystemExit(
                    f"REFUSED (over-long body): the server sent more than the {length} bytes asked "
                    f"for. The surplus is discarded and nothing is kept."
                )
            chunks.append(chunk)
        body = b"".join(chunks)
    budget.spend(len(body), {
        "why": why,
        "url": url,
        "range": f"bytes={start}-{end}",
        "requested_bytes": length,
        "received_bytes": len(body),
        "status": r.status_code,
        "content_range": r.headers.get("Content-Range"),
        "sha256": hashlib.sha256(body).hexdigest(),
    })
    return body


def find_eocd(tail):
    """Offset of the end-of-central-directory record inside `tail`, searching backwards."""
    i = tail.rfind(EOCD_SIG)
    if i < 0:
        raise SystemExit("no end-of-central-directory record in the tail -- not a zip, or the tail is too short")
    return i


def central_directory_extent(tail, tail_start, file_size):
    """Return (offset, size, entries, zip64) of the central directory."""
    i = find_eocd(tail)
    entries, cd_size, cd_offset = struct.unpack("<HII", tail[i + 10:i + 20])
    zip64 = False
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or entries == 0xFFFF:
        # zip64: the real values are in the zip64 end-of-central-directory record,
        # found through the locator that precedes the 32-bit record
        j = tail.rfind(EOCD64_LOC_SIG, 0, i)
        if j < 0:
            raise SystemExit("zip64 end-of-central-directory locator not found in the tail")
        (rec_offset,) = struct.unpack("<Q", tail[j + 8:j + 16])
        if not (tail_start <= rec_offset < tail_start + len(tail)):
            raise SystemExit("the zip64 record sits before the tail; enlarge --tail-bytes")
        k = rec_offset - tail_start
        if tail[k:k + 4] != EOCD64_SIG:
            raise SystemExit("the zip64 end-of-central-directory record is not where the locator says")
        entries, cd_size, cd_offset = struct.unpack("<QQQ", tail[k + 32:k + 56])
        zip64 = True
    if cd_offset + cd_size > file_size:
        raise SystemExit("the central directory extent runs past the end of the file")
    return cd_offset, cd_size, entries, zip64


def parse_central_directory(cd, expected_entries):
    """Entry (name, compressed size, uncompressed size) triples. Reads no member data."""
    out = []
    p = 0
    while p + 46 <= len(cd) and cd[p:p + 4] == CEN_SIG:
        csize, usize = struct.unpack("<II", cd[p + 20:p + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[p + 28:p + 34])
        name = cd[p + 46:p + 46 + name_len].decode("utf-8", "replace")
        extra = cd[p + 46 + name_len:p + 46 + name_len + extra_len]
        if usize == 0xFFFFFFFF or csize == 0xFFFFFFFF:
            usize, csize = zip64_sizes(extra, usize, csize)
        out.append((name, csize, usize))
        p += 46 + name_len + extra_len + comment_len
    if expected_entries and len(out) != expected_entries:
        print(f"  note: parsed {len(out)} entries, the record claims {expected_entries}", file=sys.stderr)
    return out


def zip64_sizes(extra, usize, csize):
    q = 0
    while q + 4 <= len(extra):
        hid, hlen = struct.unpack("<HH", extra[q:q + 4])
        body = extra[q + 4:q + 4 + hlen]
        if hid == 0x0001:
            vals = list(struct.unpack("<" + "Q" * (len(body) // 8), body[: len(body) // 8 * 8]))
            if usize == 0xFFFFFFFF and vals:
                usize = vals.pop(0)
            if csize == 0xFFFFFFFF and vals:
                csize = vals.pop(0)
            break
        q += 4 + hlen
    return usize, csize


def case_of(name):
    """('cartservice_f1', '1', 'traces.csv') from 'RE3-OB/cartservice_f1/1/traces.csv'."""
    parts = [p for p in name.split("/") if p and not p.startswith("__MACOSX")]
    if parts and parts[0].upper().startswith("RE3-OB"):
        parts = parts[1:]
    template = parts[0] if parts else ""
    repetition = parts[1] if len(parts) > 1 else ""
    leaf = parts[-1] if len(parts) > 2 else ""
    return template, repetition, leaf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--byte-cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--tail-bytes", type=int, default=TAIL_BYTES)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    import requests  # imported here so --help needs no network stack

    budget = Budget(args.byte_cap)
    session = requests.Session()

    head = session.head(URL, headers=UA, timeout=60, allow_redirects=True)
    if head.status_code >= 400:
        raise SystemExit(f"HEAD failed: HTTP {head.status_code}")
    file_size = int(head.headers["Content-Length"])
    accept_ranges = head.headers.get("Accept-Ranges", "")
    print(f"RE3-OB.zip: {file_size} bytes, Accept-Ranges: {accept_ranges or 'not advertised'}")
    budget.log.append({"why": "HEAD for the file length", "url": URL, "range": None,
                       "requested_bytes": 0, "received_bytes": 0, "status": head.status_code,
                       "content_length": file_size, "accept_ranges": accept_ranges})

    tail_start = max(0, file_size - args.tail_bytes)
    tail = http_get_range(session, URL, tail_start, file_size - 1, budget,
                          "the tail, to find the end-of-central-directory record")
    cd_offset, cd_size, entries, zip64 = central_directory_extent(tail, tail_start, file_size)
    print(f"central directory: offset {cd_offset}, {cd_size} bytes, {entries} entries"
          f"{', zip64' if zip64 else ''}")

    if cd_offset + cd_size - 1 >= file_size:
        raise SystemExit("REFUSED: the central directory extent is not inside the file")
    if cd_size > args.byte_cap:
        raise SystemExit(f"REFUSED (byte cap): the central directory is {cd_size} bytes")

    if cd_offset >= tail_start:
        cd = tail[cd_offset - tail_start:cd_offset - tail_start + cd_size]
        print("  the central directory was already inside the tail; no second request needed")
    else:
        cd = http_get_range(session, URL, cd_offset, cd_offset + cd_size - 1, budget,
                            "the central directory extent itself (names and sizes only)")

    names = parse_central_directory(cd, entries)
    print(f"index: {len(names)} entries, {budget.used} bytes transferred "
          f"({100.0 * budget.used / file_size:.4f}% of the file)")

    rows = []
    for name, csize, usize in names:
        template, repetition, leaf = case_of(name)
        if not template or name.endswith("/"):
            continue
        service, _, fault = template.rpartition("_")
        rows.append({"template": template, "service": service, "fault": fault,
                     "repetition": repetition, "file": leaf,
                     "compressed_bytes": csize, "uncompressed_bytes": usize})

    index_path = os.path.join(args.out, "re3ob_case_index.csv")
    with open(index_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["template", "service", "fault", "repetition", "file",
                            "compressed_bytes", "uncompressed_bytes"])
        w.writeheader()
        w.writerows(rows)

    services = sorted({r["service"] for r in rows if r["service"]})
    faults = sorted({r["fault"] for r in rows if r["fault"]})
    templates = sorted({r["template"] for r in rows if r["template"]})
    cases = sorted({(r["template"], r["repetition"]) for r in rows if r["repetition"]})
    files = sorted({r["file"] for r in rows if r["file"]})
    per_template = {t: len({r["repetition"] for r in rows if r["template"] == t and r["repetition"]})
                    for t in templates}

    summary = {
        "dataset": "RE3-OB",
        "source": URL,
        "zenodo_record": RECORD,
        "accessed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file_size_bytes": file_size,
        "bytes_transferred": budget.used,
        "share_of_file_transferred": round(budget.used / file_size, 8),
        "index_entries": len(names),
        "root_cause_services": services,
        "n_root_cause_services": len(services),
        "fault_types": faults,
        "n_fault_types": len(faults),
        "templates": templates,
        "n_templates": len(templates),
        "n_cases": len(cases),
        "repetitions_per_template": per_template,
        "files_per_case": files,
    }

    # rung 2: the record's own metadata, as corroboration of the file length
    try:
        meta = session.get(API, headers=UA, timeout=60).json()
        summary["zenodo_files"] = [
            {"key": f.get("key"), "size": f.get("size"), "checksum": f.get("checksum")}
            for f in meta.get("files", [])
        ]
        summary["zenodo_title"] = meta.get("metadata", {}).get("title")
        budget.log.append({"why": "the record metadata, to corroborate the file length",
                           "url": API, "range": None, "requested_bytes": 0,
                           "received_bytes": 0, "status": 200})
    except Exception as e:  # corroboration only; never fatal
        summary["zenodo_files_error"] = repr(e)

    with open(os.path.join(args.out, "re3ob_index_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(args.out, "zenodo_fetch_log.json"), "w") as f:
        json.dump({"byte_cap": args.byte_cap, "bytes_transferred": budget.used,
                   "file_size_bytes": file_size, "requests": budget.log}, f, indent=2)

    print(f"\nS3 (RE3-OB) index: {len(services)} root-cause services x {len(faults)} fault types "
          f"= {len(templates)} templates, {len(cases)} cases")
    print(f"  services: {', '.join(services)}")
    print(f"  faults:   {', '.join(faults)}")
    print(f"  files per case: {', '.join(files)}")
    print(f"wrote {index_path}")
    print("ALL_DONE")


if __name__ == "__main__":
    main()
