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

"""Tests for SensorWorker, the legacy xFileWatcher, and package odds and ends.

gpsd is optional and usually absent, so the gpsd path is driven by swapping
adsbcot.classes._gpsd for a fake module object.
"""

import asyncio
import importlib.metadata
import os
import xml.etree.ElementTree as ET

from configparser import ConfigParser
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import aiohttp.web
import pytest

import adsbcot
import adsbcot.classes
import adsbcot.commands
from adsbcot.classes import SensorWorker, _NoStatus, make_status, xFileWatcher

_real_sleep = asyncio.sleep

CRAFT = {
    "hex": "a9ee47",
    "flight": "N739UL  ",
    "alt_baro": 3700,
    "alt_geom": 3750,
    "lat": 37.836449,
    "lon": -122.030281,
}


class _Stop(Exception):
    """Raised by a test double to break out of an endless worker loop."""


def _config(**overrides):
    parser = ConfigParser()
    parser.read_dict(
        {
            "DEFAULT": {
                "INCLUDE_TISB": "false",
                "TISB_ONLY": "false",
                "INCLUDE_ALL_CRAFT": "false",
                **overrides,
            }
        }
    )
    return parser["DEFAULT"]


def _stop_on_sleep(delay_to_stop, sleeps):
    """Fake asyncio.sleep: record, raise _Stop at the worker's period.

    Patching adsbcot.classes.asyncio.sleep patches asyncio.sleep for every
    module, so any other delay (aiohttp internals) is passed through.
    """

    async def _sleep(delay, *args, **kwargs):
        if delay == delay_to_stop:
            sleeps.append(delay)
            raise _Stop
        return await _real_sleep(delay, *args, **kwargs)

    return _sleep


def _drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


# --------------------------------------------------------------------------
# SensorWorker


class _Packet:
    def __init__(self, mode=3, position=(1.5, 2.5), altitude=123.0, error=None):
        self.mode = mode
        self._position = position
        self._altitude = altitude
        if error is not None:
            self.error = error

    def position(self):
        if isinstance(self._position, Exception):
            raise self._position
        return self._position

    def altitude(self):
        if isinstance(self._altitude, Exception):
            raise self._altitude
        return self._altitude


def _fake_gpsd(packet=None, connect_exc=None):
    calls = []

    def _connect():
        calls.append("connect")
        if connect_exc is not None:
            raise connect_exc

    def _get_current():
        calls.append("get_current")
        return packet

    return SimpleNamespace(connect=_connect, get_current=_get_current, calls=calls)


def _get_position(config, gpsd=None):
    async def _main():
        worker = SensorWorker(asyncio.Queue(), config)
        with patch.object(adsbcot.classes, "_gpsd", gpsd):
            return await worker._get_position()

    return asyncio.run(_main())


def test_sensor_run_puts_beacon_cot_then_sleeps_period():
    sleeps = []
    config = _config(
        SENSOR_ID="test01",
        SENSOR_LAT="37.5",
        SENSOR_LON="-122.25",
        SENSOR_HAE="10",
        SENSOR_KEEPALIVE_PERIOD="7",
    )

    async def _main():
        queue: asyncio.Queue = asyncio.Queue()
        worker = SensorWorker(queue, config)
        with patch.object(adsbcot.classes, "_gpsd", None), patch(
            "adsbcot.classes.asyncio.sleep", _stop_on_sleep(7, sleeps)
        ):
            with pytest.raises(_Stop):
                await worker.run()
        return _drain(queue)

    items = asyncio.run(_main())
    assert sleeps == [7]
    assert len(items) == 1
    assert isinstance(items[0], bytes)
    event = ET.fromstring(items[0])
    assert event.tag == "event"
    assert event.get("uid") == "SENSOR.test01"
    assert event.get("type") == adsbcot.DEFAULT_SENSOR_COT_TYPE
    point = event.find("point")
    assert float(point.get("lat")) == 37.5
    assert float(point.get("lon")) == -122.25
    assert float(point.get("hae")) == 10.0
    assert event.find("detail/sensor").get("sensor_id") == "test01"


def test_sensor_run_skips_put_when_no_cot():
    sleeps = []

    async def _main():
        queue: asyncio.Queue = asyncio.Queue()
        worker = SensorWorker(queue, _config())
        with patch.object(adsbcot.classes, "_gpsd", None), patch.object(
            adsbcot, "gen_sensor_cot", return_value=None
        ), patch(
            "adsbcot.classes.asyncio.sleep",
            _stop_on_sleep(adsbcot.DEFAULT_SENSOR_KEEPALIVE_PERIOD, sleeps),
        ):
            with pytest.raises(_Stop):
                await worker.run()
        return _drain(queue)

    assert asyncio.run(_main()) == []
    assert sleeps == [adsbcot.DEFAULT_SENSOR_KEEPALIVE_PERIOD]


def test_get_position_uses_config_without_gpsd():
    config = _config(SENSOR_LAT="12.5", SENSOR_LON="-45.25", SENSOR_HAE="99")
    assert _get_position(config) == (12.5, -45.25, 99.0, "9999999.0", "9999999.0")


@pytest.mark.parametrize("value", [None, ""])
def test_get_position_defaults(value):
    overrides = {}
    if value is not None:
        overrides = {"SENSOR_LAT": value, "SENSOR_LON": value, "SENSOR_HAE": value}
    lat, lon, hae, ce, le = _get_position(_config(**overrides))
    assert (lat, lon, hae) == (
        adsbcot.DEFAULT_SENSOR_LAT,
        adsbcot.DEFAULT_SENSOR_LON,
        adsbcot.DEFAULT_SENSOR_HAE,
    )
    assert isinstance(lat, float) and isinstance(hae, float)
    assert (ce, le) == ("9999999.0", "9999999.0")


def test_get_position_prefers_gpsd_fix():
    gpsd = _fake_gpsd(_Packet(error={"x": 4.5, "v": 7.25}))
    result = _get_position(_config(SENSOR_LAT="9", SENSOR_LON="9"), gpsd)
    assert result == (1.5, 2.5, 123.0, "4.5", "7.25")
    assert gpsd.calls == ["connect", "get_current"]


@pytest.mark.parametrize("mode", [0, 1])
def test_get_position_no_gps_fix_falls_back_to_config(mode):
    gpsd = _fake_gpsd(_Packet(mode=mode))
    result = _get_position(_config(SENSOR_LAT="3", SENSOR_LON="4"), gpsd)
    assert result == (3.0, 4.0, 0.0, "9999999.0", "9999999.0")


def test_get_position_gpsd_error_falls_back_to_config():
    gpsd = _fake_gpsd(connect_exc=ConnectionRefusedError("no gpsd"))
    result = _get_position(_config(SENSOR_LAT="3", SENSOR_LON="4"), gpsd)
    assert result == (3.0, 4.0, 0.0, "9999999.0", "9999999.0")
    assert gpsd.calls == ["connect"]


def _poll(packet):
    with patch.object(adsbcot.classes, "_gpsd", _fake_gpsd(packet)):
        return SensorWorker._poll_gpsd()


def test_poll_gpsd_mode_below_2d_is_none():
    assert _poll(_Packet(mode=1)) is None


def test_poll_gpsd_2d_fix_is_accepted():
    assert _poll(_Packet(mode=2))[:3] == (1.5, 2.5, 123.0)


def test_poll_gpsd_position_error_is_none():
    assert _poll(_Packet(position=RuntimeError("no position"))) is None


def test_poll_gpsd_altitude_error_is_zero():
    lat, lon, hae, _, _ = _poll(_Packet(altitude=RuntimeError("2D only")))
    assert (lat, lon, hae) == (1.5, 2.5, 0.0)


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, ("9999999.0", "9999999.0")),  # packet has no `error` attribute
        ({}, ("9999999.0", "9999999.0")),
        ({"x": 3.0, "v": 8.0}, ("3.0", "8.0")),
        ({"x": 0, "v": None}, ("9999999.0", "9999999.0")),  # falsy -> unknown
        ({"x": 2.5}, ("2.5", "9999999.0")),
    ],
)
def test_poll_gpsd_error_estimates(error, expected):
    assert _poll(_Packet(error=error))[3:] == expected


# --------------------------------------------------------------------------
# xFileWatcher: __init__ and handle_data


def _known_craft_csv(tmp_path):
    path = tmp_path / "known_craft.csv"
    path.write_text("HEX,REG,COT,CALLSIGN\nA9EE47,N739UL,a-f-A-C-F,KNOWN1\n")
    return str(path)


def _handle(data, tmp_path=None, **overrides):
    async def _main():
        queue: asyncio.Queue = asyncio.Queue()
        if tmp_path is not None:
            overrides["KNOWN_CRAFT"] = _known_craft_csv(tmp_path)
        watcher = xFileWatcher(queue, _config(**overrides))
        await watcher.handle_data(data)
        return _drain(queue)

    return asyncio.run(_main())


def test_xfilewatcher_init_defaults():
    async def _main():
        watcher = xFileWatcher(asyncio.Queue(), _config())
        assert watcher.known_craft_db is None
        assert watcher.session is None
        assert watcher.uid_key == "ICAO"

    asyncio.run(_main())


def test_xfilewatcher_init_reads_known_craft(tmp_path):
    async def _main():
        return xFileWatcher(
            asyncio.Queue(),
            _config(KNOWN_CRAFT=_known_craft_csv(tmp_path), UID_KEY="REG"),
        )

    watcher = asyncio.run(_main())
    assert watcher.uid_key == "REG"
    assert watcher.known_craft_db["hex_index"]["A9EE47"]["REG"] == "N739UL"


@pytest.mark.parametrize(
    "data",
    [
        {"hex": "a9ee47", "lat": 1.0, "lon": 2.0},  # dict, not list
        "aircraft",
        None,
        [],
        ["not a dict", 42, None],
        [{"flight": "NOHEX", "lat": 1.0, "lon": 2.0}],
        [{"hex": "   ", "lat": 1.0, "lon": 2.0}],  # whitespace-only hex
    ],
    ids=["dict", "str", "none", "empty", "non-dict", "no-hex", "blank-hex"],
)
def test_xfilewatcher_handle_data_ignores_bad_input(data):
    assert _handle(data, INCLUDE_ALL_CRAFT="true") == []


def test_xfilewatcher_handle_data_success_puts_cot():
    items = _handle([dict(CRAFT)], INCLUDE_ALL_CRAFT="true")
    assert len(items) == 1
    event = ET.fromstring(items[0])
    assert event.get("uid") == "ICAO-A9EE47"
    assert float(event.find("point").get("lat")) == pytest.approx(37.836449, abs=1e-3)


def test_xfilewatcher_handle_data_skips_bad_items_but_keeps_good():
    items = _handle(
        ["junk", {"lat": 1.0}, dict(CRAFT), {"hex": "abcdef"}],
        INCLUDE_ALL_CRAFT="true",
    )
    assert len(items) == 1


def test_xfilewatcher_tisb_excluded_by_default():
    assert _handle([dict(CRAFT, hex="~a9ee47")], INCLUDE_ALL_CRAFT="true") == []


def test_xfilewatcher_tisb_included_when_enabled():
    items = _handle(
        [dict(CRAFT, hex="~a9ee47")], INCLUDE_ALL_CRAFT="true", INCLUDE_TISB="true"
    )
    assert len(items) == 1


def test_xfilewatcher_tisb_only_drops_non_tisb():
    items = _handle(
        [dict(CRAFT), dict(CRAFT, hex="~a9ee48")],
        INCLUDE_ALL_CRAFT="true",
        INCLUDE_TISB="true",
        TISB_ONLY="true",
    )
    assert len(items) == 1
    assert "A9EE48" in ET.fromstring(items[0]).get("uid")


def test_xfilewatcher_unknown_craft_skipped_with_known_db(tmp_path):
    items = _handle([dict(CRAFT, hex="abcdef"), dict(CRAFT)], tmp_path)
    assert len(items) == 1
    event = ET.fromstring(items[0])
    # The known craft's CoT type / callsign override the defaults.
    assert event.get("type") == "a-f-A-C-F"


def test_xfilewatcher_unknown_craft_kept_with_include_all(tmp_path):
    items = _handle(
        [dict(CRAFT, hex="abcdef"), dict(CRAFT)], tmp_path, INCLUDE_ALL_CRAFT="true"
    )
    assert len(items) == 2


def test_xfilewatcher_empty_event_is_skipped():
    craft = {"hex": "a9ee47", "flight": "NOPOS"}  # no lat/lon -> no CoT
    assert _handle([craft], INCLUDE_ALL_CRAFT="true") == []


# --------------------------------------------------------------------------
# xFileWatcher: get_feed and run against a real local HTTP server


def _app(hits):
    async def _ok(request):
        hits.append(request.path)
        return aiohttp.web.json_response({"aircraft": [CRAFT]})

    async def _ac(request):
        hits.append(request.path)
        return aiohttp.web.json_response({"ac": [CRAFT, dict(CRAFT, hex="abcdef")]})

    async def _err(request):
        hits.append(request.path)
        return aiohttp.web.Response(status=500, text="boom")

    async def _null(request):
        hits.append(request.path)
        return aiohttp.web.Response(text="null", content_type="application/json")

    async def _none(request):
        hits.append(request.path)
        return aiohttp.web.json_response({"now": 1, "messages": 2})

    app = aiohttp.web.Application()
    app.router.add_get("/ok", _ok)
    app.router.add_get("/ac", _ac)
    app.router.add_get("/err", _err)
    app.router.add_get("/null", _null)
    app.router.add_get("/none", _none)
    return app


async def _serve(hits):
    runner = aiohttp.web.AppRunner(_app(hits))
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, "http://127.0.0.1:%d" % port


def test_xfilewatcher_get_feed_without_session_is_noop():
    async def _main():
        queue: asyncio.Queue = asyncio.Queue()
        watcher = xFileWatcher(queue, _config(INCLUDE_ALL_CRAFT="true"))
        await watcher.get_feed("http://127.0.0.1:9/never")
        return _drain(queue)

    assert asyncio.run(_main()) == []


@pytest.mark.parametrize(
    "path,expected", [("/ok", 1), ("/ac", 2), ("/err", 0), ("/null", 0), ("/none", 0)]
)
def test_xfilewatcher_get_feed(path, expected):
    hits = []
    handled = []

    async def _main():
        runner, base = await _serve(hits)
        try:
            queue: asyncio.Queue = asyncio.Queue()
            watcher = xFileWatcher(queue, _config(INCLUDE_ALL_CRAFT="true"))
            real_handle = watcher.handle_data

            async def _spy(data):
                handled.append(data)
                await real_handle(data)

            watcher.handle_data = _spy
            async with aiohttp.ClientSession() as watcher.session:
                await watcher.get_feed(base + path)
            return _drain(queue)
        finally:
            await runner.cleanup()

    items = asyncio.run(_main())
    assert hits == [path]
    assert len(items) == expected
    assert len(handled) == (1 if expected else 0)


def test_xfilewatcher_run_requires_feed_url():
    async def _main():
        watcher = xFileWatcher(asyncio.Queue(), _config(FEED_URL=""))
        with pytest.raises(ValueError, match="FEED_URL"):
            await watcher.run()

    asyncio.run(_main())


def test_xfilewatcher_run_polls_then_sleeps(tmp_path):
    hits = []
    sleeps = []

    async def _main():
        runner, base = await _serve(hits)
        try:
            queue: asyncio.Queue = asyncio.Queue()
            watcher = xFileWatcher(
                queue,
                _config(FEED_URL=base + "/ok", POLL_INTERVAL="3"),
            )
            # KNOWN_CRAFT is read in run(), not only in __init__.
            watcher.config["KNOWN_CRAFT"] = _known_craft_csv(tmp_path)
            with patch("adsbcot.classes.asyncio.sleep", _stop_on_sleep(3, sleeps)):
                with pytest.raises(_Stop):
                    await watcher.run()
            assert watcher.known_craft_db["hex_index"]["A9EE47"]["REG"] == "N739UL"
            assert watcher.session.closed
            return _drain(queue)
        finally:
            await runner.cleanup()

    items = asyncio.run(_main())
    assert hits == ["/ok"]
    assert sleeps == [3]
    assert len(items) == 1


# --------------------------------------------------------------------------
# make_status / _NoStatus


def test_nostatus_methods_are_noops():
    status = _NoStatus()
    assert status.count("rx") is None
    assert status.record(icao="A", placed=True) is None
    assert status.set(tracked=3) is None
    assert status.write() is False
    assert status.write(force=True) is False


def test_make_status_without_statuswriter_degrades():
    with patch.object(adsbcot.classes, "_StatusWriter", None):
        assert isinstance(make_status("adsbcot", "1.0"), _NoStatus)


def test_make_status_uses_statuswriter():
    calls = []

    def _writer(app_name, version=None):
        calls.append((app_name, version))
        return "writer"

    with patch.object(adsbcot.classes, "_StatusWriter", _writer):
        assert make_status("adsbcot", "1.2.3") == "writer"
    assert calls == [("adsbcot", "1.2.3")]


# --------------------------------------------------------------------------
# adsbcot._read_version


def _no_version_file(*args, **kwargs):
    raise FileNotFoundError("VERSION")


def test_read_version_prefers_version_file():
    path = os.path.join(os.path.dirname(adsbcot.__file__), "VERSION")
    if not os.path.exists(path):
        pytest.skip("no VERSION file in this build")
    with open(path, encoding="utf-8") as version_fd:
        expected = version_fd.read().strip()
    assert adsbcot._read_version() == expected


def test_read_version_falls_back_to_metadata():
    with patch.object(adsbcot, "open", _no_version_file, create=True), patch.object(
        importlib.metadata, "version", return_value="9.8.7"
    ) as version:
        assert adsbcot._read_version() == "9.8.7"
    version.assert_called_once_with("adsbcot")
    assert "open" not in vars(adsbcot)  # patch restored builtins.open lookup


def test_read_version_unknown_when_metadata_fails():
    def _missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    with patch.object(adsbcot, "open", _no_version_file, create=True), patch.object(
        importlib.metadata, "version", _missing
    ):
        assert adsbcot._read_version() == "unknown"


# --------------------------------------------------------------------------
# adsbcot.commands


def test_commands_main_runs_pytak_cli():
    with patch.object(adsbcot.commands.pytak, "cli") as cli:
        adsbcot.commands.main()
    cli.assert_called_once_with("adsbcot")
