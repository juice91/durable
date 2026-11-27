#!/bin/sh
# `docker compose exec api setup-link`: the link that creates the admin account (docs/quickstart-compose.md).
exec eap-entrypoint setup-link "$@"
