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
