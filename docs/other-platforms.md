# Other platforms

[Back to the README](../README.md)

## Termux
> [!WARNING]
> These instructions are outdated and have not been tested with the v3 miner.
> They may not work and could cause unexpected problems. Use them at your own
> risk.

**1. Upgrade packages**
```
pkg upgrade
```

**2. Install packages to Termux**
```
pkg install python git libcrypt ndk-sysroot clang zlib binutils
LDFLAGS="-L${PREFIX}/lib/" CFLAGS="-I${PREFIX}/include/" pip install --upgrade wheel
```

**3. Clone this repository**

`git clone https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3`

**4. Go to the miner's directory**

`cd Twitch-Channel-Points-Miner-v3`

**5. Create and edit the configuration**

```sh
mkdir -p config
cp config.example.py config/config.py
nano config/config.py
```

**6. Install packages**
```
pip install -r requirements.txt
pip install Twitch-Channel-Points-Miner-v2
```

**7. Run the miner!**

`python -m TwitchChannelPointsMiner.runner --config-dir ./config`

Read more at [#92](https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/issues/92) [#76](https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2/issues/76)

## Replit

This repository does not currently maintain an official Replit deployment.
