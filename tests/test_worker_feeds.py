#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright Sensors & Signals LLC https://www.snstac.com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Tests for ADSBWorker's feed readers: HTTP, file and inotify, and run().

The HTTP tests use a real aiohttp server bound to 127.0.0.1 on an ephemeral
port, so the request the worker builds (URL, headers) and the way it parses
the response are both exercised for real rather than against a mock of
aiohttp's API.

As in tests/test_status.py, coroutines are driven with asyncio.run() from
plain test functions, and workers (which own an asyncio.Queue) are built
INSIDE the running loop so the tests are portable to Python 3.9.
"""

import asyncio
import importlib.util
import json
import logging
import os
import sys

from configparser import ConfigParser
from unittest.mock import patch
from urllib.parse import urlparse

import aiohttp
import pytest

from aiohttp import web
from aiohttp.test_utils import TestServer

import adsbcot
from adsbcot import classes
from adsbcot.classes import ADSBWorker

try:
    import asyncinotify  # NOQA pylint: disable=unused-import

    _HAVE_INOTIFY = sys.platform.startswith("linux")
except Exception:  # NOQA pylint: disable=broad-except
    _HAVE_INOTIFY = False

needs_inotify = pytest.mark.skipif(
    not _HAVE_INOTIFY, reason="asyncinotify (Linux inotify) not available"
)

# A dump1090 aircraft record with a position, so it renders to CoT.
CRAFT = {
    "hex": "a9ee47",
    "flight": "N739UL  ",
    "alt_baro": 3700,
    "alt_geom": 3750,
    "gs": 79.5,
    "lat": 37.836449,
    "lon": -122.030281,
    "track": 50.1,
}

# A Stratux traffic record: the ICAO address is a 24-bit integer.
STRATUX_CRAFT = {
    "Icao_addr": 0xA9EE47,
    "Tail": "N739UL",
    "Lat": 37.836449,
    "Lng": -122.030281,
    "Speed": 79,
}

_REAL_SLEEP = asyncio.sleep


class _Stop(Exception):
    """Raised by a test double to break out of an endless worker loop."""


class _FakeStatus:
    """Records every call the worker makes on its status writer."""

    def __init__(self):
        self.calls = []

    def count(self, *args, **kwargs):
        self.calls.append(("count", args, kwargs))

    def record(self, *args, **kwargs):
        self.calls.append(("record", args, kwargs))

    def set(self, *args, **kwargs):
        self.calls.append(("set", args, kwargs))

    def write(self, *args, **kwargs):
        self.calls.append(("write", args, kwargs))
        return True


class _Logs(logging.Handler):
    """Collect a worker's log messages; pytak loggers do not propagate."""

    def __init__(self, logger):
        super().__init__(logging.DEBUG)
        self.messages = []
        self.logger = logger
        self.old_level = logger.level
        logger.addHandler(self)
        logger.setLevel(logging.DEBUG)

    def emit(self, record):
        self.messages.append(record.getMessage())

    def close(self):
        self.logger.removeHandler(self)
        self.logger.setLevel(self.old_level)
        super().close()

    def has(self, text):
        return any(text in msg for msg in self.messages)


def _config(**overrides):
    parser = ConfigParser()
    parser.read_dict(
        {
            "DEFAULT": {
                "INCLUDE_TISB": "false",
                "TISB_ONLY": "false",
                "INCLUDE_ALL_CRAFT": "true",
                "TAK_PROTO": "0",
                **overrides,
            }
        }
    )
    return parser["DEFAULT"]


def _build_worker(**overrides):
    """Build an ADSBWorker. MUST be called from inside a running event loop."""
    worker = ADSBWorker(asyncio.Queue(), _config(**overrides))
    worker.status = _FakeStatus()
    return worker


def _record_handle_data(worker, stop_after=None):
    """Replace worker.handle_data with a recorder; return the record list."""
    handled = []

    async def _handle(data):
        handled.append(data)
        if stop_after is not None and len(handled) >= stop_after:
            raise _Stop

    worker.handle_data = _handle
    return handled


def _known_craft_csv(tmp_path):
    path = tmp_path / "known_craft.csv"
    path.write_text(
        "HEX,REG,COT,CALLSIGN\nA9EE47,N739UL,a-f-A-C-F,FRIENDLY1\n",
        encoding="utf-8",
    )
    return str(path)


# --------------------------------------------------------------------------
# __init__, heartbeat, handle_data, process_craft
# --------------------------------------------------------------------------


def test_init_loads_existing_known_craft_csv(tmp_path):
    csv_path = _known_craft_csv(tmp_path)

    async def _main():
        return ADSBWorker(asyncio.Queue(), _config(KNOWN_CRAFT=csv_path))

    worker = asyncio.run(_main())
    assert worker.known_craft_db is not None
    assert worker.known_craft_db["hex_index"]["A9EE47"]["COT"] == "a-f-A-C-F"
    assert worker.uid_key == "ICAO"
    assert worker.session is None


def test_init_ignores_missing_known_craft_path(tmp_path):
    missing = str(tmp_path / "nope.csv")

    async def _main():
        return ADSBWorker(asyncio.Queue(), _config(KNOWN_CRAFT=missing, UID_KEY="REG"))

    worker = asyncio.run(_main())
    assert worker.known_craft_db is None
    assert worker.uid_key == "REG"


def test_status_heartbeat_force_writes_every_interval():
    delays = []

    async def _sleep(delay):
        delays.append(delay)
        if len(delays) > 2:
            raise _Stop

    async def _main():
        worker = _build_worker()
        with patch("adsbcot.classes.asyncio.sleep", _sleep):
            with pytest.raises(_Stop):
                await worker._status_heartbeat(interval=0.25)
        return worker

    worker = asyncio.run(_main())
    assert delays == [0.25, 0.25, 0.25]
    writes = [c for c in worker.status.calls if c[0] == "write"]
    assert writes == [("write", (), {"force": True})] * 2


def test_handle_data_accepts_a_single_dict():
    processed = []

    async def _main():
        worker = _build_worker()

        async def _process(craft):
            processed.append(craft)
            return "A9EE47"

        worker.process_craft = _process
        await worker.handle_data(dict(CRAFT))
        return worker

    worker = asyncio.run(_main())
    assert processed == [CRAFT]
    # A single record is not a list "in view", so no tracked level is set.
    assert not [c for c in worker.status.calls if c[0] == "set"]


def test_process_craft_stratux_integer_icao_is_plotted():
    async def _main():
        worker = _build_worker()
        icao = await worker.process_craft(dict(STRATUX_CRAFT))
        return worker, icao

    worker, icao = asyncio.run(_main())
    assert icao == "A9EE47"
    assert worker.queue.qsize() == 1
    event = worker.queue.get_nowait()
    assert isinstance(event, bytes)
    assert b"ICAO-A9EE47" in event
    record = [c for c in worker.status.calls if c[0] == "record"][0]
    assert record[2]["icao"] == "A9EE47"
    assert record[2]["flight"] == "N739UL"
    assert record[2]["speed"] == 79
    assert record[2]["placed"] is True


def test_process_craft_known_craft_db_filters_and_plots(tmp_path):
    """With KNOWN_CRAFT and INCLUDE_ALL_CRAFT=false only listed craft plot."""
    csv_path = _known_craft_csv(tmp_path)

    async def _main():
        worker = _build_worker(KNOWN_CRAFT=csv_path, INCLUDE_ALL_CRAFT="false")
        listed = await worker.process_craft(dict(CRAFT))
        unlisted = await worker.process_craft(dict(CRAFT, hex="abcdef"))
        return worker, listed, unlisted

    worker, listed, unlisted = asyncio.run(_main())
    assert listed == "A9EE47"
    assert unlisted is None
    assert worker.queue.qsize() == 1
    event = worker.queue.get_nowait()
    assert isinstance(event, bytes)
    # The known-craft row's CoT type replaces the derived one.
    assert b'type="a-f-A-C-F"' in event
    counts = [c[1][0] for c in worker.status.calls if c[0] == "count"]
    assert counts == ["rx", "emitted", "rx", "filtered_unknown"]


# --------------------------------------------------------------------------
# get_feed against a real local HTTP server
# --------------------------------------------------------------------------


def _make_app(seen):
    async def _record(request):
        seen.append({"path": request.path, "headers": dict(request.headers)})

    async def aircraft(request):
        await _record(request)
        return web.json_response({"aircraft": [CRAFT]})

    async def ac(request):
        await _record(request)
        return web.json_response({"ac": [CRAFT, CRAFT]})

    async def neither(request):
        await _record(request)
        return web.json_response({"now": 1, "messages": 2})

    async def null(request):
        await _record(request)
        return web.Response(text="null", content_type="application/json")

    async def error(request):
        await _record(request)
        return web.Response(status=500, text="upstream exploded")

    app = web.Application()
    app.router.add_get("/aircraft", aircraft)
    app.router.add_get("/ac", ac)
    app.router.add_get("/neither", neither)
    app.router.add_get("/null", null)
    app.router.add_get("/error", error)
    app.router.add_get("/rapidapi/v2/aircraft", aircraft)
    return app


def _get_feed(paths, **overrides):
    """Call get_feed once per path against a live server.

    Returns (handled, seen requests, log messages).
    """
    seen = []
    out = {}

    async def _main():
        server = TestServer(_make_app(seen), host="127.0.0.1", port=0)
        await server.start_server()
        worker = _build_worker(**overrides)
        handled = _record_handle_data(worker)
        logs = _Logs(worker._logger)
        try:
            async with aiohttp.ClientSession() as worker.session:
                for path in paths:
                    await worker.get_feed(str(server.make_url(path)))
        finally:
            logs.close()
            await server.close()
        out["handled"] = handled
        out["logs"] = logs

    asyncio.run(_main())
    return out["handled"], seen, out["logs"]


def test_get_feed_aircraft_key_sends_api_auth_header():
    handled, seen, _ = _get_feed(["/aircraft"], API_KEY="secret-key")
    assert handled == [[CRAFT]]
    assert seen[0]["headers"]["api-auth"] == "secret-key"
    assert "x-rapidapi-key" not in {k.lower() for k in seen[0]["headers"]}


def test_get_feed_ac_key():
    handled, seen, logs = _get_feed(["/ac"])
    assert handled == [[CRAFT, CRAFT]]
    # No API_KEY configured: the header is still sent, empty.
    assert seen[0]["headers"]["api-auth"] == ""
    assert logs.has("Retrieved 2 ADS-B aircraft messages.")


def test_get_feed_rapidapi_headers_default_host():
    handled, seen, _ = _get_feed(["/rapidapi/v2/aircraft"], API_KEY="rk")
    assert handled == [[CRAFT]]
    headers = {k.lower(): v for k, v in seen[0]["headers"].items()}
    assert headers["x-rapidapi-key"] == "rk"
    assert headers["x-rapidapi-host"] == adsbcot.DEFAULT_RAPIDAPI_HOST
    assert "api-auth" not in headers


def test_get_feed_rapidapi_host_override():
    _, seen, _ = _get_feed(
        ["/rapidapi/v2/aircraft"], API_KEY="rk", RAPIDAPI_HOST="h.example"
    )
    headers = {k.lower(): v for k, v in seen[0]["headers"].items()}
    assert headers["x-rapidapi-host"] == "h.example"


def test_get_feed_without_aircraft_null_or_error_handles_nothing():
    handled, seen, logs = _get_feed(["/neither", "/null", "/error"])
    assert handled == []
    assert [s["path"] for s in seen] == ["/neither", "/null", "/error"]
    assert logs.has("No aircraft data returned from")
    assert logs.has("Empty JSON response from")
    assert logs.has("Received HTTP Status 500")
    assert logs.has("upstream exploded")


def test_get_feed_without_open_session_returns_early():
    async def _main():
        worker = _build_worker()
        handled = _record_handle_data(worker)
        logs = _Logs(worker._logger)
        try:
            # No session at all.
            await worker.get_feed("http://127.0.0.1:9/never")
            # A session that has been closed.
            session = aiohttp.ClientSession()
            await session.close()
            worker.session = session
            await worker.get_feed("http://127.0.0.1:9/never")
        finally:
            logs.close()
        return handled, logs

    handled, logs = asyncio.run(_main())
    assert handled == []
    assert sum("Session is closed" in m for m in logs.messages) == 2


# --------------------------------------------------------------------------
# get_file_feed
# --------------------------------------------------------------------------


def _get_file_feed(tmp_path, content):
    path = tmp_path / "aircraft.json"
    path.write_text(content, encoding="utf-8")

    async def _main():
        worker = _build_worker()
        handled = _record_handle_data(worker)
        await worker.get_file_feed(urlparse("file://" + str(path)))
        return handled

    return asyncio.run(_main())


def test_get_file_feed_aircraft(tmp_path):
    content = json.dumps({"now": 1, "aircraft": [CRAFT]})
    assert _get_file_feed(tmp_path, content) == [[CRAFT]]


def test_get_file_feed_ac(tmp_path):
    content = json.dumps({"ac": [CRAFT, CRAFT]})
    assert _get_file_feed(tmp_path, content) == [[CRAFT, CRAFT]]


def test_get_file_feed_empty_file(tmp_path):
    assert _get_file_feed(tmp_path, "") == []


def test_get_file_feed_no_aircraft(tmp_path):
    assert _get_file_feed(tmp_path, json.dumps({"now": 1})) == []
    assert _get_file_feed(tmp_path, json.dumps({"aircraft": []})) == []


# --------------------------------------------------------------------------
# run()
# --------------------------------------------------------------------------


def _run(raise_exc=None, **overrides):
    """Drive run() with _run_feed and the heartbeat replaced by recorders."""
    out = {"feed_calls": [], "heartbeat": []}

    async def _main():
        worker = _build_worker(**overrides)
        out["worker"] = worker
        logs = _Logs(worker._logger)
        out["logs"] = logs

        async def _heartbeat():
            out["heartbeat"].append("started")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                out["heartbeat"].append("cancelled")
                raise

        async def _run_feed(url, feed_url, poll_interval):
            # The status file must already exist before the feed starts.
            out["status_before_feed"] = list(worker.status.calls)
            out["feed_calls"].append((url, feed_url, poll_interval))
            await _REAL_SLEEP(0)  # let the heartbeat task start
            if raise_exc is not None:
                raise raise_exc

        worker._status_heartbeat = _heartbeat
        worker._run_feed = _run_feed
        try:
            await worker.run()
        finally:
            await _REAL_SLEEP(0)  # let the cancellation be delivered
            logs.close()

    if raise_exc is not None:
        with pytest.raises(type(raise_exc)):
            asyncio.run(_main())
    else:
        asyncio.run(_main())
    return out


def test_run_without_feed_url_raises():
    async def _main():
        worker = _build_worker()
        await worker.run()

    with pytest.raises(ValueError, match="FEED_URL"):
        asyncio.run(_main())

    async def _empty():
        worker = _build_worker(FEED_URL="")
        await worker.run()

    with pytest.raises(ValueError, match="FEED_URL"):
        asyncio.run(_empty())


def test_run_defaults_poll_interval_and_reports_before_feed():
    url = "http://127.0.0.1:9/data/aircraft.json"
    out = _run(FEED_URL=url)

    assert len(out["feed_calls"]) == 1
    called_url, feed_url, poll_interval = out["feed_calls"][0]
    assert called_url == url
    assert feed_url == urlparse(url)
    assert poll_interval == adsbcot.DEFAULT_POLL_INTERVAL
    assert out["logs"].has("POLL_INTERVAL not set")
    assert not out["logs"].has("Using Altitude Filters")
    assert out["status_before_feed"] == [
        ("set", (), {"feed": url}),
        ("write", (), {"force": True}),
    ]
    assert out["heartbeat"] == ["started", "cancelled"]


def test_run_uses_configured_poll_interval_known_craft_and_alt_filters(tmp_path):
    csv_path = _known_craft_csv(tmp_path)
    out = _run(
        FEED_URL="file:///tmp/aircraft.json",
        POLL_INTERVAL="7",
        KNOWN_CRAFT=csv_path,
        ALT_UPPER="40000",
        ALT_LOWER="100",
    )

    assert out["feed_calls"][0][2] == "7"
    assert not out["logs"].has("POLL_INTERVAL not set")
    # Loaded once, by __init__; run() only warns about a missing file.
    assert not out["logs"].has("KNOWN_CRAFT file not found")
    assert out["logs"].has("Using Altitude Filters: Upper = 40000, Lower = 100")
    assert "A9EE47" in out["worker"].known_craft_db["hex_index"]


def test_run_cancels_heartbeat_when_feed_raises():
    out = _run(raise_exc=_Stop(), FEED_URL="http://127.0.0.1:9/")
    assert len(out["feed_calls"]) == 1
    assert out["heartbeat"] == ["started", "cancelled"]


# --------------------------------------------------------------------------
# _run_feed: http and file branches
# --------------------------------------------------------------------------


def _poll_sleep(delays, interval):
    """A sleep that stops the poll loop; other sleeps (aiohttp's) are real."""

    async def _sleep(delay, *args, **kwargs):
        if delay == interval:
            delays.append(delay)
            raise _Stop
        return await _REAL_SLEEP(delay, *args, **kwargs)

    return _sleep


def test_run_feed_http_polls_live_server_then_sleeps():
    seen = []
    delays = []

    async def _main():
        server = TestServer(_make_app(seen), host="127.0.0.1", port=0)
        await server.start_server()
        try:
            url = str(server.make_url("/aircraft"))
            worker = _build_worker(FEED_URL=url)
            handled = _record_handle_data(worker)
            with patch("adsbcot.classes.asyncio.sleep", _poll_sleep(delays, 11)):
                with pytest.raises(_Stop):
                    await worker._run_feed(url, urlparse(url), "11")
            # The session is the worker's own, and closed on the way out.
            assert worker.session is not None and worker.session.closed
            return handled
        finally:
            await server.close()

    handled = asyncio.run(_main())
    assert handled == [[CRAFT]]
    assert [s["path"] for s in seen] == ["/aircraft"]
    assert delays == [11]


def test_run_feed_file_polls_without_asyncinotify(tmp_path):
    path = tmp_path / "aircraft.json"
    path.write_text(json.dumps({"aircraft": [CRAFT]}), encoding="utf-8")
    url = "file://" + str(path)
    delays = []
    looked_up = []

    def _find_spec(name, *args, **kwargs):
        looked_up.append(name)
        return None

    async def _main():
        worker = _build_worker(FEED_URL=url)
        handled = _record_handle_data(worker)
        logs = _Logs(worker._logger)
        try:
            with patch.object(
                classes.importlib.util, "find_spec", _find_spec
            ), patch("adsbcot.classes.asyncio.sleep", _poll_sleep(delays, 2)):
                with pytest.raises(_Stop):
                    await worker._run_feed(url, urlparse(url), "2")
        finally:
            logs.close()
        return handled, logs

    handled, logs = asyncio.run(_main())
    assert looked_up == ["asyncinotify"]
    assert handled == [[CRAFT]]
    assert delays == [2]
    assert logs.has("asyncinotify not installed, using file polling.")


def _inotify_with_ready(ready):
    """An Inotify subclass that signals once its watch is in place."""

    class _Inotify(classes.Inotify):
        def add_watch(self, *args, **kwargs):
            watch = super().add_watch(*args, **kwargs)
            ready.set()
            return watch

    return _Inotify


@needs_inotify
def test_run_feed_file_inotify_reads_watched_file_only(tmp_path):
    path = tmp_path / "aircraft.json"
    url = "file://" + str(path)
    assert importlib.util.find_spec("asyncinotify") is not None

    async def _main():
        ready = asyncio.Event()
        worker = _build_worker(FEED_URL=url)
        handled = _record_handle_data(worker, stop_after=1)
        with patch.object(classes, "Inotify", _inotify_with_ready(ready)):
            task = asyncio.ensure_future(worker._run_feed(url, urlparse(url), "1"))
            await asyncio.wait_for(ready.wait(), 2)
            # A sibling file in the watched directory must not be read as the
            # feed: if it were, get_file_feed would open the not-yet-existing
            # feed file and raise FileNotFoundError instead of _Stop.
            (tmp_path / "other.json").write_text("{}", encoding="utf-8")
            # Atomic replace, as dump1090 does: one MOVED_TO, full content.
            tmp = tmp_path / "aircraft.json.tmp"
            tmp.write_text(json.dumps({"aircraft": [CRAFT]}), encoding="utf-8")
            os.replace(str(tmp), str(path))
            with pytest.raises(_Stop):
                await asyncio.wait_for(task, 2)
        return handled

    handled = asyncio.run(_main())
    assert handled == [[CRAFT]]


@needs_inotify
def test_run_feed_file_inotify_raises_when_watch_removed(tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    path = feed_dir / "aircraft.json"
    url = "file://" + str(path)

    async def _main():
        ready = asyncio.Event()
        worker = _build_worker(FEED_URL=url)
        handled = _record_handle_data(worker)
        with patch.object(classes, "Inotify", _inotify_with_ready(ready)):
            task = asyncio.ensure_future(worker._run_feed(url, urlparse(url), "1"))
            await asyncio.wait_for(ready.wait(), 2)
            os.rmdir(str(feed_dir))
            with pytest.raises(RuntimeError, match="inotify watch was removed"):
                await asyncio.wait_for(task, 2)
        return handled

    assert asyncio.run(_main()) == []
