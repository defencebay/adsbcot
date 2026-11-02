"""Exercise the WebSockets 14/15 client against a real local feed."""

import asyncio
from configparser import ConfigParser
from contextlib import suppress
from http import HTTPStatus
import json
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import pytest
import pytak
import websockets

import adsbcot


@pytest.mark.parametrize("reject_first", [False, True])
def test_websocket_feed_emits_cot_and_retries_upgrade(
    tmp_path, monkeypatch, reject_first
):
    monkeypatch.setattr(adsbcot, "DEFAULT_RECONNECT_DELAY", 0.01)

    async def run():
        attempts = 0

        def upgrade(connection, _request):
            nonlocal attempts
            attempts += 1
            if reject_first and attempts == 1:
                return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Try again")
            return None

        async def feed(connection):
            await connection.send("not json")
            await connection.send(
                json.dumps(
                    {
                        "hex": "ABC123",
                        "lat": 52.1,
                        "lon": 21.0,
                        "flight": "BUILDTEST",
                        "alt_geom": 12000,
                    }
                )
            )

        parser = ConfigParser()
        parser.read_dict({"DEFAULT": {"INCLUDE_ALL_CRAFT": "true"}})
        queue = asyncio.Queue()
        worker = adsbcot.ADSBWorker(queue, parser["DEFAULT"])
        worker.status = pytak.StatusWriter("test", path=str(tmp_path / "status.json"))
        async with websockets.serve(
            feed, "127.0.0.1", 0, process_request=upgrade
        ) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            task = asyncio.create_task(worker._run_feed(url, urlparse(url), 1))
            try:
                event = ET.fromstring(await asyncio.wait_for(queue.get(), timeout=5))
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)

        assert attempts >= (2 if reject_first else 1)
        assert event.get("uid") == "ICAO-ABC123"
        assert float(event.find("point").get("lat")) == 52.1
        assert event.find("detail/__adsb").get("icao") == "ABC123"

    asyncio.run(run())
