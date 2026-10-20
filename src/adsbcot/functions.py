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

"""ADSBCOT Functions."""

import asyncio
import importlib
import importlib.metadata
import logging
import os
import re
import warnings
import xml.etree.ElementTree as ET

from configparser import SectionProxy
from typing import Any, Dict, Optional, Set, Union
from urllib.parse import ParseResult, urlparse

import aircot
import pytak
import adsbcot

# We won't use pyModeS if it isn't installed:
try:
    import pyModeS  # NOQA pylint: disable=unused-import
except ImportError as exc:
    warnings.warn(str(exc))
    warnings.warn("ADSBCOT ignoring ImportError for: pyModeS")


# Stratux TargetType values, see cyoung/stratux main/traffic.go.
TARGET_TYPE_NAMES: Dict[int, str] = {
    0: "Mode S",
    1: "ADS-B",
    2: "ADS-R",
    3: "TIS-B S",
    4: "TIS-B",
}

# A configparser section, or a plain dict in tests and library use.
Config = Union[SectionProxy, Dict[str, Any]]

APP_NAME = "adsbcot"
Logger = logging.getLogger(__name__)
Debug = bool(os.getenv("DEBUG", False))


def _first(craft: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the value of the first of `keys` present in `craft` and not None.

    Feeds send JSON ``null`` for fields they have not decoded yet, and
    ``dict.get(key, default)`` returns that None instead of the default (#24).
    """
    for key in keys:
        value = craft.get(key)
        if value is not None:
            return value
    return default


def _text(value: Any) -> str:
    """Normalize a craft text field; None is empty, not the word "NONE"."""
    if value is None:
        return ""
    return str(value).strip().upper()


def _number(value: Any) -> Optional[float]:
    """Return `value` as a float, or None if it is null or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Data types ADSBNetWorker can decode; pyModeS exits the process on others.
TCP_DATA_TYPES = ("raw", "beast", "skysense")

# pyModeS releases before 2.18 crash unpacking their own nuc_p()/nuc_v().
MIN_PYMODES_VERSION = (2, 18)


def _pymodes_version() -> Optional[str]:
    """Return the installed pyModeS version, or None if it is unknown."""
    try:
        return importlib.metadata.version("pyModeS")
    except Exception:  # NOQA pylint: disable=broad-except
        return None


def _pymodes_error() -> Optional[str]:
    """Return why pyModeS cannot decode TCP feeds, or None if it can.

    ADSBNetWorker needs the pyModeS 2.x streamer API, which pyModeS 3 removed;
    an installed-but-v3 pyModeS passes a plain import check and then fails at
    the first message, so import the modules that are actually used.
    """
    try:
        importlib.import_module("pyModeS.streamer.decode")
        importlib.import_module("pyModeS.streamer.source")
    except Exception as exc:  # NOQA pylint: disable=broad-except
        return str(exc).splitlines()[0] if str(exc) else type(exc).__name__

    version = _pymodes_version()
    if version:
        release = tuple(int(part) for part in re.findall(r"\d+", version)[:2])
        if release < MIN_PYMODES_VERSION:
            return f"pyModeS {version} is installed, 2.18 or newer is required"
    return None


def sensor_beacon_enabled(config: Config) -> bool:
    """Return whether the periodic receiver beacon is enabled."""
    value = config.get("SENSOR_BEACON", "1")
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def create_tasks(
    config: SectionProxy, clitool: pytak.CLITool
) -> Set[pytak.Worker]:
    """Create specific coroutine task set for this application.

    Parameters
    ----------
    config : `SectionProxy`
        Configuration options & values.
    clitool : `pytak.CLITool`
        A PyTAK Worker class instance.

    Returns
    -------
    `set`
        Set of PyTAK Worker classes for this application.
    """
    tasks = set()

    _feed_url: str = config.get("FEED_URL", adsbcot.DEFAULT_FEED_URL)
    config.setdefault("FEED_URL", _feed_url)
    _feed_url = config.get("FEED_URL", "")

    if "://" not in _feed_url:
        warnings.warn(f"Invalid FEED_URL: '{_feed_url}'", SyntaxWarning)
        raise ValueError(
            "Please specify FEED_URL with full URL, including '://', for "
            "example: tcp+beast://example.com:30005"
        )

    # Gateway code:
    feed_url: ParseResult = urlparse(config.get("FEED_URL", ""))

    # ADS-B Workers (receivers):
    if feed_url.scheme in ["http", "https", "file", "ws", "wss"]:
        # HTTP, WebSocket, or file-based input:
        tasks.add(adsbcot.ADSBWorker(clitool.tx_queue, config))
    elif "tcp" in feed_url.scheme:
        pymodes_error = _pymodes_error()
        if pymodes_error is not None:
            raise ValueError(
                f"FEED_URL={_feed_url} needs pyModeS 2.x ({pymodes_error}). "
                f"Install it with: python3 -m pip install '{APP_NAME}[with_pymodes]' "
                "or: python3 -m pip install 'pymodes>=2.18,<3'"
            )

        net_queue: "asyncio.Queue[bytes]" = asyncio.Queue()

        _, _, data_type = feed_url.scheme.partition("+")
        data_type = data_type or "raw"
        if data_type not in TCP_DATA_TYPES:
            raise ValueError(
                f"Unsupported FEED_URL={_feed_url}: TCP feeds must be "
                "tcp:// (raw), tcp+raw://, tcp+beast:// or tcp+skysense:// "
                "(raw, beast or skysense)"
            )

        tasks.add(adsbcot.ADSBNetReceiver(net_queue, config, data_type))

        tasks.add(adsbcot.ADSBNetWorker(clitool.tx_queue, net_queue, config, data_type))

    if sensor_beacon_enabled(config):
        tasks.add(adsbcot.SensorWorker(clitool.tx_queue, config))

    return tasks


def adsb_to_cot_xml(  # NOQA pylint: disable=too-many-locals,too-many-branches,too-many-statements
    craft: Dict[str, Any],
    config: Optional[Config] = None,
    known_craft: Optional[Dict[str, Any]] = None,
) -> Optional[ET.Element]:
    """
    Serialize ADS-B data as Cursor on Target.

    Parameters
    ----------
    craft : `dict`
        Key/Value data struct of decoded ADS-B aircraft data.
    config : `configparser.SectionProxy`
        Configuration options and values.
        Uses config options: UID_KEY, COT_STALE, COT_HOST_ID
    kown_craft : `dict`
        Optional list of know craft to transform CoT data.

    Returns
    -------
    `xml.etree.ElementTree.Element`
        Cursor-On-Target XML ElementTree object.
    """
    lastPosition = craft.get("lastPosition")
    if lastPosition:
        craft.update(lastPosition)

    lat = _first(craft, "lat", "Lat")
    lon = _first(craft, "lon", "Lon", "Lng")

    if lat is None or lon is None:
        Logger.warning(f"No value for lat={lat} lon={lon}")
        return None

    remarks_fields = []
    known_craft = known_craft or {}
    config = config or {}
    category = None
    tisb: bool = False

    uid_key: str = config.get("UID_KEY", "ICAO")
    cot_stale: int = int(config.get("COT_STALE", pytak.DEFAULT_COT_STALE))
    cot_host_id: str = config.get("COT_HOST_ID", pytak.DEFAULT_HOST_ID)

    __adsb = ET.Element("__adsb")
    __adsb.set("cot_host_id", cot_host_id)

    # Stratux reports Icao_addr as an int:
    icao_addr: str = aircot.icao_int_to_hex(
        _first(craft, "icao_addr", "Icao_addr", default=0)
    )
    icao_hex: str = _text(_first(craft, "hex", "icao", default=icao_addr))

    flight: str = _text(_first(craft, "flight", "Tail"))
    reg: str = _text(_first(craft, "reg", "r", "Reg"))
    cat: str = _text(_first(craft, "cat", "Category", "category"))
    squawk: str = _text(_first(craft, "squawk", "Squawk"))
    craft_type: str = _text(_first(craft, "t", "TargetType"))
    # Stratux reports the ADS-B target type as an int, see TARGET_TYPE_NAMES.
    target_type = craft.get("TargetType")
    # Model description, e.g. "AIRBUS A-320", from readsb-based APIs (#60).
    desc: str = _text(craft.get("desc"))

    alt_upper: int = int(config.get("ALT_UPPER", "0"))
    alt_lower: int = int(config.get("ALT_LOWER", "0"))

    alt_geom = _number(craft.get("alt_geom"))

    if alt_geom:
        remarks_fields.append(f"Alt:{craft.get('alt_geom')}")
        if alt_upper and alt_upper != 0:
            if alt_geom > alt_upper:
                Logger.warning(
                    f"alt_upper={alt_upper} alt_geom={alt_geom} "
                    "altitude too high, ignoring COT"
                )
                return None
        if alt_lower and alt_lower != 0:
            if alt_geom < alt_lower:
                Logger.warning(
                    f"alt_lower={alt_lower} alt_geom={alt_geom} "
                    "altitude too low, ignoring COT"
                )
                return None
    __adsb.set("alt_geom", str(craft.get("alt_geom")))
    __adsb.set("x_alt_geom", str(craft.get("x_alt_geom")))
    __adsb.set("alt_baro", str(craft.get("alt_baro")))
    __adsb.set("x_alt_baro_offset", str(craft.get("x_alt_baro_offset")))

    if flight:
        remarks_fields.append(flight)
        __adsb.set("flight", flight)

    if reg:
        remarks_fields.append(reg)
        __adsb.set("reg", reg)

    if squawk:
        remarks_fields.append(f"Squawk: {squawk}")
        __adsb.set("squawk", squawk)

    if icao_hex:
        remarks_fields.append(icao_hex)
        __adsb.set("icao", icao_hex)

    if cat:
        category = aircot.set_category(cat, known_craft)
        remarks_fields.append(f"Cat.: {cat}")
        __adsb.set("cat", cat)

    if desc:
        remarks_fields.append(desc)
        __adsb.set("desc", desc)

    if craft_type:
        __adsb.set("craft_type", str(craft_type))
        remarks_fields.append(f"Type:{craft_type}")

        craft_type_name = (
            TARGET_TYPE_NAMES.get(target_type) if isinstance(target_type, int) else None
        )
        if craft_type_name:
            tisb = craft_type_name.startswith("TIS-B")
            remarks_fields.append(f"ADS-B Type: {craft_type_name}")

    cot_uid: str = ""
    if "REG" in uid_key and reg:
        cot_uid = f"REG-{reg}"
    elif "FLIGHT" in uid_key and flight:
        cot_uid = f"FLIGHT-{flight}"
    elif icao_hex:
        cot_uid = f"ICAO-{icao_hex}"
    elif flight:
        cot_uid = f"FLIGHT-{flight}"
    else:
        Logger.warning("No ICAO, flight or UID_KEY=REG registration for craft")
        return None

    if flight:
        callsign = flight
    elif reg:
        callsign = reg
    else:
        callsign = icao_hex

    _, callsign = aircot.set_name_callsign(
        icao_hex, reg, craft_type, flight, known_craft
    )

    cat = aircot.set_category(cat, known_craft)

    if tisb or not icao_hex:
        # aircot derives the type from the ICAO address and cannot parse ""
        cot_type = known_craft.get("COT") or "a-u-A"
    else:
        cot_type = aircot.set_cot_type(icao_hex, category, flight, known_craft)

    nac_p = _number(_first(craft, "NACp", "nac_p")) or 0.0
    nac_v = _number(_first(craft, "NACv", "nac_v"))
    if nac_v is None:
        nac_v = nac_p

    if craft.get("OnGround"):
        ground_const = 51.56
        hae = pytak.DEFAULT_COT_VAL
    else:
        ground_const = 56.57
        # Multiply alt_geom by "Clarke 1880 (international foot)"
        hae = aircot.functions.get_hae(
            _number(_first(craft, "Alt", "alt_geom", "alt_geom_x"))
        )

    ce = str(float(nac_p) + ground_const)
    le = str(float(nac_v) + 12.5)

    contact: ET.Element = ET.Element("contact")
    contact.set("callsign", callsign)

    uid = ET.Element("UID")
    uid.set("Droid", str(callsign))

    track: ET.Element = ET.Element("track")

    course = _first(craft, "trk", "track", "Track", default=pytak.DEFAULT_COT_VAL)
    track.set("course", str(course))

    speed = aircot.functions.get_speed(_number(_first(craft, "gs", "Speed")))
    track.set("speed", str(speed))

    track.set("slope", str(craft.get("Vvel", pytak.DEFAULT_COT_VAL)))

    _radio = ET.Element("_radio")
    _signal = _first(craft, "SignalLevel", "rssi")
    if _signal:
        __adsb.set("signalLevel", str(_signal))
        _radio.set("signal", str(_signal))

    remarks_fields.append(f"FEED_URL: {config.get('FEED_URL', '')}")
    __adsb.set("feed_url", config.get("FEED_URL", ""))
    # Remarks should always be the first sub-entity within the Detail entity.
    remarks = ET.Element("remarks")
    remarks_fields.append(f"{cot_host_id}")
    _remarks = " ".join(list(filter(None, remarks_fields)))
    remarks.text = _remarks

    detail = ET.Element("detail")
    detail.append(track)
    detail.append(contact)
    detail.append(remarks)
    detail.append(__adsb)
    detail.append(_radio)

    icon = known_craft.get("ICON")
    if icon:
        usericon = ET.Element("usericon")
        usericon.set("iconsetpath", icon)
        detail.append(usericon)

    return pytak.cot_event(
        uid=cot_uid,
        cot_type=cot_type,
        stale=cot_stale,
        point=pytak.cot_point(lat=lat, lon=lon, ce=ce, hae=hae, le=le),
        detail=pytak.cot_detail(
            track,
            contact,
            remarks,
            __adsb,
            _radio,
            *([usericon] if icon else []),
            flow_tag_host_id=cot_host_id,
        ),
        access=config.get("COT_ACCESS", pytak.DEFAULT_COT_ACCESS),
        qos="1-r-c",
    )


def gen_sensor_cot(
    config: Optional[Config] = None,
    lat: float = 0.0,
    lon: float = 0.0,
    hae: float = 0.0,
    ce: str = "9999999.0",
    le: str = "9999999.0",
) -> Optional[ET.Element]:
    """Generate a periodic sensor beacon CoT (a-f-G-E-S-E)."""
    config = config or {}
    sensor_id = config.get("SENSOR_ID", adsbcot.DEFAULT_SENSOR_ID)
    cot_type = config.get("SENSOR_COT_TYPE", adsbcot.DEFAULT_SENSOR_COT_TYPE)
    cot_stale = int(config.get("COT_STALE", pytak.DEFAULT_COT_STALE))
    callsign = str(config.get("SENSOR_CALLSIGN", sensor_id))
    payload_type = config.get(
        "SENSOR_PAYLOAD_TYPE", adsbcot.DEFAULT_SENSOR_PAYLOAD_TYPE
    )

    contact = ET.Element("contact")
    contact.set("callsign", callsign)

    sensor_elem = ET.Element("sensor")
    sensor_elem.set("sensor_id", sensor_id)
    sensor_elem.set("type", payload_type)

    return pytak.cot_event(
        uid=f"SENSOR.{sensor_id}",
        cot_type=cot_type,
        stale=cot_stale,
        point=pytak.cot_point(lat=lat, lon=lon, hae=hae, ce=ce, le=le),
        detail=pytak.cot_detail(contact, sensor_elem, flow_tag=False),
        how="m-g",
        access=config.get("COT_ACCESS", pytak.DEFAULT_COT_ACCESS),
    )


def adsb_to_cot(
    craft: Dict[str, Any],
    config: Optional[Config] = None,
    known_craft: Optional[Dict[str, Any]] = None,
) -> Optional[bytes]:
    """Return CoT XML object as an XML string."""
    cot: Optional[ET.Element] = adsb_to_cot_xml(craft, config, known_craft)
    return pytak.serialize_cot(cot) if cot is not None else None
