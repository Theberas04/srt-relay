#!/usr/bin/env bash
#
# SRT-RELAY-GUEST-SETUP
#
# Draait ín de container (als root). Zet de eigen relay neer: de hub (SRT, RTMP,
# HLS, live TS, momentopnamen, API) en het uitzendbord. Geen kant-en-klare
# streamserver: de hub is eigen code (hub/hub.py) bovenop ffmpeg en libsrt.
#
# Wordt normaal door srt-relay.sh op de Proxmox-host gestart, maar kan ook los:
#   bash guest-setup.sh
#
# Omgevingsvariabelen: SRT_PORT RTMP_PORT HTTP_PORT BORD_POORT REPO BRANCH REPO_RAW
#
set -uo pipefail

REPO="${REPO:-Theberas04/srt-relay}"
BRANCH="${BRANCH:-main}"
RAW="${REPO_RAW:-https://raw.githubusercontent.com/$REPO/$BRANCH}"

SRT_PORT="${SRT_PORT:-8890}"
RTMP_PORT="${RTMP_PORT:-1935}"
HTTP_PORT="${HTTP_PORT:-8080}"
BORD_POORT="${BORD_POORT:-8095}"
PUB_USER="${PUB_USER:-tom}"

BASE="/opt/srt-relay"
ETC="/etc/srt-relay"
MEDIA="/srv/streams/bronnen"
WERK="/srv/streams/werk"
HLS="/var/lib/srt-relay/hls"

FOUTEN=0
zeg()  { printf '%s\n' "$*"; }
stap() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
let_op(){ printf '  \033[33mlet op\033[0m %s\n' "$*"; }
fout() { printf '  \033[31m[fout]\033[0m %s\n' "$*"; FOUTEN=$((FOUTEN + 1)); }

# ---------------------------------------------------------------- controles

stap "Controles in de container"

if [ "$(id -u)" -ne 0 ]; then
    fout "dit script moet als root draaien (nu uid $(id -u))"
    exit 1
fi
ok "root"

for cmd in apt-get curl python3; do
    command -v "$cmd" >/dev/null 2>&1 && ok "$cmd" || fout "$cmd ontbreekt"
done
[ "$FOUTEN" -eq 0 ] || exit 1

zeg "  $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"

# ---------------------------------------------------------------- pakketten

stap "Pakketten"

export DEBIAN_FRONTEND=noninteractive
if ! apt-get update -qq >/dev/null 2>&1; then
    let_op "apt-get update gaf een melding; toch verder"
fi

# ffmpeg is het rekenwerk (beeld en geluid). libsrt komt mee als ffmpeg-afhankelijkheid
# en wordt door de hub met ctypes aangestuurd; python3 draait de hub zelf.
if apt-get install -y -qq --no-install-recommends ffmpeg python3 curl ca-certificates \
        >/tmp/apt.log 2>&1; then
    ok "ffmpeg + python3 geïnstalleerd"
else
    fout "pakketten installeren mislukte"
    tail -n 15 /tmp/apt.log | sed 's/^/       /'
    exit 1
fi

if command -v ffmpeg >/dev/null 2>&1; then
    ok "$(ffmpeg -version | head -1 | cut -d' ' -f1-3)"
else
    fout "ffmpeg staat er niet"
fi

# De hub praat via ctypes met de systeembibliotheek van libsrt. Zoek hem op naam,
# zodat een andere versie (1.4, 1.5, of een andere sonaam) ook werkt.
SRT_LIB="$(ldconfig -p 2>/dev/null | awk '/libsrt/ && /\.so\./ {print $1; exit}')"
if [ -z "$SRT_LIB" ]; then
    let_op "geen libsrt gevonden; nu libsrt1.5-gnutls proberen"
    apt-get install -y -qq --no-install-recommends libsrt1.5-gnutls >>/tmp/apt.log 2>&1 || true
    SRT_LIB="$(ldconfig -p 2>/dev/null | awk '/libsrt/ && /\.so\./ {print $1; exit}')"
fi
if [ -n "$SRT_LIB" ]; then ok "libsrt: $SRT_LIB"
else fout "geen libsrt te vinden — SRT kan niet werken"; fi

# De hub moet de bibliotheek ook echt kunnen laden; dat is een andere vraag dan of
# het bestand bestaat (sonaam, ontbrekende afhankelijkheid).
if python3 -c "
import ctypes
from ctypes.util import find_library
naam = find_library('srt') or find_library('srt-gnutls')
lib = ctypes.CDLL(naam)
assert lib.srt_getversion() > 0
" 2>/dev/null; then
    ok "python3 kan libsrt laden"
else
    fout "python3 kan libsrt niet laden — de hub start wel maar weigert elke verbinding"
fi

# ---------------------------------------------------------------- code ophalen

stap "Code van de relay ophalen"

mkdir -p "$BASE/hub" "$BASE/dashboard"

BESTANDEN="hub/hub.py hub/tssplit.py hub/srtkanaal.py dashboard/streams.py dashboard/uitzendbord.py dashboard/bord.html"

for bestand in $BESTANDEN; do
    doel="$BASE/$bestand"
    if [ -f "$(dirname "${BASH_SOURCE[0]}")/$bestand" ]; then
        cp "$(dirname "${BASH_SOURCE[0]}")/$bestand" "$doel"     # naast dit script (checkout)
    elif ! curl -fsSL --max-time 60 -o "$doel" "$RAW/$bestand"; then
        fout "kon $bestand niet ophalen van $RAW"
        continue
    fi
    if [ -s "$doel" ]; then ok "$bestand ($(wc -c <"$doel") bytes)"
    else fout "$bestand is leeg"; fi
done

if [ "$FOUTEN" -gt 0 ]; then
    fout "de code is niet compleet — stoppen voordat er iets half draait"
    exit 1
fi

if python3 -m py_compile "$BASE"/hub/*.py "$BASE"/dashboard/*.py 2>/tmp/pyc.log; then
    ok "alle pythonbestanden zijn leesbaar"
else
    fout "een van de bestanden is geen geldige python"
    sed 's/^/       /' /tmp/pyc.log
    exit 1
fi

# ---------------------------------------------------------------- geheimen

stap "Wachtwoorden en instellingen"

mkdir -p "$ETC"
chmod 700 "$ETC"

heeft() { [ -f "$1" ] && grep -q "^$2=" "$1"; }
waarde() { sed -n "s/^$2=//p" "$1" | head -1; }

PUB_PASS=""
if heeft "$ETC/relay.env" PUB_PASS; then
    PUB_PASS="$(waarde "$ETC/relay.env" PUB_PASS)"
    ok "bestaand publiceerwachtwoord bewaard"
fi
[ -n "$PUB_PASS" ] || PUB_PASS="$(openssl rand -hex 12 2>/dev/null \
    || python3 -c 'import secrets; print(secrets.token_hex(12))')"

cat >"$ETC/relay.env" <<EOF
# Instellingen van de relay (hub). Beheer: systemctl restart srt-relay
SRT_PORT=$SRT_PORT
RTMP_PORT=$RTMP_PORT
HTTP_PORT=$HTTP_PORT
PUB_USER=$PUB_USER
PUB_PASS=$PUB_PASS
SRT_LATENCY=120
HLS_DIR=$HLS
HLS_SEGMENT=2
HLS_KEEP=6
RTMP_NAAM=obs
EOF
chmod 600 "$ETC/relay.env"
ok "relay-instellingen in $ETC/relay.env"

BORD_WW=""
if heeft "$ETC/uitzendbord.env" BORD_WACHTWOORD; then
    BORD_WW="$(waarde "$ETC/uitzendbord.env" BORD_WACHTWOORD)"
    ok "bestaand bordwachtwoord bewaard"
fi
[ -n "$BORD_WW" ] || BORD_WW="$(openssl rand -hex 9 2>/dev/null \
    || python3 -c 'import secrets; print(secrets.token_hex(9))')"

cat >"$ETC/uitzendbord.env" <<EOF
# Instellingen van het uitzendbord. Beheer: systemctl restart srt-relay-bord
BORD_POORT=$BORD_POORT
BORD_WACHTWOORD=$BORD_WW
SRT_API=http://127.0.0.1:$HTTP_PORT
SRT_PUB_HOST=127.0.0.1
SRT_PORT=$SRT_PORT
PUB_USER=$PUB_USER
SRT_PUB_PASS=$PUB_PASS
SRT_MEDIA_DIR=$MEDIA
SRT_WERK_DIR=$WERK
EOF
chmod 600 "$ETC/uitzendbord.env"
ok "bord-instellingen in $ETC/uitzendbord.env"

mkdir -p "$MEDIA" "$WERK" "$HLS"
ok "mappen: $MEDIA, $WERK, $HLS"

# ---------------------------------------------------------------- diensten

stap "Diensten"

cat >/etc/systemd/system/srt-relay.service <<EOF
[Unit]
Description=SRT-relay / livestream-hub (eigen hub)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $BASE/hub/hub.py --env $ETC/relay.env
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/srt-relay-bord.service <<EOF
[Unit]
Description=SRT-relay uitzendbord
After=network-online.target srt-relay.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $BASE/dashboard/uitzendbord.py --env $ETC/uitzendbord.env
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet srt-relay srt-relay-bord >/dev/null 2>&1
systemctl restart srt-relay >/dev/null 2>&1
systemctl restart srt-relay-bord >/dev/null 2>&1

if systemctl is-active --quiet srt-relay; then ok "dienst srt-relay actief"
else fout "dienst srt-relay start niet"; journalctl -u srt-relay -n 20 --no-pager | sed 's/^/       /'; fi

if systemctl is-active --quiet srt-relay-bord; then ok "dienst srt-relay-bord actief"
else fout "dienst srt-relay-bord start niet"; journalctl -u srt-relay-bord -n 20 --no-pager | sed 's/^/       /'; fi

# De hub moet zijn poorten openen voordat de zelftest iets kan meten.
for _ in $(seq 1 30); do
    curl -fsS --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/streams" >/dev/null 2>&1 && break
    sleep 1
done
if curl -fsS --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/streams" >/dev/null 2>&1; then
    ok "hub antwoordt op http://127.0.0.1:$HTTP_PORT (API)"
else
    fout "hub antwoordt niet op de API — kijk in: journalctl -u srt-relay -n 40"
    exit 1
fi

# ---------------------------------------------------------------- hulpmiddelen

stap "Hulpmiddelen"

cat >/usr/local/bin/srt-streams <<EOF
#!/usr/bin/env bash
# Wat staat er online op deze relay?
curl -fsS --max-time 5 http://127.0.0.1:$HTTP_PORT/api/streams | python3 -c '
import json, sys
gegevens = json.load(sys.stdin)
stromen = gegevens.get("streams", [])
if not stromen:
    print("er is nu niets in de lucht")
for s in stromen:
    sporen = ", ".join(s.get("sporen") or []) or "nog geen sporen"
    print("%-16s %8.1f MB  kijkers %2d  segmenten %4d  %s" % (
        s.get("naam", "?"), s.get("bytes", 0) / 1e6, s.get("kijkers", 0),
        s.get("segmenten", 0), sporen))
' 2>/dev/null || echo "de relay antwoordt niet op poort $HTTP_PORT"
EOF
chmod +x /usr/local/bin/srt-streams
ok "srt-streams (wat staat er online)"

cat >/usr/local/bin/srt-feed <<EOF
#!/usr/bin/env bash
# Een bron naar de relay duwen:  srt-feed <naam> <url of bestand>
set -uo pipefail
NAAM="\${1:-}"; BRON="\${2:-}"
if [ -z "\$NAAM" ] || [ -z "\$BRON" ]; then
    echo "gebruik: srt-feed <naam> <bron-url of bestand>" >&2; exit 2
fi
WW="\$(sed -n 's/^PUB_PASS=//p' $ETC/relay.env | head -1)"
exec ffmpeg -hide_banner -loglevel warning -re -i "\$BRON" \\
    -map 0:v -map 0:a? -c copy -f mpegts \\
    "srt://127.0.0.1:$SRT_PORT?streamid=publish:\$NAAM:$PUB_USER:\$WW"
EOF
chmod +x /usr/local/bin/srt-feed
ok "srt-feed (een bron naar de relay duwen)"

# ---------------------------------------------------------------- zelftest relay

stap "Zelftest: uitzenden en terugkijken over het relay"

api() { curl -fsS --max-time 5 "http://127.0.0.1:$HTTP_PORT$1" 2>/dev/null; }

# Een korte testbron (beeld + toon) naar de relay duwen en daarna terugkijken.
ffmpeg -hide_banner -loglevel error -re \
    -f lavfi -i "testsrc=size=640x360:rate=25" \
    -f lavfi -i "sine=frequency=1000:sample_rate=48000" \
    -t 14 -c:v libx264 -preset veryfast -g 50 -bf 0 -pix_fmt yuv420p -b:v 800k \
    -c:a aac -b:a 96k -ac 2 \
    -f mpegts "srt://127.0.0.1:$SRT_PORT?streamid=publish:zelftest:$PUB_USER:$PUB_PASS" \
    >/tmp/zelftest-uit.log 2>&1 &
UITZENDER=$!

KENNIS=""
for _ in $(seq 1 20); do
    sleep 1
    KENNIS="$(api /api/streams || true)"
    case "$KENNIS" in *zelftest*) break ;; esac
done

if [ -z "$KENNIS" ] || ! printf '%s' "$KENNIS" | grep -q zelftest; then
    fout "het relay ziet de uitzender niet (poort $SRT_PORT/udp)"
    sed 's/^/       /' /tmp/zelftest-uit.log
    kill "$UITZENDER" 2>/dev/null
    exit 1
fi
ok "het relay ziet de uitzending 'zelftest'"

SPOREN="$(printf '%s' "$KENNIS" | python3 -c '
import json, sys
gegevens = json.load(sys.stdin)
for s in gegevens.get("streams", []):
    if s.get("naam") == "zelftest":
        print(s.get("sporen_aantal", 0), ",".join(s.get("sporen") or []))
' 2>/dev/null)"
AANTAL="${SPOREN%% *}"
if [ "${AANTAL:-0}" -ge 2 ]; then ok "sporen in de uitzending: $AANTAL (${SPOREN#* })"
else fout "het relay telt ${AANTAL:-0} sporen; verwacht minstens beeld + geluid"; fi

# Terugkijken via de relay: dit is de kern — er moet een echte stream uitkomen.
if ffmpeg -hide_banner -loglevel error -i \
        "srt://127.0.0.1:$SRT_PORT?streamid=read:zelftest" \
        -frames:v 30 -f null - >/tmp/zelftest-terug.log 2>&1; then
    ok "terugkijken over SRT leverde 30 decodeerbare beelden"
else
    fout "terugkijken over SRT mislukte"
    sed 's/^/       /' /tmp/zelftest-terug.log | head -8
fi

# HLS: de hub knipt de uitzending zelf in segmenten van twee seconden.
SEGMENTEN=0
for _ in $(seq 1 20); do
    SEGMENTEN="$(ls "$HLS/zelftest"/seg*.ts 2>/dev/null | wc -l)"
    [ "$SEGMENTEN" -ge 2 ] && break
    sleep 1
done
if [ "$SEGMENTEN" -ge 2 ]; then ok "eigen HLS: $SEGMENTEN segmenten in $HLS/zelftest"
else fout "geen HLS-segmenten gemaakt (gevonden: $SEGMENTEN)"; fi

if api /hls/zelftest/index.m3u8 | grep -q "#EXTINF"; then
    ok "afspeellijst met segmenten (http://<ip>:$HTTP_PORT/hls/zelftest/index.m3u8)"
else
    fout "de afspeellijst is leeg of niet bereikbaar"
fi

# Momentopname: het bord gebruikt die als voorbeeldbeeld.
BEELD="/tmp/zelftest-beeld.jpg"
if curl -fsS --max-time 20 -o "$BEELD" "http://127.0.0.1:$HTTP_PORT/beeld/zelftest.jpg" \
        && [ "$(head -c2 "$BEELD" | od -An -tx1 | tr -d ' \n')" = "ffd8" ] \
        && [ "$(wc -c <"$BEELD")" -gt 4000 ]; then
    ok "momentopname is een echte jpeg ($(wc -c <"$BEELD") bytes)"
else
    fout "de momentopname is geen geldige jpeg"
fi

# Live beeld voor browsers en telefoons (beeldstroom).
if timeout 12 curl -s -o /tmp/zelftest-stroom.bin \
        "http://127.0.0.1:$HTTP_PORT/beeldstroom/zelftest"; then
    RAMEN="$(grep -c -- "--raam" /tmp/zelftest-stroom.bin 2>/dev/null || echo 0)"
    if [ "${RAMEN:-0}" -ge 2 ]; then ok "beeldstroom voor de telefoon: $RAMEN beelden"
    else fout "de beeldstroom gaf $RAMEN beelden"; fi
else
    fout "de beeldstroom is niet bereikbaar"
fi

kill "$UITZENDER" 2>/dev/null
wait "$UITZENDER" 2>/dev/null

for _ in $(seq 1 10); do
    api /api/streams | grep -q zelftest || break
    sleep 1
done
if api /api/streams | grep -q zelftest; then
    let_op "de uitzending 'zelftest' blijft in de lijst staan (de hub ruimt hem niet op)"
else
    ok "het relay ruimt een gestopte uitzending netjes op"
fi

# ---------------------------------------------------------------- zelftest bord

stap "Zelftest: het uitzendbord bouwt en zendt uit"

# Een bron voor het bord: een korte lus met beeld en twee geluidssporen.
if ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc=size=640x360:rate=25" \
        -f lavfi -i "sine=frequency=440:sample_rate=48000" \
        -f lavfi -i "sine=frequency=880:sample_rate=48000" \
        -t 10 -c:v libx264 -preset veryfast -g 50 -bf 0 -pix_fmt yuv420p \
        -c:a aac -b:a 96k -ac 2 -map 0:v -map 1:a -map 2:a \
        -f mp4 "$MEDIA/zelftest-lus.mp4" >/tmp/lus.log 2>&1; then
    ok "bron voor het bord gemaakt: zelftest-lus.mp4"
else
    fout "kon geen bron voor het bord maken"
fi

BORD="http://127.0.0.1:$BORD_POORT"

bord_post() { curl -fsS --max-time 90 -u "$PUB_USER:$BORD_WW" \
    -H "Content-Type: application/json" -X POST "$BORD$1" -d "${2:-{\}}"; }
bord_get()  { curl -fsS --max-time 5 -u "$PUB_USER:$BORD_WW" "$BORD$1"; }

if bord_get / >/dev/null 2>&1; then ok "het bord antwoordt op http://127.0.0.1:$BORD_POORT"
else fout "het bord antwoordt niet — kijk in: journalctl -u srt-relay-bord -n 30"; fi

if bord_post /api/instellingen '{"afspeellijst":["zelftest-lus.mp4"]}' >/dev/null 2>&1; then
    ok "afspeellijst ingesteld op zelftest-lus.mp4"
else
    fout "kon de afspeellijst niet instellen"
fi

bord_post /api/bouw >/dev/null 2>&1 || fout "het bord begon niet te bouwen"
GEBOUWD=0
for _ in $(seq 1 60); do
    sleep 2
    STAAT="$(bord_get /api/staat 2>/dev/null || true)"
    case "$STAAT" in *'"bezig": false'*) GEBOUWD=1; break ;; esac
done
if [ "$GEBOUWD" -eq 1 ]; then ok "de lus is gebouwd uit de afspeellijst"
else fout "het bouwen van de lus kwam niet klaar"; fi

bord_post /api/start >/dev/null 2>&1 || fout "het bord begon niet uit te zenden"
ONLINE=0
for _ in $(seq 1 30); do
    sleep 2
    STAAT="$(bord_get /api/staat 2>/dev/null || true)"
    case "$STAAT" in *'"online": true'*) ONLINE=1; break ;; esac
done
if [ "$ONLINE" -eq 1 ]; then ok "het bord zendt uit en het relay ziet het"
else fout "het bord zendt niet aantoonbaar uit"; fi

if bord_post /api/stop >/dev/null 2>&1; then ok "uitzending weer gestopt"
else let_op "stoppen gaf een melding"; fi

# ---------------------------------------------------------------- samenvatting

stap "Klaar"

zeg "  WACHTWOORD $PUB_PASS"
zeg "  BORD wachtwoord=$BORD_WW"
zeg ""

if [ "$FOUTEN" -gt 0 ]; then
    zeg "  $FOUTEN controle(s) mislukt — zie [fout] hierboven."
    exit 1
fi

echo "ZELFTEST OK"
echo "ZELFTEST BORD OK"
exit 0
