#!/usr/bin/env bash
# Interactive installer for photo-archive. Safe to re-run: current settings are
# offered as defaults, existing data and passwords are kept.
#
#   1 System packages (sudo apt): Docker, Python 3, exiftool, ffmpeg, sqlite3, openssl
#   2 Data folder (Takeout staging, Library, Archive, state, logs)
#   3 Immich photo server (Docker), its API key and external library
#   4 photo-archive code + rclone (checksum-verified)
#   5 Google service account key
#   6 Drive Takeout folder (shared with the service account as Editor)
#   7 Apps Script that trashes verified Takeout files (runs in your Google account)
#   8 Monthly cron job
#
# Linux (Debian/Ubuntu, amd64/arm64). Run as your normal user; it uses sudo for steps 1 and 3.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
LIB_DIR="$HOME/.local/lib/photo-archive"
CONFIG_DIR="$HOME/.config/photo-archive"
CONFIG_FILE="$CONFIG_DIR/config.env"
CRON_TAG="# photo-archive"
MIN_RCLONE="1.63"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { echo; bold "== $* =="; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
ask() {
    local reply
    if [[ -n "$2" ]]; then read -r -p "$1 [$2]: " reply </dev/tty
    else read -r -p "$1: " reply </dev/tty; fi
    printf '%s' "${reply:-$2}"
}
confirm() {
    local hint="y/N"; [[ "$2" == y ]] && hint="Y/n"
    local reply; read -r -p "$1 [$hint]: " reply </dev/tty
    reply="$(printf '%s' "${reply:-$2}" | tr '[:upper:]' '[:lower:]')"
    [[ "$reply" == y || "$reply" == yes ]]
}
# Single-quote for config.env (parsed by both bash and Python's shlex).
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
json_field() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$2" | head -n 1; }
version_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN { n = split(a, x, "."); m = split(b, y, ".")
        for (i = 1; i <= (n > m ? n : m); i++) { if (x[i]+0 > y[i]+0) exit 0; if (x[i]+0 < y[i]+0) exit 1 }
        exit 0 }'
}

bold "photo-archive installer"
echo "Monthly: download Google Takeout from Drive -> verify -> let Google trash it -> update the photo library."
[[ $EUID -ne 0 ]] || die "run as your normal user (it asks for sudo when needed)"
[[ "$(uname -s)" == Linux ]] || die "Linux only"
command -v apt-get >/dev/null || die "Debian/Ubuntu (apt) only"
case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) die "unsupported CPU $(uname -m)" ;;
esac

# Previous settings become defaults.
BASE_DIR=""; RCLONE_BIN=""; RCLONE_REMOTE="photo-archive-drive"; SERVICE_ACCOUNT_FILE=""
SERVICE_ACCOUNT_EMAIL=""; FOLDER_ID=""; FOLDER_NAME="Takeout"; IMMICH_URL="http://localhost:2283"
WORKERS=6
if [[ -f "$CONFIG_FILE" ]]; then
    echo "Found $CONFIG_FILE; its values are offered as defaults."
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
fi

# --- 1. system packages --------------------------------------------------------------
step "1/8 System packages"
pkgs=(python3 sqlite3 unzip curl ca-certificates openssl libimage-exiftool-perl ffmpeg cron)
command -v docker >/dev/null || pkgs+=(docker.io)
if ! docker compose version >/dev/null 2>&1 && ! sudo docker compose version >/dev/null 2>&1; then
    if apt-cache show docker-compose-v2 >/dev/null 2>&1; then pkgs+=(docker-compose-v2)
    elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then pkgs+=(docker-compose-plugin)
    else pkgs+=(docker-compose); fi
fi
missing=()
for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if (( ${#missing[@]} )); then
    echo "Installing: ${missing[*]}"
    sudo apt-get update -qq
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
else
    echo "All present."
fi
sudo systemctl enable --now docker >/dev/null
id -nG | grep -qw docker || { sudo usermod -aG docker "$USER"; echo "Added $USER to the docker group (applies at next login)."; }

# --- 2. data folder --------------------------------------------------------------------
step "2/8 Data folder"
echo "Holds Takeout/ (download staging), Library/ (your photos), Archive/, state/, logs/, immich/."
echo "Needs room for the library plus one full Takeout export (several hundred GB is typical)."
while true; do
    BASE_DIR="$(ask "Data folder" "${BASE_DIR:-$HOME/PhotoArchive}")"
    BASE_DIR="${BASE_DIR/#\~/$HOME}"; BASE_DIR="${BASE_DIR%/}"
    [[ "$BASE_DIR" == /* ]] || { warn "Use an absolute path."; continue; }
    break
done
sudo mkdir -p "$BASE_DIR"
sudo chown "$USER": "$BASE_DIR"
mkdir -p "$BASE_DIR"/{Takeout,Library,Archive,state,logs,immich}
df -h "$BASE_DIR" | tail -n 1 | awk '{print "Free space: " $4 " on " $6}'

# --- 3. Immich ---------------------------------------------------------------------------
step "3/8 Immich photo server"
IMMICH_DIR="$BASE_DIR/immich"
REL="https://github.com/immich-app/immich/releases/latest/download"
set_env() {  # set_env KEY VALUE FILE
    if grep -qE "^#?\s*$1=" "$3"; then sed -i -E "s|^#?\s*$1=.*|$1=$2|" "$3"; else echo "$1=$2" >>"$3"; fi
}
(
    cd "$IMMICH_DIR"
    # An earlier root-run install may own these; the database folder stays as Docker made it.
    for f in docker-compose.yml .env docker-compose.override.yml; do
        [[ -e "$f" && ! -O "$f" ]] && sudo chown "$USER": "$f"
    done
    curl -fsSL -o docker-compose.yml "$REL/docker-compose.yml"
    if [[ ! -f .env ]]; then
        curl -fsSL -o .env "$REL/example.env"
        set_env DB_PASSWORD "$(openssl rand -hex 24)" .env
    fi
    chmod 600 .env
    set_env UPLOAD_LOCATION "$IMMICH_DIR/upload" .env
    set_env DB_DATA_LOCATION "$IMMICH_DIR/postgres" .env
    set_env TZ "$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo Etc/UTC)" .env
    # Library is writable so Immich can save your edits (dates, descriptions) into
    # <photo>.xmp sidecars; read-only makes Immich silently revert them.
    # Immich runs as you, not root: the sidecars it writes stay editable by the
    # pipeline, and a compromised Immich can't act as root on the Library.
    mkdir -p upload ml-cache/{cache,home-cache,home-config}
    [[ -z "$(find upload ml-cache ! -user "$USER" -print -quit)" ]] \
        || sudo chown -R "$USER": upload ml-cache
    cat >docker-compose.override.yml <<EOF
# Generated by photo-archive install.sh -- edits will be overwritten.
services:
  immich-server:
    user: "$(id -u):$(id -g)"
    volumes:
      - $BASE_DIR/Library:/mnt/library
  immich-machine-learning:
    user: "$(id -u):$(id -g)"
    volumes:
      - $IMMICH_DIR/ml-cache/cache:/cache
      - $IMMICH_DIR/ml-cache/home-cache:/.cache
      - $IMMICH_DIR/ml-cache/home-config:/.config
EOF
    echo "Starting Immich (the first start downloads several GB)..."
    for attempt in 1 2 3 4 5; do   # ghcr.io rate-limits bursts of parallel pulls
        sudo env COMPOSE_PARALLEL_LIMIT=1 docker compose pull -q && break
        (( attempt == 5 )) && die "Immich image download failed; re-run later"
        warn "Download failed, retrying in $((attempt * 30))s"; sleep $((attempt * 30))
    done
    sudo docker compose up -d
)
for _ in $(seq 1 60); do curl -fs "$IMMICH_URL/api/server/ping" >/dev/null && break; sleep 5; done
curl -fs "$IMMICH_URL/api/server/ping" >/dev/null || die "Immich didn't start; check: cd $IMMICH_DIR && sudo docker compose logs"
ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

KEY_FILE="$BASE_DIR/state/immich_api_key"
immich() { curl -fsS -H "x-api-key: $(cat "$KEY_FILE")" -H 'content-type: application/json' "$@"; }
key_ok() { [[ -s "$KEY_FILE" ]] && immich "$IMMICH_URL/api/users/me" >/dev/null 2>&1; }
if ! key_ok; then
    cat <<EOF

Immich needs an admin account and an API key for photo-archive:
  1. Open http://${ip:-<this-host>}:2283 and create the admin account (first visit only).
  2. Click your avatar > Account Settings > API Keys > New API Key (all permissions).
EOF
    while true; do
        k="$(ask "Paste the API key" "")"
        (umask 077; printf '%s\n' "$k" >"$KEY_FILE")
        key_ok && break
        warn "Immich rejected that key; try again."
    done
fi
chmod 600 "$KEY_FILE"
echo "Immich API key OK."
if ! immich "$IMMICH_URL/api/libraries" | grep -q '"/mnt/library"'; then
    owner="$(immich "$IMMICH_URL/api/users/me" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
    immich -X POST "$IMMICH_URL/api/libraries" \
        -d "{\"ownerId\":\"$owner\",\"name\":\"Google Photos\",\"importPaths\":[\"/mnt/library\"]}" >/dev/null
    echo "Created Immich external library for $BASE_DIR/Library."
else
    echo "Immich external library already set up."
fi

# --- 4. code + rclone --------------------------------------------------------------------
step "4/8 Install photo-archive and rclone"
mkdir -p "$BIN_DIR" "$LIB_DIR" "$CONFIG_DIR"
install -m 644 "$HERE"/lib/*.py "$LIB_DIR/"
chmod 755 "$LIB_DIR/pipeline.py" "$LIB_DIR/takeout_sync.py"
install -m 755 "$HERE/bin/photo-archive" "$BIN_DIR/photo-archive"
echo "Installed photo-archive to $BIN_DIR (code in $LIB_DIR)."
rclone_version() { "$1" version 2>/dev/null | head -n 1 | sed -E 's/^rclone v([0-9.]+).*/\1/'; }
RCLONE=""
for cand in "$RCLONE_BIN" "$BIN_DIR/rclone" "$(command -v rclone || true)"; do
    if [[ -n "$cand" && -x "$cand" ]] && version_ge "$(rclone_version "$cand")" "$MIN_RCLONE"; then
        RCLONE="$cand"; break
    fi
done
if [[ -z "$RCLONE" ]]; then
    version="$(curl -fsS https://downloads.rclone.org/version.txt | awk '{print $2}')"
    zip="rclone-$version-linux-$arch.zip"
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    echo "Downloading rclone $version..."
    curl -fsSL -o "$tmp/$zip" "https://downloads.rclone.org/$version/$zip"
    curl -fsSL -o "$tmp/SHA256SUMS" "https://downloads.rclone.org/$version/SHA256SUMS"
    (cd "$tmp" && grep " $zip\$" SHA256SUMS | sha256sum -c -) || die "rclone checksum mismatch"
    unzip -q "$tmp/$zip" -d "$tmp"
    install -m 755 "$tmp/rclone-$version-linux-$arch/rclone" "$BIN_DIR/rclone"
    RCLONE="$BIN_DIR/rclone"
fi
echo "Using rclone $(rclone_version "$RCLONE") at $RCLONE"

# --- 5. service account ---------------------------------------------------------------------
step "5/8 Google service account"
cat <<'EOF'
photo-archive signs in to Drive as a Google Cloud service account (JSON key): no browser
sign-in, nothing expires. It needs the Google Drive API enabled in its project, and it
only sees the folder you share with it in the next step.
EOF
KEY_DEST="$CONFIG_DIR/service-account.json"
default_key="${SERVICE_ACCOUNT_FILE:-}"
[[ -f "$HERE/service-account.json" ]] && default_key="$HERE/service-account.json"
while true; do
    key="$(ask "Path to the service account JSON key" "$default_key")"; key="${key/#\~/$HOME}"
    [[ -r "$key" ]] || { warn "Can't read '$key'."; continue; }
    [[ "$(json_field type "$key")" == service_account ]] && grep -q '"private_key"' "$key" \
        || { warn "That isn't a service account key."; continue; }
    break
done
if ! [[ "$key" -ef "$KEY_DEST" ]]; then
    (umask 077; cp "$key" "$KEY_DEST.tmp") && mv "$KEY_DEST.tmp" "$KEY_DEST"
    [[ "$key" == "$HERE/"* ]] && warn "You can now delete the bundled key $key (and its tarball)."
fi
chmod 600 "$KEY_DEST"
SERVICE_ACCOUNT_FILE="$KEY_DEST"
SERVICE_ACCOUNT_EMAIL="$(json_field client_email "$KEY_DEST")"
echo "Service account: $SERVICE_ACCOUNT_EMAIL"
# rclone only downloads, so its remote is read-only; tagging uses the API directly.
"$RCLONE" listremotes | grep -qx "$RCLONE_REMOTE:" && "$RCLONE" config delete "$RCLONE_REMOTE"
"$RCLONE" config create "$RCLONE_REMOTE" drive scope=drive.readonly \
    service_account_file="$KEY_DEST" config_change_team_drive=false </dev/null >/dev/null
if ! err="$("$RCLONE" lsf --max-depth 1 "$RCLONE_REMOTE:" 2>&1 >/dev/null)"; then
    echo "$err" | tail -n 3 | sed 's/^/  /'
    grep -qiE 'accessNotConfigured|has not been used|SERVICE_DISABLED' <<<"$err" && die "enable the Google Drive API:
  https://console.cloud.google.com/apis/library/drive.googleapis.com (in the service account's project), then re-run"
    die "Google rejected the key (deleted/disabled?). Create a new key and re-run."
fi
echo "Key OK."

# --- 6. Takeout folder ------------------------------------------------------------------------
step "6/8 Drive Takeout folder"
cat <<EOF
Google Takeout delivers exports into a folder called "Takeout" in your My Drive.
In Google Drive, right-click that folder > Share, add
    $SERVICE_ACCOUNT_EMAIL
as an **Editor** (untick "Notify people"). Editor lets photo-archive mark files it has
verified; it still can't delete your files (only you, via the Apps Script, can).
If the folder doesn't exist yet, create one called Takeout in My Drive and share it.
EOF
check_folder() { python3 "$LIB_DIR/drive.py" check "$KEY_DEST" "$1"; }
while true; do
    if [[ -n "$FOLDER_ID" ]] && out="$(check_folder "$FOLDER_ID" 2>/dev/null)"; then
        if grep -q '"canEdit": true' <<<"$out"; then
            confirm "Use '$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$out")' ($FOLDER_ID)?" y && break
        else
            warn "The service account can only *view* that folder; change its share to Editor."
            read -r -p "Press Enter to check again..." </dev/tty; continue
        fi
    fi
    ids=(); names=()
    while IFS=$'\t' read -r id name; do ids+=("$id"); names+=("$name"); done < <(
        "$RCLONE" lsjson --dirs-only --drive-shared-with-me "$RCLONE_REMOTE:" 2>/dev/null |
        python3 -c 'import json,sys; [print(f["ID"]+"\t"+f["Name"]) for f in json.load(sys.stdin)]')
    if (( ${#ids[@]} )); then
        echo "Folders shared with the service account:"
        for i in "${!ids[@]}"; do printf '  %3d) %s\n' "$((i + 1))" "${names[$i]}"; done
    else
        echo "Nothing shared with the service account yet (it can take a minute to show)."
    fi
    reply="$(ask "Pick #, paste the folder's link or ID, or Enter to refresh" "")"
    [[ -z "$reply" ]] && continue
    if [[ "$reply" =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= ${#ids[@]} )); then
        FOLDER_ID="${ids[$((reply - 1))]}"
    else
        FOLDER_ID="$(sed -E 's#.*/folders/([A-Za-z0-9_-]+).*#\1#; s#.*[?&]id=([A-Za-z0-9_-]+).*#\1#' <<<"$reply")"
    fi
    out="$(check_folder "$FOLDER_ID" 2>&1)" || { warn "Can't open that folder: $out"; FOLDER_ID=""; continue; }
done
FOLDER_NAME="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$out")"

# --- 7. Apps Script ------------------------------------------------------------------------------
step "7/8 Apps Script (trashes verified Takeout files)"
GS="$CONFIG_DIR/trash_verified.gs"
sed "s/__FOLDER_ID__/$FOLDER_ID/" "$HERE/apps_script/trash_verified.gs" >"$GS"
cat <<EOF
Only you (the owner) can trash your Takeout files, so a small script in your Google
account does it, daily, for files photo-archive has verified. One-time setup:
  1. Open https://script.google.com and click "New project"; name it photo-archive.
  2. Replace the editor contents with the file below, and save (Ctrl+S).
  3. Choose the function "install" in the toolbar, click Run, and allow access
     (Google warns that the app is unverified: Advanced > Go to photo-archive).
  4. The log should say "daily trigger installed". Done.
The script: $GS
EOF
if confirm "Show the script here to copy it?" y; then echo; cat "$GS"; echo; fi
confirm "Have you installed the Apps Script (you can also do it later)?" y \
    || warn "Until it's installed, verified files stay in Drive; photo-archive status will remind you."

# --- 8. config + schedule ---------------------------------------------------------------------------
step "8/8 Schedule"
q() { sq "$1"; }
[[ -f "$CONFIG_FILE" ]] && cp "$CONFIG_FILE" "$CONFIG_FILE.bak"
cat >"$CONFIG_FILE" <<EOF
# photo-archive config, written by install.sh on $(date). Re-run install.sh to change.
BASE_DIR=$(q "$BASE_DIR")
RCLONE_BIN=$(q "$RCLONE")
RCLONE_REMOTE=$(q "$RCLONE_REMOTE")
SERVICE_ACCOUNT_FILE=$(q "$SERVICE_ACCOUNT_FILE")
SERVICE_ACCOUNT_EMAIL=$(q "$SERVICE_ACCOUNT_EMAIL")
# The Takeout folder by ID, so renaming/moving it in Drive doesn't break anything.
FOLDER_ID=$(q "$FOLDER_ID")
FOLDER_NAME=$(q "$FOLDER_NAME")
IMMICH_URL=$(q "$IMMICH_URL")
WORKERS=$(q "$WORKERS")
# Advanced (defaults in pipeline.py): SETTLE_HOURS STALE_TAG_DAYS EXPORT_OVERDUE_DAYS TRANSIENT_ESCALATE_AFTER
EOF
chmod 600 "$CONFIG_FILE"
echo "Wrote $CONFIG_FILE"

cat <<'EOF'
Default schedule: 01:00 on the 3rd of each month. The job is also listed for the 4th-9th
at 01:00, but those runs exit immediately once the month has succeeded; they only do
work if the 3rd failed (network down, export still arriving...).
EOF
SCHEDULE="0 1 3-9 * *"
prev="$(crontab -l 2>/dev/null | grep -F "$CRON_TAG" | head -n 1 | awk '{print $1, $2, $3, $4, $5}' || true)"
if [[ -n "$prev" && "$prev" != "$SCHEDULE" ]] && confirm "Keep your current schedule ($prev)?" y; then
    SCHEDULE="$prev"
elif ! confirm "Use the default schedule?" y; then
    while true; do
        SCHEDULE="$(ask "Cron expression (min hour day-of-month month day-of-week)" "$SCHEDULE")"
        [[ "$(wc -w <<<"$SCHEDULE")" -eq 5 ]] && break
    done
fi
cron_line="$SCHEDULE $BIN_DIR/photo-archive run >/dev/null 2>>$BASE_DIR/logs/cron-errors.log $CRON_TAG"
old="$(crontab -l 2>/dev/null | grep -E 'google-photo-mirror|# google-drive-mirror' || true)"
if [[ -n "$old" ]]; then
    echo "Older photo/drive mirror cron entries found:"; echo "$old" | sed 's/^/  /'
    confirm "Remove them (photo-archive replaces them)?" y && drop_old=1 || drop_old=0
else
    drop_old=0
fi
{
    crontab -l 2>/dev/null | grep -vF "$CRON_TAG" |
        { if (( drop_old )); then grep -vE 'google-photo-mirror|# google-drive-mirror'; else cat; fi; } || true
    echo "$cron_line"
} | crontab -
echo "Cron job: $cron_line"

cat <<EOF

$(bold "Almost done.") If you haven't yet, schedule Google Takeout to deliver here:
  https://takeout.google.com > Deselect all > tick Google Photos > Next step >
  Destination "Add to Drive", Frequency "Export every 2 months for 1 year",
  File type .zip, size 2 GB > Create export.
  (Takeout schedules last a year; photo-archive status warns when exports stop arriving.)
EOF
if confirm "Preview a run now (dry run, changes nothing)?" y; then
    "$BIN_DIR/photo-archive" --dry-run || true
fi
echo
bold "All set."
echo "  Photos:        http://${ip:-<this-host>}:2283"
echo "  Status:        photo-archive status"
echo "  Run now:       photo-archive run --force"
echo "  Logs:          $BASE_DIR/state/pipeline/logs/"
echo "  Change setup:  re-run this installer"
