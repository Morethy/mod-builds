# Your own build server on a Linux machine

When a player taps **Compile**, Starling downloads each native mod's library from Starling's store, already built for
their game. A mod the store doesn't have yet goes to a build server, which compiles it and puts the answer on GitHub,
where the phone downloads it. Starling's own server runs on Cloudflare and builds on GitHub Actions, with daily limits.

This page sets one up on a Linux machine of your own, such as a VPS: your machine compiles, and your fork of this
repository holds the answers. Players reach it with **Build server...** on the Compile card.

> **The builds you serve run on your players' phones with the game's full access.** Starling shows a red warning for
> any server that isn't its own, with your server's name on it. Only serve mods you'd install yourself, and keep the
> machine up to date.

## What you need

- A Linux machine (Ubuntu 24.04 or Debian 12) with **8 GB of RAM or more** (16 GB is better), **4 or more cores** and
  **60 GB of free disk**. A game build's base takes about 45 minutes the first time on 4 cores; a mod then takes 1 to 5.
- **A domain name** pointing at the machine, or its IP through [sslip.io](https://sslip.io) (`1-2-3-4.sslip.io` for
  `1.2.3.4`). Starling only talks HTTPS to a host name.
- **A GitHub account** for your fork.

## 1. Fork this repository

1. Fork [starling-client/mod-builds](https://github.com/starling-client/mod-builds). Keep it **public**: phones
   download the answers from its releases without an account.
2. Make a [fine-grained token](https://github.com/settings/personal-access-tokens/new) for **that repository only**,
   with **Contents: Read and write** and nothing else. Copy it; you'll put it on the machine in step 4.

## 2. A user for the builds

The base script replaces its user's Rust and Android tools, and the sandbox needs `sudo`, so give the server a user of its
own on a machine that does nothing else:

```sh
sudo adduser --disabled-password --gecos "" starling
echo "starling ALL=(root) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/starling
sudo chmod 440 /etc/sudoers.d/starling
```

The sandbox (`scripts/sandbox.sh`) uses `sudo` to build each mod with no network, none of the machine's files and no way
to get privileges back, over a copy of the base it throws away after. That's why the user needs it, and why the machine
should run nothing else.

## 3. Tools

```sh
sudo apt update
sudo apt install -y git curl python3 rsync unzip build-essential pkg-config openjdk-17-jre-headless gh
```

The Android command line tools, which the base script uses to install the NDK it pins:

```sh
sudo -iu starling
mkdir -p ~/android-sdk/cmdline-tools && cd ~/android-sdk/cmdline-tools
curl -fsSLO https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
unzip -q commandlinetools-linux-*_latest.zip && mv cmdline-tools latest && rm commandlinetools-linux-*_latest.zip
yes | ~/android-sdk/cmdline-tools/latest/bin/sdkmanager --licenses > /dev/null
```

Then your fork, as that user:

```sh
git clone https://github.com/YOUR-NAME/mod-builds ~/mod-builds
```

## 4. The service

As root, put the token where only the service reads it:

```sh
sudo install -d -m 700 -o starling /etc/starling-build
echo "GH_TOKEN=github_pat_..." | sudo tee /etc/starling-build/env > /dev/null
echo "STARLING_REPO=YOUR-NAME/mod-builds" | sudo tee -a /etc/starling-build/env > /dev/null
sudo chmod 600 /etc/starling-build/env && sudo chown starling /etc/starling-build/env
```

Then the service, from this folder:

```sh
sudo cp ~starling/mod-builds/vps/starling-build.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now starling-build
journalctl -u starling-build -f
```

It listens on `127.0.0.1:8790` only. `MAX_PER_DAY` in the env file changes how many builds one address may ask for in a
day (20 unless set).

## 5. HTTPS

[Caddy](https://caddyserver.com/docs/install) gets and renews the certificate itself:

```sh
sudo apt install -y caddy
sudo cp ~starling/mod-builds/vps/Caddyfile /etc/caddy/Caddyfile
sudo sed -i "s/build.example.com/YOUR-HOST/" /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Open ports 80 and 443 in the machine's firewall.

## 6. Try it

In Starling: **Mods → Compile → Build server...**, type `https://YOUR-HOST`, and Compile a mod the store doesn't have.
The first mod for a game build makes its base, about 45 minutes; the card waits. After that, a mod takes minutes, and a
mod anyone already compiled here comes back at once.

What the server does, in `journalctl -u starling-build -f`: `store_base.sh` for a new game build, `sandbox.sh` for each
mod, then `store.py publish` and `store.py index` putting the answer in your fork's `mods-<build>` release.

## Keeping it going

- **Updates:** `git -C ~starling/mod-builds pull` as `starling`, then `sudo systemctl restart starling-build`.
- **A new Starling version** needs nothing: the first mod asked for its game build makes the base. One base is kept at a
  time, the last one asked for, so serving two game builds at once rebuilds bases back and forth.
- **Disk:** a base is about 25 GB (`~/store`, `~/android`, `~/.rustup`).
- **Your fork's releases** grow with each mod. GitHub allows 1000 files in a release; delete old `mods-<build>`
  releases you no longer serve.

## How it fits with Starling's own server

The phone asks Starling's store first, so mods the store already has never reach your server. Only the mods it doesn't
have are sent, as the `.hmod` the player has. The answer is what Starling's own `mod.yml` makes, with the same sandbox
and the same scripts, so a mod that fails here fails the same way there.
