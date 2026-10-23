#!/usr/bin/env bash
# Package photo-archive for another machine:
#   scripts/make-dist.sh                 code only (install.sh asks for the key file)
#   scripts/make-dist.sh --key KEY.json  also bundle the service account key (keep it private!)
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -z "$(git status --porcelain)" ]] || { echo "commit your changes first" >&2; exit 1; }
rev="$(git rev-parse --short HEAD)"
mkdir -p dist
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
git archive --prefix=photo-archive/ HEAD | tar -x -C "$tmp"
name="photo-archive-$rev"
if [[ "${1:-}" == --key ]]; then
    [[ -r "${2:-}" ]] || { echo "usage: $0 --key KEY.json" >&2; exit 1; }
    install -m 600 "$2" "$tmp/photo-archive/service-account.json"
    name="$name-with-key"
fi
(umask 077; tar -C "$tmp" -czf "dist/$name.tar.gz" photo-archive)
echo "dist/$name.tar.gz"
if [[ "$name" == *with-key ]]; then echo "Contains a private key: copy it securely and delete it after install."; fi
