ADSBCOT's configuration parameters can be set two ways:

1. In an INI-style configuration file. (ex. ``adsbcot -c config.ini``)
2. As environment variables. (ex. ``export DEBUG=1;adsbcot``)

In the INI file, write values **without quotes** and put comments on their own
line. ``FEED_URL = "https://..."`` passes the quotes through as part of the
URL, and ``LOG_LEVEL = DEBUG  # verbose`` makes the comment part of the value.

```ini
[adsbcot]
# Comments go on their own line.
FEED_URL = https://api.airplanes.live/v2/point/37.8/-122.0/25
```

ADSBCOT has the following built-in configuration parameters:

* **`FEED_URL`**:
    * Default: ``file:///run/dump1090-fa/aircraft.json``

    ADS-B data source URL. Supported URL types:

    - ``file://`` The absolute local folder path to an ADS-B data file in JSON format.
    - ``http://`` or ``https://`` An aircraft data JSON URL: a local piaware / readsb web server (ex. ``http://piaware.local:8080/data/aircraft.json``) or an aggregator API such as airplanes.live or adsb.lol (ex. ``https://api.airplanes.live/v2/point/37.8/-122.0/25``).
    - ``ws://`` or ``wss://`` A WebSocket feed of aircraft JSON. Dropped connections are re-established.
    - ``tcp://`` or ``tcp+raw://`` A dump1090 raw (AVR, ``*8D...;``) output host & port URL (ex. ``tcp+raw://sensor.example.com:30002``). The port defaults to ``30002``.
    - ``tcp+beast://`` A dump1090 Beast binary mode host & port URL (ex. ``tcp+beast://sensor.example.com:30005``). The port defaults to ``30005``.

    IPv6 hosts go in brackets (ex. ``tcp+beast://[2001:db8::1]:30005``). TCP feeds need pyModeS 2.x, see [Installation](installation.md), and reconnect automatically if the decoder restarts.

    > **N.B.** dump1090's port ``30003`` is BaseStation (SBS-1) CSV, which ADSBCOT cannot decode. Use the raw (``30002``) or Beast (``30005``) port.

* **`POLL_INTERVAL`**:
    * Default: ``3`` seconds

    If the `FEED_URL` is of type HTTP, the period, in seconds, to poll this URL.

* **`SENSOR_BEACON`**:
    * Default: ``1``

    Set to ``0`` to disable the periodic receiver marker. Aircraft tracks remain enabled.
    
* **`ALT_UPPER`**:
    * Default: unset

    Upper Altitude Limit, geometric (GNSS / INS) altitude in feet referenced to the WGS84 ellipsoid.

* **`ALT_LOWER`**:
    * Default: unset
    
    Lower Altitude Limit, geometric (GNSS / INS) altitude in feet referenced to the WGS84 ellipsoid.

* **`KNOWN_CRAFT`**:
    * Default: unset

    CSV-style aircraft hints file for overriding callsign, icon, COT Type, etc.

* **`INCLUDE_ALL_CRAFT`**:
    * Default: ``False``

    If ``True`` and ``KNOWN_CRAFT`` is set, will forward all aircraft, including those transformed by the ``KNOWN_CRAFT`` database.

* **`INCLUDE_TISB`**:
    * Default: ``False``

    If ``True``, includes TIS-B tracks.

* **`TISB_ONLY`**:
    * Default: ``False``

    If `True`, only passes TIS-B tracks (`INCLUDE_TISB` must also be `True`).

Additional configuration parameters, including TAK Server configuration, are included in the [PyTAK Configuration](https://pytak.readthedocs.io/en/latest/configuration/) documentation.




