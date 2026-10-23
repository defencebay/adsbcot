ADSBCOT's functionality provided by a command-line program called `adsbcot`.

There are several methods of installing ADSBCOT. They are listed below, in order of complexity.

## Debian, Ubuntu, Raspberry Pi

Install from the signed snstac apt repository (PyTAK & AIRCOT resolve
automatically):

```sh linenums="1"
sudo curl -fsSL -o /usr/share/keyrings/snstac.gpg https://snstac.github.io/packages/snstac.gpg
sudo curl -fsSL -o /etc/apt/sources.list.d/snstac.sources https://snstac.github.io/packages/snstac.sources
sudo apt update -qq
sudo apt install adsbcot
```

> **N.B.** This installation method only supports `http://` & `file://` based ADS-B data feeds. To use TCP raw (AVR) or TCP binary Beast feeds, you'll need to install pyModeS, see below.

## Windows, Linux

Install from the Python Package Index (PyPI) [Advanced Users]::

```sh
sudo python3 -m pip install adsbcot
```

## TCP raw & TCP binary Beast support

If you'd like to read decoded ADS-B data over the network, you must install ADSBCOT with the extra pymodes package:

```sh
sudo python3 -m pip install 'adsbcot[with_pymodes]'
```

ADSBCOT needs **pyModeS 2.x, version 2.18 or newer**. pyModeS 3 removed the
streaming decoder ADSBCOT uses, and releases before 2.18 fail with
``ValueError: too many values to unpack``. To fix an existing install:

```sh
sudo python3 -m pip install 'pymodes>=2.18,<3'
```

## ADSBExchange.com Raspberry Pi

These instructions are exclusively for systems running the ADSBExchange.com Raspberry Pi image.

```sh linenums="1"
sudo apt update
sudo apt install -y python3-pip
sudo python3 -m pip install adsbcot
```

## Docker

Build the docker container. The source repository is private (org members
only). With access:

```sh linenums="1"
git clone https://github.com/snstac/adsbcot.git
cd adsbcot/
docker build -t adsbcot -f docker/Dockerfile .
```

Run the docker container with config folder mounted locally. Config files can be edited in the local folder ```/opt/docker/adsbcot/```.

```sh linenums="1"
docker run -d -v /opt/docker/adsbcot/:/etc/adsbcot --name adsbcot adsbcot
```

## Developers

PRs welcome! The source repository is private (org members only). With access:

```sh linenums="1"
git clone https://github.com/snstac/adsbcot.git
cd adsbcot/
python3 setup.py install
```
