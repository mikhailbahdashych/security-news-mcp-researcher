#!/bin/sh
#
# Hand the container to the non-root `app` user, after making sure it can write
# /data.
#
# The image chowns /data before `VOLUME`, which is enough for a *fresh* named
# volume: Docker seeds one from the image, ownership included. It is not enough
# for a volume that already exists — Docker seeds only an empty volume, so an
# `appdata` created by an earlier, root-running image stays owned by root. The
# app would then fail to open /data/app.db (SQLite needs to create -wal and -shm
# next to it), the container would exit, and `restart: unless-stopped` would turn
# that into a restart loop whose error names neither Docker nor ownership.
#
# So: fix the ownership if it is wrong, then drop privileges for real. The chown
# is guarded by the directory's own owner, because a recursive chown over a data
# volume (the SQLite database, npx's package cache) is not something to do on
# every start.
set -eu

APP_USER=app
DATA_DIR=/data

if [ "$(id -u)" = "0" ]; then
    app_uid="$(id -u "$APP_USER")"
    if [ -d "$DATA_DIR" ] && [ "$(stat -c %u "$DATA_DIR")" != "$app_uid" ]; then
        echo "entrypoint: $DATA_DIR is not owned by $APP_USER; fixing it once." >&2
        chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
    fi
    # setpriv, not su/sudo/gosu: it is in util-linux, which the base image
    # already has, and it execs directly with no extra process to sit between
    # the init signal and the server. --init-groups gives the process the
    # supplementary groups `app` actually has, rather than root's.
    exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups "$@"
fi

# Already non-root (`docker run --user ...`): nothing to drop, and no way to
# chown anyway. Whatever /data's ownership is, it is the operator's choice.
exec "$@"
