#!/usr/bin/env bash
# SRT-RELAY-GUEST-SETUP
#
# Installeert de SRT-relay (MediaMTX) in deze machine/container. Wordt door
# srt-relay.sh in de nieuwe Proxmox-container gestart, maar je kunt dit script
# ook los draaien op Debian/Ubuntu:
#
#   sudo SRT_PORT=8890 bash guest-setup.sh
#
# Alles is herhaalbaar: een tweede run laat het wachtwoord en de streams staan.
set -uo pipefail

SRT_PORT="${SRT_PORT:-8890}"
RTSP_PORT="${RTSP_PORT:-8554}"
RTMP_PORT="${RTMP_PORT:-1935}"
HLS_PORT="${HLS_PORT:-8888}"
WEBRTC_PORT="${WEBRTC_PORT:-8889}"
API_PORT="${API_PORT:-9997}"
INSTALL_DIR="${INSTALL_DIR:-/opt/srt-relay}"
MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.21.4}"
SERVICE_NAME="${SERVICE_NAME:-srt-relay}"
PUB_USER="${PUB_USER:-tom}"

CONF="$INSTALL_DIR/mediamtx.yml"
ENVFILE="$INSTALL_DIR/srt-relay.env"

if [ "$(id -u)" -eq 0 ]; then MODE=system; else MODE=user; fi
if [ "$MODE" = user ]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
fi

# Roept systemctl aan op de juiste plek (systeem- of gebruikersservice).
dienst() {
    if [ "$MODE" = system ]; then
        systemctl "$@"
    else
        systemctl --user "$@"
    fi
}

FOUTEN=0
stap() { printf '\n== %s\n' "$*"; }
ok()   { printf 'ok   %s\n' "$*"; }
let_op(){ printf 'let op %s\n' "$*"; }
fout() { printf '[fout] %s\n' "$*"; FOUTEN=$((FOUTEN + 1)); }

stap "Systeem"

if [ "$MODE" = system ]; then
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq >/dev/null 2>&1 || let_op "apt-get update gaf een waarschuwing"
        if apt-get install -y -qq --no-install-recommends \
                ffmpeg curl ca-certificates openssl python3 >/dev/null 2>&1; then
            ok "ffmpeg, curl, openssl en python3 aanwezig"
        else
            fout "kon ffmpeg/curl/python3 niet installeren"
        fi
    else
        let_op "geen apt-get: ik ga ervan uit dat ffmpeg, curl, openssl en python3 er al staan"
    fi
else
    let_op "geen root: pakketten niet geïnstalleerd, alleen de service wordt een gebruikersservice"
fi

for cmd in ffmpeg curl openssl python3; do
    if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd"; else fout "$cmd ontbreekt"; fi
done
if [ "$FOUTEN" -ne 0 ]; then
    printf "\n[stoppen] los eerst de ontbrekende programma's op.\n"
    exit 1
fi

case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    armv7l) ARCH=armv7 ;;
    *) fout "onbekende architectuur $(uname -m)"; exit 1 ;;
esac
ok "architectuur $ARCH"

stap "MediaMTX"

mkdir -p "$INSTALL_DIR" || { fout "kon $INSTALL_DIR niet aanmaken"; exit 1; }

if [ -x "$INSTALL_DIR/mediamtx" ] && "$INSTALL_DIR/mediamtx" --version 2>/dev/null | grep -q "^v"; then
    ok "mediamtx $("$INSTALL_DIR/mediamtx" --version) staat er al"
else
    TARBALL="mediamtx_v${MEDIAMTX_VERSION}_linux_${ARCH}.tar.gz"
    URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/${TARBALL}"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    if curl -fsSL --max-time 180 -o "$TMP/m.tar.gz" "$URL"; then
        if tar -xzf "$TMP/m.tar.gz" -C "$INSTALL_DIR" mediamtx mediamtx.yml 2>/dev/null \
           || tar -xzf "$TMP/m.tar.gz" -C "$INSTALL_DIR"; then
            chmod +x "$INSTALL_DIR/mediamtx"
            # Het meegeleverde voorbeeldbewaren we, zodat je alle opties kunt nalezen.
            [ -f "$INSTALL_DIR/mediamtx.yml" ] && mv -f "$INSTALL_DIR/mediamtx.yml" "$INSTALL_DIR/mediamtx-volledig-voorbeeld.yml"
            ok "mediamtx $("$INSTALL_DIR/mediamtx" --version) geïnstalleerd in $INSTALL_DIR"
        else
            fout "uitpakken van $TARBALL mislukte"
            exit 1
        fi
    else
        fout "kon $URL niet downloaden"
        exit 1
    fi
fi

stap "Configuratie"

if [ -f "$CONF" ] && grep -q 'pass: ' "$CONF"; then
    WACHTWOORD="$(sed -n "/^  - user: ${PUB_USER}$/,/permissions/p" "$CONF" | awk '/^ *pass:/{print $2; exit}')"
    ok "bestaande config hergebruikt (wachtwoord blijft hetzelfde)"
fi
if [ -z "${WACHTWOORD:-}" ]; then
    WACHTWOORD="$(openssl rand -hex 9)"
    ok "nieuw wachtwoord gegenereerd"
fi

cat > "$CONF" <<EOF
###############################################
# SRT-relay / livestream-hub (MediaMTX)
# Gegenereerd door guest-setup.sh — aanpassen mag, maar laat de
# inloggegevens staan als je de streams niet opnieuw wilt instellen.
#
# Publiceren:  srt://<dit-ip>:$SRT_PORT?streamid=publish:<naam>:$PUB_USER:$WACHTWOORD
# Kijken:      srt://<dit-ip>:$SRT_PORT?streamid=read:<naam>
# Browser:     http://<dit-ip>:$WEBRTC_PORT/<naam>/    (WebRTC)
#              http://<dit-ip>:$HLS_PORT/<naam>/       (HLS)
###############################################

logLevel: info
logDestinations: [stdout]

rtsp: true
rtmp: true
hls: true
webrtc: true
srt: true
api: true

rtspAddress: :$RTSP_PORT
rtmpAddress: :$RTMP_PORT
hlsAddress: :$HLS_PORT
webrtcAddress: :$WEBRTC_PORT
srtAddress: :$SRT_PORT
apiAddress: 127.0.0.1:$API_PORT

srtMinLatency: 120ms

authInternalUsers:
    # Publiceren mag alleen met gebruikersnaam en wachtwoord.
  - user: $PUB_USER
    pass: $WACHTWOORD
    ips: []
    permissions:
      - action: publish

    # Kijken mag iedereen met de link. Wil je kijken achter een wachtwoord,
    # zet deze twee regels dan onder de gebruiker hierboven en haal ze hier weg.
  - user: any
    pass:
    ips: []
    permissions:
      - action: read
      - action: playback

    # Beheer/API alleen vanaf deze machine zelf.
  - user: any
    pass:
    ips: ["127.0.0.1", "::1"]
    permissions:
      - action: api
      - action: metrics

# Bronnen die de relay zelf binnenhaalt staan hieronder. Toevoegen gaat het
# makkelijkst met:  srt-feed <naam> <bron-url>
paths:
  all_others:
EOF
ok "config geschreven: $CONF"

cat > "$ENVFILE" <<EOF
# Instellingen van de SRT-relay op deze machine (gelezen door srt-feed en srt-streams)
INSTALL_DIR=$INSTALL_DIR
CONF=$CONF
SERVICE_NAME=$SERVICE_NAME
MODE=$MODE
SRT_PORT=$SRT_PORT
RTSP_PORT=$RTSP_PORT
RTMP_PORT=$RTMP_PORT
HLS_PORT=$HLS_PORT
WEBRTC_PORT=$WEBRTC_PORT
API_PORT=$API_PORT
PUB_USER=$PUB_USER
EOF
ok "instellingen vastgelegd: $ENVFILE"

stap "Dienst"

if [ "$MODE" = system ]; then
    UNIT=/etc/systemd/system/$SERVICE_NAME.service
    cat > "$UNIT" <<EOF
[Unit]
Description=SRT-relay / livestream-hub (MediaMTX)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/mediamtx $CONF
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    if dienst enable --now "$SERVICE_NAME" >/dev/null 2>&1; then ok "dienst $SERVICE_NAME actief (systeemservice)"; else fout "kon $SERVICE_NAME niet starten"; fi
else
    UNIT="$HOME/.config/systemd/user/$SERVICE_NAME.service"
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<EOF
[Unit]
Description=SRT-relay / livestream-hub (MediaMTX)

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/mediamtx $CONF
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    systemctl --user daemon-reload
    if dienst enable --now "$SERVICE_NAME" >/dev/null 2>&1; then ok "dienst $SERVICE_NAME actief (gebruikersservice)"; else fout "kon $SERVICE_NAME niet starten"; fi
fi

stap "Hulpmiddelen"

write_tool() {
    # $1 = naam, $2 = inhoud
    ZIEL="$INSTALL_DIR/$1"
    printf '%s\n' "$2" > "$ZIEL"
    chmod +x "$ZIEL"
    if [ "$MODE" = system ]; then
        ln -sf "$ZIEL" "/usr/local/bin/$1"
        ok "$1  ->  /usr/local/bin/$1"
    else
        mkdir -p "$HOME/.local/bin"
        ln -sf "$ZIEL" "$HOME/.local/bin/$1"
        ok "$1  ->  $HOME/.local/bin/$1"
    fi
}

FEED_PY=$(cat <<'PYEOF'
#!/usr/bin/env python3
"""Bron op je netwerk toevoegen aan de SRT-relay:  srt-feed <naam> <bron-url> [--on-demand]

Verwijderen: srt-feed --verwijder <naam>       Overzicht: srt-streams

Leest de instellingen uit srt-relay.env naast dit bestand, dus de poorten en
paden komen altijd overeen met wat de installer heeft neergezet.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

MAP = os.path.dirname(os.path.realpath(__file__))
ENV = os.path.join(MAP, "srt-relay.env")

INSTALL_DIR = MAP
SERVICE_NAME = "srt-relay"
MODE = "system"
SRT_PORT = "8890"
HLS_PORT = "8888"
WEBRTC_PORT = "8889"
API_PORT = "9997"

if os.path.exists(ENV):
    with open(ENV) as fh:
        for regel in fh:
            if "=" in regel and not regel.strip().startswith("#"):
                sleutel, waarde = regel.strip().split("=", 1)
                globals()[sleutel] = waarde

CONF = os.environ.get("CONF", os.path.join(INSTALL_DIR, "mediamtx.yml"))
API = f"http://127.0.0.1:{API_PORT}/v3"
NAAM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def api(pad):
    try:
        with urllib.request.urlopen(f"{API}{pad}", timeout=4) as r:
            return json.load(r)
    except Exception:
        return None


def dienst_actief():
    if MODE == "system":
        opdracht = ["systemctl", "is-active", SERVICE_NAME]
    else:
        omgeving = dict(os.environ)
        omgeving.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        opdracht = ["systemctl", "--user", "is-active", SERVICE_NAME]
        return subprocess.run(opdracht, capture_output=True, text=True, env=omgeving).stdout.strip() == "active"
    return subprocess.run(opdracht, capture_output=True, text=True).stdout.strip() == "active"


def vind_path_blok(regels):
    for i, r in enumerate(regels):
        if r.rstrip() == "paths:":
            return i
    return None


def haal_bron(regels, naam):
    kop = f"  {naam}:"
    for i, r in enumerate(regels):
        if r.rstrip() == kop:
            bron = None
            for r2 in regels[i + 1:]:
                if not r2.startswith(("    ", "\t")):
                    break
                if r2.strip().startswith("source:"):
                    bron = r2.strip().split(":", 1)[1].strip()
            return bron
    return None


def vervang_blok(regels, naam, blok):
    kop = f"  {naam}:"
    for i, r in enumerate(regels):
        if r.rstrip() == kop:
            j = i + 1
            while j < len(regels) and regels[j].startswith(("    ", "\t")):
                j += 1
            return regels[:i] + blok + regels[j:]
    p = vind_path_blok(regels)
    if p is None:
        raise SystemExit("FOUT: geen 'paths:'-blok in de config.")
    return regels[:p + 1] + blok + regels[p + 1:]


def wis_blok(regels, naam):
    kop = f"  {naam}:"
    for i, r in enumerate(regels):
        if r.rstrip() == kop:
            j = i + 1
            while j < len(regels) and regels[j].startswith(("    ", "\t")):
                j += 1
            return regels[:i] + regels[j:], True
    return regels, False


def schrijf(regels, naam, weg=False):
    shutil.copy2(CONF, CONF + ".bak")
    with open(CONF, "w") as fh:
        fh.writelines(regels)
    time.sleep(2.5)
    if not dienst_actief():
        shutil.copy2(CONF + ".bak", CONF)
        time.sleep(2.5)
        raise SystemExit("FOUT: MediaMTX weigerde de nieuwe config — oude versie teruggezet.")
    d = api(f"/paths/get/{naam}")
    if weg:
        print(f"  -> '{naam}' is weg." if d is None else f"  -> Let op: '{naam}' staat nog in de config.")
    elif d and d.get("confName"):
        print(f"  -> '{naam}' is bekend (config-regel: {d['confName']})")
    else:
        print(f"  -> Let op: '{naam}' staat nog niet in de API.")


def main():
    if "--verwijder" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--verwijder"]
        if len(rest) != 1:
            raise SystemExit("Gebruik: srt-feed --verwijder <naam>")
        with open(CONF) as fh:
            regels = fh.readlines()
        nieuw, gevonden = wis_blok(regels, rest[0])
        if not gevonden:
            raise SystemExit(f"FOUT: '{rest[0]}' staat niet in de config.")
        print(f"[verwijderen] {rest[0]}")
        schrijf(nieuw, rest[0], weg=True)
        return

    args = [a for a in sys.argv[1:] if a != "--on-demand"]
    if len(args) != 2:
        raise SystemExit("Gebruik: srt-feed <naam> <bron-url> [--on-demand]\n"
                         "Bijvoorbeeld: srt-feed voordeur rtsp://192.168.1.50:554/h264")
    naam, bron = args
    if not NAAM_RE.match(naam):
        raise SystemExit("FOUT: naam mag alleen letters, cijfers, - en _ bevatten.")
    if ":" not in bron or not bron.split(":", 1)[0].isalpha():
        raise SystemExit("FOUT: bron moet een URL zijn, bijvoorbeeld rtsp://192.168.1.50:554/h264")

    with open(CONF) as fh:
        regels = fh.readlines()
    oud = haal_bron(regels, naam)
    blok = [f"  {naam}:\n", f"    source: {bron}\n"]
    if "--on-demand" in sys.argv:
        blok.append("    sourceOnDemand: true\n")
    nieuw = vervang_blok(regels, naam, blok)
    print(f"[{'wijzig' if oud else 'toevoegen'}] {naam} -> {bron}")
    schrijf(nieuw, naam)
    ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    ip = ip[0] if ip else "<dit-ip>"
    print(f"Kijken:  http://{ip}:{WEBRTC_PORT}/{naam}/   of   srt://{ip}:{SRT_PORT}?streamid=read:{naam}")


if __name__ == "__main__":
    main()
PYEOF
)

STREAMS_PY=$(cat <<'PYEOF'
#!/usr/bin/env python3
"""Overzicht van de streams op de SRT-relay:  srt-streams"""
import json
import os
import subprocess
import urllib.request

MAP = os.path.dirname(os.path.realpath(__file__))
ENV = os.path.join(MAP, "srt-relay.env")
SRT_PORT, HLS_PORT, WEBRTC_PORT, API_PORT = "8890", "8888", "8889", "9997"

if os.path.exists(ENV):
    with open(ENV) as fh:
        for regel in fh:
            if "=" in regel and not regel.strip().startswith("#"):
                sleutel, waarde = regel.strip().split("=", 1)
                globals()[sleutel] = waarde

ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
ip = ip[0] if ip else "<dit-ip>"

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/v3/paths/list", timeout=4) as antwoord:
        items = json.load(antwoord)["items"]
except Exception as fout:
    raise SystemExit(f"Geen antwoord van de relay op poort {API_PORT} ({fout}). "
                     "Draai: systemctl status srt-relay")

if not items:
    print("Geen streams: er publiceert nog niemand en er staan geen bronnen in de config.")
else:
    print(f"{'naam':16} {'online':7} {'bron':12} tracks")
    for i in sorted(items, key=lambda x: x["name"]):
        print(f"{i['name']:16} {'ja' if i['online'] else 'nee':7} "
              f"{i.get('source', {}).get('type', '-'):12} {', '.join(i.get('tracks') or []) or '-'}")
    print(f"\nKijken in de browser: http://{ip}:{WEBRTC_PORT}/<naam>/")
    print(f"Kijken met VLC/OBS:   srt://{ip}:{SRT_PORT}?streamid=read:<naam>")
PYEOF
)

write_tool srt-feed "$FEED_PY"
write_tool srt-streams "$STREAMS_PY"

stap "Zelftest"

if ! dienst is-active --quiet "$SERVICE_NAME"; then sleep 3; fi
if dienst is-active --quiet "$SERVICE_NAME"; then
    TEST="/tmp/srt-zelftest-$$"
    mkdir -p "$TEST"
    ffmpeg -hide_banner -loglevel error -re -t 8 \
        -f lavfi -i "testsrc2=size=640x360:rate=25" \
        -f lavfi -i "sine=frequency=440" \
        -c:v libx264 -preset ultrafast -tune zerolatency -c:a aac -shortest \
        -f mpegts "srt://127.0.0.1:$SRT_PORT?streamid=publish:zelftest:$PUB_USER:$WACHTWOORD" \
        >"$TEST/push.log" 2>&1 &
    PUSH=$!
    sleep 6
    ffmpeg -hide_banner -loglevel error -i "srt://127.0.0.1:$SRT_PORT?streamid=read:zelftest" \
        -t 3 -c copy -y "$TEST/terug.ts" >"$TEST/pull.log" 2>&1
    kill "$PUSH" 2>/dev/null
    sleep 1
    if [ -s "$TEST/terug.ts" ] && ffmpeg -hide_banner -v error -i "$TEST/terug.ts" -frames:v 10 -f null - 2>/dev/null; then
        ok "publiceren via SRT en terugkijken werkt (10 videoframes gelezen)"
        printf 'ZELFTEST OK\n'
    else
        fout "zelftest mislukt: geen beeld terug via SRT"
        tail -n 3 "$TEST/push.log" "$TEST/pull.log" 2>/dev/null
        printf 'ZELFTEST MISLUKT\n'
    fi
    rm -rf "$TEST"
else
    fout "de dienst $SERVICE_NAME draait niet, dus geen zelftest"
    dienst status "$SERVICE_NAME" --no-pager 2>&1 | tail -n 8
    printf 'ZELFTEST MISLUKT\n'
fi

stap "Klaar in de container"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf 'WACHTWOORD %s\n' "$WACHTWOORD"
printf 'SRT-RELAY-GUEST-KLAAR ip=%s poort=%s wachtwoord=%s\n' "${IP:-?}" "$SRT_PORT" "$WACHTWOORD"

if [ "$FOUTEN" -gt 0 ]; then
    printf '\n%s controle(s) met een fout.\n' "$FOUTEN"
    exit 1
fi
exit 0
