#!/usr/bin/env bash
#
# export-brave-cookies.sh — run on YOUR MAC (where Brave lives).
#
# Extracts cookies from Brave, keeps only the video platforms we support,
# writes Netscape cookies.txt files, and pushes them to the VPS worker.
# The VPS never touches a browser; it only receives the filtered files.
#
# Usage:
#   ./export-brave-cookies.sh              # extract + filter + push
#   ./export-brave-cookies.sh --dry-run    # extract + filter, no push
#   COOKIE_BROWSER=chrome ./export-brave-cookies.sh
#
# Requirements: python3 + browser_cookie3 (pip install browser_cookie3)
#               or yt-dlp (brew install yt-dlp), plus ssh/scp access to the VPS.
#
# Safety: only domains in ALLOWED_DOMAINS are exported. Your banking, mail
# and other sessions are dropped before anything leaves this machine.

set -euo pipefail

BROWSER="${COOKIE_BROWSER:-brave}"
BRAVE_PROFILE="${BRAVE_PROFILE:-Profile 2}"
VPS_HOST="${VPS_HOST:-root@103.116.104.223}"
REMOTE_COOKIE_DIR="${REMOTE_COOKIE_DIR:-/opt/video-worker/data/cookies}"
REMOTE_STAGING="${REMOTE_COOKIE_DIR}/incoming"
# legacy path for backwards compat with old Update-Douyin-Cookie.command
REMOTE_LEGACY_DIR="/opt"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_OUT="${COOKIE_LOCAL_OUT:-${SCRIPT_DIR}/../cookies}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# platform -> "domain1 domain2 ..."
PLATFORM_DOMAINS="
youtube youtube.com youtu.be googlevideo.com youtube-nocookie.com
instagram instagram.com cdninstagram.com
facebook facebook.com fbcdn.net fbsbx.com
tiktok tiktok.com tiktokcdn.com tiktokv.com
douyin douyin.com iesdouyin.com douyinpic.com douyinvod.com
pinterest pinterest.com pinimg.com
"

ALLOWED_DOMAINS="$(printf '%s\n' "$PLATFORM_DOMAINS" | awk 'NF>1 {for(i=2;i<=NF;i++) printf "%s ", $i}')"

# Find yt-dlp (system or venv)
YTDLP_BIN=""
if command -v yt-dlp >/dev/null 2>&1; then
  YTDLP_BIN="$(command -v yt-dlp)"
elif [ -x "${SCRIPT_DIR}/../.venv/bin/yt-dlp" ]; then
  YTDLP_BIN="${SCRIPT_DIR}/../.venv/bin/yt-dlp"
elif [ -x "${SCRIPT_DIR}/../../.venv/bin/yt-dlp" ]; then
  YTDLP_BIN="${SCRIPT_DIR}/../../.venv/bin/yt-dlp"
fi

# Find python with browser_cookie3
PYTHON_BIN=""
for cand in "${SCRIPT_DIR}/../.venv/bin/python" /opt/homebrew/bin/python3 /usr/bin/python3 python3; do
  if [ -x "$cand" ] && "$cand" -c "import browser_cookie3" 2>/dev/null; then
    PYTHON_BIN="$cand"
    break
  fi
done
if [ -z "$PYTHON_BIN" ] && command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
fi

tmp_raw="$(mktemp -t brave-cookies-raw.XXXXXX)"
trap 'rm -f "$tmp_raw"' EXIT

extracted=0

# Method 1: browser_cookie3 (most reliable, matches Update-Douyin-Cookie.command)
# Tries Profile 2 first (where Douyin login lives), then Default
if [ -n "$PYTHON_BIN" ] && "$PYTHON_BIN" -c "import browser_cookie3" 2>/dev/null; then
  echo "==> Trying browser_cookie3 with Brave profile: $BRAVE_PROFILE"
  for profile in "$BRAVE_PROFILE" "Default"; do
    cookie_path="$HOME/Library/Application Support/BraveSoftware/Brave-Browser/$profile/Cookies"
    if [ ! -f "$cookie_path" ]; then
      echo "    profile $profile not found, skipping"
      continue
    fi
    echo "    reading $profile..."
    if "$PYTHON_BIN" - <<PY 2>&1 | head -5
import browser_cookie3, sys
try:
    cj = browser_cookie3.brave(cookie_file="$cookie_path")
    cookies = list(cj)
    print(f"FOUND {len(cookies)} total cookies in $profile", file=sys.stderr)
    # Write all cookies to tmp file in Netscape format
    lines = ['# Netscape HTTP Cookie File']
    for c in cookies:
        domain = c.domain or ''
        if not domain:
            continue
        flag = 'TRUE' if domain.startswith('.') else 'FALSE'
        path = c.path or '/'
        secure = 'TRUE' if c.secure else 'FALSE'
        exp = int(c.expires) if c.expires else 0
        lines.append('\t'.join([domain, flag, path, secure, str(exp), c.name, c.value]))
    open("$tmp_raw", "w").write('\n'.join(lines) + '\n')
    print("OK")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PY
    then
      if [ -s "$tmp_raw" ] && [ "$(grep -vc '^#' "$tmp_raw" || true)" -gt 0 ]; then
        extracted=1
        echo "    -> extracted $(grep -vc '^#' "$tmp_raw" || true) cookies via browser_cookie3 ($profile)"
        break
      fi
    fi
  done
fi

# Method 2: yt-dlp fallback (if browser_cookie3 failed)
if [ "$extracted" -eq 0 ]; then
  if [ -z "$YTDLP_BIN" ]; then
    echo "No cookies were extracted. Tried browser_cookie3 (no Profile 2/Default with cookies) and yt-dlp not found." >&2
    echo "Install: pip install browser_cookie3  or  brew install yt-dlp" >&2
    exit 1
  fi
  echo "==> browser_cookie3 failed, trying yt-dlp ($YTDLP_BIN) with $BROWSER"
  for profile in "$BRAVE_PROFILE" "Default" ""; do
    browser_arg="$BROWSER"
    [ -n "$profile" ] && browser_arg="$BROWSER:$profile"
    echo "    trying $browser_arg..."
    rm -f "$tmp_raw"
    "$YTDLP_BIN" \
      --cookies-from-browser "$browser_arg" \
      --cookies "$tmp_raw" \
      --simulate --no-warnings --ignore-config --skip-download \
      "https://www.youtube.com/watch?v=BaW_jenozKc" >/dev/null 2>&1 || true
    if [ -s "$tmp_raw" ] && [ "$(grep -vc '^#' "$tmp_raw" || true)" -gt 0 ]; then
      extracted=1
      echo "    -> extracted $(grep -vc '^#' "$tmp_raw" || true) cookies via yt-dlp ($browser_arg)"
      break
    fi
  done
fi

if [ "$extracted" -eq 0 ] || [ ! -s "$tmp_raw" ]; then
  echo "No cookies were extracted. Is $BROWSER installed and has it been used to log in?" >&2
  echo "Tried profiles: $BRAVE_PROFILE, Default via both browser_cookie3 and yt-dlp" >&2
  echo "Tip: check Brave is logged into Douyin/YouTube in Profile 2" >&2
  exit 1
fi

total_raw="$(grep -vc '^#' "$tmp_raw" || true)"
echo "==> Browser jar: ${total_raw} cookies (before filter)"

mkdir -p "$LOCAL_OUT"

filter_domains() { # $1 = output file, $2 = space separated allowed domains
  # The header line is required: yt-dlp rejects a jar without
  # "# Netscape HTTP Cookie File" as "does not look like a Netscape format".
  awk -v allowed="$2" '
    BEGIN { n = split(allowed, a, " "); FS = "\t"; OFS = "\t"
            print "# Netscape HTTP Cookie File" }
    /^#/ || NF == 0 { next }
    {
      dom = $1; sub(/^\./, "", dom)
      for (i = 1; i <= n; i++) {
        if (dom == a[i] || dom ~ ("\\." a[i] "$")) { print; next }
      }
    }
  ' "$tmp_raw" > "$1"
}

combined="$LOCAL_OUT/combined.txt"
filter_domains "$combined" "$ALLOWED_DOMAINS"
echo "==> combined.txt: $(grep -vc '^#' "$combined" || true) cookies (allowlist only)"

while read -r platform domains; do
  [[ -z "${platform:-}" ]] || true
  [[ -z "${platform:-}" ]] && continue
  out="$LOCAL_OUT/${platform}.txt"
  filter_domains "$out" "$domains"
  echo "    ${platform}.txt: $(grep -vc '^#' "$out" || true) cookies"
  # f2 needs a logged-in Douyin jar (sessionid/sid_guard). Exporting from a
  # browser where you are signed in is exactly that jar.
  if [[ "$platform" == "douyin" ]]; then
    cp "$out" "$LOCAL_OUT/douyin_logged_in.txt"
    echo "    douyin_logged_in.txt: $(grep -vc '^#' "$LOCAL_OUT/douyin_logged_in.txt" || true) cookies"
  fi
done <<< "$PLATFORM_DOMAINS"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "==> dry-run: wrote files to $LOCAL_OUT (nothing pushed)"
  exit 0
fi

echo "==> Pushing to ${VPS_HOST}:${REMOTE_STAGING} (and legacy ${REMOTE_LEGACY_DIR}/douyin_cookies*.txt)"
ssh "$VPS_HOST" "mkdir -p '$REMOTE_STAGING' '$REMOTE_COOKIE_DIR' '$REMOTE_LEGACY_DIR'"
scp -q "$LOCAL_OUT"/*.txt "${VPS_HOST}:${REMOTE_STAGING}/"
if ssh "$VPS_HOST" "test -x /opt/video-worker/scripts/cookiectl" 2>/dev/null; then
  ssh "$VPS_HOST" "COOKIE_DIR='$REMOTE_COOKIE_DIR' /opt/video-worker/scripts/cookiectl install-all"
else
  # VPS not yet provisioned with video-worker — place directly
  ssh "$VPS_HOST" "cp '$REMOTE_STAGING'/*.txt '$REMOTE_COOKIE_DIR'/ 2>/dev/null; chmod 600 '$REMOTE_COOKIE_DIR'/*.txt 2>/dev/null; rm -f '$REMOTE_STAGING'/*.txt; echo '(cookiectl not found — placed directly into '\"$REMOTE_COOKIE_DIR\"')'"
fi
# Also push Douyin to legacy path for backwards compat with old Update-Douyin-Cookie.command consumers
if [ -s "$LOCAL_OUT/douyin.txt" ]; then
  scp -q "$LOCAL_OUT/douyin.txt" "${VPS_HOST}:${REMOTE_LEGACY_DIR}/douyin_cookies.txt" 2>/dev/null || true
  scp -q "$LOCAL_OUT/douyin_logged_in.txt" "${VPS_HOST}:${REMOTE_LEGACY_DIR}/douyin_cookies_logged_in.txt" 2>/dev/null || true
  ssh "$VPS_HOST" "chmod 600 ${REMOTE_LEGACY_DIR}/douyin_cookies*.txt 2>/dev/null || true"
fi

echo "==> Done. Freshness:"
if ssh "$VPS_HOST" "test -x /opt/video-worker/scripts/cookiectl" 2>/dev/null; then
  ssh "$VPS_HOST" "COOKIE_DIR='$REMOTE_COOKIE_DIR' /opt/video-worker/scripts/cookiectl list"
else
  ssh "$VPS_HOST" "ls -lh '$REMOTE_COOKIE_DIR'/*.txt 2>/dev/null | awk '{print \$9, \$5}' && echo; ls -lh ${REMOTE_LEGACY_DIR}/douyin_cookies*.txt 2>/dev/null | awk '{print \$9, \$5}'"
fi
