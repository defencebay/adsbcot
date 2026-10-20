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

"""Regression tests for defects found while raising test coverage."""

import asyncio

from configparser import ConfigParser
from unittest.mock import patch

import pytest

import adsbcot
from adsbcot.classes import ADSBWorker

BASE = {"hex": "a9ee47", "lat": 37.8, "lon": -122.0, "alt_geom": 3750}


def _xml(craft, config=None, known_craft=None):
    return adsbcot.functions.adsb_to_cot_xml(dict(craft), config or {}, known_craft)


def test_uid_key_reg_is_honoured():
    """The FLIGHT/ICAO block after the REG check always overwrote REG-<reg>."""
    event = _xml({**BASE, "r": "N739UL"}, {"UID_KEY": "REG"})
    assert event.attrib["uid"] == "REG-N739UL"


def test_uid_key_reg_without_hex():
    event = _xml({"lat": 1.0, "lon": 2.0, "r": "N739UL"}, {"UID_KEY": "REG"})
    assert event is not None
    assert event.attrib["uid"] == "REG-N739UL"


def test_uid_key_reg_without_reg_falls_back_to_icao():
    assert _xml(BASE, {"UID_KEY": "REG"}).attrib["uid"] == "ICAO-A9EE47"


def test_flight_without_hex_does_not_crash():
    """An empty ICAO reached aircot.adsb_to_cot_type, which int()s it."""
    event = _xml({"lat": 1.0, "lon": 2.0, "flight": "UAL1"})
    assert event is not None
    assert event.attrib["uid"] == "FLIGHT-UAL1"


def test_missing_type_is_not_rendered_as_zero():
    """The craft type defaulted to 0, which became the text "0": "Type:0" in
    every dump1090 remark and "<reg>-0" callsigns."""
    event = _xml({**BASE, "r": "N1"})
    assert event.find("detail/contact").attrib["callsign"] == "N1 (A9EE47)"
    assert "Type:0" not in event.find("detail/remarks").text
    assert event.find("detail/__adsb").get("craft_type") is None


@pytest.mark.parametrize("target_type", [3, 4])
def test_stratux_tisb_target_type_is_marked_unknown(target_type):
    """Stratux TargetType 3/4 is TIS-B. The int comparisons were made against a
    str, so TIS-B was never recognised."""
    event = _xml({**BASE, "TargetType": target_type})
    assert event.attrib["type"] == "a-u-A"
    assert "ADS-B Type: TIS-B" in event.find("detail/remarks").text


def test_stratux_adsb_target_type_keeps_normal_type():
    event = _xml({**BASE, "category": "A1", "TargetType": 1})
    assert event.attrib["type"] != "a-u-A"
    assert "ADS-B Type: ADS-B" in event.find("detail/remarks").text


def test_null_signal_level_falls_back_to_rssi():
    event = _xml({**BASE, "SignalLevel": None, "rssi": -5.5})
    assert event.find("detail/_radio").get("signal") == "-5.5"


def _config(**values):
    parser = ConfigParser()
    parser.read_dict({"DEFAULT": values})
    return parser["DEFAULT"]


def test_run_with_missing_known_craft_file_does_not_crash(tmp_path):
    """__init__ skipped a missing KNOWN_CRAFT file but run() loaded it anyway."""
    seen = {}

    async def _main():
        worker = ADSBWorker(
            asyncio.Queue(),
            _config(FEED_URL="file:///x.json", KNOWN_CRAFT=str(tmp_path / "no.csv")),
        )

        async def _run_feed(*args):
            seen["ran"] = True

        worker._run_feed = _run_feed
        await worker.run()
        return worker

    worker = asyncio.run(_main())
    assert seen == {"ran": True}
    assert worker.known_craft_db is None


def test_unknown_hex_is_not_matched_by_literal_hex_registration(tmp_path):
    """process_craft passed the string "HEX" as the registration to look up,
    so a KNOWN_CRAFT row with REG=HEX matched every unknown aircraft."""
    csv = tmp_path / "known.csv"
    csv.write_text("HEX,REG,CALLSIGN,COT\nABCDEF,HEX,DECOY,a-h-A\n", encoding="utf-8")
    queued = []

    async def _main():
        worker = ADSBWorker(
            asyncio.Queue(),
            _config(KNOWN_CRAFT=str(csv), INCLUDE_ALL_CRAFT="false"),
        )

        async def _put(event):
            queued.append(event)

        worker.put_queue = _put
        return await worker.process_craft(dict(BASE))

    with patch.object(adsbcot.classes, "make_status"):
        assert asyncio.run(_main()) is None
    assert queued == []


def test_zero_nac_v_is_not_replaced_by_nac_p():
    """nac_v 0 is a real value; only a missing nac_v falls back to nac_p."""
    event = _xml({**BASE, "nac_p": 9, "nac_v": 0})
    assert event.find("point").attrib["le"] == "12.5"
    event = _xml({**BASE, "nac_p": 9})
    assert event.find("point").attrib["le"] == "21.5"
