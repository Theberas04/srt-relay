#!/usr/bin/env python3
"""Testbank voor het uitzendbord (dashboard/uitzendbord.py).

Draait zonder relay en zonder netwerk: hij bouwt proefmateriaal met ffmpeg,
stuurt de opdrachtbouwer aan en praat daarna met het bord via HTTP.

    python3 tests/test-dashboard.py

Wat er wordt nagerekend:
  * de filtergraph levert het gevraagde formaat op (25p en 50i, met de juiste
    veldvolgorde en het juiste aantal audiosporen)
  * een afspeellijst van twee bestanden wordt aan elkaar gezet
  * het bord: pagina, instellingen, uploaden (ook met een kwaadaardige naam),
    bouwen, uitzenden, stoppen, verwijderen en het wachtwoord
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
BORD = os.path.join(os.path.dirname(HIER), "dashboard")
sys.path.insert(0, BORD)
import streams  # noqa: E402

GOED = 0
FOUT = 0


def ok(tekst: str) -> None:
    global GOED
    GOED += 1
    print(f"  ok   {tekst}")


def mis(tekst: str) -> None:
    global FOUT
    FOUT += 1
    print(f"  FOUT {tekst}")


def eis(waar: bool, tekst: str) -> None:
    ok(tekst) if waar else mis(tekst)


def ffprobe(pad: str, velden: str) -> list[str]:
    uit = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", velden, "-of", "csv=p=0", pad],
        capture_output=True, text=True)
    return [r for r in uit.stdout.strip().splitlines() if r]


def veld(pad: str, naam: str) -> str:
    """Eén eigenschap van het eerste spoor (ffprobe levert bij één veld één regel)."""
    uit = ffprobe(pad, f"stream={naam}")
    return uit[0] if uit else ""


def maak_proef(pad: str, seconden: int, bron: str = "testsrc2=size=640x360:rate=25") -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", bron, "-t", str(seconden),
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", pad],
        check=True)


def vrij_poort() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def haal(url: str, methode: str = "GET", lichaam=None, wachtwoord: str = ""):
    gegevens = None
    koppen = {}
    if lichaam is not None and isinstance(lichaam, dict):
        gegevens = json.dumps(lichaam).encode()
        koppen["Content-Type"] = "application/json"
    elif isinstance(lichaam, bytes):
        gegevens = lichaam
        koppen["Content-Type"] = "application/octet-stream"
    if wachtwoord:
        # moet erbij zitten vóór het Request-object wordt gemaakt: dat neemt
        # een kopie van de koppen, dus later toevoegen helpt niet
        import base64
        koppen["Authorization"] = "Basic " + base64.b64encode(
            f"tom:{wachtwoord}".encode()).decode()
    verzoek = urllib.request.Request(url, data=gegevens, headers=koppen, method=methode)
    def uit_antwoord(antwoord):
        ruw = antwoord.read()
        try:
            return antwoord.status, json.loads(ruw or b"{}")
        except ValueError:
            # de bordpagina is html, geen json
            return antwoord.status, {"ruw": ruw[:200].decode("utf-8", "replace")}

    try:
        with urllib.request.urlopen(verzoek, timeout=30) as antwoord:
            return uit_antwoord(antwoord)
    except urllib.error.HTTPError as fout:
        return uit_antwoord(fout)


def test_opdrachtbouwer(werk: str) -> None:
    print("\n== opdrachtbouwer (filtergraph en formaat)")

    bron1 = os.path.join(werk, "proef1.mp4")
    bron2 = os.path.join(werk, "proef2.mp4")
    maak_proef(bron1, 3)
    maak_proef(bron2, 2, "testsrc2=size=1280x720:rate=50")

    peiling = streams.peil(bron1)
    eis(peiling["breedte"] == 640 and peiling["hoogte"] == 360,
        f"peil() leest het formaat ({peiling['breedte']}x{peiling['hoogte']})")
    eis(abs(peiling["duur"] - 3) < 0.3, f"peil() leest de lengte ({peiling['duur']} s)")

    def bouw(naam, bronnen, extra):
        uitpad = os.path.join(werk, naam)
        opties = {"breedte": 1280, "hoogte": 720, "fps": 25, "interlaced": False,
                  "titel": "PROEF — BORD", "crawl": "PROEF +++ ", "tijdcode": True,
                  "kanaalvak": True, "bitrate": "2M", "preset": "ultrafast",
                  "kanalen": 8, "slot": 2.0, "duur": 3.0}
        opties.update(extra)
        tonen = os.path.join(werk, f"{naam}.wav")
        streams.maak_tonen(tonen, opties["slot"], opties["kanalen"], duur=opties["duur"])
        return uitpad, streams.bouw_render_opdracht(
            bronnen, opties, uitpad, os.path.join(werk, f"{naam}.filter"), tonen, None)

    # 1. 720p25 met acht kanalen
    uitpad, opdracht = bouw("p25.mp4", [bron1], {})
    uit = subprocess.run(opdracht, capture_output=True, text=True)
    eis(uit.returncode == 0, "renderen van 720p25 lukt")
    if uit.returncode != 0:
        print(uit.stderr[-800:])
    if os.path.exists(uitpad):
        sporen = ffprobe(uitpad, "stream=codec_type")
        eis(sporen.count("video") == 1 and sporen.count("audio") == 4,
            f"720p25 heeft 1 videospoor en 4 audiosporen ({sporen})")
        eis(veld(uitpad, "width") == "1280" and veld(uitpad, "height") == "720",
            f"720p25 is 1280x720 (kreeg {veld(uitpad, 'width')}x{veld(uitpad, 'height')})")
        eis(veld(uitpad, "has_b_frames") == "0",
            f"720p25 heeft geen B-frames (kreeg {veld(uitpad, 'has_b_frames')})")
        eis(veld(uitpad, "field_order") == "progressive",
            f"720p25 is progressief (kreeg {veld(uitpad, 'field_order')})")

    # 2. 720i50: veldsnelheid 50, dus 25 frames met twee velden (tff)
    uitpad, opdracht = bouw("i50.mp4", [bron1],
                            {"interlaced": True, "fps": 50})
    uit = subprocess.run(opdracht, capture_output=True, text=True)
    eis(uit.returncode == 0, "renderen van 720i50 lukt")
    if uit.returncode != 0:
        print(uit.stderr[-800:])
    if os.path.exists(uitpad):
        veld_orde = veld(uitpad, "field_order")
        fps = veld(uitpad, "avg_frame_rate")
        eis(veld_orde == "tt", f"720i50 is interlaced met het bovenste veld eerst (kreeg '{veld_orde}')")
        eis(fps == "25/1", f"720i50 loopt op 25 frames per seconde (kreeg '{fps}')")
        eis(veld(uitpad, "has_b_frames") == "0",
            f"720i50 heeft geen B-frames (kreeg {veld(uitpad, 'has_b_frames')})")

    # 3. twee bestanden achter elkaar
    uitpad, opdracht = bouw("twee.mp4", [bron1, bron2], {"duur": 5.0})
    uit = subprocess.run(opdracht, capture_output=True, text=True)
    eis(uit.returncode == 0, "renderen van een afspeellijst met twee bestanden lukt")
    if uit.returncode != 0:
        print(uit.stderr[-800:])
    if os.path.exists(uitpad):
        lengte = float(ffprobe(uitpad, "format=duration")[0])
        eis(lengte > 4.5, f"de afspeellijst is zo lang als beide bestanden samen ({lengte:.1f} s)")


def test_bord(werk: str) -> None:
    print("\n== het bord zelf (via HTTP)")

    bronnen = os.path.join(werk, "bronnen")
    bordwerk = os.path.join(werk, "bordwerk")
    os.makedirs(bronnen, exist_ok=True)
    os.makedirs(bordwerk, exist_ok=True)
    maak_proef(os.path.join(bronnen, "proef.mp4"), 2)
    maak_proef(os.path.join(bronnen, "proef2.mp4"), 2)

    env = os.path.join(werk, "bord.env")
    with open(env, "w") as f:
        f.write(f"BORD_WACHTWOORD=geheim123\nSRT_MEDIA_DIR={bronnen}\n"
                f"SRT_WERK_DIR={bordwerk}\nSRT_PUB_PASS=nepwachtwoord\n"
                f"SRT_PUB_HOST=127.0.0.1\nSRT_PORT=18890\n")

    poort = vrij_poort()
    logpad = os.path.join(werk, "bord.log")
    logbestand = open(logpad, "w")
    proces = subprocess.Popen(
        [sys.executable, os.path.join(BORD, "uitzendbord.py"), "--env", env,
         "--poort", str(poort)],
        stdout=logbestand, stderr=subprocess.STDOUT, text=True)
    basis = f"http://127.0.0.1:{poort}"
    try:
        klaar = False
        laatste = ""
        for _ in range(30):
            tijd = time.sleep
            tijd(0.5)
            if proces.poll() is not None:
                laatste = f"het bord stopte (code {proces.returncode})"
                break
            try:
                code, _ = haal(f"{basis}/", wachtwoord="geheim123")
                if code == 200:
                    klaar = True
                    break
                laatste = f"HTTP {code}"
            except Exception as fout:                       # noqa: BLE001
                laatste = f"{type(fout).__name__}: {fout}"
        if not klaar:
            mis(f"het bord start niet binnen 15 seconden ({laatste})")
            logbestand.flush()
            print("    --- log van het bord ---")
            with open(logpad) as f:
                for regel in f.read().splitlines()[-12:]:
                    print(f"    {regel}")
            return
        ok("het bord start en antwoordt")

        code, _ = haal(f"{basis}/")
        eis(code == 401, f"zonder wachtwoord krijg je 401 (kreeg {code})")
        code, _ = haal(f"{basis}/api/staat", wachtwoord="verkeerd")
        eis(code == 401, f"met een verkeerd wachtwoord ook 401 (kreeg {code})")

        code, staat = haal(f"{basis}/api/staat", wachtwoord="geheim123")
        eis(code == 200 and "instellingen" in staat, "de staat van het bord komt binnen")
        eis(staat["instellingen"]["streamnaam"] == "testbeeld",
            "de standaard streamnaam staat klaar")

        code, gegevens = haal(f"{basis}/api/bestanden", wachtwoord="geheim123")
        namen = [b["naam"] for b in gegevens.get("bestanden", [])]
        eis(sorted(namen) == ["proef.mp4", "proef2.mp4"],
            f"de bibliotheek leest de mediabestanden ({namen})")
        eis(all(b["duur"] > 1 for b in gegevens["bestanden"]),
            "van elk bestand is de lengte bekend")

        # uploaden: eerst met een naam die buiten de map probeert te komen
        brok = b"\0" * 1000
        code, antwoord = haal(f"{basis}/api/upload?naam=../../boos.mp4", "POST", brok,
                              "geheim123")
        eis(code == 200 and antwoord.get("naam") == "boos.mp4",
            f"een gevaarlijke bestandsnaam wordt onschadelijk gemaakt ({antwoord})")
        eis(not os.path.exists(os.path.join(werk, "boos.mp4")),
            "het bestand is niet buiten de bibliotheek beland")

        code, _ = haal(f"{basis}/api/upload?naam=kwaad.sh", "POST", brok, "geheim123")
        eis(code == 400, f"een verkeerde extensie wordt geweigerd (kreeg {code})")

        code, _ = haal(f"{basis}/api/verwijder", "POST", {"naam": "onbekend.mp4"},
                       "geheim123")
        eis(code == 404, f"een onbekend bestand verwijderen geeft 404 (kreeg {code})")

        # instellingen: acht kanalen, 50i, twee bestanden in de lijst
        code, antwoord = haal(f"{basis}/api/instellingen", "POST",
                              {"afspeellijst": ["proef.mp4", "proef2.mp4"],
                               "formaat": "720", "fps": 50, "interlaced": True,
                               "kanalen": 8, "preset": "ultrafast",
                               "streamnaam": "bordproef"}, "geheim123")
        eis(code == 200 and antwoord["instellingen"]["kanalen"] == 8,
            "de instellingen worden overgenomen (acht kanalen)")

        code, _ = haal(f"{basis}/api/verwijder", "POST", {"naam": "proef.mp4"},
                       "geheim123")
        eis(code == 409, f"een bestand dat in de afspeellijst staat wordt niet verwijderd (kreeg {code})")

        code, _ = haal(f"{basis}/api/bouw", "POST", {}, "geheim123")
        eis(code == 200, "bouwen wordt gestart")

        gebouwd = None
        for _ in range(60):
            time.sleep(1)
            _, staat = haal(f"{basis}/api/staat", wachtwoord="geheim123")
            if staat.get("render", {}).get("klaar"):
                gebouwd = staat["render"]
                break
            if staat.get("render", {}).get("fout"):
                mis(f"bouwen mislukte: {staat['render']['fout']}")
                break
        if gebouwd:
            ok(f"het bord bouwt het testbeeld ({gebouwd['voortgang']}%)")
            # Het bord bouwt naar een nieuw bestand (lus-<tijdstip>.mp4), zodat
            # het bestand dat een lopende uitzending leest nooit overschreven
            # wordt: daar struikelt ffmpeg anders halverwege over.
            lus = gebouwd.get("bestand") or os.path.join(bordwerk, "lust.mp4")
            eis(os.path.basename(lus).startswith("lus-"),
                f"het bouwen schrijft naar een nieuw lusbestand ({os.path.basename(lus)})")
            eis(os.path.exists(lus), "het nieuwe lusbestand staat er")
            sporen = ffprobe(lus, "stream=codec_type")
            eis(sporen.count("audio") == 4,
                f"het lusbestand heeft vier stereosporen ({sporen})")
            eis(ffprobe(lus, "stream=field_order")[0] == "tt",
                "het lusbestand is interlaced (50i)")

        code, _ = haal(f"{basis}/api/start", "POST", {}, "geheim123")
        eis(code == 200, "uitzenden wordt gestart (zonder relay: alleen de poging)")
        time.sleep(3)
        _, staat = haal(f"{basis}/api/staat", wachtwoord="geheim123")
        eis(staat["uitzending"]["actief"] is True, "het bord meldt dat het uitzendt")
        eis("snelheid" in staat["uitzending"] and "achterstand" in staat["uitzending"],
            "het bord meldt de snelheid en de achterstand van de uitzending")
        eis(staat["uitzending"]["naam"] == "bordproef",
            f"de stream heet zoals ingesteld ({staat['uitzending']['naam']})")

        code, _ = haal(f"{basis}/api/stop", "POST", {}, "geheim123")
        eis(code == 200, "stoppen lukt")
        time.sleep(2)
        _, staat = haal(f"{basis}/api/staat", wachtwoord="geheim123")
        eis(staat["uitzending"]["actief"] is False, "het bord meldt dat het gestopt is")

        code, _ = haal(f"{basis}/api/onbekend", wachtwoord="geheim123")
        eis(code == 404, f"een onbekende route geeft 404 (kreeg {code})")

        _, logboek = haal(f"{basis}/api/log?vanaf=0", wachtwoord="geheim123")
        regels = " ".join(r["regel"] for r in logboek.get("regels", []))
        eis("klaar: lus-" in regels, "het log vertelt dat het bouwen klaar is")
    finally:
        proces.terminate()
        try:
            proces.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proces.kill()
        logbestand.close()


def main() -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg en ffprobe zijn nodig voor deze testbank")
        return 1
    werk = tempfile.mkdtemp(prefix="bordtest-")
    try:
        test_opdrachtbouwer(werk)
        test_bord(werk)
    finally:
        shutil.rmtree(werk, ignore_errors=True)
    print(f"\n{GOED} controle(s) geslaagd, {FOUT} mislukt.")
    return 1 if FOUT else 0


if __name__ == "__main__":
    sys.exit(main())
