#!/usr/bin/env bash
#
# srt-relay — zet een SRT-relay / livestream-hub (MediaMTX) neer in een eigen
# Proxmox-LXC. Draai dit op de Proxmox-host als root:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Theberas04/srt-relay/main/srt-relay.sh)
#
# Opties (als omgevingsvariabele mee te geven):
#   CTID=130            vast container-ID (standaard: eerst vrije)
#   CTNAME=srt-relay    hostnaam van de container
#   SRT_PORT=8890       UDP-poort van het relay
#   DEBIAN=12           gastversie (12 aanbevolen; 13 vraagt Proxmox 8.2+)
#   CORES=1 RAM=512 DISK=3
#   BRIDGE=vmbr0  TEMPLATE_STORAGE=local  ROOTFS_STORAGE=local-lvm
#   FETCH_ONLY=1        alleen de bestanden ophalen en controleren, niets aanmaken
#
set -uo pipefail

REPO="${REPO:-Theberas04/srt-relay}"
BRANCH="${BRANCH:-main}"
RAW="${REPO_RAW:-https://raw.githubusercontent.com/$REPO/$BRANCH}"

CTID="${CTID:-}"
CTNAME="${CTNAME:-srt-relay}"
SRT_PORT="${SRT_PORT:-8890}"
DEBIAN="${DEBIAN:-12}"
CORES="${CORES:-1}"
RAM="${RAM:-512}"
DISK="${DISK:-3}"
BRIDGE="${BRIDGE:-vmbr0}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-local-lvm}"
FETCH_ONLY="${FETCH_ONLY:-0}"
PVE_ETC="${PVE_ETC:-/etc/pve}"   # alleen anders bij een afwijkende opstelling of in tests

FOUTEN=0
zeg()  { printf '%s\n' "$*"; }
stap() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
let_op(){ printf '  \033[33mlet op\033[0m %s\n' "$*"; }
fout() { printf '  \033[31m[fout]\033[0m %s\n' "$*"; FOUTEN=$((FOUTEN + 1)); }

# ---------------------------------------------------------------- vooraf

stap "Controles"

if [ "$(id -u)" -ne 0 ]; then
    fout "dit script moet als root op de Proxmox-host draaien (nu uid $(id -u))."
    zeg "     Draai:  sudo bash <(curl -fsSL $RAW/srt-relay.sh)"
    exit 1
fi
ok "root"

if [ ! -d "$PVE_ETC" ]; then
    fout "geen $PVE_ETC — dit lijkt geen Proxmox-host. Voor een gewone machine kun je"
    zeg  "     guest-setup.sh direct draaien; zie de README."
    exit 1
fi
ok "Proxmox ($PVE_ETC aanwezig)"

for cmd in pct pveam curl; do
    if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd"; else fout "$cmd ontbreekt"; fi
done
[ "$FOUTEN" -eq 0 ] || exit 1

zeg "  $(pveversion | head -1)"

# ---------------------------------------------------------------- bestanden ophalen

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

stap "Installatiebestanden ophalen"

# Lokaal bestand naast dit script wint (handig als je vanuit een checkout draait),
# anders vanaf GitHub.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/guest-setup.sh" ]; then
    cp "$(dirname "${BASH_SOURCE[0]}")/guest-setup.sh" "$TMP/guest-setup.sh"
    ok "guest-setup.sh (uit deze map)"
else
    if curl -fsSL --max-time 60 -o "$TMP/guest-setup.sh" "$RAW/guest-setup.sh"; then
        ok "guest-setup.sh opgehaald van $RAW"
    else
        fout "kon guest-setup.sh niet ophalen van $RAW"
        exit 1
    fi
fi

if [ ! -s "$TMP/guest-setup.sh" ] || ! grep -q "SRT-RELAY-GUEST-SETUP" "$TMP/guest-setup.sh"; then
    fout "guest-setup.sh is leeg of onvolledig (marker ontbreekt)"
    exit 1
fi
ok "$(wc -c <"$TMP/guest-setup.sh") bytes, marker gevonden"

if [ "$FETCH_ONLY" = "1" ]; then
    stap "FETCH_ONLY: niets aangemaakt"
    ok "alle bestanden compleet"
    exit 0
fi

# ---------------------------------------------------------------- template

stap "Debian-template"

TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '{print $1}' \
    | grep -E "^${TEMPLATE_STORAGE}:vztmpl/debian-${DEBIAN}-standard_.*_amd64\.tar\.(zst|gz)$" \
    | sort | tail -1)"

if [ -z "$TEMPLATE" ]; then
    zeg "  geen debian-${DEBIAN}-template in $TEMPLATE_STORAGE; nu ophalen"
    pveam update >/dev/null 2>&1 || let_op "pveam update gaf een fout, toch verder"
    NAAM="$(pveam available --section system 2>/dev/null | awk '{print $2}' \
        | grep -E "^debian-${DEBIAN}-standard_.*_amd64\.tar\.(zst|gz)$" | sort | tail -1)"
    if [ -z "$NAAM" ]; then
        fout "geen debian-${DEBIAN}-template beschikbaar via pveam"
        exit 1
    fi
    if pveam download "$TEMPLATE_STORAGE" "$NAAM" >/dev/null 2>&1; then
        TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$NAAM"
        ok "gedownload: $NAAM"
    else
        fout "downloaden van $NAAM mislukt"
        exit 1
    fi
else
    ok "al aanwezig: $TEMPLATE"
fi

# ---------------------------------------------------------------- container-id

stap "Container"

if [ -z "$CTID" ]; then
    for i in $(seq 130 199); do
        if ! pct status "$i" >/dev/null 2>&1; then CTID="$i"; break; fi
    done
fi
if [ -z "$CTID" ]; then
    fout "geen vrij container-ID gevonden tussen 130 en 199 — geef er zelf een mee: CTID=200 bash ..."
    exit 1
fi
if pct status "$CTID" >/dev/null 2>&1; then
    fout "container $CTID bestaat al. Kies een ander ID of ruim hem eerst op: pct destroy $CTID"
    exit 1
fi
ok "container-ID $CTID"

zeg "  aanmaken ($CTNAME: ${CORES} core, ${RAM} MB, ${DISK} GB, DHCP op $BRIDGE)"
if ! pct create "$CTID" "$TEMPLATE" \
        --hostname "$CTNAME" \
        --cores "$CORES" --memory "$RAM" --swap 256 \
        --rootfs "$ROOTFS_STORAGE:$DISK" \
        --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
        --unprivileged 1 \
        --features nesting=1 \
        --onboot 1 \
        --description "SRT-relay / livestream-hub (MediaMTX). Beheer: /usr/local/bin/srt-feed" \
        >/dev/null 2>"$TMP/create.log"; then
    fout "pct create mislukt"
    sed 's/^/       /' "$TMP/create.log"
    exit 1
fi
ok "container aangemaakt"

# ---------------------------------------------------------------- starten (ladder)

stap "Starten"

start_lukt=0
if pct start "$CTID" >/dev/null 2>&1; then
    start_lukt=1
    ok "gestart"
else
    let_op "eerste startpoging mislukt; opnieuw met apparmor unconfined"
    pct set "$CTID" --lxc.apparmor.profile unconfined >/dev/null 2>&1
    if pct start "$CTID" >/dev/null 2>&1; then
        start_lukt=1
        ok "gestart met lxc.apparmor.profile=unconfined"
    else
        let_op "tweede poging mislukt; opnieuw met nesting=1"
        pct set "$CTID" --features nesting=1 >/dev/null 2>&1
        if pct start "$CTID" >/dev/null 2>&1; then
            start_lukt=1
            ok "gestart met nesting=1"
        fi
    fi
fi

if [ "$start_lukt" -ne 1 ]; then
    fout "container $CTID wil niet starten"
    zeg "  --- pveversion ---";        pveversion | head -2 | sed 's/^/     /'
    zeg "  --- pct config ---";        pct config "$CTID" 2>&1 | sed 's/^/     /'
    zeg "  --- laatste regels ---";    tail -n 15 /var/log/pve/tasks/index 2>/dev/null | sed 's/^/     /'
    zeg "  --- dmesg ---";             dmesg 2>/dev/null | tail -n 15 | sed 's/^/     /'
    zeg ""
    zeg "  Repareren kan zo:"
    zeg "     bash <(curl -fsSL $RAW/srt-relay.sh)   # met CTID=$CTID na het opruimen"
    exit 1
fi

# ---------------------------------------------------------------- netwerk + gast

stap "Netwerk van de container"

IP=""
for _ in $(seq 1 40); do
    IP="$(pct exec "$CTID" -- ip -4 -o addr show eth0 2>/dev/null \
        | awk '{print $4}' | cut -d/ -f1 | head -1)"
    [ -n "$IP" ] && break
    sleep 1
done
if [ -z "$IP" ]; then
    fout "geen IP-adres op eth0 (DHCP). Draai: pct exec $CTID -- ip -4 a"
    exit 1
fi
ok "IP-adres $IP"

stap "Relay installeren in de container"
zeg "  (ffmpeg en MediaMTX downloaden, kan een paar minuten duren)"

if ! pct push "$CTID" "$TMP/guest-setup.sh" /root/guest-setup.sh; then
    fout "kon guest-setup.sh niet in de container zetten"
    exit 1
fi

LOG="$TMP/guest.log"
if ! pct exec "$CTID" -- env SRT_PORT="$SRT_PORT" bash /root/guest-setup.sh >"$LOG" 2>&1; then
    fout "installatie in de container mislukte (laatste regels):"
    tail -n 25 "$LOG" | sed 's/^/     /'
    zeg "     Volledige log op de host: pct exec $CTID -- journalctl -u srt-relay -n 50"
    exit 1
fi
sed 's/^/  /' "$LOG" | grep -E "ok |\[fout\]|ZELFTEST|WACHTWOORD|let op" || true

if ! grep -q "ZELFTEST OK" "$LOG"; then
    fout "de relay draait niet aantoonbaar: zelftest in de container is niet geslaagd"
    tail -n 20 "$LOG" | sed 's/^/     /'
    exit 1
fi
ok "zelftest in de container geslaagd (publiceren + terugkijken over SRT)"

# ---------------------------------------------------------------- controle van buitenaf

stap "Controle vanaf de host"

if pct exec "$CTID" -- systemctl is-active --quiet srt-relay; then
    ok "dienst srt-relay is actief"
else
    fout "dienst srt-relay is niet actief"
fi

if pct exec "$CTID" -- bash -c "ss -lun | grep -q ':$SRT_PORT '"; then
    ok "UDP-poort $SRT_PORT luistert"
else
    fout "UDP-poort $SRT_PORT luistert niet in de container"
fi

WEB="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$IP:8888/controle/")"
if [ "$WEB" = "200" ]; then
    ok "relay antwoordt op http://$IP:8888 (HTTP 200)"
else
    fout "geen antwoord van http://$IP:8888 (kreeg '$WEB')"
fi

# ---------------------------------------------------------------- samenvatting

WACHTWOORD="$(awk '/^WACHTWOORD /{print $2; exit}' "$LOG" 2>/dev/null)"
if [ -z "$WACHTWOORD" ]; then
    WACHTWOORD="$(pct exec "$CTID" -- bash -c 'grep -A2 "^  - user: tom$" /opt/srt-relay/mediamtx.yml | awk "/pass:/{print \$2; exit}"')"
fi

stap "Klaar"

zeg "  Relay:            $IP  (container $CTID, $CTNAME)"
zeg "  SRT-poort:        $SRT_PORT/udp"
zeg ""
zeg "  Publiceren (OBS, ffmpeg, telefoon):"
zeg "     srt://$IP:$SRT_PORT?streamid=publish:mijnstream:tom:$WACHTWOORD"
zeg "     rtmp://$IP:1935   sleutel: mijnstream?user=tom&pass=$WACHTWOORD"
zeg ""
zeg "  Kijken:"
zeg "     srt://$IP:$SRT_PORT?streamid=read:mijnstream"
zeg "     http://$IP:8889/mijnstream/     (WebRTC, laagste vertraging)"
zeg "     http://$IP:8888/mijnstream/     (HLS)"
zeg ""
zeg "  Beheer in de container:"
zeg "     pct exec $CTID -- srt-streams            # wat staat er online"
zeg "     pct exec $CTID -- srt-feed voordeur rtsp://192.168.1.50:554/h264"
zeg "     pct exec $CTID -- systemctl status srt-relay"
zeg ""
zeg "  Van buitenaf: zet in je router UDP $SRT_PORT door naar $IP,"
zeg "  en gebruik dan srt://<jouw-domein>:$SRT_PORT?streamid=read:mijnstream."
zeg ""
zeg "  Opruimen:  pct destroy $CTID"

if [ "$FOUTEN" -gt 0 ]; then
    zeg ""
    zeg "  $FOUTEN controle(s) met een fout — zie [fout] hierboven."
    exit 1
fi
exit 0
