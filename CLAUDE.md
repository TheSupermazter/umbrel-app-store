# Notes for Claude

Before adding or updating an app in this store, read "Repository visibility:
private by default" in README.md. The repository is normally private and must
be made public by Thomas before Umbrel can install from it; remind him to set it
back to private after a successful install.

App updates only copy top-level `docker-compose.yml`, `*.template`, `exports.sh`,
`torrc` and `umbrel-app.yml` files into an installed app; anything else (like a
subfolder of code) only arrives on a fresh install. `*.template` files are run
through `envsubst`, so never put `$` followed by `{` or a letter in them; check
with `envsubst < file.template | cmp - file.template`.
