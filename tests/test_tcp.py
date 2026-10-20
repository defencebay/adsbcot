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

"""Tests for TCP (raw / Beast) feeds and their pyModeS dependency."""

import asyncio
import configparser
import pathlib
import re

from configparser import ConfigParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import adsbcot
import adsbcot.functions
from adsbcot.classes import ADSBNetReceiver


class _Stop(Exception):
    """Raised by a test double to break out of an endless worker loop."""


def _config(feed_url):
    parser = ConfigParser()
    parser.read_dict({"DEFAULT": {"FEED_URL": feed_url}})
    return parser["DEFAULT"]


def test_default_raw_port_is_dump1090_raw_output():
    """dump1090 serves AVR raw frames on 30002; 30003 is BaseStation/SBS CSV,
    which the pyModeS raw reader cannot parse."""
    assert adsbcot.DEFAULT_TCP_RAW_PORT == 30002
    assert adsbcot.DEFAULT_TCP_BEAST_PORT == 30005


def test_pymodes_extra_excludes_v3():
    """pyModeS 3 removed pyModeS.streamer; < 2.18 has a streamer that cannot
    unpack its own nuc_p()/nuc_v() results (#22, #33)."""
    setup_cfg = pathlib.Path(__file__).resolve().parent.parent / "setup.cfg"
    parser = configparser.ConfigParser()
    parser.read(setup_cfg)
    requirement = parser["options.extras_require"]["with_pymodes"]
    lower = re.search(r">=\s*2\.(\d+)", requirement)
    assert lower and int(lower.group(1)) >= 18, requirement
    assert re.search(r"<\s*3", requirement), requirement


@pytest.mark.parametrize("reason", ["missing", "v3"])
def test_create_tasks_explains_unusable_pymodes(reason):
    """A tcp FEED_URL without a usable pyModeS used to raise a bare ValueError
    (#30, #23) -- or, with pyModeS 3, start a worker that died on NameError."""
    clitool = SimpleNamespace(tx_queue=object())
    config = {"FEED_URL": "tcp+beast://127.0.0.1:30005", "SENSOR_BEACON": "0"}

    def _fake_import(name):
        if reason == "missing":
            raise ModuleNotFoundError(f"No module named {name!r}")
        raise ImportError("pyModeS.streamer is part of the pyModeS v2 API")

    with patch.object(adsbcot.functions.importlib, "import_module", _fake_import):
        with pytest.raises(ValueError, match=r"pymodes"):
            adsbcot.functions.create_tasks(config, clitool)


def _create_tasks(config, clitool):
    """create_tasks() builds an asyncio.Queue: on Python 3.9 that needs a loop,
    as it has in production, where pytak calls it from its main coroutine."""

    async def _main():
        return adsbcot.functions.create_tasks(config, clitool)

    return asyncio.run(_main())


def test_create_tasks_tcp_creates_receiver_and_decoder():
    clitool = SimpleNamespace(tx_queue=object())
    config = {"FEED_URL": "tcp+beast://127.0.0.1:30005", "SENSOR_BEACON": "0"}
    with patch.object(
        adsbcot.functions, "_pymodes_error", return_value=None
    ), patch.object(adsbcot, "ADSBNetReceiver", return_value="rx"), patch.object(
        adsbcot, "ADSBNetWorker", return_value="decoder"
    ) as net_worker:
        tasks = _create_tasks(config, clitool)
    assert tasks == {"rx", "decoder"}
    assert net_worker.call_args.args[-1] == "beast"


def test_create_tasks_plain_tcp_is_raw():
    clitool = SimpleNamespace(tx_queue=object())
    config = {"FEED_URL": "tcp://127.0.0.1:30002", "SENSOR_BEACON": "0"}
    with patch.object(
        adsbcot.functions, "_pymodes_error", return_value=None
    ), patch.object(
        adsbcot, "ADSBNetReceiver", return_value="rx"
    ) as receiver, patch.object(
        adsbcot, "ADSBNetWorker", return_value="decoder"
    ):
        _create_tasks(config, clitool)
    assert receiver.call_args.args[-1] == "raw"


def _connect_target(feed_url, data_type):
    """Return the (host, port) ADSBNetReceiver tries to connect to."""
    seen = {}

    async def _open_connection(host, port):
        seen["target"] = (host, port)
        raise _Stop

    async def _main():
        receiver = ADSBNetReceiver(asyncio.Queue(), _config(feed_url), data_type)
        with patch("asyncio.open_connection", _open_connection):
            with pytest.raises(_Stop):
                await receiver.run()

    asyncio.run(_main())
    return seen["target"]


@pytest.mark.parametrize(
    "feed_url,data_type,target",
    [
        ("tcp+raw://10.1.2.3:31002", "raw", ("10.1.2.3", 31002)),
        ("tcp+raw://10.1.2.3", "raw", ("10.1.2.3", 30002)),
        ("tcp+beast://10.1.2.3", "beast", ("10.1.2.3", 30005)),
        ("tcp+beast://[::1]:30005", "beast", ("::1", 30005)),
        ("tcp+beast://[fe80::1]", "beast", ("fe80::1", 30005)),
    ],
)
def test_receiver_connect_target(feed_url, data_type, target):
    assert _connect_target(feed_url, data_type) == target


def test_receiver_rejects_unknown_type_without_port():
    async def _main():
        receiver = ADSBNetReceiver(asyncio.Queue(), _config("tcp+sbs://h"), "sbs")
        with pytest.raises(ValueError, match="sbs"):
            await receiver.run()

    asyncio.run(_main())


class _EofReader:
    """A StreamReader whose peer sent `chunks` and then closed."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def _next(self):
        return self.chunks.pop(0) if self.chunks else b""

    async def readline(self):
        return await self._next()

    async def read(self, _n=-1):
        return await self._next()


class _Writer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _GuardQueue:
    """Records puts; aborts if the receiver busy-loops on an empty read."""

    def __init__(self):
        self.items = []
        self.empty_puts = 0

    def put_nowait(self, item):
        if not item:
            self.empty_puts += 1
            if self.empty_puts > 3:
                raise AssertionError("receiver is spinning on EOF")
        self.items.append(item)


@pytest.mark.parametrize("data_type", ["raw", "beast"])
def test_receiver_reconnects_after_eof_instead_of_spinning(data_type):
    """At EOF readline()/read() return b"" immediately, without yielding to the
    event loop. The receiver used to put b"" on the queue forever, starving
    every other task -- including the one that sends CoT."""
    connects = []
    writers = []
    sleeps = []

    async def _open_connection(host, port):
        connects.append((host, port))
        if len(connects) > 2:
            raise _Stop
        writers.append(_Writer())
        return _EofReader([b"*8D4840D6202CC371C32CE0576098;\n"]), writers[-1]

    async def _sleep(delay):
        sleeps.append(delay)

    async def _main():
        queue = _GuardQueue()
        receiver = ADSBNetReceiver(queue, _config(f"tcp+{data_type}://h:1"), data_type)
        with patch("asyncio.open_connection", _open_connection), patch(
            "adsbcot.classes.asyncio.sleep", _sleep
        ):
            with pytest.raises(_Stop):
                await receiver.run()
        return queue

    queue = asyncio.run(_main())
    assert queue.empty_puts == 0
    assert queue.items == [b"*8D4840D6202CC371C32CE0576098;\n"] * 2
    assert len(connects) == 3
    assert all(writer.closed for writer in writers)
    assert len(sleeps) == 2 and all(delay > 0 for delay in sleeps)


def test_receiver_retries_when_connection_refused():
    attempts = []

    async def _open_connection(host, port):
        attempts.append((host, port))
        if len(attempts) > 1:
            raise _Stop
        raise ConnectionRefusedError

    async def _sleep(delay):
        return None

    async def _main():
        receiver = ADSBNetReceiver(asyncio.Queue(), _config("tcp+raw://h:1"), "raw")
        with patch("asyncio.open_connection", _open_connection), patch(
            "adsbcot.classes.asyncio.sleep", _sleep
        ):
            with pytest.raises(_Stop):
                await receiver.run()

    asyncio.run(_main())
    assert len(attempts) == 2


class _FailingReader:
    """A reader whose first read raises `exc`."""

    def __init__(self, exc):
        self.exc = exc

    async def readline(self):
        raise self.exc

    async def read(self, _n=-1):
        raise self.exc


@pytest.mark.parametrize(
    "data_type,exc",
    [
        ("beast", ConnectionResetError(104, "Connection reset by peer")),
        ("raw", ConnectionResetError(104, "Connection reset by peer")),
        # readline() on a stream with no newline in 64 KiB, e.g. tcp+raw
        # pointed at a Beast port.
        ("raw", ValueError("Separator is not found, and chunk exceed the limit")),
    ],
)
def test_receiver_reconnects_after_read_error(data_type, exc):
    """A reset (dump1090 killed, network drop) ended the task and with it the
    whole gateway, as pytak stops on the first task exception."""
    connects = []
    writers = []

    async def _open_connection(host, port):
        connects.append((host, port))
        if len(connects) > 1:
            raise _Stop
        writers.append(_Writer())
        return _FailingReader(exc), writers[-1]

    async def _sleep(_delay):
        return None

    async def _main():
        receiver = ADSBNetReceiver(
            asyncio.Queue(), _config(f"tcp+{data_type}://h:1"), data_type
        )
        with patch("asyncio.open_connection", _open_connection), patch(
            "adsbcot.classes.asyncio.sleep", _sleep
        ):
            with pytest.raises(_Stop):
                await receiver.run()

    asyncio.run(_main())
    assert len(connects) == 2
    assert writers[0].closed


@pytest.mark.parametrize(
    "url", ["tcp+avr://h:30002", "tcp+sbs://h:30003", "tcp+a+b://h"]
)
def test_create_tasks_rejects_unknown_tcp_type(url):
    """pyModeS calls os._exit(1) for an unknown type; fail with a message."""
    clitool = SimpleNamespace(tx_queue=object())
    with patch.object(adsbcot.functions, "_pymodes_error", return_value=None):
        with pytest.raises(ValueError, match="raw, beast or skysense"):
            _create_tasks({"FEED_URL": url, "SENSOR_BEACON": "0"}, clitool)


@pytest.mark.parametrize("version", ["2.8", "2.15", "2.17"])
def test_pymodes_before_2_18_is_rejected(version):
    with patch.object(adsbcot.functions.importlib, "import_module"), patch.object(
        adsbcot.functions, "_pymodes_version", return_value=version
    ):
        assert "2.18" in adsbcot.functions._pymodes_error()


@pytest.mark.parametrize("version", ["2.18", "2.22.0", None])
def test_pymodes_2_18_or_unknown_version_is_accepted(version):
    with patch.object(adsbcot.functions.importlib, "import_module"), patch.object(
        adsbcot.functions, "_pymodes_version", return_value=version
    ):
        assert adsbcot.functions._pymodes_error() is None


def test_pymodes_version_unknown_without_metadata():
    def _missing(_name):
        raise adsbcot.functions.importlib.metadata.PackageNotFoundError

    with patch.object(adsbcot.functions.importlib.metadata, "version", _missing):
        assert adsbcot.functions._pymodes_version() is None


def test_pymodes_version_reads_metadata():
    with patch.object(
        adsbcot.functions.importlib.metadata, "version", return_value="2.22.0"
    ):
        assert adsbcot.functions._pymodes_version() == "2.22.0"
