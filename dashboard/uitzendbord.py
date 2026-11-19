#!/usr/bin/env python3
"""uitzendbord — een webbord om het SRT-testbeeld te bedienen.

Wat je op het bord kunt:
  * bestanden kiezen uit de bibliotheek (slepen of uploaden) en in een
    afspeellijst zetten; ze spelen in die volgorde, in een lus
  * de opmaak instellen: titel, crawl, tijdcode, kanaalvak, logo
  * het formaat kiezen: 1080/720/576, 25 of 50 beelden per seconde, p of i,
    bitrate, preset en het aantal audiokanalen
  * bouwen en uitzenden naar de relay, met voortgang, log en een voorbeeld
  * kijklinken: HLS, WebRTC en de SRT-URL voor speler-apps

Alleen de standaardbibliotheek van Python plus ffmpeg; geen pip-pakketten.
"""

from __future__ import annotations

import argparse
import base64
import collections
import glob
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import streams  # noqa: E402

HIER = os.path.dirname(os.path.abspath(__file__))
BORD_HTML = os.path.join(HIER, "bord.html")

STANDAARD = {
    "titel": "TESTBEELD — WERDENS MEDIA",
    "crawl": "SRT-TESTBEELD +++ WERDENS MEDIA +++ ",
    "tijdcode": "serverklok",
    "kanaalvak": True,
    "logo": True,
    "formaat": "720",
    "fps": 25,
    "interlaced": False,
    "bitrate": "4M",
    "preset": "veryfast",
    "kanalen": 8,
    "slot": 3.0,
    "streamnaam": "testbeeld",
    "afspeellijst": [],
}

MEDIA_EXTENSIES = streams.MEDIA_EXTENSIES

# Hoeveel het beeld mag achterlopen op de klok voordat we ingrijpen. Een file-lus
# met een klok erin hoort in echte tijd te lopen; een machine die het formaat niet
# bijhoudt loopt anders uren achter (het beeld toont dan een tijd van lang geleden).
DREMPEL_LET_OP = 15.0        # seconden: hier waarschuwen we
DREMPEL_HERSTART = 30.0      # seconden: hier zetten we de uitzending opnieuw in
MAX_HERSTARTS = 3

PRESETS = ["ultrafast", "veryfast", "faster", "fast", "medium", "slow"]
LOGO_PAD = os.environ.get(
    "SRT_LOGO", "")          # leeg = geen logo; zet SRT_LOGO op een png om er een te gebruiken

# ---------------------------------------------------------------- toestand ---

SLOT = threading.Lock()
S = {
    "log": [],
    "lognr": 0,
    "render": {"bezig": False, "voortgang": 0.0, "klaar": False, "fout": None,
               "duur": 0.0, "bestand": None, "gestart": None},
    "uitzending": {"actief": False, "naam": None, "gestart": None, "fout": None},
    "proc": None,
    "peilingen": {},
}
INSTELLINGEN = dict(STANDAARD)
CONFIG = {
    "media_dir": "/srv/streams/bronnen",
    "werk_dir": "/srv/streams/werk",
    "poort": 8095,
    "wachtwoord": "",
    "gebruiker": "tom",
    "max_upload": 8 * 1024 ** 3,
    "api": "http://127.0.0.1:8080",      # eigen hub (HTTP-poort van de relay)
    "http_poort": 8080,                  # HLS, live TS en API van de eigen hub
    "relay": {"host": "127.0.0.1", "poort": 8890, "gebruiker": "tom",
              "wachtwoord": ""},
}


def meld(regel: str) -> None:
    with SLOT:
        S["lognr"] += 1
        S["log"].append({"n": S["lognr"],
                         "tijd": time.strftime("%H:%M:%S"),
                         "regel": regel})
        if len(S["log"]) > 800:
            del S["log"][:200]
    print(f"[bord] {regel}", flush=True)


# ------------------------------------------------------------------ paden ----


def veilige_naam(naam: str) -> str:
    naam = os.path.basename((naam or "").strip())
    naam = re.sub(r"[^A-Za-z0-9._ ()\-]+", "_", naam).strip(". ")
    if not naam:
        raise ValueError("geen bestandsnaam")
    return naam[:180]


def medi_pad(naam: str) -> str:
    return os.path.join(CONFIG["media_dir"], veilige_naam(naam))


def bestandslijst() -> list[dict]:
    map_ = CONFIG["media_dir"]
    os.makedirs(map_, exist_ok=True)
    uit = []
    for naam in sorted(os.listdir(map_)):
        if naam.startswith(".") or not naam.lower().endswith(MEDIA_EXTENSIES):
            continue
        pad = os.path.join(map_, naam)
        if not os.path.isfile(pad):
            continue
        try:
            stat = os.stat(pad)
        except OSError:
            continue
        sleutel = f"{naam}:{stat.st_size}:{int(stat.st_mtime)}"
        peiling = S["peilingen"].get(sleutel)
        if peiling is None:
            try:
                peiling = streams.peil(pad)
            except Exception as fout:                      # noqa: BLE001
                peiling = {"fout": str(fout)[:200], "duur": 0, "breedte": 0,
                           "hoogte": 0, "fps": 0, "geluid": False}
            S["peilingen"][sleutel] = peiling
        item = dict(peiling)
        item["naam"] = naam
        item["bytes"] = stat.st_size
        item["gewijzigd"] = int(stat.st_mtime)
        uit.append(item)
    uit.sort(key=lambda i: i["gewijzigd"], reverse=True)
    return uit


def bewaar_instellingen() -> None:
    pad = os.path.join(CONFIG["werk_dir"], "bord-instellingen.json")
    os.makedirs(CONFIG["werk_dir"], exist_ok=True)
    tijdelijk = pad + ".nieuw"
    with open(tijdelijk, "w", encoding="utf-8") as f:
        json.dump(INSTELLINGEN, f, ensure_ascii=False, indent=1)
    os.replace(tijdelijk, pad)


def laad_instellingen() -> None:
    pad = os.path.join(CONFIG["werk_dir"], "bord-instellingen.json")
    if not os.path.exists(pad):
        return
    try:
        with open(pad, encoding="utf-8") as f:
            gegevens = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(gegevens, dict):
        # "lusbestand" hoort erbij: dat is het bestand dat de uitzending leest, en
        # zonder deze uitzondering vergeet het bord na een herstart welke lus er is
        INSTELLINGEN.update({k: v for k, v in gegevens.items()
                             if k in STANDAARD or k == "lusbestand"})


def werk_pad(naam: str) -> str:
    os.makedirs(CONFIG["werk_dir"], exist_ok=True)
    return os.path.join(CONFIG["werk_dir"], naam)


# ------------------------------------------------------------- relay-status --


def relay_status(naam: str) -> dict:
    """Vraag de eigen hub hoe het met deze uitzending staat."""
    adres = f"{CONFIG['api']}/api/hub/stroom/{quote(naam)}"
    try:
        with urllib.request.urlopen(adres, timeout=3) as antwoord:
            gegevens = json.load(antwoord)
    except (urllib.error.URLError, ValueError, OSError):
        return {"bekend": False}
    sporen = list(gegevens.get("sporen") or [])
    return {
        "bekend": True,
        "online": bool(gegevens.get("online")),
        "tracks": sporen,
        # Het aantal sporen zoals de relay ze telt (uit de programmatabel), met
        # terugval op het aantal codenamen als de relay het niet doorgeeft.
        "sporen": int(gegevens.get("sporen_aantal") or len(sporen)),
        "kijkers": int(gegevens.get("kijkers") or 0),
        "bytes": int(gegevens.get("bytes") or 0),
        "bron": gegevens.get("bron"),
        "conf": gegevens.get("bron"),
    }


def relay_sporen(naam: str) -> dict:
    """Alle uitzendingen die de eigen hub kent."""
    try:
        with urllib.request.urlopen(f"{CONFIG['api']}/api/streams", timeout=3) as a:
            gegevens = json.load(a)
    except (urllib.error.URLError, ValueError, OSError):
        return {"bekend": False, "paden": []}
    paden = []
    for item in gegevens.get("streams", []):
        sporen = list(item.get("sporen") or [])
        paden.append({"naam": item.get("naam"), "online": bool(item.get("online")),
                      "tracks": sporen,
                      "kijkers": int(item.get("kijkers") or 0)})
    return {"bekend": True, "paden": paden}


# ------------------------------------------------------------------ render ---


def bouw_opties() -> dict:
    formaat = next((f for f in streams.BEELDFORMATEN
                    if f["id"] == INSTELLINGEN.get("formaat")), None)
    bronnen = [medi_pad(n) for n in INSTELLINGEN.get("afspeellijst", [])]
    if not bronnen:
        raise ValueError("de afspeellijst is leeg — kies eerst bestanden")
    ontbreekt = [os.path.basename(b) for b in bronnen if not os.path.exists(b)]
    if ontbreekt:
        raise ValueError("bestaat niet meer: " + ", ".join(ontbreekt))

    peilingen = [streams.peil(b) for b in bronnen]
    duur = sum(p["duur"] for p in peilingen)
    if duur <= 0:
        raise ValueError("de gekozen bestanden hebben geen leesbare lengte")

    breedte = formaat["breedte"] if formaat else 0
    hoogte = formaat["hoogte"] if formaat else 0
    if not breedte or not hoogte:
        # zelfde als de bron, maar wel even getallen (yuv420p eist dat)
        breedte = peilingen[0]["breedte"] - peilingen[0]["breedte"] % 2
        hoogte = peilingen[0]["hoogte"] - peilingen[0]["hoogte"] % 2

    kanalen = int(INSTELLINGEN["kanalen"])
    # alleen 2, 4 of 8 kanalen: dat zijn hele stereosporen (1, 2 of 4 sporen).
    kanalen = 8 if kanalen >= 8 else (4 if kanalen >= 4 else 2)
    return {
        "bronnen": bronnen,
        "breedte": int(breedte),
        "hoogte": int(hoogte),
        "fps": int(INSTELLINGEN["fps"]),
        "interlaced": bool(INSTELLINGEN["interlaced"]),
        "titel": INSTELLINGEN["titel"],
        "crawl": INSTELLINGEN["crawl"],
        "tijdcode": str(INSTELLINGEN["tijdcode"]),
        "kanaalvak": bool(INSTELLINGEN["kanaalvak"]),
        "logo": bool(INSTELLINGEN["logo"]),
        "bitrate": INSTELLINGEN["bitrate"],
        "preset": INSTELLINGEN["preset"],
        "kanalen": kanalen,
        "slot": float(INSTELLINGEN["slot"]),
        "duur": round(duur, 3),
        "peilingen": peilingen,
    }


def render_klaar(uitpad: str, opties: dict) -> None:
    if not os.path.exists(uitpad):
        raise ValueError("geen uitvoerbestand gemaakt")
    uit = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", uitpad], capture_output=True, text=True)
    gegevens = json.loads(uit.stdout or "{}")
    sporen = gegevens.get("streams", [])
    beeld = [s for s in sporen if s.get("codec_type") == "video"]
    geluid = [s for s in sporen if s.get("codec_type") == "audio"]
    bframes = int((beeld[0].get("has_b_frames") if beeld else 0) or 0)
    meld(f"controle: {len(beeld)} videospoor, {len(geluid)} audiospoor, "
         f"B-frames={bframes}, formaat "
         f"{beeld[0]['width']}x{beeld[0]['height']} {beeld[0].get('avg_frame_rate')}"
         if beeld else "controle: geen videospoor")
    if bframes:
        meld("LET OP: B-frames aanwezig — MediaMTX weigert die voor WebRTC")
    if len(geluid) != opties["kanalen"] // 2:
        meld(f"LET OP: verwacht {opties['kanalen'] // 2} audiosporen, "
             f"het bestand heeft er {len(geluid)}")
    grootte = os.path.getsize(uitpad) / 1e6
    meld(f"klaar: {os.path.basename(uitpad)} ({grootte:.0f} MB, "
         f"{opties['duur']:.0f} s, {streams.noem_formaat(opties['breedte'], opties['hoogte'], opties)}"
         f")")


def doe_render() -> None:
    # Nooit schrijven naar het bestand dat een lopende uitzending leest: ffmpeg
    # struikelt dan halverwege over nieuwe inhoud (decodeerfouten) en de
    # uitzending valt stil. Bouw naar een nieuw bestand en zet de uitzending pas
    # over als het klaar is.
    uitpad = werk_pad("lus-" + time.strftime("%Y%m%d-%H%M%S") + ".mp4")
    filterpad = werk_pad("beeld.filter")
    tonenpad = werk_pad("tonen.wav")
    try:
        opties = bouw_opties()
        logo = LOGO_PAD if opties["logo"] else None
        if logo and not os.path.exists(logo):
            meld(f"logo ontbreekt ({logo}) — zonder logo verder")
            logo = None
        meld("toontrack maken "
             f"({opties['duur']:.0f} s, {opties['kanalen']} kanalen)...")
        streams.maak_tonen(tonenpad, opties["slot"], opties["kanalen"],
                           duur=opties["duur"])
        opdracht = streams.bouw_render_opdracht(
            opties["bronnen"], opties, uitpad, filterpad, tonenpad, logo)
        meld("bouwen: " + " ".join(f"{o}" if " " not in o else repr(o)
                                   for o in opdracht[:10]) + " ...")
        with SLOT:
            S["render"].update({"duur": opties["duur"], "voortgang": 0.0})
        proces = subprocess.Popen(opdracht, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  bufsize=1)
        with SLOT:
            S["proc"] = proces
        for regel in (proces.stdout or ()):
            regel = regel.strip()
            if regel.startswith("out_time_us="):
                waarde = regel.split("=", 1)[1]
                if waarde.isdigit():
                    seconde = int(waarde) / 1e6
                    with SLOT:
                        S["render"]["voortgang"] = round(
                            min(99.0, 100.0 * seconde / max(0.1, opties["duur"])), 1)
                continue
            if regel.startswith(("frame=", "fps=", "bitrate=", "total_size=",
                                 "out_time=", "out_time_ms=", "out_time_us=",
                                 "speed=", "progress=", "dup_frames=",
                                 "drop_frames=", "stream_", "encoder=")):
                continue
            if regel:
                meld(regel[:400])
        code = proces.wait()
        if code != 0:
            raise ValueError(f"ffmpeg stopte met code {code} — zie het log")
        render_klaar(uitpad, opties)
        with SLOT:
            S["render"].update({"voortgang": 100.0, "klaar": True,
                                "bestand": uitpad, "fout": None})
        nieuwe_lus_in_gebruik(uitpad)
    except Exception as fout:                              # noqa: BLE001
        with SLOT:
            S["render"]["fout"] = str(fout)[:400]
        meld(f"FOUT bij bouwen: {fout}")
    finally:
        with SLOT:
            S["render"]["bezig"] = False
            S["proc"] = None


def start_render() -> bool:
    with SLOT:
        if S["render"]["bezig"]:
            return False
        S["render"].update({"bezig": True, "voortgang": 0.0, "klaar": False,
                            "fout": None, "gestart": time.time()})

    def veilig() -> None:
        # Vangt ook fouten die buiten doe_render om gaan (bijvoorbeeld in het
        # klaarzetten van de opdracht), zodat het bord niet voor altijd op
        # "bezig" blijft staan.
        try:
            doe_render()
        except Exception as fout:                           # noqa: BLE001
            with SLOT:
                S["render"]["fout"] = str(fout)[:400]
            meld(f"FOUT bij bouwen: {fout}")
        finally:
            with SLOT:
                S["render"]["bezig"] = False
                S["proc"] = None

    threading.Thread(target=veilig, daemon=True).start()
    return True


def nieuwe_lus_in_gebruik(uitpad: str) -> None:
    """De verse lus in gebruik nemen en een lopende uitzending overzetten."""
    with SLOT:
        INSTELLINGEN["lusbestand"] = uitpad
        loopt = bool(S["uitzending"]["actief"])
    bewaar_instellingen()
    meld(f"nieuwe lus in gebruik: {os.path.basename(uitpad)}")
    if loopt:
        meld("de uitzending loopt — ik zet hem over op de nieuwe lus")
        veilige_thread(herstart_uitzending, naam="overzetten")
    veilige_thread(ruim_oude_lussen_op, uitpad, naam="lussen-opruimen")


def herstart_uitzending() -> None:
    """De uitzending opnieuw inzetten (voor een overzetting of om bij te komen)."""
    stop_uitzending()
    time.sleep(2.0)
    try:
        start_uitzending()
    except ValueError as fout:                              # noqa: BLE001
        meld(f"opnieuw starten van de uitzending lukte niet: {fout}")


def ruim_oude_lussen_op(huidig: str) -> None:
    """Oude lusbestanden weg, met ruime marge voor wat nog gelezen kan worden."""
    nu = time.time()
    for pad in glob.glob(werk_pad("lus-*.mp4")) + [werk_pad("lust.mp4")]:
        if pad == huidig or not os.path.exists(pad):
            continue
        if nu - os.path.getmtime(pad) < 600:                # tien minuten marge
            continue
        try:
            os.remove(pad)
            meld(f"oude lus opgeruimd: {os.path.basename(pad)}")
        except OSError:
            pass


# -------------------------------------------------------------- uitzending ---


def bewaak_uitzending(proces: subprocess.Popen, naam: str, verwacht: int) -> None:
    """Meld in het log wat de relay van de stream vindt."""
    time.sleep(4)
    stand = relay_status(naam)
    if not stand.get("bekend"):
        meld("de relay-API is niet bereikbaar — kan de stream niet controleren")
    elif not stand.get("online"):
        meld(f"LET OP: '{naam}' staat niet online in de relay (nog)")
    else:
        meld(f"relay ziet '{naam}': online, {stand['sporen']} sporen "
             f"({', '.join(stand['tracks'])})")
        if verwacht and stand["sporen"] < verwacht:
            meld(f"LET OP: verwacht {verwacht} sporen, relay telt er "
                 f"{stand['sporen']} — gaat er onderweg een spoor verloren?")
    herstarts = 0
    while proces.poll() is None:
        stand = relay_status(naam)
        with SLOT:
            if stand.get("bekend"):
                S["uitzending"]["relay"] = stand
            media = S["uitzending"].get("media_tijd")
            gestart = S["uitzending"].get("gestart") or time.time()
        looptijd = time.time() - gestart
        if media and looptijd > 10:
            # De uitzending hoort in echte tijd te lopen: het beeld mag niet
            # achterblijven bij de klok. media_tijd is hoever ffmpeg gekomen is.
            achterstand = looptijd - media
            with SLOT:
                S["uitzending"]["achterstand"] = round(achterstand, 1)
            if achterstand > DREMPEL_LET_OP:
                meld(f"LET OP: de uitzending loopt {achterstand:.0f} s achter — "
                     "deze machine houdt dit formaat niet bij. Kies een lichter "
                     "formaat, een snellere preset of meer cores voor de container.")
            if achterstand > DREMPEL_HERSTART and herstarts < MAX_HERSTARTS:
                herstarts += 1
                meld(f"de uitzending loopt {achterstand:.0f} s achter op de klok — "
                     f"ik zet hem opnieuw in om weer bij de tijd te komen "
                     f"({herstarts} van {MAX_HERSTARTS})")
                veilige_thread(herstart_uitzending, naam="bijkomen")
                return
            if herstarts >= MAX_HERSTARTS and achterstand > DREMPEL_HERSTART:
                herstarts += 1                     # één keer melden, niet blijven zeuren
                meld("de uitzending blijft achterlopen; ik stop met opnieuw "
                     "inzetten. Neem een lichter formaat of meer cores.")
        time.sleep(5)
    with SLOT:
        # Alleen opruimen als dit nog steeds de lopende uitzending is: na een
        # overzetting loopt er al een nieuwe, en een oude bewaker mag die
        # toestand niet wissen (het bord zou dan "uit" melden terwijl het beeld
        # gewoon loopt).
        if S["uitzending"].get("proc") is proces:
            S["uitzending"].update({"actief": False, "proc": None,
                                    "snelheid": None, "achterstand": None,
                                    "media_tijd": None})
    if proces.returncode not in (0, -15, -9):
        meld(f"uitzending gestopt met code {proces.returncode} — zie het log")
    else:
        meld(f"uitzending van '{naam}' gestopt")


def start_uitzending() -> dict:
    with SLOT:
        if S["uitzending"]["actief"]:
            raise ValueError("er loopt al een uitzending — stop die eerst")
    lusbestand = INSTELLINGEN.get("lusbestand") or werk_pad("lust.mp4")
    if not lusbestand or not os.path.exists(lusbestand):
        raise ValueError("er is nog geen lusbestand — klik eerst op 'bouwen'")
    relay = CONFIG["relay"]
    if not relay.get("wachtwoord"):
        raise ValueError("geen publiceerwachtwoord gevonden; zet SRT_PUB_PASS "
                         "in /opt/srt-relay/uitzendbord.env")
    naam = veilige_naam(INSTELLINGEN["streamnaam"]).lower().replace(" ", "-")
    if not naam:
        raise ValueError("kies een streamnaam")
    opties = None
    try:
        opties = bouw_opties()
    except ValueError:
        pass
    verwacht = (opties["kanalen"] // 2 + 1) if opties else 0
    opdracht = streams.bouw_uitzend_opdracht(
        lusbestand, relay["host"], int(relay["poort"]), naam,
        relay["gebruiker"], relay["wachtwoord"], opties=opties)
    meld(f"uitzenden naar srt://{relay['host']}:{relay['poort']} als '{naam}'")
    proces = subprocess.Popen(opdracht, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, bufsize=1)
    with SLOT:
        S["uitzending"] = {"actief": True, "naam": naam, "proc": proces,
                           "gestart": time.time(), "fout": None, "relay": {}}
    veilige_thread(bewaak_uitzending, proces, naam, verwacht,
                   naam="uitzending-bewaker")
    veilige_thread(lees_procesuitvoer, proces, naam="uitzending-uitvoer")
    return {"naam": naam}


def lees_procesuitvoer(proces: subprocess.Popen) -> None:
    """Leest de uitvoer van de uitzender: voortgang eruit, meldingen in het log."""
    for regel in (proces.stdout or ()):
        regel = regel.strip()
        if not regel:
            continue
        if regel.startswith("out_time_us="):
            waarde = regel.split("=", 1)[1]
            if waarde.isdigit():
                with SLOT:
                    S["uitzending"]["media_tijd"] = int(waarde) / 1e6
            continue
        if regel.startswith("speed="):
            with SLOT:
                S["uitzending"]["snelheid"] = regel.split("=", 1)[1].strip()
            continue
        if regel.startswith("fps="):
            with SLOT:
                S["uitzending"]["beelden_per_seconde"] = regel.split("=", 1)[1].strip()
            continue
        if regel.startswith(("frame=", "bitrate=", "total_size=", "out_time=",
                             "out_time_ms=", "dup_frames=", "drop_frames=",
                             "stream_", "progress=")):
            continue
        meld(regel[:400])


def veilige_thread(doel, *argumenten, naam: str = "taak") -> threading.Thread:
    """Start een achtergrondtaak die een fout meldt in plaats van te verdwijnen."""
    def omslag() -> None:
        try:
            doel(*argumenten)
        except Exception as fout:                            # noqa: BLE001
            meld(f"FOUT in {naam}: {type(fout).__name__}: {fout}")

    draad = threading.Thread(target=omslag, daemon=True, name=naam)
    draad.start()
    return draad


def stop_uitzending() -> bool:
    with SLOT:
        proces = S["uitzending"].get("proc")
        gestopt = bool(S["uitzending"]["actief"])
    if proces and proces.poll() is None:
        proces.terminate()
        try:
            proces.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proces.kill()
    with SLOT:
        S["uitzending"].update({"actief": False, "proc": None})
    return gestopt


# ------------------------------------------------------------------ server ---


class Bord(BaseHTTPRequestHandler):
    server_version = "uitzendbord/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:     # stilte in de console
        return

    # --- hulpjes -----------------------------------------------------------
    def stuur_json(self, gegevens, code: int = 200) -> None:
        brok = json.dumps(gegevens, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(brok)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(brok)

    def stuur_fout(self, code: int, tekst: str) -> None:
        # Bij een fout op een verzoek met inhoud sluiten we de verbinding: anders
        # leest de volgende keer de rest van de upload als nieuw verzoek.
        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
        self.stuur_json({"fout": tekst}, code)

    def lees_json(self) -> dict:
        lengte = int(self.headers.get("Content-Length") or 0)
        if not lengte:
            return {}
        try:
            return json.loads(self.rfile.read(lengte).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def host_van_verzoek(self) -> str:
        ruw = self.headers.get("Host") or ""
        host = ruw.rsplit(":", 1)[0].strip("[]")
        if host and host not in ("localhost", "127.0.0.1"):
            return host
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"

    def heeft_toegang(self) -> bool:
        wachtwoord = CONFIG["wachtwoord"]
        if not wachtwoord:
            return True
        kop = self.headers.get("Authorization") or ""
        if not kop.startswith("Basic "):
            return False
        try:
            ruw = base64.b64decode(kop[6:]).decode("utf-8")
            gebruiker, _, gegeven = ruw.partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return (hmac.compare_digest(gebruiker, CONFIG["gebruiker"]) and
                hmac.compare_digest(gegeven, wachtwoord))

    def eis_toegang(self) -> bool:
        if self.heeft_toegang():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="uitzendbord"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # --- GET ---------------------------------------------------------------
    def do_GET(self) -> None:
        deel = urlsplit(self.path)
        pad = deel.path
        query = parse_qs(deel.query)
        if not self.eis_toegang():
            return
        if pad in ("/", "/index.html"):
            return self.stuur_bord()
        if pad == "/api/staat":
            return self.stuur_json(self.staat())
        if pad == "/api/bestanden":
            return self.stuur_json({"bestanden": bestandslijst(),
                                    "media_dir": CONFIG["media_dir"]})
        if pad == "/api/log":
            vanaf = int((query.get("vanaf") or ["0"])[0] or 0)
            with SLOT:
                regels = [r for r in S["log"] if r["n"] > vanaf]
                nummer = S["lognr"]
            return self.stuur_json({"regels": regels, "nummer": nummer})
        if pad == "/api/streams":
            return self.stuur_json(relay_sporen(""))
        if pad == "/api/peil":
            naam = (query.get("naam") or [""])[0]
            pad_bestand = medi_pad(naam)
            if not os.path.exists(pad_bestand):
                return self.stuur_fout(404, "bestand niet gevonden")
            try:
                return self.stuur_json(streams.peil(pad_bestand))
            except Exception as fout:                      # noqa: BLE001
                return self.stuur_fout(400, str(fout))
        return self.stuur_fout(404, "onbekend pad")

    def stuur_bord(self) -> None:
        with open(BORD_HTML, "rb") as f:
            brok = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(brok)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(brok)

    # --- POST --------------------------------------------------------------
    def do_POST(self) -> None:
        deel = urlsplit(self.path)
        pad = deel.path
        query = parse_qs(deel.query)
        if not self.eis_toegang():
            return
        try:
            if pad == "/api/upload":
                return self.doe_upload(query)
            if pad == "/api/instellingen":
                gegevens = self.lees_json()
                with SLOT:
                    for sleutel, waarde in gegevens.items():
                        if sleutel in STANDAARD:
                            INSTELLINGEN[sleutel] = waarde
                    if INSTELLINGEN["formaat"] not in [f["id"] for f in
                                                       streams.BEELDFORMATEN]:
                        INSTELLINGEN["formaat"] = "1080"
                    INSTELLINGEN["fps"] = 50 if int(INSTELLINGEN["fps"]) == 50 else 25
                    if str(INSTELLINGEN["tijdcode"]) not in streams.TIJDCODE_STANDEN:
                        INSTELLINGEN["tijdcode"] = ("uitzending"
                                                    if INSTELLINGEN["tijdcode"]
                                                    else "uit")
                    INSTELLINGEN["kanalen"] = int(INSTELLINGEN["kanalen"])
                    INSTELLINGEN["afspeellijst"] = [
                        veilige_naam(n) for n in INSTELLINGEN["afspeellijst"]
                        if isinstance(n, str)]
                bewaar_instellingen()
                return self.stuur_json({"ok": True,
                                        "instellingen": INSTELLINGEN})
            if pad == "/api/bouw":
                if not start_render():
                    return self.stuur_fout(409, "er wordt al gebouwd")
                return self.stuur_json({"ok": True})
            if pad == "/api/start":
                return self.stuur_json({"ok": True, **start_uitzending()})
            if pad == "/api/stop":
                gestopt = stop_uitzending()
                return self.stuur_json({"ok": True, "gestopt": gestopt})
            if pad == "/api/verwijder":
                naam = veilige_naam((self.lees_json() or {}).get("naam", ""))
                pad_bestand = medi_pad(naam)
                if not os.path.exists(pad_bestand):
                    return self.stuur_fout(404, "bestand niet gevonden")
                if naam in INSTELLINGEN["afspeellijst"]:
                    return self.stuur_fout(409, "bestand staat nog in de "
                                                "afspeellijst — haal het daar eerst uit")
                os.remove(pad_bestand)
                meld(f"verwijderd: {naam}")
                return self.stuur_json({"ok": True})
        except Exception as fout:                          # noqa: BLE001
            return self.stuur_fout(400, str(fout))
        return self.stuur_fout(404, "onbekend pad")

    def doe_upload(self, query: dict) -> None:
        naam = veilige_naam((query.get("naam") or [""])[0])
        if not naam.lower().endswith(MEDIA_EXTENSIES):
            return self.stuur_fout(400, "alleen mediabestanden zijn toegestaan")
        lengte = int(self.headers.get("Content-Length") or 0)
        if lengte <= 0:
            return self.stuur_fout(400, "geen inhoud ontvangen")
        if lengte > CONFIG["max_upload"]:
            return self.stuur_fout(413, "bestand is te groot")
        os.makedirs(CONFIG["media_dir"], exist_ok=True)
        eindpad = medi_pad(naam)
        tijdelijk = eindpad + ".deel"
        over = lengte
        with open(tijdelijk, "wb") as f:
            while over > 0:
                brok = self.rfile.read(min(1024 * 1024, over))
                if not brok:
                    break
                f.write(brok)
                over -= len(brok)
        if over > 0:
            os.remove(tijdelijk)
            return self.stuur_fout(400, "de verbinding viel weg tijdens het uploaden")
        os.replace(tijdelijk, eindpad)
        meld(f"geüpload: {naam} ({lengte / 1e6:.0f} MB)")
        self.stuur_json({"ok": True, "naam": naam})

    # --- staat -------------------------------------------------------------
    def staat(self) -> dict:
        host = self.host_van_verzoek()
        with SLOT:
            instellingen = json.loads(json.dumps(INSTELLINGEN))
            render = dict(S["render"])
            uitzending = {"actief": S["uitzending"]["actief"],
                          "naam": S["uitzending"].get("naam"),
                          "gestart": S["uitzending"].get("gestart"),
                          "snelheid": S["uitzending"].get("snelheid"),
                          "beelden_per_seconde":
                              S["uitzending"].get("beelden_per_seconde"),
                          "achterstand": S["uitzending"].get("achterstand"),
                          "relay": S["uitzending"].get("relay", {})}
        naam = uitzending["naam"] or veilige_naam(instellingen["streamnaam"])
        return {
            "naam": naam,
            "instellingen": instellingen,
            "formaten": streams.BEELDFORMATEN,
            "presets": PRESETS,
            "render": render,
            "uitzending": uitzending,
            "relay": relay_status(naam),
            "paden": relay_sporen("")["paden"],
            "lusbestand": (os.path.exists(werk_pad("lust.mp4")) and {
                "pad": werk_pad("lust.mp4"),
                "bytes": os.path.getsize(werk_pad("lust.mp4")),
                "gewijzigd": int(os.path.getmtime(werk_pad("lust.mp4")))}),
            "media_dir": CONFIG["media_dir"],
            "werk_dir": CONFIG["werk_dir"],
            "adressen": {
                "host": host,
                # live MPEG-TS over HTTP: de link die in VLC meteen werkt
                "ts": f"http://{host}:{CONFIG['http_poort']}/ts/{naam}",
                # momentopname uit de uitzending (het bord ververst die 1×/s)
                "voorbeeld": f"http://{host}:{CONFIG['http_poort']}/beeld/{naam}.jpg",
                "hls": (f"http://{host}:{CONFIG['http_poort']}/hls/{naam}/index.m3u8"),
                # kale SRT-link: VLC laat de query in een SRT-URL vallen, dus
                # zonder streamid; de hub geeft dan de lopende uitzending
                "srt": f"srt://{host}:{CONFIG['relay']['poort']}",
                "srt_met_streamid": (f"srt://{host}:{CONFIG['relay']['poort']}"
                                     f"?streamid=read:{naam}"),
            },
        }


# -------------------------------------------------------------------- main ---


def lees_env(pad: str) -> None:
    if not os.path.exists(pad):
        return
    for regel in open(pad, encoding="utf-8", errors="replace"):
        regel = regel.strip()
        if not regel or regel.startswith("#") or "=" not in regel:
            continue
        sleutel, waarde = regel.split("=", 1)
        waarde = waarde.strip().strip('"').strip("'")
        if sleutel == "BORD_POORT":
            CONFIG["poort"] = int(waarde)
        elif sleutel == "BORD_WACHTWOORD":
            CONFIG["wachtwoord"] = waarde
        elif sleutel == "BORD_GEBRUIKER":
            CONFIG["gebruiker"] = waarde
        elif sleutel == "SRT_MEDIA_DIR":
            CONFIG["media_dir"] = waarde
        elif sleutel == "SRT_WERK_DIR":
            CONFIG["werk_dir"] = waarde
        elif sleutel == "SRT_API":
            CONFIG["api"] = waarde
        elif sleutel == "HLS_PORT":
            CONFIG["hls_poort"] = int(waarde)
        elif sleutel == "WEBRTC_PORT":
            CONFIG["webrtc_poort"] = int(waarde)


def main() -> None:
    p = argparse.ArgumentParser(description="Webbord voor de SRT-teststream")
    p.add_argument("--poort", type=int, default=int(os.environ.get("BORD_POORT", 8095)))
    p.add_argument("--media-dir", default=os.environ.get("SRT_MEDIA_DIR"))
    p.add_argument("--werk-dir", default=os.environ.get("SRT_WERK_DIR"))
    p.add_argument("--wachtwoord", default=os.environ.get("BORD_WACHTWOORD", ""))
    p.add_argument("--env", default=os.environ.get("SRT_UITZENDBORD_ENV",
                                                  "/opt/srt-relay/uitzendbord.env"))
    p.add_argument("--eenmalig-testen", action="store_true",
                   help="opties uit de omgeving testen en stoppen")
    args = p.parse_args()

    # Algemene installatiegegevens eerst, dan de bordinstellingen eroverheen:
    # het laatste bestand dat gelezen wordt wint.
    for pad in [os.environ.get("SRT_RELAY_ENV", "/opt/srt-relay/srt-relay.env")]:
        lees_env(pad)
    gezocht = [args.env, os.path.join(HIER, "uitzendbord.env")]
    gevonden = next((p for p in gezocht if p and os.path.exists(p)), "")
    if gevonden:
        lees_env(gevonden)
    else:
        print(f"let op: geen instellingenbestand gevonden (gezocht: "
              f"{', '.join(p for p in gezocht if p)})", flush=True)
    if args.poort:
        CONFIG["poort"] = args.poort
    if args.media_dir:
        CONFIG["media_dir"] = args.media_dir
    if args.werk_dir:
        CONFIG["werk_dir"] = args.werk_dir
    if args.wachtwoord:
        CONFIG["wachtwoord"] = args.wachtwoord
    CONFIG["relay"] = streams.lees_relay_gegevens(
        [gevonden, os.environ.get("SRT_RELAY_ENV", "/opt/srt-relay/srt-relay.env")],
        ["/opt/srt-relay/mediamtx.yml", os.path.join(HIER, "mediamtx.yml")])
    if os.environ.get("SRT_PUB_HOST"):
        CONFIG["relay"]["host"] = os.environ["SRT_PUB_HOST"]

    os.makedirs(CONFIG["media_dir"], exist_ok=True)
    os.makedirs(CONFIG["werk_dir"], exist_ok=True)
    laad_instellingen()

    if args.eenmalig_testen:
        rustig = {k: v for k, v in CONFIG.items() if k != "wachtwoord"}
        rustig["relay"] = {**CONFIG["relay"],
                           "wachtwoord": "***" if CONFIG["relay"]["wachtwoord"] else ""}
        print(json.dumps({"config": rustig}, ensure_ascii=False, indent=1))
        return

    meld(f"uitzendbord op http://0.0.0.0:{CONFIG['poort']} — "
         f"bibliotheek {CONFIG['media_dir']}")
    if not CONFIG["relay"].get("wachtwoord"):
        meld("geen publiceerwachtwoord gevonden — uitzenden lukt pas als je "
             "SRT_PUB_PASS zet in uitzendbord.env")
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG["poort"]), Bord)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        meld("gestopt")
    finally:
        stop_uitzending()


if __name__ == "__main__":
    main()
