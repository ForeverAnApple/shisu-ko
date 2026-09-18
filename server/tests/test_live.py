"""Live streams: the segment buffer, the planner at the live edge, and the follower's bookkeeping.

No network, PyAV or Whisper: the follower gets a fake source that hands out silent segments.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE
SEG = 5.0


def make_args(**overrides):
    base = dict(first_window=20.0, window=40.0, lookahead=900.0, idle_minutes=30, client_timeout=30.0)
    base.update(overrides)
    return SimpleNamespace(**base)


def chunk(seconds: float, fill: float = 1.0) -> np.ndarray:
    return np.full(int(seconds * RATE), fill, dtype=np.float32)


def buffer(*starts, seconds=SEG):
    buf = server.LiveAudio()
    for start in starts:
        buf.add(start, chunk(seconds, fill=start))
    return buf


def live_session(buf, **kwargs):
    base = dict(video_id=VIDEO, url="u", status="ready", live=True, live_audio=buf, duration=buf.end())
    base.update(kwargs)
    return server.Session(**base)


# --------------------------------------------------------------------------- LiveAudio

def test_live_audio_reports_contiguous_ranges_and_gaps():
    assert buffer(100.0, 105.0, 110.0).available() == [[100.0, 115.0]]
    assert buffer(100.0, 110.0).available() == [[100.0, 105.0], [110.0, 115.0]]
    assert server.LiveAudio().available() == []
    assert server.LiveAudio().end() == 0.0


def test_live_audio_slices_across_chunks_on_the_stream_clock():
    out = buffer(100.0, 105.0).slice(103.0, 107.0)
    assert len(out) == 4 * RATE
    assert out[0] == pytest.approx(100.0)          # still inside the first chunk
    assert out[2 * RATE + 10] == pytest.approx(105.0)  # the second chunk, at 105 s


def test_live_audio_refuses_a_range_that_is_not_fully_fetched():
    buf = buffer(100.0, 110.0)
    assert buf.slice(103.0, 111.0) is None   # runs through the hole
    assert buf.slice(99.0, 101.0) is None    # starts before the first chunk
    assert buf.slice(104.0, 104.0) is None   # empty
    assert buf.slice(110.5, 114.0) is not None


def test_live_audio_ignores_a_repeated_segment_and_trims_old_ones():
    buf = buffer(100.0, 105.0)
    buf.add(100.0, chunk(SEG, fill=-1.0))
    assert len(buf.chunks) == 2
    buf.trim(105.5)  # the first chunk ends at 105, before the cut
    assert buf.available() == [[105.0, 110.0]]


# --------------------------------------------------------------------------- plan_window for a live session

def test_live_planner_starts_at_the_playhead_inside_the_fetched_audio():
    s = live_session(buffer(*[100.0 + SEG * i for i in range(8)]), want_t=103.0)  # audio 100-140
    assert server.plan_window(s, make_args()) == (102.5, 122.5)


def test_live_planner_waits_for_enough_audio_at_the_live_edge():
    s = live_session(buffer(100.0, 105.0), want_t=103.0, covered=[[100.0, 108.0]])
    assert server.plan_window(s, make_args()) is None  # 2 s at the edge is not a window yet
    assert s.covered == [[100.0, 108.0]]               # and it was not marked covered either
    s.live_audio.add(110.0, chunk(SEG))
    s.live_audio.add(115.0, chunk(SEG))
    assert server.plan_window(s, make_args()) == (108.0, 120.0)


def test_live_planner_clamps_to_the_edge_but_never_beyond_a_window():
    s = live_session(buffer(*[100.0 + SEG * i for i in range(20)]), want_t=101.0, covered=[[100.0, 110.0]])
    assert server.plan_window(s, make_args(window=40.0)) == (110.0, 150.0)


def test_live_planner_idles_while_the_playhead_is_outside_the_buffer():
    s = live_session(buffer(100.0, 105.0), want_t=300.0)
    assert server.plan_window(s, make_args()) is None
    s.want_t = 50.0
    assert server.plan_window(s, make_args()) is None


def test_live_planner_respects_the_lookahead_and_the_ready_status():
    s = live_session(buffer(*[100.0 + SEG * i for i in range(20)]), want_t=100.0, covered=[[100.0, 130.0]])
    assert server.plan_window(s, make_args(lookahead=20.0)) is None
    s.status = "downloading"
    assert server.plan_window(s, make_args()) is None


def test_live_planner_fills_a_sliver_between_covered_ranges_as_the_normal_one_does():
    s = live_session(buffer(*[100.0 + SEG * i for i in range(8)]), want_t=100.0,
                     covered=[[100.0, 110.0], [110.8, 130.0]])
    assert server.plan_window(s, make_args()) is None
    assert s.covered == [[100.0, 130.0]]
    assert server.plan_window(s, make_args()) == (130.0, 140.0)


def test_audio_slice_reads_the_live_buffer():
    s = live_session(buffer(100.0, 105.0))
    assert len(server.audio_slice(s, 101.0, 109.0)) == 8 * RATE


# --------------------------------------------------------------------------- LiveFollower

class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSource:
    """Silent 5 s segments; the head advances with the fake clock, each fetch costs 0.2 s of it."""

    def __init__(self, clock, head0: int = 1000):
        self.clock = clock
        self.seg_seconds = SEG
        self.t0 = clock()
        self.head0 = head0
        self.last_head = None
        self.requests: list = []
        self.refreshes = 0
        self.fail: dict = {}  # seq -> how many more times its fetch fails
        self.on_request = lambda seq: None

    def head(self) -> int:
        self.last_head = self.head0 + int((self.clock() - self.t0) // SEG)
        return self.last_head

    def segment(self, seq: int):
        self.requests.append(seq)
        self.on_request(seq)
        self.clock.sleep(0.2)
        if self.fail.get(seq, 0) > 0:
            self.fail[seq] -= 1
            raise RuntimeError("503")
        self.head()
        return seq * SEG + 0.07, chunk(SEG, fill=float(seq))

    def refresh(self):
        self.refreshes += 1


class Viewer:
    """A client that syncs on every tick of the follower until `until()` holds, then closes the tab."""

    def __init__(self, s, args, clock, source, until):
        self.s, self.clock, self.source, self.until = s, clock, source, until
        self.idle_limit = args.idle_minutes * 60
        self.gone = False
        source.on_request = lambda seq: self.tick()
        with s.lock:
            s.last_sync = clock()

    def tick(self):
        with self.s.lock:
            if self.gone:
                return
            if self.until():
                self.gone = True
                self.s.last_sync = self.clock() - self.idle_limit - 1
            else:
                self.s.last_sync = self.clock()

    def sleep(self, seconds):
        self.clock.sleep(seconds)
        self.tick()


def run_follower(s, args, until, clock=None, source=None):
    clock = clock or FakeClock()
    source = source or FakeSource(clock)
    viewer = Viewer(s, args, clock, source, until)
    server.LiveFollower(s, source, args, sleep=viewer.sleep, clock=clock).run()
    return source


def after(source, n):
    return lambda: len(source.requests) >= n


def test_follower_starts_one_segment_behind_the_playhead_and_follows_the_head():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)
    s = server.Session(video_id=VIDEO, url="u", want_t=4990.0)  # inside segment 998
    run_follower(s, make_args(), after(source, 6), clock, source)
    assert source.requests[:6] == [996, 997, 998, 999, 1000, 1001]  # 1001 became the head as the clock ran
    assert s.status == "evicted"
    assert s.live is True
    assert s.live_audio is None


def test_follower_publishes_the_session_as_ready_with_the_audio_on_the_stream_clock():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)
    s = server.Session(video_id=VIDEO, url="u", want_t=4990.0)
    seen = {}

    def until():
        if s.status == "ready" and not seen:
            seen.update(avail=s.live_audio.available(), duration=s.duration)
        return bool(seen) and len(source.requests) >= 3

    run_follower(s, make_args(), until, clock, source)
    assert seen["avail"][0][0] == pytest.approx(996 * SEG + 0.07)
    assert seen["duration"] == pytest.approx(seen["avail"][-1][1])


def test_follower_starts_behind_the_head_when_the_playhead_is_unknown():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)
    s = server.Session(video_id=VIDEO, url="u", want_t=0.0)
    run_follower(s, make_args(), after(source, 3), clock, source)
    assert source.requests[0] == 1000 - server.LIVE_START_BEHIND


def test_follower_jumps_after_a_seek_and_keeps_the_old_audio_within_reach():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)
    s = server.Session(video_id=VIDEO, url="u", want_t=4990.0)
    seen = {}

    def until():
        if len(source.requests) == 4:
            s.want_t = 4700.0  # a minute and a half back into the DVR window
        if len(source.requests) == 8:
            seen["avail"] = s.live_audio.available()
        return bool(seen)

    run_follower(s, make_args(), until, clock, source)
    assert source.requests[:4] == [996, 997, 998, 999]
    assert source.requests[4:8] == [938, 939, 940, 941]
    assert len(seen["avail"]) == 2  # the run around 4990 and the new one around 4700


def test_follower_pauses_while_no_client_syncs_and_resumes_after():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)
    s = server.Session(video_id=VIDEO, url="u", want_t=4990.0)
    args = make_args(client_timeout=30.0)
    viewer = Viewer(s, args, clock, source, after(source, 4))
    away = {}

    def on_request(seq):
        if len(source.requests) == 2:
            away["since"] = clock()
            with s.lock:
                s.last_sync = clock() - 31.0  # the tab closed a while ago: over --client-timeout
        else:
            viewer.tick()

    def sleep(seconds):
        clock.sleep(seconds)
        if away and clock() - away["since"] < 120.0:
            away["requests"] = len(source.requests)
            return  # nobody syncs: last_sync stays where it was
        viewer.tick()

    source.on_request = on_request
    server.LiveFollower(s, source, args, sleep=sleep, clock=clock).run()
    assert away["requests"] == 2   # nothing was fetched in those two minutes
    assert len(source.requests) >= 4  # and it carried on once the viewer was back


def test_follower_retries_a_failed_segment_instead_of_skipping_it():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)
    source.fail = {997: 1}
    s = server.Session(video_id=VIDEO, url="u", want_t=4990.0)
    run_follower(s, make_args(), after(source, 4), clock, source)
    assert source.requests[:4] == [996, 997, 997, 998]


def test_follower_gives_up_after_too_many_failures():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)
    source.fail = {996: 10_000}
    s = server.Session(video_id=VIDEO, url="u", want_t=4990.0)
    with pytest.raises(RuntimeError):
        run_follower(s, make_args(), lambda: False, clock, source)
    assert len(source.requests) == server.LIVE_MAX_ERRORS
    assert source.refreshes == 1


def test_follower_ends_the_session_when_the_stream_is_over():
    clock = FakeClock()
    source = FakeSource(clock, head0=1000)

    def refresh():
        raise server.LiveEnded("The live stream has ended")

    source.refresh = refresh
    source.fail = {996: 10_000}
    s = server.Session(video_id=VIDEO, url="u", want_t=4990.0)
    run_follower(s, make_args(), lambda: False, clock, source)
    assert s.status == "error"
    assert s.error == "The live stream has ended"


def test_place_cursor_keeps_a_run_the_playhead_is_heading_into():
    place = server.LiveFollower.place_cursor
    assert place(None, 4990.0, [], 1000, SEG) == 996
    assert place(1001, 4990.0, [[4985.0, 5005.0]], 1000, SEG) == 1001   # inside the buffer
    assert place(1001, 5008.0, [[4985.0, 5005.0]], 1000, SEG) == 1001   # just ahead, being fetched
    assert place(1001, 4700.0, [[4985.0, 5005.0]], 1000, SEG) == 938    # a seek
    assert place(None, 0.0, [], 1000, SEG) == 1000 - server.LIVE_START_BEHIND


# --------------------------------------------------------------------------- App

def make_app(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = make_args(model="large-v3", language="ja")
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return app


def test_sync_reports_live_and_the_cache_is_never_written_for_a_stream(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = live_session(buffer(100.0, 105.0), cues=[{"id": 0, "start": 101.0, "end": 102.0, "text": "あ", "seg": 0}])
    with app.lock:
        app.sessions[VIDEO] = s
    resp = app.sync(VIDEO, "u", 103.0, 0)
    assert resp["live"] is True
    assert resp["duration"] == pytest.approx(110.0)
    assert [c["text"] for c in resp["cues"]] == ["あ"]
    app.save_cache(s)
    assert not list(tmp_path.glob("*.cues.json"))


def test_clip_comes_from_the_live_buffer(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = live_session(buffer(100.0, 105.0))
    with app.lock:
        app.sessions[VIDEO] = s
    encoded = {}
    monkeypatch.setattr(server, "encode_clip", lambda samples, fmt: encoded.update(n=len(samples), fmt=fmt) or (b"wav", "audio/wav", "wav"))
    assert app.clip(VIDEO, 101.0, 103.0, "wav") == (b"wav", "audio/wav", "wav")
    assert encoded == {"n": 2 * RATE, "fmt": "wav"}
    with pytest.raises(ValueError):
        app.clip(VIDEO, 50.0, 52.0, "wav")  # trimmed away long ago


def test_idle_live_session_without_a_follower_is_evicted(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = live_session(buffer(100.0, 105.0), status="error")
    s.last_sync = time.time() - 31 * 60
    with app.lock:
        app.sessions[VIDEO] = s
    app.last_evict = 0
    app.maybe_evict()
    assert s.live_audio is None
    assert s.status == "evicted"
