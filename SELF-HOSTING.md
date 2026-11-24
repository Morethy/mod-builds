# Your own build server (Compile v2)

When a player taps **Compile**, Starling downloads each native mod's library from Starling's store, already built for
their game build. A mod the store doesn't have yet goes to a build server, which compiles it and puts the answer in a
GitHub release, where the phone downloads it. Starling's own server has daily limits; your own has yours.

There are two ways to run one:

| | GitHub Actions | A Linux machine (VPS) |
|---|---|---|
| Compiles on | GitHub's runners, free for a public fork | your machine |
| The door the phone talks to | a Cloudflare Worker (free plan) | `vps/server.py` behind Caddy |
| Answers kept in | your fork's releases | your fork's releases |
| A mod takes | 5 to 10 minutes (a runner starts, restores the base) | 1 to 5 minutes once the base is made |
| You pay | nothing | the machine |
| Guide | below | [vps/README.md](vps/README.md) |

Players use either one with **Mods → Compile → Build server...**, typing its `https://` address. The phone still asks
Starling's store first: only mods it doesn't have reach your server.

> **The builds you serve run on your players' phones with the game's full access.** Starling shows a red warning for
> any server that isn't its own, with your server's name on it. Only build mods you'd install yourself.

## On GitHub Actions

You need a GitHub account and a [Cloudflare](https://dash.cloudflare.com/sign-up) account (the free plan is enough), and
Node.js 20 or newer on your computer for the Worker's deploy tool.

### 1. Fork this repository

1. Fork [starling-client/mod-builds](https://github.com/starling-client/mod-builds) and keep it **public**: runners are
   free there, and phones download the answers from its releases without an account.
2. In the fork, **Actions → I understand my workflows, go ahead and enable them**.
3. Make the release the Worker leaves requests in, from the fork's **Releases → Draft a new release**: tag `requests`,
   title `requests`, **Set as a pre-release**, publish. Or with the [gh CLI](https://cli.github.com/):

   ```sh
   gh release create requests --prerelease --title requests --notes "" -R YOUR-NAME/mod-builds
   ```

### 2. A base for each game build you serve

A phone asks for its own game build: the commit its APK was built from. Your fork needs a base for each one you serve,
made once by `store-base.yml`, which also lists the build in your fork's `store/builds.json` (the Worker refuses a build
that isn't there).

1. Find the build in Starling's store: open
   <https://github.com/starling-client/mod-builds/releases/download/store/builds.json>. Each entry has `build` (the
   commit) and `profile`. The newest is first.
2. In the fork, **Actions → store-base → Run workflow**, with `build` set to that commit and `profile` to that
   `profile` (`fast` for releases and Previews built with it). It takes about 45 minutes.

Run it again for each new Starling version you want to serve. A base is a cache entry; GitHub drops one that isn't used
for a week, and the first mod asked for after that fails with "lost the job" until you run `store-base` again.

### 3. A token for the Worker

The Worker starts `mod.yml` in your fork and keeps the sealed mods in its `requests` release. Either:

- **A GitHub App** (better: its token has its own hourly limit). **Settings → Developer settings → GitHub Apps → New
  GitHub App**: any name, any homepage, untick **Webhook → Active**, and under **Repository permissions** set **Actions**
  and **Contents** to **Read and write**. Create it, note its **App ID**, **Generate a private key** (a `.pem` file),
  then **Install App** on your fork only.
- **Or a fine-grained token** for your fork only, with **Actions** and **Contents** set to **Read and write**. Simpler,
  but it shares your own hourly limit and GitHub mails you about each failed run.

### 4. The Worker

From your fork's `worker/` folder on your computer:

```sh
cd worker
npm install
```

In `wrangler.toml`, give the Worker a name of its own and your fork:

```toml
name = "my-starling-compile"

[vars]
REPO = "YOUR-NAME/mod-builds"
```

Then deploy it and give it the token:

```sh
npx wrangler login
npx wrangler deploy
# A GitHub App:
echo <the App's id> | npx wrangler secret put GITHUB_APP_ID
openssl pkcs8 -topk8 -nocrypt -in <the App's .pem> | npx wrangler secret put GITHUB_APP_KEY
# Or a token instead:
echo <the token> | npx wrangler secret put GITHUB_TOKEN
```

`wrangler deploy` prints the address, `https://my-starling-compile.<you>.workers.dev`. That's what players type.

### 5. Try it

In Starling: **Mods → Compile → Build server...**, type the Worker's address, and Compile a mod Starling's store
doesn't have. Your fork's **Actions** shows a `mod` run; when it ends, its answer is in the release `mods-<build>`, and
the phone loads the mod at the next start. A mod anyone already compiled there comes back at once.

### Limits

GitHub's free runners have no minute limit on a public repository, but a repository that keeps them busy all day may be
asked to stop. The Worker counts builds per address and per day (`limit` in `worker/src/index.js`), and the free
Cloudflare plan allows 100,000 requests a day. A release holds at most 1000 files: delete old `mods-<build>` releases you
no longer serve.

## On a Linux machine

See [vps/README.md](vps/README.md). The machine compiles and answers the phone itself, with the same sandbox and the same
scripts as `mod.yml`, and puts the answers in your fork's releases as above, so you still fork this repository
(step 1 above, without the `requests` release) and make a token for it (Contents only).
