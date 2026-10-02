# Thomas's Umbrel app store

A community app store for umbrelOS. Add it in Umbrel under
App Store → ⋯ → Community App Stores with this repository's URL.

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

### TorBox Media Center (`thomas-torbox-media-center`)

[TorBox Media Center](https://github.com/TorBox-App/torbox-media-center) 2.0.0.
It turns the video files in a TorBox account into a library of `.strm` link
files that Jellyfin and Emby stream straight from TorBox.

- **No credentials in this repository.** The TorBox API key is entered on the
  app's setup page and stays in the app's data folder on the device. The media
  center prints the key when it starts; the launcher hides it in the logs.
- **Setup page:** upstream has no web UI, so `launcher.py.template` adds one. It
  takes the API key and settings, shows the status and log, and runs the media
  center. It sits behind the Umbrel login and has no login of its own.
- **Library location:** a `torbox` folder inside the folder selected for the app
  (Downloads by default). In Jellyfin on Umbrel that is `/downloads/torbox/movies`
  and `/downloads/torbox/series`.
- **The `torbox` folder is emptied on every start.** That is how the media center
  works. The launcher only starts it when the folder is new, was used by the app
  before, or contains nothing but `.strm` files; otherwise the setup page asks first.
- **`.strm` only.** The FUSE method (needed for Plex) is not packaged, because it
  needs extra privileges and a shared mount on the host.
- **Updating:** change the image tag and digest in
  `thomas-torbox-media-center/docker-compose.yml`, `version` in `umbrel-app.yml`
  and the version in `USER_AGENT` in `launcher.py.template`. The template must
  not contain dollar signs, because Umbrel renders it with `envsubst`.
