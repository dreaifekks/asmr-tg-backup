#!/bin/sh
set -eu

case "${1:-}" in
  ""|--*|-*|run|poll|process|status|enqueue|init|init-config|setup)
    set -- asmr-tg-backup "$@"
    ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  exec "$@"
fi

puid=${PUID:-1000}
pgid=${PGID:-1000}

case "$puid" in
  ""|*[!0-9]*)
    echo "PUID must be a positive integer" >&2
    exit 64
    ;;
esac
case "$pgid" in
  ""|*[!0-9]*)
    echo "PGID must be a positive integer" >&2
    exit 64
    ;;
esac
if [ "$puid" -eq 0 ] || [ "$pgid" -eq 0 ]; then
  echo "PUID and PGID must be greater than zero" >&2
  exit 64
fi

if [ "$(id -g app)" -ne "$pgid" ]; then
  groupmod --non-unique --gid "$pgid" app
fi
if [ "$(id -u app)" -ne "$puid" ]; then
  usermod --non-unique --uid "$puid" app
fi

chown app:app /config
if [ "$(stat -c '%u:%g' /data)" != "$puid:$pgid" ]; then
  chown -R app:app /data
fi

exec gosu app:app "$@"
