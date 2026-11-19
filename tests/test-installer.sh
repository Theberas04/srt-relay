#!/usr/bin/env bash
# Testbank voor srt-relay.sh: zet nep-pct/pveam/pveversion/id op PATH en laat
# het hostscript door twee scenario's lopen (alles goed, en starten faalt).
set -uo pipefail

HIER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STUB="$HIER/stub"
CTDIR="${CTDIR:-/tmp/srt-relay-test/ct}"

mkdir -p "$STUB" "$CTDIR"

# --- nep 'id': doe alsof we root zijn -------------------------------------
cat > "$STUB/id" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
EOF

# --- nep 'pveversion' ----------------------------------------------------
cat > "$STUB/pveversion" <<'EOF'
#!/usr/bin/env bash
echo "pve-manager/8.2.4/db6f2e6a3e4ff3d3 (running kernel: 6.8.12-1-pve)"
EOF

# --- nep 'pveam' ---------------------------------------------------------
cat > "$STUB/pveam" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  update) exit 0 ;;
  list)   exit 0 ;;                      # niets aanwezig -> downloadpad
  available) printf 'system           debian-13-standard_13.1-1_amd64.tar.zst\nsystem           debian-12-standard_12.7-1_amd64.tar.zst\n' ;;
  download) echo "downloaded" ; exit 0 ;;
  *) exit 0 ;;
esac
EOF

# --- nep 'pct' met een toestand per container-ID -------------------------
cat > "$STUB/pct" <<'PYEOF'
#!/usr/bin/env bash
# Scenario's: BESTAANDE_ID=<id> (al in gebruik), START_FAALT=1 (starten mislukt)
CTDIR="${CTDIR:?}"
ID="${2:-}"
mkdir -p "$CTDIR"

bestaat() { [ -f "$CTDIR/$1" ]; }

case "${1:-}" in
  status)
    if bestaat "$ID"; then echo "status: running"; exit 0; else
      echo "Configuration file 'nodes/pve/lxc/$ID.conf' does not exist" >&2; exit 1
    fi ;;
  create)
    if [ -n "${BESTAANDE_ID:-}" ] && [ "$ID" = "$BESTAANDE_ID" ]; then
      echo "CT $ID already exists" >&2; exit 1
    fi
    if ! printf '%s\n' "$@" | grep -q -- '--hostname'; then
      echo "create: --hostname ontbreekt" >&2; exit 1
    fi
    : > "$CTDIR/$ID"; echo "created $ID" ;;
  start)
    if [ "${START_FAALT:-0}" = "1" ]; then
      echo "lxc-start: $ID: startup failed" >&2; exit 1
    fi
    bestaat "$ID" || { echo "geen CT $ID" >&2; exit 1; }
    echo "started" ;;
  set)  echo "set $ID" ;;
  push) echo "pushed $3 naar $ID" ;;
  exec)
    shift 2
    [ "${1:-}" = "--" ] && shift
    case "$*" in
      *"ip -4 -o addr show eth0"*) echo "2: eth0    inet ${GAST_IP:-192.168.1.50}/24 brd 192.168.1.255 scope global eth0" ;;
      *guest-setup.sh*)  printf 'ok   ffmpeg 7.1.5\nok   libsrt: libsrt-gnutls.so.1.5\n  WACHTWOORD testwachtwoord123\n  BORD wachtwoord=bordtest123\nZELFTEST OK\nZELFTEST BORD OK\n' ;;
      *"is-active --quiet srt-relay"*) exit 0 ;;
      *"ss -lun"*) exit 0 ;;
      *"/api/streams"*) echo '{"streams": [], "srt_poort": 8890, "geladen": true}' ;;
      *"relay.env"*) echo "testwachtwoord123" ;;
      *) echo "pct exec: onbekend: $*" >&2; exit 1 ;;
    esac ;;
  destroy) rm -f "$CTDIR/$ID"; echo "destroyed $ID" ;;
  config)  echo "hostname: srt-relay" ;;
  *) echo "pct: onbekend subcommando ${1:-}" >&2; exit 1 ;;
esac
PYEOF

chmod +x "$STUB/id" "$STUB/pct" "$STUB/pveam" "$STUB/pveversion"
echo "stubs staan in $STUB"

run() {
  # run <omschrijving> <verwachte exitcode> [extra env...]
  local naam="$1" verwacht="$2"; shift 2
  echo
  echo "################################################################"
  echo "# $naam"
  echo "################################################################"
  LAATSTE_UIT="$(env PATH="$STUB:$PATH" CTDIR="$CTDIR" PVE_ETC=/tmp/srt-relay-test/pve "$@" \
        bash "$HIER/../srt-relay.sh" 2>&1)"
  local code=$?
  printf '%s\n' "$LAATSTE_UIT"
  echo "--- exitcode $code (verwacht $verwacht) ---"
  if [ "$code" -ne "$verwacht" ]; then
    echo "MISLUKT: verkeerde exitcode"
    return 1
  fi
  return 0
}

mkdir -p /tmp/srt-relay-test/pve
rm -f "$CTDIR"/* 2>/dev/null

# Een nep-bordpagina op een eigen poort, zodat de HTTP-controle van buitenaf echt
# gemeten wordt (en niet afhangt van wat er toevallig op de testmachine draait).
BORD_POORT_NEP=18888
WEBMAP=/tmp/srt-relay-test/web
mkdir -p "$WEBMAP/controle"
printf 'ok\n' > "$WEBMAP/controle/index.html"
python3 -m http.server "$BORD_POORT_NEP" --bind 127.0.0.1 --directory "$WEBMAP" >/dev/null 2>&1 &
WEBPID=$!
trap 'kill $WEBPID 2>/dev/null; true' EXIT
sleep 1
if curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$BORD_POORT_NEP/controle/" | grep -q 200; then
  echo "nep-bord op 127.0.0.1:$BORD_POORT_NEP draait (pid $WEBPID)"
else
  echo "MISLUKT: kon de nep-relay niet starten"; exit 1
fi

# Scenario 0: FETCH_ONLY (mag niets aanmaken)
run "FETCH_ONLY: alleen bestanden ophalen" 0 FETCH_ONLY=1 || exit 1

# Scenario 0b: de bestanden echt van GitHub halen (als REPO_RAW is meegegeven)
if [ -n "${REPO_RAW:-}" ]; then
  WERKMAP="$(mktemp -d)"
  cp "$HIER/../srt-relay.sh" "$WERKMAP/"        # zonder guest-setup.sh ernaast
  rm -f "$CTDIR"/*
  uit="$(env PATH="$STUB:$PATH" CTDIR="$CTDIR" PVE_ETC=/tmp/srt-relay-test/pve \
        REPO_RAW="$REPO_RAW" FETCH_ONLY=1 bash "$WERKMAP/srt-relay.sh" 2>&1)"
  echo
  echo "################################################################"
  echo "# bestanden ophalen van $REPO_RAW"
  echo "################################################################"
  printf '%s\n' "$uit"
  if printf '%s' "$uit" | grep -q "alle bestanden compleet"; then
    echo "ok: de one-liner haalt alle bestanden van GitHub"
  else
    echo "MISLUKT: ophalen van GitHub lukte niet"; exit 1
  fi
  rm -rf "$WERKMAP"
fi

# Scenario 1: alles goed (gast = de nep-relay op 127.0.0.1)
rm -f "$CTDIR"/*
run "gewone installatie" 0 CTID=140 GAST_IP=127.0.0.1 BORD_POORT="$BORD_POORT_NEP" || exit 1
[ -f "$CTDIR/140" ] || { echo "MISLUKT: container 140 niet aangemaakt"; exit 1; }
if printf '%s' "$LAATSTE_UIT" | grep -q "uitzendbord antwoordt op http://127.0.0.1:$BORD_POORT_NEP"; then
  echo "ok: HTTP-controle van buitenaf geslaagd"
else
  echo "MISLUKT: HTTP-controle van buitenaf niet geslaagd"; exit 1
fi

# Scenario 2: starten faalt in alle drie de trappen
rm -f "$CTDIR"/*
run "starten mislukt (ladder + diagnose)" 1 CTID=141 START_FAALT=1 || exit 1

# Scenario 3: gast meldt een mislukte zelftest
rm -f "$CTDIR"/*
uit="$(env PATH="$STUB:$PATH" CTDIR="$CTDIR" PVE_ETC=/tmp/srt-relay-test/pve \
      GAST_IP=192.0.2.99 CTID=142 BORD_POORT=8888 bash "$HIER/../srt-relay.sh" 2>&1)"
echo "$uit" | tail -6
echo "--- controle op de HTTP-controle van buitenaf ---"
if printf '%s' "$uit" | grep -q "geen antwoord van het uitzendbord op http://192.0.2.99:8888"; then
  echo "ok: onbereikbare gast wordt als fout gemeld"
else
  echo "MISLUKT: onbereikbare gast niet gemeld"; exit 1
fi

# Scenario 4: een zelfgekozen ID die al in gebruik is
rm -f "$CTDIR"/*
: > "$CTDIR/130"
uit="$(env PATH="$STUB:$PATH" CTDIR="$CTDIR" PVE_ETC=/tmp/srt-relay-test/pve \
      CTID=130 bash "$HIER/../srt-relay.sh" 2>&1)"
if printf '%s' "$uit" | grep -q "container 130 bestaat al"; then
  echo "ok: bezet ID wordt gemeld"
else
  echo "MISLUKT: bezet ID niet gemeld"; printf '%s\n' "$uit" | tail -8; exit 1
fi

# Scenario 5: geen CTID meegegeven -> eerste vrije (131, want 130 is bezet)
rm -f "$CTDIR"/*
: > "$CTDIR/130"
uit="$(env PATH="$STUB:$PATH" CTDIR="$CTDIR" PVE_ETC=/tmp/srt-relay-test/pve \
      GAST_IP=127.0.0.1 BORD_POORT="$BORD_POORT_NEP" bash "$HIER/../srt-relay.sh" 2>&1)"
if printf '%s' "$uit" | grep -q "container-ID 131" && [ -f "$CTDIR/131" ]; then
  echo "ok: eerste vrije ID (131) gekozen"
else
  echo "MISLUKT: vrije ID niet correct gekozen"; printf '%s\n' "$uit" | tail -8; exit 1
fi

echo
echo "ALLE SCENARIO'S DOORLOPEN"
