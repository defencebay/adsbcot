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

"""Aircraft records with null, missing or oddly-typed fields.

Aggregator APIs and decoders emit JSON ``null`` for fields they have not
decoded yet. ``dict.get(key, default)`` returns that ``None`` rather than the
default, which used to crash the converter or leak the literal text "NONE"
into callsigns and remarks (#24).
"""

import pytest

import adsbcot

BASE = {"hex": "a9ee47", "lat": 37.836449, "lon": -122.030281, "alt_geom": 3750}


def _xml(craft, config=None):
    event = adsbcot.functions.adsb_to_cot_xml(dict(craft), config or {})
    assert event is not None
    return event


@pytest.mark.parametrize("field", ["flight", "reg", "r", "squawk", "category", "t"])
def test_null_text_fields_do_not_become_the_word_none(field):
    event = _xml({**BASE, field: None})
    callsign = event.find("detail/contact").attrib["callsign"]
    remarks = event.find("detail/remarks").text
    assert "NONE" not in callsign.upper()
    assert "NONE" not in remarks.upper()
    adsb = event.find("detail/__adsb").attrib
    for attr in ("flight", "reg", "squawk", "cat", "craft_type"):
        assert adsb.get(attr, "").upper() != "NONE"


def test_null_flight_uses_icao_for_callsign():
    event = _xml({**BASE, "flight": None})
    callsign = event.find("detail/contact").attrib["callsign"]
    assert "NONE" not in callsign.upper()
    assert callsign.endswith("(A9EE47)")
    assert event.find("detail/__adsb").get("flight") is None


@pytest.mark.parametrize("field", ["nac_p", "NACp", "nac_v", "NACv"])
def test_null_accuracy_fields_do_not_crash(field):
    event = _xml({**BASE, field: None})
    point = event.find("point")
    assert float(point.attrib["ce"]) > 0
    assert float(point.attrib["le"]) > 0


def test_string_altitude_is_still_filtered():
    """Some feeds deliver numbers as strings; the altitude filter compared
    str > int and raised TypeError."""
    assert (
        adsbcot.functions.adsb_to_cot_xml(
            {**BASE, "alt_geom": "40000"}, {"ALT_UPPER": "30000"}
        )
        is None
    )
    assert (
        adsbcot.functions.adsb_to_cot_xml(
            {**BASE, "alt_geom": "1000"}, {"ALT_LOWER": "2000"}
        )
        is None
    )
    assert (
        adsbcot.functions.adsb_to_cot_xml(
            {**BASE, "alt_geom": "3750"}, {"ALT_UPPER": "30000"}
        )
        is not None
    )


def test_unparseable_altitude_does_not_crash_the_filter():
    event = adsbcot.functions.adsb_to_cot_xml(
        {**BASE, "alt_geom": "ground"}, {"ALT_UPPER": "30000"}
    )
    assert event is not None


def test_null_position_is_dropped_not_crashed():
    assert adsbcot.functions.adsb_to_cot_xml({**BASE, "lat": None}) is None


def test_aircraft_description_is_passed_through():
    """airplanes.live / adsb.lol / readsb include a "desc" model field (#60)."""
    event = _xml({**BASE, "t": "A320", "desc": "AIRBUS A-320"})
    assert event.find("detail/__adsb").get("desc") == "AIRBUS A-320"
    assert "AIRBUS A-320" in event.find("detail/remarks").text


def test_null_or_blank_description_is_omitted():
    for value in (None, "", "   "):
        event = _xml({**BASE, "desc": value})
        assert event.find("detail/__adsb").get("desc") is None


class _RecordingStatus:
    def __init__(self):
        self.records = []

    def count(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None

    def write(self, *args, **kwargs):
        return True

    def record(self, **kwargs):
        self.records.append(kwargs)


def test_null_flight_is_reported_as_no_flight_in_status():
    import asyncio
    from configparser import ConfigParser

    from adsbcot.classes import ADSBWorker

    async def _main():
        parser = ConfigParser()
        parser.read_dict({"DEFAULT": {"INCLUDE_ALL_CRAFT": "true"}})
        worker = ADSBWorker(asyncio.Queue(), parser["DEFAULT"])
        worker.status = _RecordingStatus()
        await worker.process_craft({**BASE, "flight": None, "gs": None})
        return worker.status.records

    records = asyncio.run(_main())
    assert records[0]["flight"] is None
