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

"""Tests for ws:// and wss:// FEED_URLs."""

import asyncio
import json

from configparser import ConfigParser
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
import websockets
import websockets.exceptions

from adsbcot.classes import ADSBWorker


class _Stop(Exception):
    """Raised by a test double to break out of an endless worker loop."""


class _FakeSocket:
    def __init__(self, messages, error=None):
        self.messages = list(messages)
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.pop(0)
        if self.error is not None:
            raise self.error
        raise StopAsyncIteration


def _run(sessions):
    """Drive ADSBWorker over the ws:// path; each connect() pops a session."""
    connects = []
    handled = []

    def _connect(url):
        connects.append(url)
        if not sessions:
            raise _Stop
        return sessions.pop(0)

    async def _sleep(_delay):
        return None

    async def _main():
        parser = ConfigParser()
        parser.read_dict({"DEFAULT": {"FEED_URL": "ws://feed.example/ws"}})
        worker = ADSBWorker(asyncio.Queue(), parser["DEFAULT"])

        async def _handle(data):
            handled.append(data)

        worker.handle_data = _handle
        with patch.object(websockets, "connect", _connect), patch(
            "adsbcot.classes.asyncio.sleep", _sleep
        ):
            with pytest.raises(_Stop):
                url = "ws://feed.example/ws"
                await worker._run_feed(url, urlparse(url), 1)

    asyncio.run(_main())
    return connects, handled


def test_websocket_reconnects_after_error_close():
    """The worker logged "reconnecting..." and then returned, ending the task."""
    craft = {"hex": "a9ee47", "lat": 1.0, "lon": 2.0}
    closed = websockets.exceptions.ConnectionClosedError(None, None)
    connects, handled = _run(
        [_FakeSocket([json.dumps(craft)], closed), _FakeSocket(["", json.dumps(craft)])]
    )
    assert len(connects) == 3
    assert handled == [craft, craft]


def test_websocket_reconnects_after_connect_failure():
    class _Refused:
        async def __aenter__(self):
            raise ConnectionRefusedError

        async def __aexit__(self, *exc):
            return False

    connects, handled = _run([_Refused(), _FakeSocket([])])
    assert len(connects) == 3
    assert handled == []


class _Raises:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "exc",
    [
        websockets.exceptions.InvalidStatusCode(503, None),
        asyncio.TimeoutError(),
    ],
)
def test_websocket_retries_handshake_failures(exc):
    """A 5xx on the upgrade, or an open timeout (not an OSError before Python
    3.11), ended the worker."""
    connects, handled = _run([_Raises(exc), _FakeSocket([])])
    assert len(connects) == 3


def test_websocket_skips_a_bad_json_frame():
    craft = {"hex": "a9ee47", "lat": 1.0, "lon": 2.0}
    connects, handled = _run([_FakeSocket(["not json", json.dumps(craft)])])
    assert handled == [craft]
