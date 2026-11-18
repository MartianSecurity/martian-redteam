#!/bin/sh
set -eu
last_tag="$(git describe --tags --abbrev=0)"
git log --merges --format='%s' "${last_tag}..HEAD"
