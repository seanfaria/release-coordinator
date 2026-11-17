#!/bin/sh
# Runs inside the nginx container on every start, before nginx itself starts
# (the official nginx image runs every executable script in /docker-entrypoint.d/).
#
# active.conf says which app container gets traffic. It's generated (deploy.py
# rewrites it on every deploy), so it isn't stored in git. On a fresh checkout
# it doesn't exist yet, so create it from the placeholder ("nothing deployed").
set -e
dir=/etc/nginx/upstream
if [ ! -f "$dir/active.conf" ]; then
    cp "$dir/active.placeholder.conf" "$dir/active.conf"
    echo "$0: created $dir/active.conf from the placeholder"
fi
