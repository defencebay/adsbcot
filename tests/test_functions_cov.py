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

"""Coverage tests for adsbcot.functions.

Exercises the field helpers, the pyModeS probe, task creation and the CoT
XML produced for dump1090, readsb/ADSBExchange and Stratux style records.
"""

import importlib
import sys
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest import mock

import pytest
import pytak

import adsbcot
from adsbcot import functions

BASE = {"hex": "a9ee47", "lat": 37.836449, "lon": -122.030281, "alt_geom": 3750}


def _xml(craft, config=None, known_craft=None):
    event = functions.adsb_to_cot_xml(dict(craft), config or {}, known_craft)
    assert event is not None
    return event


def _remarks(event):
    return event.find("detail/remarks").text


def _adsb(event):
    return event.find("detail/__adsb").attrib


# _first / _text / _number


def test_first_returns_first_non_none_value():
    craft = {"a": None, "b": 0, "c": "x"}
    assert functions._first(craft, "a", "b", "c") == 0
    assert functions._first(craft, "missing", "c") == "x"


def test_first_default_when_all_missing_or_none():
    assert functions._first({}, "a") is None
    assert functions._first({"a": None}, "a", "b", default="d") == "d"
    assert functions._first({"a": False}, "a", default="d") is False


def test_text_normalizes():
    assert functions._text(None) == ""
    assert functions._text("  ual123 ") == "UAL123"
    assert functions._text(0) == "0"
    assert functions._text(42) == "42"


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (True, None),
        (False, None),
        ("123", 123.0),
        ("-1.5", -1.5),
        (" 7 ", 7.0),
        (10, 10.0),
        (2.5, 2.5),
        ("ground", None),
        ("", None),
        ([1], None),
        ({}, None),
    ],
)
def test_number(value, expected):
    assert functions._number(value) == expected


def test_module_import_tolerates_missing_pymodes():
    """functions.py only warns when pyModeS is not installed."""
    try:
        with mock.patch.dict(sys.modules, {"pyModeS": None}):
            with pytest.warns(UserWarning, match="ignoring ImportError"):
                importlib.reload(functions)
        assert callable(functions.adsb_to_cot_xml)
    finally:
        importlib.reload(functions)


# _pymodes_error


def test_pymodes_error_none_when_modules_import():
    with mock.patch.object(functions.importlib, "import_module") as imp:
        assert functions._pymodes_error() is None
    imported = [c.args[0] for c in imp.call_args_list]
    assert imported == ["pyModeS.streamer.decode", "pyModeS.streamer.source"]


def test_pymodes_error_returns_first_line_of_message():
    err = ImportError("No module named 'pyModeS.streamer'\nsecond line")
    with mock.patch.object(functions.importlib, "import_module", side_effect=err):
        assert functions._pymodes_error() == "No module named 'pyModeS.streamer'"


def test_pymodes_error_empty_message_uses_type_name():
    with mock.patch.object(
        functions.importlib, "import_module", side_effect=AttributeError()
    ):
        assert functions._pymodes_error() == "AttributeError"


def test_pymodes_error_second_import_failing():
    def _imp(name):
        if name.endswith("source"):
            raise RuntimeError("no source")
        return SimpleNamespace()

    with mock.patch.object(functions.importlib, "import_module", side_effect=_imp):
        assert functions._pymodes_error() == "no source"


# create_tasks


def test_create_tasks_feed_url_without_scheme_raises():
    clitool = SimpleNamespace(tx_queue=object())
    with pytest.warns(SyntaxWarning, match="Invalid FEED_URL"):
        with pytest.raises(ValueError, match="://"):
            functions.create_tasks({"FEED_URL": "example.com:30005"}, clitool)


def test_create_tasks_default_feed_url():
    clitool = SimpleNamespace(tx_queue=object())
    config = {"SENSOR_BEACON": "0"}
    with mock.patch.object(adsbcot, "ADSBWorker", return_value="aircraft") as w:
        tasks = functions.create_tasks(config, clitool)
    assert config["FEED_URL"] == adsbcot.DEFAULT_FEED_URL
    assert tasks == {"aircraft"}
    w.assert_called_once_with(clitool.tx_queue, config)


def test_create_tasks_unknown_scheme_only_sensor_worker():
    clitool = SimpleNamespace(tx_queue=object())
    config = {"FEED_URL": "udp://example.com:30005"}
    with mock.patch.object(adsbcot, "ADSBWorker") as feed, mock.patch.object(
        adsbcot, "ADSBNetReceiver"
    ) as rx, mock.patch.object(
        adsbcot, "SensorWorker", return_value="receiver"
    ) as sensor:
        tasks = functions.create_tasks(config, clitool)
    assert tasks == {"receiver"}
    feed.assert_not_called()
    rx.assert_not_called()
    sensor.assert_called_once_with(clitool.tx_queue, config)


def test_create_tasks_tcp_without_pymodes_raises():
    clitool = SimpleNamespace(tx_queue=object())
    config = {"FEED_URL": "tcp+beast://example.com:30005"}
    with mock.patch.object(functions, "_pymodes_error", return_value="boom"):
        with pytest.raises(ValueError, match=r"pyModeS 2\.x \(boom\)"):
            functions.create_tasks(config, clitool)


# adsb_to_cot_xml


def test_last_position_is_merged():
    """readsb/adsbx records carry lat/lon only in lastPosition when stale."""
    craft = {
        "hex": "a9ee47",
        "flight": "UAL1",
        "lastPosition": {"lat": 10.5, "lon": -20.25, "nic": 8},
    }
    event = _xml(craft)
    point = event.find("point")
    assert float(point.get("lat")) == 10.5
    assert float(point.get("lon")) == -20.25


def test_no_position_returns_none():
    assert functions.adsb_to_cot_xml({"hex": "a9ee47", "lat": 1.0}) is None
    assert functions.adsb_to_cot_xml({"hex": "a9ee47", "lastPosition": {}}) is None


@pytest.mark.parametrize(
    "alt,config,dropped",
    [
        (40000, {"ALT_UPPER": 30000}, True),
        (20000, {"ALT_UPPER": 30000}, False),
        (500, {"ALT_LOWER": 1000}, True),
        (1500, {"ALT_LOWER": 1000}, False),
        (1500, {"ALT_LOWER": 1000, "ALT_UPPER": 2000}, False),
        (2500, {"ALT_LOWER": 1000, "ALT_UPPER": 2000}, True),
        (99999, {"ALT_UPPER": 0, "ALT_LOWER": 0}, False),
    ],
)
def test_altitude_filters_numeric(alt, config, dropped):
    event = functions.adsb_to_cot_xml({**BASE, "alt_geom": alt}, config)
    assert (event is None) is dropped
    if not dropped:
        assert f"Alt:{alt}" in _remarks(event)
        assert _adsb(event)["alt_geom"] == str(alt)


def test_squawk_in_remarks_and_adsb():
    event = _xml({**BASE, "squawk": "7700"})
    assert "Squawk: 7700" in _remarks(event)
    assert _adsb(event)["squawk"] == "7700"


def test_category_sets_cat_and_remarks():
    event = _xml({**BASE, "category": "a7"})
    assert _adsb(event)["cat"] == "A7"
    assert "Cat.: A7" in _remarks(event)
    # A7 (rotorcraft) maps to a helicopter CoT type.
    assert event.get("type").endswith("-H")


def test_stratux_category_key():
    event = _xml({**BASE, "Category": 1})
    assert _adsb(event)["cat"] == "1"


def test_flight_and_reg_in_adsb_and_remarks():
    event = _xml({**BASE, "flight": "ual1  ", "r": "n123ua"})
    adsb = _adsb(event)
    assert adsb["flight"] == "UAL1"
    assert adsb["reg"] == "N123UA"
    assert adsb["icao"] == "A9EE47"
    remarks = _remarks(event)
    assert "UAL1" in remarks and "N123UA" in remarks and "A9EE47" in remarks


def test_uid_key_icao_default():
    assert _xml({**BASE, "flight": "UAL1"}).get("uid") == "ICAO-A9EE47"
    assert (
        _xml({**BASE, "flight": "UAL1"}, {"UID_KEY": "ICAO"}).get("uid")
        == "ICAO-A9EE47"
    )


def test_uid_key_flight_with_flight():
    event = _xml({**BASE, "flight": "UAL1 "}, {"UID_KEY": "FLIGHT"})
    assert event.get("uid") == "FLIGHT-UAL1"


def test_uid_key_flight_without_flight_falls_back_to_icao():
    event = _xml(BASE, {"UID_KEY": "FLIGHT"})
    assert event.get("uid") == "ICAO-A9EE47"


def test_uid_no_hex_with_flight():
    # known_craft COT sidesteps aircot.adsb_to_cot_type, which cannot parse an
    # empty ICAO hex (reported separately).
    craft = {"lat": 1.0, "lon": 2.0, "flight": "UAL1"}
    event = _xml(craft, known_craft={"COT": "a-n-A-C-F"})
    assert event.get("uid") == "FLIGHT-UAL1"
    assert event.find("detail/contact").get("callsign").startswith("UAL1")


def test_reg_only_craft_keeps_icao_uid_by_default():
    event = _xml({**BASE, "reg": "n123ua"})
    assert event.get("uid") == "ICAO-A9EE47"
    assert _adsb(event)["reg"] == "N123UA"
    assert "flight" not in _adsb(event)


def test_uid_key_reg_still_produces_event():
    # Only checks the event is built; which UID wins is reported separately.
    event = _xml({**BASE, "reg": "N123UA"}, {"UID_KEY": "REG"})
    assert _adsb(event)["reg"] == "N123UA"
    assert event.get("uid")


def test_uid_no_hex_no_flight_returns_none():
    assert functions.adsb_to_cot_xml({"lat": 1.0, "lon": 2.0}) is None


def test_stratux_int_icao_addr():
    craft = {"Lat": 1.0, "Lng": 2.0, "Icao_addr": 0xA9EE46, "Tail": "N1"}
    event = _xml(craft)
    assert event.get("uid") == "ICAO-A9EE46"
    assert _adsb(event)["flight"] == "N1"


def test_on_ground_stratux_branch():
    craft = {
        "Lat": 1.0,
        "Lng": 2.0,
        "Icao_addr": 0xA9EE46,
        "OnGround": True,
        "Alt": 1000,
        "NACp": 8,
        "NACv": 2,
    }
    point = _xml(craft).find("point")
    assert point.get("hae") == pytak.DEFAULT_COT_VAL
    assert float(point.get("ce")) == pytest.approx(8 + 51.56)
    assert float(point.get("le")) == pytest.approx(2 + 12.5)


def test_airborne_ce_le_and_hae():
    event = _xml({**BASE, "nac_p": 9})
    point = event.find("point")
    assert float(point.get("ce")) == pytest.approx(9 + 56.57)
    # nac_v falls back to nac_p.
    assert float(point.get("le")) == pytest.approx(9 + 12.5)
    assert point.get("hae") != pytak.DEFAULT_COT_VAL
    assert float(point.get("hae")) > 0


def test_track_speed_slope():
    event = _xml({**BASE, "track": 270, "gs": 100, "Vvel": 5})
    track = event.find("detail/track")
    assert track.get("course") == "270"
    assert float(track.get("speed")) > 0
    assert track.get("slope") == "5"


@pytest.mark.parametrize("key", ["SignalLevel", "rssi"])
def test_signal_level(key):
    event = _xml({**BASE, key: -18.2})
    assert _adsb(event)["signalLevel"] == "-18.2"
    assert event.find("detail/_radio").get("signal") == "-18.2"


def test_no_signal_leaves_radio_empty():
    event = _xml(BASE)
    assert "signalLevel" not in _adsb(event)
    assert event.find("detail/_radio").get("signal") is None


def test_known_craft_icon_and_callsign():
    known = {"ICON": "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Military/C17.png"}
    known["CALLSIGN"] = "REACH01"
    event = _xml({**BASE, "flight": "UAL1"}, known_craft=known)
    usericon = event.find("detail/usericon")
    assert usericon is not None
    assert usericon.get("iconsetpath") == known["ICON"]
    callsign = event.find("detail/contact").get("callsign")
    assert callsign == "REACH01 (A9EE47)"


def test_known_craft_cot_type_override():
    event = _xml(BASE, known_craft={"COT": "a-f-A-M-H"})
    assert event.get("type") == "a-f-A-M-H"
    assert event.find("detail/usericon") is None


def test_cot_access_config():
    event = _xml(BASE, {"COT_ACCESS": "SECRET"})
    assert event.get("access") == "SECRET"
    assert event.get("qos") == "1-r-c"


def test_cot_access_default_and_empty():
    event = _xml(BASE)
    assert event.get("access") == pytak.DEFAULT_COT_ACCESS
    # pytak.cot_event omits falsy optional attributes.
    assert _xml(BASE, {"COT_ACCESS": ""}).get("access") is None


def test_host_id_feed_url_and_stale():
    config = {"COT_HOST_ID": "host-1", "FEED_URL": "file:///x.json", "COT_STALE": 60}
    event = _xml(BASE, config)
    adsb = _adsb(event)
    assert adsb["cot_host_id"] == "host-1"
    assert adsb["feed_url"] == "file:///x.json"
    remarks = _remarks(event)
    assert "FEED_URL: file:///x.json" in remarks
    assert remarks.endswith("host-1")
    assert event.get("stale") > event.get("start")


# gen_sensor_cot


def test_gen_sensor_cot_defaults():
    event = functions.gen_sensor_cot()
    sid = adsbcot.DEFAULT_SENSOR_ID
    assert event.get("uid") == f"SENSOR.{sid}"
    assert event.get("type") == adsbcot.DEFAULT_SENSOR_COT_TYPE
    assert event.get("how") == "m-g"
    assert event.get("access") == pytak.DEFAULT_COT_ACCESS
    assert event.find("detail/contact").get("callsign") == sid
    sensor = event.find("detail/sensor")
    assert sensor.get("sensor_id") == sid
    assert sensor.get("type") == adsbcot.DEFAULT_SENSOR_PAYLOAD_TYPE
    point = event.find("point")
    assert float(point.get("lat")) == 0.0
    assert float(point.get("lon")) == 0.0
    assert float(point.get("ce")) == 9999999.0
    assert float(point.get("le")) == 9999999.0
    assert event.find("detail/_flow-tags_") is None


def test_gen_sensor_cot_overrides():
    config = {
        "SENSOR_ID": "rx1",
        "SENSOR_COT_TYPE": "a-f-G-E-S",
        "COT_STALE": "600",
        "SENSOR_CALLSIGN": "Roof Receiver",
        "SENSOR_PAYLOAD_TYPE": "UAT-Receiver",
        "COT_ACCESS": "CUI",
    }
    event = functions.gen_sensor_cot(
        config, lat=37.5, lon=-122.25, hae=12.0, ce="10.0", le="5.0"
    )
    assert event.get("uid") == "SENSOR.rx1"
    assert event.get("type") == "a-f-G-E-S"
    assert event.get("access") == "CUI"
    assert event.find("detail/contact").get("callsign") == "Roof Receiver"
    sensor = event.find("detail/sensor")
    assert sensor.get("sensor_id") == "rx1"
    assert sensor.get("type") == "UAT-Receiver"
    point = event.find("point")
    assert float(point.get("lat")) == 37.5
    assert float(point.get("lon")) == -122.25
    assert float(point.get("hae")) == 12.0
    assert float(point.get("ce")) == 10.0
    assert float(point.get("le")) == 5.0
    # 600 s stale lands later than the default stale.
    default_stale = functions.gen_sensor_cot({"SENSOR_ID": "rx1"}).get("stale")
    assert event.get("stale") > default_stale


# adsb_to_cot


def test_adsb_to_cot_bytes():
    data = functions.adsb_to_cot(dict(BASE), {"COT_HOST_ID": "h"})
    assert isinstance(data, bytes)
    event = ET.fromstring(data)
    assert event.tag == "event"
    assert event.get("uid") == "ICAO-A9EE47"


def test_adsb_to_cot_none_without_position():
    assert functions.adsb_to_cot({"hex": "a9ee47"}) is None
