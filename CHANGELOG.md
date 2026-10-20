## Unreleased

### Fixed

- Accept `https://` `FEED_URL`s (previously no worker was created and nothing
  was reported). Fixes #59.
- Require `aircot >= 4.0.0`; callsigns now carry the ICAO hex suffix that the
  test suite and CoT output expect. Fixes #61.
- TCP feeds no longer crash with `AttributeError` when pyModeS has not yet
  decoded a callsign; the ICAO hex is used instead. Fixes #67 (thanks
  @Macjja for the report and @Jah-yee for the fix in #68).
- JSON `null` fields no longer crash the converter or show up as the word
  `NONE` in callsigns, remarks and the status feed; numeric fields sent as
  strings are handled. Fixes #24.
- A TCP feed that disconnects is reconnected instead of spinning on the
  closed socket (which starved every other task, including CoT output).
  Refused connections, connection resets and unreadable raw streams are
  retried after `DEFAULT_RECONNECT_DELAY`, and IPv6 `[host]:port` URLs work.
- WebSocket feeds reconnect after the connection drops, a failed handshake
  (e.g. HTTP 503) or an open timeout instead of ending; non-JSON messages are
  skipped.
- `DEFAULT_TCP_RAW_PORT` is now 30002, dump1090's raw output. 30003 is SBS
  CSV, which pyModeS cannot read. Docs and examples corrected.
- A `tcp` `FEED_URL` without a usable pyModeS now fails with a message saying
  what to install, instead of a bare `ValueError` (#30, #23) or, with pyModeS
  3, a crash on the first message. pyModeS releases before 2.18 are reported
  the same way.
- An unsupported `tcp+<type>://` `FEED_URL` (e.g. `tcp+sbs://`) fails with a
  message instead of pyModeS exiting the process.
- `UID_KEY=REG` is honoured.
- Stratux `TargetType` 3/4 (TIS-B) tracks are typed `a-u-A`; craft without a
  type no longer get `Type:0` remarks or `<reg>-0` callsigns.
- A flight-only craft (no ICAO hex) no longer crashes the converter.
- `SignalLevel: null` no longer hides `rssi`.
- A missing `KNOWN_CRAFT` file no longer crashes `ADSBWorker.run()`.
- Known-craft lookups no longer match a CSV row whose registration is
  literally `HEX`.

### Changed

- The `with_pymodes` extra requires `pymodes >= 2.18, < 3`: pyModeS 3 removed
  the streaming decoder, and earlier 2.x releases fail with "too many values
  to unpack" (#22, #33).
- Python 3.9 or newer is required (`asyncio.to_thread` was already used).
- New constant `DEFAULT_RECONNECT_DELAY` (5 seconds).

### Added

- The aircraft model description (`desc`, e.g. `AIRBUS A-320`) from
  readsb-based APIs is included in remarks and `__adsb`. Fixes #60.
- Type hints throughout, checked with `mypy --strict` in CI.
- Test coverage raised to 98% and enforced at 90% in CI; flake8 now actually
  lints `src/` (the Makefile pointed at a path that does not exist).
- Docs: INI values take no quotes and no inline comments (#29), and new
  troubleshooting entries for pyModeS, ports, multicast and stale tracks.

## ADSBCoT 9.2.2

- Add `SENSOR_BEACON=0` to disable the periodic receiver marker without
  disabling aircraft tracks.

## ADSBCoT 9.2.1

- Use PyTAK shared CoT event, point, detail, remarks, and serialization helpers.
- Require `pytak >= 7.3.12`.

## ADSBCoT 9.2.0

- Add `SensorWorker`: periodic `a-f-G-E-S-E` sensor CoT heartbeat every `SENSOR_KEEPALIVE_PERIOD` seconds (default 30).
- Position sourced from system gpsd → static `SENSOR_LAT`/`SENSOR_LON`/`SENSOR_HAE` config → null island fallback.
- Add `gen_sensor_cot()`: reusable CoT builder for sensor beacon events.
- New constants: `DEFAULT_SENSOR_KEEPALIVE_PERIOD=30`, `DEFAULT_SENSOR_LAT/LON/HAE=0.0`.

## ADSBCOT 9.0.0

- Merged ADSBXCOT functionality into ADSBCOT. 
- Merged StratuxCOT functionality into ADSBCOT.
- Code fixes & documentation improvements.
- Remove Python 3.6 support.
- Removed legacy feed url parameters. Only use FEED_URL going forward.
- Renamed _aircotx_ CoT Element to __adsb.
- Added _radio CoT Element with SIGINT data (when available).
- Fixed #49: Windows MIME Type error.
- Fixed #26: Added Dockerfile for Docker support (Thanks @chriselsen)
- Fixed #43: Inotify file moved, reading stops.
- Updated Github Workflows.
- Major version bump from 8.0 to 9.0.
- Moved adsbcot module to src/ directory.
- Moved VERSION to flat-file.
- Refactored handle_data to split out parsing fuctions.
- Added Logging to functions.
- 

## ADSBCOT 8.0.2

Happy Summer Solstice!

- Rewrote GitHub actions, moved most logic to shell script and Makefile.
- Renamed Debian package from python3-adsbcot to adsbcot.
- Standardized Makefile for all PyTAK based programs.
- Cleaned, simplified and expanded documentation.
- Created Makefile jobs for Debian packaging and PyTAK customization.
- Moved all media to media sub directory under docs/.
- Converted README.rst to README.md.
- Style & Linting of code.
- Removed support for Python 3.6.
- Removed support for DUMP1090_URL configuration parameter.
- Added Debian service: /lib/systemd/system/adsbcot.service
- Added Debian conffile: /etc/default/adsbcot
- Added Debian postinst with service config, debug and startup instructions.
- Added with_asyncinotify as an optional extra.

## ADSBCOT 7.0.1

- Fixes #42: ADSBCOT CoT Events are not showing up in iTAK.

## ADSBCOT 7.0.0

- New for 2024!
- Fixed formatting of CHANGELOG.md
- Fixes #28: COT_STALE in documentation.
- Fixes #31: Debian package build is broken.
- Fixes #37: Move RTFD docs over to Markdown.
- Fixes #38: Don't discard ImportError exception.
- Fixes #39: Move CoT generation to PyTAK's `gen_cot_xml()` function.
- Fixes #40: Add CoT `access` attribute.

## ADSBCOT 6.1.0

Added optional altitude filters: ALT_UPPER, ALT_LOWER:
- ALT_UPPER: Upper Altitude Limit, geometric (GNSS / INS) altitude in feet referenced to the WGS84 ellipsoid.
- ALT_LOWER: Lower Altitude Limit, same ref as ALT_UPPER.

## ADSBCOT 6.0.0

Improved support for AirTAK.
- Added a Read the Docs documentation site: https://adsbcot.readthedocs.io
- Added ability to read file:// URLs, including reading aircraft.json from local fs.
- Replaced setup.py metadata with setup.cfg.
- Code cleanup.

## ADSBCOT 5.1.2

- Fixed #17: Incorrect course/track for some dump1090 feeds. Thanks @dnlbaldwin
- Code cleanup.

## ADSBCOT 5.1.1

Adding CoT XML Declaration to output CoT.

## ADSBCOT 5.0.5

Updated adsbexchange.com Raspberry Pi installation instructions.
