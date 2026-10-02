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

### OpenList (`thomas-openlist`)

[OpenList](https://github.com/OpenListTeam/OpenList) 4.2.6, used to browse
debrid storage (TorBox, Real-Debrid) over WebDAV and copy files to a local
drive in the background.

- **No credentials in this repository.** Debrid logins and API keys are entered
  in OpenList's own storage settings and stay in its database on the device.
- **Admin login:** `admin` with the password Umbrel shows on the app page. It is
  applied at every start, so a password changed inside OpenList is reset on restart.
- **Data location:** choose an external drive when installing. The internal
  storage is small, and OpenList keeps its database and temp folder there.
- **Folder access "Nordflix":** pick the target folder (e.g. `External/PS4HDD/Nordflix`).
  It appears in the container at `/nordflix` (read-write). Add a Local storage
  in OpenList with root path `/nordflix`.
- **WebDAV storages:**
  - TorBox: `https://webdav.torbox.app`, user `torbox`, password = API key
    (or email + account password)
  - Real-Debrid: `https://dav.real-debrid.com`, your username + the WebDAV
    password from "My account"
- **Access:** through the Umbrel app proxy on port 5244 (LAN and Tailscale).
  Not meant to be exposed publicly.
- **Updating:** change the image tag and digest in
  `thomas-openlist/docker-compose.yml` and `version` in `umbrel-app.yml`.

### Debrid Fetch (`thomas-debrid-fetch`)

A small web app (one Python script in `thomas-debrid-fetch/app`, run by the stock
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
- **Updating:** change `app/server.py` / `app/index.html` and bump `version` in
  `umbrel-app.yml`.

### RDT-Client (`thomas-rdt-client`)

[RDT-Client](https://github.com/rogerfar/rdt-client) 2.0.142, which downloads
finished torrents from a debrid service (Real-Debrid, TorBox and others) to a
local drive.

- **No credentials in this repository.** The login and the debrid API key are
  entered in RDT-Client and stay in its database on the device.
- **Login:** the first username and password entered become the login. It sits
  behind the Umbrel login as well.
- **One provider per install.** RDT-Client connects to a single debrid service.
- **Download folder:** the folder selected for the app (Downloads by default),
  mounted at `/data/downloads`, RDT-Client's default download path.
- **Choosing files:** ticking individual files only works with Real-Debrid. For
  TorBox use the minimum file size and the include/exclude patterns per torrent.
- **`SKIP_CHOWN`:** set so the image does not change ownership of everything in
  the download folder at each start.
- **Sonarr/Radarr:** use host `thomas-rdt-client_server_1`, port `6500`, as a
  qBittorrent download client.
- **Updating:** change the image tag and digest in
  `thomas-rdt-client/docker-compose.yml` and `version` in `umbrel-app.yml`.
