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

"""End-to-end tests for ADSBNetWorker with the real pyModeS v2 streamer.

tests/test_networker.py covers the aircraft -> batch mapping with a fake
decoder. These feed real AVR ("raw") and Beast frames through the real
pyModeS NetSource framing and Decode CPR solver, so a change in either side
of that boundary shows up here.
"""

import asyncio
import time
import xml.etree.ElementTree as ET

from configparser import ConfigParser

import pytest

pytest.importorskip("pyModeS.streamer.decode")

import pyModeS.streamer.decode  # noqa: E402
import pyModeS.streamer.source  # noqa: E402

from adsbcot.classes import ADSBNetWorker  # noqa: E402

# DF17 identification, ICAO 4840D6, callsign KLM1023 ("The 1090MHz Riddle").
IDENT = "8D4840D6202CC371C32CE0576098"
# DF17 airborne position even/odd pair, ICAO 40621D, ~52.26N 3.94E, FL380.
POS_A = "8D40621D58C382D690C8AC2863A7"
POS_B = "8D40621D58C386435CC412692AD6"
# DF20 Comm-B reply: correct length, but not ADS-B.
DF20 = "A0001498BE800030AA0000C7A75F"
# IDENT with its last nibble changed: right length, DF17, CRC fails.
BAD_CRC = IDENT[:-1] + "9"


class _Stop(Exception):
    """Raised by a test double to break out of the decoder's endless loop."""


class _ScriptedQueue:
    """A net_queue that hands out `items` in order, then raises _Stop."""

    def __init__(self, items):
        self.items = list(items)

    async def get(self):
        if not self.items:
            raise _Stop
        return self.items.pop(0)


class _CountingDecoder(pyModeS.streamer.decode.Decode):
    """The real Decode, recording how often each entry point is used."""

    def __init__(self):
        super().__init__()
        self.processed = []
        self.get_calls = 0

    def process_raw(self, adsb_ts, adsb_msg, commb_ts, commb_msg, tnow=None):
        self.processed.append((list(adsb_msg), list(commb_msg)))
        return super().process_raw(adsb_ts, adsb_msg, commb_ts, commb_msg, tnow)

    def get_aircraft(self):
        self.get_calls += 1
        return super().get_aircraft()


class _SkysenseClient:
    """Stands in for NetSource; only the skysense reader returns frames."""

    def __init__(self, messages):
        self.buffer = []
        self.messages = messages
        self.calls = []

    def read_skysense_buffer(self):
        self.calls.append("skysense")
        return self.messages

    def read_raw_buffer(self):  # pragma: no cover - must not be called
        self.calls.append("raw")
        return []

    read_beast_buffer = read_raw_buffer


class _RecordingStatus:
    """Records every call made on the status writer."""

    def __init__(self):
        self.calls = []

    def set(self, **kwargs):
        self.calls.append(("set", kwargs))

    def write(self, force=False):
        self.calls.append(("write", force))
        return True

    def count(self, *args, **kwargs):
        self.calls.append(("count", args))

    def record(self, **kwargs):
        self.calls.append(("record", kwargs))


def _config(**overrides):
    parser = ConfigParser()
    parser.read_dict(
        {
            "DEFAULT": {
                "FEED_URL": "tcp+raw://127.0.0.1:30002",
                "INCLUDE_TISB": "false",
                "TISB_ONLY": "false",
                "INCLUDE_ALL_CRAFT": "true",
                **overrides,
            }
        }
    )
    return parser["DEFAULT"]


def _avr(*frames):
    return b"".join(b"*" + frame.encode() + b";\n" for frame in frames)


def _beast(frame, kind=b"\x33", timestamp=b"\x00\x00\x00\x01\x02\x03", signal=b"\x80"):
    """Return one Beast-framed message, with 0x1A escaped as 0x1A 0x1A."""
    body = kind + timestamp + signal + bytes.fromhex(frame)
    return b"\x1a" + body.replace(b"\x1a", b"\x1a\x1a")


def _decode(items, data_type="raw", net_client=None, real_handle=False):
    """Run _run_decoder over `items` with the real pyModeS streamer.

    Returns (worker, decoder, batches handed to handle_data, tx queue items).
    """
    batches = []
    result = {}

    async def _main():
        tx_queue: asyncio.Queue = asyncio.Queue()
        worker = ADSBNetWorker(tx_queue, _ScriptedQueue(items), _config(), data_type)
        worker.status = _RecordingStatus()
        if not real_handle:

            async def _handle(data):
                batches.append(data)

            worker.handle_data = _handle
        decoder = _CountingDecoder()
        client = net_client or pyModeS.streamer.source.NetSource("x", 1, data_type)
        with pytest.raises(_Stop):
            await worker._run_decoder(decoder, client)
        tx = []
        while not tx_queue.empty():
            tx.append(tx_queue.get_nowait())
        result.update(worker=worker, decoder=decoder, tx=tx)

    asyncio.run(_main())
    return result["worker"], result["decoder"], batches, result["tx"]


def _by_hex(batch):
    return {craft["hex"]: craft for craft in batch}


def _assert_decoded(batches):
    assert len(batches) == 1
    crafts = _by_hex(batches[0])
    assert set(crafts) == {"40621D", "4840D6"}

    pos = crafts["40621D"]
    assert pos["lat"] == pytest.approx(52.2658, abs=1e-3)
    assert pos["lon"] == pytest.approx(3.9389, abs=1e-3)
    assert pos["alt_geom"] == 38000
    assert pos["flight"] == "40621D"  # no identification heard: falls back

    ident = crafts["4840D6"]
    assert ident["flight"] == "KLM1023"  # pyModeS "_" padding stripped
    assert ident["lat"] is None and ident["lon"] is None


# --------------------------------------------------------------------------
# __init__ / _reset_local_buffer


def test_init_stores_queues_type_and_empty_buffers():
    async def _main():
        tx_queue: asyncio.Queue = asyncio.Queue()
        net_queue: asyncio.Queue = asyncio.Queue()
        config = _config()
        worker = ADSBNetWorker(tx_queue, net_queue, config, "beast")
        assert worker.queue is tx_queue
        assert worker.net_queue is net_queue
        assert worker.config is config
        assert worker.data_type == "beast"
        assert worker.uid_key == "ICAO"
        assert worker.known_craft_db is None
        for name in (
            "local_buffer_adsb_msg",
            "local_buffer_adsb_ts",
            "local_buffer_commb_msg",
            "local_buffer_commb_ts",
        ):
            assert getattr(worker, name) == []

        worker.local_buffer_adsb_msg.append(IDENT)
        worker.local_buffer_adsb_ts.append(1.0)
        worker.local_buffer_commb_msg.append(DF20)
        worker.local_buffer_commb_ts.append(2.0)
        old = worker.local_buffer_adsb_msg
        worker._reset_local_buffer()
        assert worker.local_buffer_adsb_msg == []
        assert worker.local_buffer_adsb_ts == []
        assert worker.local_buffer_commb_msg == []
        assert worker.local_buffer_commb_ts == []
        # Fresh lists, not cleared in place: the decoder may still hold the
        # old ones.
        assert old == [IDENT]

    asyncio.run(_main())


# --------------------------------------------------------------------------
# run()


@pytest.mark.parametrize("data_type", ["raw", "beast"])
def test_run_reports_status_and_cancels_heartbeat(data_type):
    seen = {}

    async def _main():
        worker = ADSBNetWorker(
            asyncio.Queue(),
            asyncio.Queue(),
            _config(FEED_URL="tcp+%s://10.0.0.1:1" % data_type),
            data_type,
        )
        worker.status = _RecordingStatus()
        worker.local_buffer_adsb_msg.append("stale")
        heartbeat = {"started": False, "cancelled": False}

        async def _heartbeat(interval=5.0):
            heartbeat["started"] = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                heartbeat["cancelled"] = True
                raise

        async def _run_decoder(decoder, net_client):
            seen["decoder"] = decoder
            seen["net_client"] = net_client
            seen["buffer_at_start"] = list(worker.local_buffer_adsb_msg)
            seen["status_before_decode"] = list(worker.status.calls)
            await asyncio.sleep(0)  # let the heartbeat task start
            raise _Stop

        worker._status_heartbeat = _heartbeat
        worker._run_decoder = _run_decoder
        with pytest.raises(_Stop):
            await worker.run()
        await asyncio.sleep(0)  # deliver the cancellation
        seen["heartbeat"] = heartbeat

    asyncio.run(_main())

    assert isinstance(seen["decoder"], pyModeS.streamer.decode.Decode)
    assert isinstance(seen["net_client"], pyModeS.streamer.source.NetSource)
    assert seen["net_client"].datatype == data_type
    assert seen["buffer_at_start"] == []  # run() resets the local buffers
    assert seen["status_before_decode"] == [
        ("set", {"feed": "tcp+%s://10.0.0.1:1" % data_type}),
        ("write", True),
    ]
    assert seen["heartbeat"] == {"started": True, "cancelled": True}


def test_run_decodes_real_frames_end_to_end():
    """run() with the real Decode/NetSource, fed real AVR frames."""
    batches = []

    async def _main():
        worker = ADSBNetWorker(
            asyncio.Queue(),
            _ScriptedQueue([_avr(IDENT, POS_A, POS_B)]),
            _config(),
            "raw",
        )
        worker.status = _RecordingStatus()

        async def _handle(data):
            batches.append(data)

        worker.handle_data = _handle
        with pytest.raises(_Stop):
            await worker.run()

    asyncio.run(_main())
    _assert_decoded(batches)


# --------------------------------------------------------------------------
# _run_decoder: raw (AVR)


def test_raw_frames_decode_position_and_callsign():
    worker, decoder, batches, _ = _decode([_avr(IDENT, POS_A, POS_B)])
    _assert_decoded(batches)
    assert decoder.processed == [([IDENT, POS_A, POS_B], [])]
    assert decoder.get_calls == 1
    assert worker.local_buffer_adsb_msg == []  # reset after process_raw


def test_raw_frames_through_real_handle_data_emit_cot():
    worker, _, _, tx = _decode([_avr(IDENT, POS_A, POS_B)], real_handle=True)
    # Only the aircraft with a position becomes CoT ...
    assert len(tx) == 1
    event = ET.fromstring(tx[0])
    assert event.get("uid") == "ICAO-40621D"
    point = event.find("point")
    assert float(point.get("lat")) == pytest.approx(52.2658, abs=1e-3)
    assert float(point.get("lon")) == pytest.approx(3.9389, abs=1e-3)
    # ... but both are reported as tracked.
    assert ("set", {"tracked": 2}) in worker.status.calls


def test_single_frame_is_buffered_until_a_second_arrives():
    """process_raw only runs with >1 ADS-B frames buffered; get_aircraft() is
    called every cycle regardless."""
    worker, decoder, batches, _ = _decode([_avr(POS_A), _avr(POS_B)])
    assert decoder.processed == [([POS_A, POS_B], [])]
    assert decoder.get_calls == 2
    assert len(batches) == 1
    assert batches[0][0]["hex"] == "40621D"
    assert batches[0][0]["lat"] == pytest.approx(52.2658, abs=1e-3)


def test_single_frame_alone_never_reaches_the_decoder():
    worker, decoder, batches, _ = _decode([_avr(IDENT)])
    assert decoder.processed == []
    assert decoder.get_calls == 1
    assert batches == []
    assert worker.local_buffer_adsb_msg == [IDENT]
    assert len(worker.local_buffer_adsb_ts) == 1


def test_empty_queue_item_is_skipped():
    _, decoder, batches, _ = _decode([b"", None])
    assert decoder.get_calls == 0  # `continue` before any decoding
    assert batches == []


def test_item_with_no_complete_frames_is_skipped():
    """A partial AVR line yields no messages: nothing is decoded."""
    _, decoder, batches, _ = _decode([b"*8D4840D6"])
    assert decoder.get_calls == 0
    assert batches == []


@pytest.mark.parametrize(
    "frame",
    [
        "5D484BA898F8C6",  # DF11 all-call: short (14 hex chars)
        IDENT[:-2],  # truncated long frame
        DF20,  # right length, Comm-B, not ADS-B
        BAD_CRC,  # right length, DF17, CRC fails
    ],
    ids=["df11-short", "truncated", "df20", "bad-crc"],
)
def test_rejected_frames_are_not_buffered(frame):
    worker, decoder, batches, _ = _decode([_avr(frame, frame, frame)])
    assert worker.local_buffer_adsb_msg == []
    assert worker.local_buffer_commb_msg == []
    assert decoder.processed == []
    assert decoder.get_calls == 1
    assert batches == []


def test_rejected_frames_do_not_disturb_valid_ones():
    worker, decoder, batches, _ = _decode(
        [_avr(DF20, IDENT, BAD_CRC, POS_A, "5D484BA898F8C6", POS_B)]
    )
    assert decoder.processed == [([IDENT, POS_A, POS_B], [])]
    _assert_decoded(batches)


# --------------------------------------------------------------------------
# _run_decoder: beast


def test_beast_frames_decode_position_and_callsign():
    # The trailing frame is held by NetSource as the remainder of an
    # unterminated message -- Beast frames are only complete once the next
    # <esc> arrives -- so it must not be one we depend on.
    chunk = _beast(IDENT) + _beast(POS_A) + _beast(POS_B) + _beast(IDENT)
    worker, decoder, batches, _ = _decode([chunk], data_type="beast")
    assert decoder.processed == [([IDENT, POS_A, POS_B], [])]
    _assert_decoded(batches)


def test_beast_escaped_0x1a_in_timestamp_and_signal():
    """0x1A inside a frame is sent as 0x1A 0x1A and must be unescaped."""
    ts = b"\x00\x1a\x00\x00\x1a\x01"
    chunk = (
        _beast(IDENT, timestamp=ts, signal=b"\x1a")
        + _beast(POS_A, timestamp=ts)
        + _beast(POS_B, signal=b"\x1a")
        + _beast(IDENT)
    )
    assert b"\x1a\x1a" in chunk
    _, decoder, batches, _ = _decode([chunk], data_type="beast")
    assert decoder.processed == [([IDENT, POS_A, POS_B], [])]
    _assert_decoded(batches)


def test_beast_frame_split_across_reads():
    chunk = _beast(IDENT) + _beast(POS_A) + _beast(POS_B) + _beast(IDENT)
    cut = len(_beast(IDENT)) + 5  # in the middle of POS_A's header
    _, decoder, batches, _ = _decode([chunk[:cut], chunk[cut:]], data_type="beast")
    # First read completes IDENT only (buffered, not processed); the second
    # completes POS_A and POS_B.
    assert decoder.get_calls == 2
    assert decoder.processed == [([IDENT, POS_A, POS_B], [])]
    _assert_decoded(batches)


def test_beast_short_and_non_adsb_frames_are_skipped():
    chunk = (
        _beast("5D484BA898F8C6", kind=b"\x32")  # Mode S short
        + _beast(DF20)
        + _beast(BAD_CRC)
        + _beast(IDENT)  # held as remainder
    )
    worker, decoder, batches, _ = _decode([chunk], data_type="beast")
    assert worker.local_buffer_adsb_msg == []
    assert decoder.processed == []
    assert decoder.get_calls == 1
    assert batches == []


# --------------------------------------------------------------------------
# _run_decoder: skysense


def test_skysense_dispatches_to_skysense_reader():
    now = time.time()
    client = _SkysenseClient([(IDENT, now), (POS_A, now), (POS_B, now)])
    _, decoder, batches, _ = _decode([b"\x24\x00"], "skysense", net_client=client)
    assert client.calls == ["skysense"]
    assert client.buffer == [0x24, 0x00]  # bytes extended as ints
    assert decoder.processed == [([IDENT, POS_A, POS_B], [])]
    _assert_decoded(batches)


def test_skysense_with_no_messages_is_skipped():
    client = _SkysenseClient([])
    _, decoder, batches, _ = _decode([b"\x24"], "skysense", net_client=client)
    assert client.calls == ["skysense"]
    assert decoder.get_calls == 0
    assert batches == []
