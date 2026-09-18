#!/usr/bin/env python3
"""Measure cue timing quality from cue JSON files.

Reports the metrics of docs/subtitle-quality.md section (d): cue duration distribution,
gaps between consecutive cues, characters per second, cue density, and - when speech
intervals are supplied - how much cue time sits outside speech.

Usage:
    python server/tools/cue_stats.py ~/.shisu-ko/cache/*.cues.json
    python server/tools/cue_stats.py --speech ~/.shisu-ko/cache abc.new.cues.json

A cue file is {"cues": [{"start", "end", "text", ...}], "covered": [...], "duration": ...}.
A speech file is either a list of [start, end] pairs, a {"speech": [...]} object, or a
mapping of video_id -> intervals. With a directory, <video_id>.speech.json is used.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def pct(values, q: float) -> float:
    """Percentile by linear interpolation; `values` must be sorted."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def share(values, predicate) -> float:
    return (sum(1 for v in values if predicate(v)) / len(values)) if values else 0.0


def merge_intervals(intervals, gap: float = 0.0) -> list:
    merged: list = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if float(b) > float(a)):
        if merged and a <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def overlap_seconds(a: float, b: float, intervals) -> float:
    """Seconds of [a, b) covered by a sorted, merged interval list."""
    total = 0.0
    for x, y in intervals:
        if y <= a:
            continue
        if x >= b:
            break
        total += min(b, y) - max(a, x)
    return total


def video_id_of(path: Path) -> str:
    return path.name.split(".")[0]


def load_speech(spec: str, video_id: str):
    if not spec:
        return None
    p = Path(spec).expanduser()
    if p.is_dir():
        candidates = [p / f"{video_id}.speech.json", p / f"{video_id}.new.speech.json"]
        p = next((c for c in candidates if c.is_file()), None)
        if p is None:
            return None
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("speech", data.get(video_id))
    if not isinstance(data, list):
        return None
    return merge_intervals([(iv[0], iv[1]) for iv in data if len(iv) >= 2])


BLINK_EPS = 0.005  # cue times are rounded to 10 ms, so a gap set to exactly MIN_GAP is not a blink


def stats_for(cues: list, duration: float, speech=None, covered: float = 0.0) -> dict:
    cues = sorted(
        ({"start": float(c["start"]), "end": float(c["end"]), "text": str(c.get("text") or "")}
         for c in cues if isinstance(c, dict) and "start" in c and "end" in c),
        key=lambda c: (c["start"], c["end"]),
    )
    durations = sorted(c["end"] - c["start"] for c in cues)
    gaps = [b["start"] - a["end"] for a, b in zip(cues, cues[1:])]
    cps = sorted((len(c["text"]) / d) for c, d in
                 ((c, c["end"] - c["start"]) for c in cues) if d > 0.05)
    # Density is measured over transcribed time: several caches cover only part of their video.
    span = covered or duration or (cues[-1]["end"] - cues[0]["start"] if cues else 0.0)

    out = {
        "n": len(cues),
        "dur_min": min(durations) if durations else 0.0,
        "dur_p10": pct(durations, 0.10),
        "dur_p50": pct(durations, 0.50),
        "dur_p90": pct(durations, 0.90),
        "dur_max": max(durations) if durations else 0.0,
        "under_05": share(durations, lambda d: d < 0.5),
        "under_08": share(durations, lambda d: d < 0.8),
        "over_60": share(durations, lambda d: d > 6.0),
        "gap_blink": share(gaps, lambda g: 0.1 + BLINK_EPS < g < 0.5),
        "gap_neg": share(gaps, lambda g: g < -BLINK_EPS),
        "gap_p50": pct(sorted(gaps), 0.50),
        "cps_p50": pct(cps, 0.50),
        "cps_p90": pct(cps, 0.90),
        "per_min": (len(cues) / (span / 60.0)) if span > 0 else 0.0,
        "off_speech": None,
    }
    if speech:
        cue_time = sum(durations)
        inside = sum(overlap_seconds(c["start"], c["end"], speech) for c in cues)
        out["off_speech"] = (cue_time - inside) / cue_time if cue_time > 0 else 0.0
    return out


def aggregate(files: list, speech_spec: str) -> list:
    rows = []
    all_cues, total_span, total_covered = [], 0.0, 0.0
    all_speech: list = []
    for path in files:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cues = [c for c in data.get("cues", []) if isinstance(c, dict)]
        duration = float(data.get("duration") or 0.0)
        covered = sum(float(b) - float(a) for a, b in merge_intervals(data.get("covered", [])))
        vid = video_id_of(Path(path))
        speech = load_speech(speech_spec, vid)
        row = stats_for(cues, duration, speech, covered)
        row["name"] = vid
        rows.append(row)
        # The pooled row shifts every file onto its own slot of one long timeline, so gaps
        # between files never count as cue gaps.
        base = total_span
        all_cues += [{"start": base + float(c["start"]), "end": base + float(c["end"]),
                      "text": c.get("text") or ""} for c in cues]
        if speech:
            all_speech += [[base + a, base + b] for a, b in speech]
        total_span += duration if duration > 0 else 0.0
        total_covered += covered
    if len(files) > 1:
        row = stats_for(all_cues, total_span, merge_intervals(all_speech) if all_speech else None, total_covered)
        row["name"] = "ALL"
        rows.append(row)
    return rows


def render(rows: list) -> str:
    cols = [
        ("name", "file", "{}", 14),
        ("n", "cues", "{:d}", 5),
        ("dur_min", "min", "{:.2f}", 5),
        ("dur_p10", "p10", "{:.2f}", 5),
        ("dur_p50", "p50", "{:.2f}", 5),
        ("dur_p90", "p90", "{:.2f}", 5),
        ("dur_max", "max", "{:.2f}", 6),
        ("under_05", "<0.5s", "{:.1%}", 6),
        ("under_08", "<0.8s", "{:.1%}", 6),
        ("over_60", ">6s", "{:.1%}", 6),
        ("gap_blink", "gap.1-.5", "{:.1%}", 8),
        ("gap_neg", "gap<0", "{:.1%}", 6),
        ("gap_p50", "gapp50", "{:.2f}", 6),
        ("cps_p50", "cps50", "{:.1f}", 5),
        ("cps_p90", "cps90", "{:.1f}", 5),
        ("per_min", "cues/min", "{:.1f}", 8),
        ("off_speech", "off-vad", "{:.1%}", 7),
    ]
    header = " | ".join(title.rjust(w) for _, title, _, w in cols)
    lines = [header, "-|-".join("-" * w for *_, w in cols)]
    for row in rows:
        cells = []
        for key, _, fmt, w in cols:
            v = row.get(key)
            cells.append(("-" if v is None else fmt.format(v)).rjust(w))
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def load_cues(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return sorted((c for c in data.get("cues", []) if isinstance(c, dict)), key=lambda c: float(c["start"]))


def fmt_time(t: float) -> str:
    return f"{int(t) // 60}:{int(t) % 60:02d}.{int((t % 1) * 10)}"


def print_long_gaps(path: str, count: int) -> None:
    """The biggest holes in a cue track: where music, silence or a dropped hallucination sits."""
    cues = load_cues(path)
    gaps = sorted(((float(b["start"]) - float(a["end"]), a, b) for a, b in zip(cues, cues[1:])),
                  key=lambda g: -g[0])[:count]
    print(f"# {Path(path).name}: {count} longest gaps")
    for gap, a, b in gaps:
        print(f"  {gap:6.1f}s  {fmt_time(float(a['end']))} -> {fmt_time(float(b['start']))}"
              f"  ...{a.get('text', '')[-12:]} | {b.get('text', '')[:12]}...")


def print_around(path: str, at: float, count: int) -> None:
    cues = load_cues(path)
    nearest = min(range(len(cues)), key=lambda i: abs(float(cues[i]["start"]) - at)) if cues else 0
    lo = max(0, nearest - count // 2)
    print(f"# {Path(path).name} around {fmt_time(at)}")
    for c in cues[lo:lo + count]:
        start, end = float(c["start"]), float(c["end"])
        seg = f" seg={c['seg']}" if "seg" in c else ""
        print(f"  {fmt_time(start)}-{fmt_time(end)} ({end - start:4.2f}s){seg}  {c.get('text', '')}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="cue JSON files")
    p.add_argument("--speech", default="", help="speech-interval JSON file, or a directory holding <video_id>.speech.json")
    p.add_argument("--json", action="store_true", help="print the raw numbers instead of a table")
    p.add_argument("--long-gaps", type=int, default=0, help="instead of the table, list the N longest gaps per file")
    p.add_argument("--around", type=float, default=None, help="instead of the table, print the cues near this time")
    p.add_argument("--count", type=int, default=15, help="how many cues --around prints")
    args = p.parse_args(argv)

    files = [f for f in args.files if Path(f).is_file()]
    if not files:
        print("no cue files found", file=sys.stderr)
        return 1
    if args.long_gaps:
        for f in files:
            print_long_gaps(f, args.long_gaps)
        return 0
    if args.around is not None:
        for f in files:
            print_around(f, args.around, args.count)
        return 0
    rows = aggregate(sorted(files), args.speech)
    print(json.dumps(rows, indent=2) if args.json else render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
