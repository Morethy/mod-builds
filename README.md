# Starling's mod builds

Native mods are compiled into [Starling](https://github.com/starling-client/starling), and a phone can't do that itself. When a player taps **Compile**, Starling sends their mods to the Compile service (`worker/`, on Cloudflare), which asks this repository to compile the game with them. The phone then downloads the result from this repository's `results` release.

- `worker/` is the service the game talks to, at the address in Starling's [`build-server.txt`](https://github.com/starling-client/starling/blob/starling/packaging/android/build-server.txt). It seals the mods (AES-256-CTR, a key for each build) into the `requests` release under a random name, and starts `build.yml` with the key in the run's inputs. The same mods on the same commit are the same job, so a build is never done twice. `npm test` there runs its tests, with no network.
- `build.yml` compiles one player's mods. A mod's code runs while it compiles, so that happens in a sandbox with no network, and that job's token can only read this repository. A second job, which runs nothing from the mods, puts the library and its status in `results`, and deletes the sealed mods.
- `clean.yml` deletes old builds, and sealed mods no run took, every six hours.
- `base.yml` makes the clean build cache for one version of the game. It only runs trusted code, and it's the only job that writes the cache.
- `versions.json` says which commit each version of the game is.

Runs and their logs are public, but the mods aren't: the key never shows up in a log. Builds are kept for two weeks.

The service's answer to a build names this repository (`repo`), and a Starling that knows it waits for `results-<job's first digit>/<job>.json` (or `.failed.json`) and downloads `<job>.so` straight from GitHub, asking the service only every ten minutes or so while it waits. A Starling before it asks `GET /v1/job/<job>` again two seconds after each answer, so the service holds that answer up to 25 s.

## Compile v2: a library per mod

With the game as a Rust library (a Nightly with its SDK), a native mod is its own small library, built once for each game build and ABI, and kept in a store the game reads straight from GitHub:

- `store/builds.json`: every game build the store serves, newest first: its commit, its Nightly, its SDK, and for each ABI the game's own runtime archive (url, SHA-256, size), so a game can move to a newer Nightly without a new APK.
- `mods-<build>/index.json`: that build's mods, each id, version and `.hmod` SHA-256 to its library (`<job>.so`), the library's SHA-256 and its identity, and the mods that failed there. A job is the `.hmod`'s SHA-256 (32 digits) and the ABI.
- `catalog.yml` builds every native mod of [Horizon's catalog](https://horizon-rust.github.io/horizon-mods/) for one build, ahead of the players. Given the build's `sdk` (the release download folder with its `sdk.json`), it first adds the build to `builds.json`.
- `mod.yml` builds one mod the store doesn't have, asked through the service (`POST /v2/mod?build=B&abi=A` with the `.hmod`). Its answer is `mods-<build>/<job>.json` or `.failed.json`, which the game waits for on GitHub.

Both build in a sandbox (`scripts/sandbox.sh`) with a token that can only read, and publish from a job that runs nothing of the mods (`scripts/store.py`). The compiling itself is the SDK's: `scripts/mod_lib.py` calls the compiler the repository variable `MOD_LIB_COMPILER` names, a path in the SDK, and until it's set every v2 build fails, not for good, saying it can't yet. `python -m unittest discover -s scripts` tests the scripts.

## Your own build service

Anyone can run their own copy of this, on their own GitHub and Cloudflare accounts, and point Starling at it. It costs nothing: Actions are free on a public repository, and so are Workers up to 100,000 requests a day.

1. **Fork** this repository, and keep the fork public (on a private one, every build spends your Actions minutes).
2. **Turn on Actions** in the fork: the Actions tab, then "I understand my workflows, go ahead and enable them". GitHub turns off the workflows of a fork, `clean.yml`'s schedule too.
3. **Make the releases** the builds go to. With the [GitHub CLI](https://cli.github.com/), in a Bash shell:
   ```bash
   for tag in requests results results-{0..9} results-{a..f}; do
     gh release create "$tag" -R YOU/mod-builds --prerelease --title "$tag" --notes ""
   done
   ```
4. **Build the base** of each version in `versions.json`: Actions, `base`, Run workflow, with the version (`0.8.0`). It takes about a quarter of an hour, and every player's build starts from it. Run it again when a version is added. Each base is about 3 GB of Actions cache, and a repository gets 10 GB.
5. **Give the service a way in**, one of:
   - a GitHub App (Settings, Developer settings, GitHub Apps, New), with Actions and Contents set to Read and write, installed on the fork alone. Its token has its own hourly limit, and failed runs don't mail you. Keep its id and a private key.
   - a fine-grained token (Settings, Developer settings, Personal access tokens), for the fork alone, with Actions and Contents set to Read and write. It shares your 5,000 API calls an hour, and GitHub mails you every run that fails.
6. **Put the service on Cloudflare**, with [Node.js](https://nodejs.org/) and a free Cloudflare account. In `worker/wrangler.toml`, add your fork:
   ```toml
   [vars]
   REPO = "YOU/mod-builds"
   ```
   then, in `worker/`:
   ```bash
   npm install
   npx wrangler login
   npx wrangler deploy
   ```
   and give it the App:
   ```bash
   echo 123456| npx wrangler secret put GITHUB_APP_ID
   openssl pkcs8 -topk8 -nocrypt -in the-app.private-key.pem | npx wrangler secret put GITHUB_APP_KEY
   ```
   or the token: `npx wrangler secret put GITHUB_TOKEN`, then paste it. `wrangler deploy` prints the service's address, `https://starling-compile.SOMETHING.workers.dev`.
7. **In Starling**: Options, Mods, the Native mods card, **Build server...**, then the address and **Use this server**. Only `https://` addresses are taken. **Use Starling's** goes back to Starling's own.

What a service can do, as it is: GitHub runs 20 jobs at a time on a free account (each build is two), the service starts 32 builds a day for one address and 1,500 for everyone (`PER_ADDRESS` and `PER_DAY` in `worker/src/index.js`), and the free Workers plan answers 100,000 requests a day. Past that, Cloudflare answers 429 until midnight UTC.

The library a build makes runs inside the game on the player's phone, so whoever runs a build service can put anything in it. Only point Starling at a service you trust, and only tell players to use yours if they trust you.

More in [docs/starling-native-mods.md](https://github.com/starling-client/starling/blob/starling/docs/starling-native-mods.md).
