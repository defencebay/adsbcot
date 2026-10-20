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

"""Tests for the network (pyModeS) decoder path: ADSBNetWorker."""

import asyncio

from configparser import ConfigParser

import pytest

from adsbcot.classes import ADSBNetWorker

# _run_decoder uses pyModeS's df()/crc() even with a fake decoder.
pytest.importorskip("pyModeS.streamer.decode")

# A valid DF17 identification frame (CRC passes), from "The 1090MHz Riddle".
DF17_IDENT = "8D4840D6202CC371C32CE0576098"


class _Stop(Exception):
    """Raised by a test double to break out of the decoder's endless loop."""


class _FakeNetClient:
    """Stands in for pyModeS.streamer.source.NetSource."""

    def __init__(self, messages=None):
        self.buffer = []
        self.messages = [(DF17_IDENT, 0.0)] if messages is None else messages

    def read_raw_buffer(self):
        return self.messages

    read_beast_buffer = read_raw_buffer
    read_skysense_buffer = read_raw_buffer


class _FakeDecoder:
    """Stands in for pyModeS.streamer.decode.Decode."""

    def __init__(self, aircraft):
        self.aircraft = aircraft
        self.processed = []

    def process_raw(self, adsb_ts, adsb_msg, commb_ts, commb_msg):
        self.processed.append(list(adsb_msg))

    def get_aircraft(self):
        return self.aircraft


def _config(**overrides):
    parser = ConfigParser()
    parser.read_dict(
        {"DEFAULT": {"FEED_URL": "tcp+raw://127.0.0.1:30002", **overrides}}
    )
    return parser["DEFAULT"]


def _decode_once(aircraft, data_type="raw", messages=None):
    """Run ADSBNetWorker._run_decoder for one network read; return the batch."""
    handled = []

    async def _main():
        net_queue: asyncio.Queue = asyncio.Queue()
        worker = ADSBNetWorker(asyncio.Queue(), net_queue, _config(), data_type)

        async def _handle(data):
            handled.append(data)
            raise _Stop

        worker.handle_data = _handle
        net_queue.put_nowait(b"*8D...;\n")
        try:
            await asyncio.wait_for(
                worker._run_decoder(_FakeDecoder(aircraft), _FakeNetClient(messages)),
                timeout=2,
            )
        except _Stop:
            pass

    asyncio.run(_main())
    return handled


def test_none_callsign_falls_back_to_icao():
    """pyModeS sets "call" to None until it decodes an identification message.

    dict.get("call", key) only falls back when the key is MISSING, so this
    used to raise AttributeError: 'NoneType' object has no attribute 'replace'
    and kill the worker (#67, #33).
    """
    aircraft = {"A1B2C3": {"call": None, "lat": 37.8, "lon": -122.0, "alt": 3700}}

    handled = _decode_once(aircraft)

    assert handled == [
        [
            {
                "hex": "A1B2C3",
                "lat": 37.8,
                "lon": -122.0,
                "flight": "A1B2C3",
                "alt_geom": 3700,
                "gs": None,
                "reg": None,
                "trk": None,
            }
        ]
    ]


def test_missing_callsign_falls_back_to_icao():
    handled = _decode_once({"A1B2C3": {"lat": 37.8, "lon": -122.0}})
    assert handled[0][0]["flight"] == "A1B2C3"


def test_callsign_padding_is_stripped():
    handled = _decode_once({"A1B2C3": {"call": "UAL1234_", "lat": 1.0, "lon": 2.0}})
    assert handled[0][0]["flight"] == "UAL1234"
