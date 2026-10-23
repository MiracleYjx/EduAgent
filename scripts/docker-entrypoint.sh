#!/bin/sh
set -eu

storage_root="${STORAGE_ROOT:-/app/storage/storage}"
case "$storage_root" in
    /*) ;;
    *)
        echo "STORAGE_ROOT must be an absolute path" >&2
        exit 64
        ;;
esac

mkdir -p "$storage_root"
probe="$storage_root/.readiness-entrypoint-$$.probe"
if ! (umask 077 && : > "$probe"); then
    echo "STORAGE_ROOT is not writable: $storage_root" >&2
    exit 73
fi
rm -f "$probe"

STORAGE_ROOT="$storage_root"
export STORAGE_ROOT

exec "$@"
