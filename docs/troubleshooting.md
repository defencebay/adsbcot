
To report bugs, please set the DEBUG=1 environment variable to collect logs:

```sh
DEBUG=1 adsbcot
```

Or:

```sh linenums="1"
export DEBUG=1
adsbcot
```

Or:

```sh linenums="1"
echo 'DEBUG=1' >> adsbcot.ini
adsbcot -c adsbcot.ini
```

You can view systemd/systemctl/service logs via:

```journalctl -fu adsbcot```

## Common problems

* **``ValueError: FEED_URL=tcp... needs pyModeS 2.x``**: install pyModeS 2.18 or
  newer but not 3.x: ``python3 -m pip install 'pymodes>=2.18,<3'``.
* **No aircraft from an ``https://`` feed**: upgrade; releases before this one
  silently ignored ``https://`` FEED_URLs.
* **Nothing decoded from ``tcp://host:30003``**: port 30003 is SBS CSV. Use the
  raw port 30002 or the Beast port 30005.
* **Multicast leaves on the wrong interface**: set
  ``PYTAK_MULTICAST_LOCAL_ADDR`` to the IP address of the interface to send
  from. This is a PyTAK setting; use a current PyTAK release, see the PyTAK
  configuration documentation.
* **Aircraft stay on the map after they leave**: each track disappears from
  TAK clients when its CoT goes stale; lower ``COT_STALE`` (seconds) to drop
  them sooner.

Please use GitHub issues for support requests. Please note that ADSBCOT is free open source software and comes with no warranty. See LICENSE.