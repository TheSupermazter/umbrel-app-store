# Thomas's Umbrel app store

A community app store for umbrelOS. Add it in Umbrel under
App Store → ⋯ → Community App Stores with this repository's URL.

## Repository visibility: private by default

Umbrel can only read community app stores from **public** repositories, but this
repository is meant to stay **private** except while an app is being installed
or updated.

- **After a successful install or update** on the Umbrel, set the repository
  back to private: GitHub → Settings → General → Danger Zone → Change
  visibility → Private.
- **Before installing a new app or an update**, set it to public again. Umbrel
  checks the store every 5 minutes, so wait a few minutes before the new app or
  update appears.
- **While private**, installed apps keep running. Umbrel can't check the store
  for new apps or updates, and errors about that in its logs are expected.

> **For Claude (or anyone) asking to add or update an app here:** first tell
> Thomas to set this repository to **public**, because Umbrel cannot install
> from it otherwise. Once the app is installed and works, remind him to set it
> back to **private**.

## Apps

### Caddy (`thomas-caddy`)

[Caddy](https://caddyserver.com) 2.11.6 as a reverse proxy that publishes
Jellyfin at `https://stream.thomasnorden.nl`, and nothing else.

- **Ports:** host port `40443` is Caddy's HTTPS port; the router forwards public
  TCP 443 to `192.168.1.15:40443`. Port 80 is not used.
- **Certificate:** Let's Encrypt over port 443 (TLS-ALPN challenge); stored in
  the app's data folder. No API tokens.
- **DNS:** `stream.thomasnorden.nl` is a DNS-only (grey cloud) CNAME at
  Cloudflare to `vpn.thomasnorden.nl`, which the router keeps updated.
- **Configuration:** `Caddyfile.template`, rendered by Umbrel to `Caddyfile`
  (no `$` allowed, see Debrid Fetch). Change it, bump `version` and update the
  app. Requests for names not in the file are refused.
- **Dashboard tile:** only a status page on port 8790, behind the Umbrel login.

### Debrid Fetch (`thomas-debrid-fetch`)

A small web app (`server.py.template` and `index.html.template`, run by the stock
`python:alpine` image) that lists a TorBox or Real-Debrid library, downloads the
ticked files to a local folder with aria2, and manages that folder.

- **No credentials in this repository.** API keys are entered in the app's
  Settings and stay in its data folder on the device.
- **Login:** none of its own; it sits behind the Umbrel login.
- **Download folder:** the folder selected for the app (Downloads by default),
  mounted at `/downloads`. Choose e.g. `External/PS4HDD/Nordflix`.
- **aria2:** on first start the app downloads a static aria2c 1.37.0 build from
  `abcfy2/aria2-static-build` into its data folder and checks it against the
  SHA-256 pinned in `server.py`. Without it, a built-in single-connection
  downloader is used.
- **Checkmarks and "Removed from debrid":** based on `manifest.json` in the data
  folder, which records which debrid item each downloaded file came from. Files
  already on the drive are matched by name and size.
- **Updating:** edit `server.py.template` / `index.html.template` and bump
  `version` in `umbrel-app.yml`. An Umbrel update only copies top-level
  `docker-compose.yml`, `*.template`, `exports.sh`, `torrc` and `umbrel-app.yml`
  files, which is why the code is shipped as templates. Umbrel runs templates
  through `envsubst`, so they must not contain `$` followed by `{` or a letter
  (the page uses string concatenation instead of JavaScript `${...}` templates).
