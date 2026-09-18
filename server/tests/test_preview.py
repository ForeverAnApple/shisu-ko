"""The preview decode: a few windows around the playhead, ready before the whole track is decoded.

Nothing here touches PyAV, faster-whisper or the network; the decode helpers are stubbed.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE


def make_args(**overrides):
    base = dict(first_window=20.0, window=40.0, lookahead=900.0)
    base.update(overrides)
    return SimpleNamespace(**base)


def preview(offset: float, seconds: float, fill=None):
    n = int(seconds * RATE)
    samples = np.arange(n, dtype=np.float32) if fill is None else np.full(n, fill, dtype=np.float32)
    return (offset, samples)


def session(**kwargs):
    base = dict(video_id=VIDEO, url="u", status="ready", duration=600.0)
    base.update(kwargs)
    return server.Session(**base)


# --------------------------------------------------------------------------- plan_window with a preview

def test_plan_window_uses_the_preview_when_the_full_audio_is_missing():
    s = session(preview=preview(29.0, 61.0), want_t=30.0)
    assert server.plan_window(s, make_args(first_window=20.0)) == (29.5, 49.5)


def test_plan_window_clamps_the_window_to_the_end_of_the_preview():
    s = session(preview=preview(29.0, 16.0), want_t=30.0)  # preview covers 29 - 45
    assert server.plan_window(s, make_args(first_window=20.0)) == (29.5, 45.0)


def test_plan_window_none_when_the_playhead_is_before_the_preview():
    s = session(preview=preview(100.0, 30.0), want_t=10.0)
    assert server.plan_window(s, make_args()) is None


def test_plan_window_none_when_the_playhead_is_past_the_preview():
    s = session(preview=preview(100.0, 30.0), want_t=200.0)
    assert server.plan_window(s, make_args()) is None


def test_plan_window_none_and_no_coverage_when_the_clamped_window_is_too_short():
    # The full-audio path marks such a sliver covered; the preview path must not, because the
    # audio for it simply has not been decoded yet.
    s = session(preview=preview(29.0, 1.0), want_t=30.0)
    assert server.plan_window(s, make_args()) is None
    assert s.covered == []


def test_plan_window_none_without_audio_or_preview():
    assert server.plan_window(session(want_t=30.0), make_args()) is None


def test_plan_window_prefers_the_full_audio_once_it_is_there():
    # Same session as the clamped case, but with the full decode present: no clamping.
    s = session(preview=preview(29.0, 16.0), audio=object(), want_t=30.0)
    assert server.plan_window(s, make_args(first_window=20.0)) == (29.5, 49.5)


# --------------------------------------------------------------------------- audio_slice

def test_audio_slice_reads_the_preview_at_the_right_offset():
    offset, samples = preview(10.0, 10.0)
    s = session(preview=(offset, samples))
    out = server.audio_slice(s, 12.0, 13.0)
    assert np.array_equal(out, samples[2 * RATE: 3 * RATE])


def test_audio_slice_none_outside_the_preview():
    s = session(preview=preview(10.0, 10.0))
    assert server.audio_slice(s, 5.0, 8.0) is None      # before the preview
    assert server.audio_slice(s, 19.0, 25.0) is None    # runs past its end
    assert server.audio_slice(s, 12.0, 12.0) is None    # empty range


def test_audio_slice_none_without_audio_or_preview():
    assert server.audio_slice(session(), 0.0, 5.0) is None


def test_audio_slice_uses_the_full_audio_when_present():
    audio = np.arange(20 * RATE, dtype=np.float32)
    s = session(audio=audio, preview=preview(10.0, 10.0, fill=-1.0))
    out = server.audio_slice(s, 12.0, 13.0)
    assert np.array_equal(out, audio[12 * RATE: 13 * RATE])


# --------------------------------------------------------------------------- Fetcher.fetch

def fake_decode_range(src, start, end, rate=server.CLIP_RATE):
    return np.full(int((end - start) * rate), 1000, dtype=np.int16)


def make_fetcher(monkeypatch, tmp_path, decode_audio, duration=90.0, duration_hint=0.0, decode_range=fake_decode_range):
    """A Fetcher whose audio file is already cached and whose decoders are stubs."""
    path = tmp_path / f"{VIDEO}.webm"
    path.write_bytes(b"pretend audio")
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "find_cached_audio", lambda video_id: path)
    monkeypatch.setattr(server, "probe_duration", lambda p: duration)
    monkeypatch.setattr(server, "_decode_range", decode_range)

    module = types.ModuleType("faster_whisper.audio")
    module.decode_audio = decode_audio
    package = types.ModuleType("faster_whisper")
    package.audio = module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)

    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0,
                           cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="auto")
    s = server.Session(video_id=VIDEO, url="u", want_t=30.0, duration_hint=duration_hint)
    return server.Fetcher(args), s


def test_fetch_makes_the_session_usable_before_the_full_decode_returns(monkeypatch, tmp_path):
    seen = {}

    def decode_audio(path, sampling_rate=RATE):
        # The state the transcriber would see while the slow decode is still running.
        with state["session"].lock:
            s = state["session"]
            seen.update(status=s.status, preview=s.preview, duration=s.duration, audio=s.audio)
        return np.zeros(int(90.0 * RATE), dtype=np.float32)

    state = {}
    fetcher, s = make_fetcher(monkeypatch, tmp_path, decode_audio, duration=90.0)
    state["session"] = s
    fetcher.fetch(s)

    assert seen["status"] == "ready"
    assert seen["audio"] is None
    assert seen["duration"] == pytest.approx(90.0)
    offset, samples = seen["preview"]
    assert offset == pytest.approx(29.0)                       # want_t - 1
    assert len(samples) == int((90.0 - 29.0) * RATE)           # clamped to the end of the video
    assert samples.dtype == np.float32
    assert samples[0] == pytest.approx(1000 / 32768.0)
    # A window can be planned from the preview alone.
    assert server.plan_window(server.Session(video_id=VIDEO, url="u", status="ready", duration=90.0,
                                             want_t=30.0, preview=seen["preview"]), make_args()) == (29.5, 49.5)

    assert s.status == "ready"
    assert s.preview is None
    assert s.audio is not None and len(s.audio) == int(90.0 * RATE)
    assert s.duration == pytest.approx(90.0)
    assert s.fetching is False


def test_fetch_falls_back_to_the_ytdlp_duration_when_the_container_has_none(monkeypatch, tmp_path):
    fetcher, s = make_fetcher(monkeypatch, tmp_path, lambda p, sampling_rate=RATE: np.zeros(RATE, dtype=np.float32),
                              duration=None, duration_hint=90.0)
    fetcher.make_preview(s, tmp_path / f"{VIDEO}.webm")
    assert s.status == "ready"
    assert s.preview is not None and s.preview[0] == pytest.approx(29.0)
    assert s.duration == pytest.approx(90.0)


def test_fetch_skips_the_preview_when_the_duration_is_unknown(monkeypatch, tmp_path):
    calls = []

    def decode_range(src, start, end, rate=server.CLIP_RATE):
        calls.append((start, end))
        return fake_decode_range(src, start, end, rate)

    fetcher, s = make_fetcher(monkeypatch, tmp_path, lambda p, sampling_rate=RATE: np.zeros(RATE, dtype=np.float32),
                              duration=None, decode_range=decode_range)
    fetcher.fetch(s)
    assert calls == []
    assert s.audio is not None
    assert s.preview is None
    assert s.status == "ready"


def test_fetch_survives_a_failing_preview_decode(monkeypatch, tmp_path):
    def boom(src, start, end, rate=server.CLIP_RATE):
        raise RuntimeError("no decoder for this file")

    fetcher, s = make_fetcher(monkeypatch, tmp_path,
                              lambda p, sampling_rate=RATE: np.zeros(int(90.0 * RATE), dtype=np.float32),
                              decode_range=boom)
    fetcher.fetch(s)
    assert s.status == "ready"
    assert s.error is None
    assert s.preview is None
    assert s.audio is not None
