#!/usr/bin/env python3
"""hub — de eigen stream-relay: SRT in en uit, RTMP in, eigen HLS en eigen TS.

Wat deze dienst doet (allemaal eigen code, met ffmpeg alleen als rekenmotor):

    uitzenders ──SRT──┐
                      ├──►  hub  ──SRT──►  kijkers (speler-apps, ffmpeg)
    uitzenders ──RTMP─┘        ├─ eigen HLS ──►  browsers (http://…/hls/<naam>/)
                               └─ eigen TS  ──►  http://…/ts/<naam> (VLC)

Kenmerken van de opzet:
  * één SRT-poort voor alles: de streamid bepaalt of je publiceert of kijkt, en
    onder welke naam (`publish:<naam>:<gebruiker>:<wachtwoord>`, `read:<naam>`)
  * meerdere kijkers per stream, zonder dat een trage kijker de rest ophoudt
  * knippen van HLS gebeurt op keyframes (zie tssplit.py), zodat elke speler elk
    segment zelfstandig kan beginnen
  * de relay hercodeert niets: wat binnenkomt gaat byte voor byte naar buiten
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import srtkanaal  # noqa: E402
import tssplit  # noqa: E402

HIER = os.path.dirname(os.path.abspath(__file__))
BORD_HTML = os.path.join(HIER, "..", "dashboard", "bord.html")

CONFIG = {
    "srt_poort": 8890,
    "rtmp_poort": 1935,
    "http_poort": 8080,
    "pub_user": "tom",
    "pub_pass": "",
    "read_pass": "",
    "latency": 200,
    "srt_encryptie": "",          # leeg = geen SRT-versleuteling
    "hls_map": "/var/lib/streamhub/hls",
    "segment_seconden": 2.0,
    "segmenten_bewaren": 6,
    "rtmp_naam": "obs",           # onder welke naam een RTMP-uitzending binnenkomt
    "max_kijkers": 50,
}

SLOT = threading.Lock()
LOGSLO = threading.Lock()
LOG: list[dict] = []
LOGNR = [0]


def meld(regel: str) -> None:
    """Schrijf een regel naar het log. Gebruikt een eigen slot, zodat dit ook
    mag worden aangeroepen terwijl SLOT vastgehouden wordt (SLOT is geen
    herintreedbare lock)."""
    with LOGSLO:
        LOGNR[0] += 1
        LOG.append({"n": LOGNR[0], "tijd": time.strftime("%H:%M:%S"), "regel": regel})
        if len(LOG) > 500:
            del LOG[:100]
    print(f"[hub] {regel}", flush=True)


# ------------------------------------------------------------- streamid ------

def ontleed_streamid(sid: str) -> dict:
    """Lees de streamid van SRT: wie vraagt wat, en met welke inloggegevens.

    Twee vormen komen voor:
      * `publish:naam:gebruiker:wachtwoord` of `read:naam`
      * het protocolformaat `#!::m=publish,r=naam,u=gebruiker,s=wachtwoord`
    """
    uit = {"doel": "", "naam": "", "gebruiker": "", "wachtwoord": ""}
    sid = (sid or "").strip()
    if not sid:
        return uit
    if sid.startswith("#!"):
        for deel in sid[2:].split(","):
            if "=" not in deel:
                continue
            sleutel, waarde = deel.split("=", 1)
            if sleutel == "m":
                uit["doel"] = waarde
            elif sleutel == "r":
                uit["naam"] = waarde
            elif sleutel == "u":
                uit["gebruiker"] = waarde
            elif sleutel == "s":
                uit["wachtwoord"] = waarde
        return uit
    delen = sid.split(":")
    if delen:
        uit["doel"] = delen[0]
    if len(delen) > 1:
        uit["naam"] = delen[1]
    if len(delen) > 2:
        uit["gebruiker"] = delen[2]
    if len(delen) > 3:
        uit["wachtwoord"] = ":".join(delen[3:])
    return uit


def veilige_naam(naam: str) -> str:
    schoon = "".join(c for c in (naam or "") if c.isalnum() or c in "-_")
    return schoon[:64]


def naam_van(naam: str) -> str:
    """Schone naam voor mappen en paden."""
    return veilige_naam(naam)


# -------------------------------------------------------------- httpkijker ----

class HttpKijker:
    """Een kijker via HTTP, met eigen wachtrij en eigen verzendthread.

    Bij TCP mag je nooit een brok weggooien: dan zit er een gat in de
    MPEG-TS-stroom en blijft de speler hangen (dit was precies waarom VLC
    vastliep). Daarom schuift elke kijker zijn brokken door een eigen wachtrij
    naar een eigen thread. Loopt de wachtrij vol — de kijker leest veel te
    langzaam — dan gaat die kijker eruit in plaats van de stroom te beschadigen.
    """

    def __init__(self, verbinding: socket.socket, naam: str, wachtrij: int = 300) -> None:
        self.verbinding = verbinding
        self.naam = naam
        self._rij: queue.Queue[bytes] = queue.Queue(maxsize=wachtrij)
        self.gestopt = threading.Event()
        self.verzonden = 0
        self.gemist = 0
        self.bijgesprongen = 0
        self.thread = threading.Thread(target=self._verzend, daemon=True,
                                       name=f"http-{naam}")

    def start(self) -> None:
        self.thread.start()

    def stuur(self, brok: bytes) -> bool:
        """Zet een brok in de wachtrij; onwaar = de kijker houdt het niet bij."""
        try:
            self._rij.put_nowait(brok)
            return True
        except queue.Full:
            self.gemist += 1
            return False

    def spring_naar_live(self, aanloop: bytes) -> None:
        """Haal de opgelopen achterstand weg en begin bij het laatste keyframe.

        Een kijker die niet bijhoudt mag de stroom niet beschadigen (geen gat
        midden in een beeldgroep, daar blijft een speler op hangen), maar ook
        niet eindeloos achterblijven: dan kijkt hij minuten later dan de rest.
        Daarom gooien we zijn wachtrij leeg en zetten hem op het laatste
        keyframe. Hij slaat dus hele beeldgroepen over in plaats van brokken,
        en omdat elk pakket heel blijft, blijft de stroom over TCP heel.
        """
        while True:
            try:
                self._rij.get_nowait()
            except queue.Empty:
                break
        self.bijgesprongen += 1
        try:
            self._rij.put_nowait(aanloop)
        except queue.Full:
            self.sluit()

    def _verzend(self) -> None:
        while not self.gestopt.is_set():
            try:
                brok = self._rij.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.verbinding.sendall(brok)
                self.verzonden += len(brok)
            except OSError:
                self.sluit()
                return

    def sluit(self) -> None:
        if self.gestopt.is_set():
            return
        self.gestopt.set()
        try:
            self.verbinding.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.verbinding.close()
        except OSError:
            pass


# ------------------------------------------------------------------ stroom ----

class Stroom:
    """Eén live stroom: wat er binnenkomt en wie er meekijkt."""

    def __init__(self, naam: str) -> None:
        self.naam = naam
        self.gestart = time.time()
        self.bron = "?"
        self.bron_peer = ""
        self.bron_kanaal: srtkanaal.SrtKanaal | None = None
        self.bytes_in = 0
        self.srt_kijkers: list[srtkanaal.SrtKanaal] = []
        self.http_kijkers: list[HttpKijker] = []
        self.kijker_pogingen = 0
        # Aanloop voor late kijkers: de tabellen plus het laatste keyframe en
        # alles daarna. Zonder dit krijgt een speler losse beelden zonder
        # SPS/PPS ("non-existing PPS") en blijft het beeld zwart tot het
        # volgende keyframe.
        self.aanloop: list[bytes] = []      # pakketten sinds het laatste keyframe
        self.tabellen: list[bytes] = []     # laatste PAT/PMT-pakketten
        self._pes_start = 0                 # waar begon het laatste beeld?
        self.aanloop_vol = False
        self.kijker_gemist = 0
        self.kijker_bijgesprongen = 0
        self.kijker_weggegooid = 0
        self.ts = tssplit.TsKijker()
        self.stroomtijd: float | None = None
        self.beeld_pad: str | None = None
        self.beeld_tijd = 0.0
        # Beeldstroom voor browsers: losse beelden achter elkaar. Mobiele
        # browsers kunnen MPEG-TS of HLS niet zonder hulpbibliotheek afspelen,
        # maar zo'n beeldstroom werkt overal — ook op iOS en Android.
        self.mjpeg_kijkers: list[socket.socket] = []
        self.mjpeg_thread: threading.Thread | None = None
        self.gestopped = False
        self.hls = tssplit.HlsKnipper(
            os.path.join(CONFIG["hls_map"], naam), naam,
            segment_seconden=CONFIG["segment_seconden"],
            bewaar=CONFIG["segmenten_bewaren"])
        self._rest = bytearray()
        # Het ontleden en segmenteren gebeurt in een eigen werkthread: de
        # ontvangstlus moet alleen wegzetten en doorgeven, anders loopt de
        # SRT-buffer vol zodra de uitzender even in een stoot levert.
        self._werk_rij: queue.Queue[bytes] = queue.Queue(maxsize=800)
        self._werk_gestopt = False
        self._werk_thread = threading.Thread(target=self._werk_lus, daemon=True,
                                             name=f"werk-{naam}")
        self._werk_thread.start()

    # --- kijkers -----------------------------------------------------------
    def voeg_srt_kijker_toe(self, kanaal: srtkanaal.SrtKanaal) -> None:
        with SLOT:
            if len(self.srt_kijkers) + len(self.http_kijkers) >= CONFIG["max_kijkers"]:
                self.kijker_weggegooid += 1
                kanaal.sluit()
                meld(f"{self.naam}: kijker geweigerd, maximum bereikt")
                return
            # Geen aanloopje over SRT: SRT levert pakketten op hun tijdstempel
            # (TSBPD) en gooit alles weg dat ouder is dan het latentievenster.
            # Een aanloop van een paar seconden wordt dus juist gedropt en de
            # speler krijgt losse beelden. Een late SRT-kijker wacht netjes op
            # het volgende keyframe; de uitzender zet de SPS/PPS bij elk
            # keyframe (repeat_headers), dus dat duurt hooguit één GOP.
            self.srt_kijkers.append(kanaal)
        meld(f"{self.naam}: SRT-kijker erbij ({kanaal.peer}), "
             f"nu {self.aantal_kijkers}")

    def voeg_http_kijker_toe(self, verbinding: socket.socket) -> HttpKijker | None:
        with SLOT:
            if len(self.srt_kijkers) + len(self.http_kijkers) >= CONFIG["max_kijkers"]:
                self.kijker_weggegooid += 1
                return None
            kijker = HttpKijker(verbinding, self.naam)
            aanloop = self._aanloop_bytes()
            if aanloop:
                kijker.stuur(aanloop)          # begint meteen met de wachtrij
            kijker.start()
            self.http_kijkers.append(kijker)
            aantal = self.aantal_kijkers
        meld(f"{self.naam}: kijker via HTTP erbij, nu {aantal}")
        return kijker

    # --- beeldstroom -------------------------------------------------------
    def klein_beeld(self) -> bytes | None:
        """Een klein JPEG-beeld uit het nieuwste segment (voor de beeldstroom)."""
        map_ = os.path.join(CONFIG["hls_map"], naam_van(self.naam))
        tijdelijk = os.path.join(map_, "klein.jpg.nieuw")
        laatste = "geen segment"
        for nieuwste in self._kandidaten(map_)[:4]:
            if not os.path.exists(nieuwste):
                continue                     # intussen opgeruimd: volgende proberen
            klaar = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 # yadif=deint=interlaced ontdubbelt alleen wat interlaced is:
                 # een telefoon laat een interlaced frame anders zien als een
                 # beeld met strepen halverwege.
                 "-i", nieuwste, "-frames:v", "1",
                 "-vf", "yadif=deint=interlaced,scale=640:-2",
                 "-q:v", "6", "-strict", "unofficial", "-update", "1",
                 "-t", "3", "-f", "image2", tijdelijk],
                capture_output=True, timeout=15)
            if klaar.returncode == 0 and os.path.exists(tijdelijk):
                with open(tijdelijk, "rb") as f:
                    return f.read()
            laatste = (f"code {klaar.returncode}: "
                       + klaar.stderr.decode("utf-8", "replace")[-160:].replace("\n", " "))
        meld(f"{self.naam}: beeld maken mislukte ({laatste})")
        return None

    @staticmethod
    def _af_segment(map_: str) -> str | None:
        """Het nieuwste segment dat niet meer geschreven wordt.

        Op het segment dat nú geschreven wordt moet je niet lezen: ffmpeg blijft
        dan wachten op meer gegevens en loopt in zijn tijdslimiet. Daarom kiezen
        we op tijd (niet op naam — na een nieuwe uitzending begint de nummering
        weer bij 1 en staat er nog een oud segment) en slaan we het segment over
        dat minder dan een seconde geleden is aangeraakt.
        """
        for pad in Stroom._kandidaten(map_):
            return pad
        return None

    @staticmethod
    def _kandidaten(map_: str) -> list[str]:
        """Segmentsleutels van nieuw naar oud, zonder het segment dat nu loopt.

        Het segment dat gekozen wordt kan intussen door de HLS-opruiming
        verdwenen zijn; de aanroeper probeert daarom de volgende uit deze lijst.
        """
        segmenten = sorted(glob.glob(os.path.join(map_, "seg*.ts")),
                           key=os.path.getmtime, reverse=True)
        nu = time.time()
        klaar = [p for p in segmenten if nu - os.path.getmtime(p) > 0.8]
        if not klaar and segmenten:
            klaar = segmenten[-1:]
        return klaar

    def voeg_mjpeg_kijker_toe(self, verbinding: socket.socket) -> None:
        with SLOT:
            self.mjpeg_kijkers.append(verbinding)
        meld(f"{self.naam}: kijker op de beeldstroom erbij "
             f"(nu {len(self.mjpeg_kijkers)})")
        self.start_mjpeg()

    def verwijder_mjpeg_kijker(self, verbinding: socket.socket) -> None:
        with SLOT:
            if verbinding in self.mjpeg_kijkers:
                self.mjpeg_kijkers.remove(verbinding)

    def start_mjpeg(self) -> None:
        with SLOT:
            if self.mjpeg_thread and self.mjpeg_thread.is_alive():
                return
            self.mjpeg_thread = threading.Thread(target=self._mjpeg_lus, daemon=True,
                                                 name=f"beeld-{self.naam}")
        self.mjpeg_thread.start()

    def _mjpeg_lus(self) -> None:
        """Stuur beelden naar elke kijker die erom vraagt, zolang die er is."""
        while True:
            with SLOT:
                kijkers = list(self.mjpeg_kijkers)
                levend = self.bytes_in > 0 and not self.gestopped
            if not kijkers:
                if not levend:
                    return
                time.sleep(0.5)
                continue
            try:
                beeld = self.klein_beeld()
            except Exception as fout:                         # noqa: BLE001
                meld(f"{self.naam}: beeld maken mislukte ({fout})")
                beeld = None
            if not beeld:
                meld(f"{self.naam}: kon geen beeld maken voor de beeldstroom")
            if beeld:
                brok = (b"--raam\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(beeld)).encode() + b"\r\n\r\n" + beeld + b"\r\n")
                for verbinding in kijkers:
                    try:
                        verbinding.sendall(brok)
                    except OSError:
                        self.verwijder_mjpeg_kijker(verbinding)
            time.sleep(0.2)

    def _aanloop_lengte(self) -> int:
        return len(self.aanloop) * 188

    def _aanloop_bytes(self) -> bytes:
        """Het aanloopje voor een nieuwe kijker: tabellen + laatste keyframe.

        De SPS/PPS (beeldinstellingen) staan in dezelfde PES-eenheid als het
        keyframe zelf, maar vaak in een eerder pakket. Daarom begint de aanloop
        bewust bij het begin van die PES-eenheid en niet bij het pakket waarin
        het keyframe gevonden is; anders klaagt de speler over ontbrekende
        beeldinstellingen ("non-existing PPS").
        """
        return b"".join(self.tabellen) + b"".join(self.aanloop)

    def verwijder_http_kijker(self, kijker: HttpKijker) -> None:
        kijker.sluit()
        with SLOT:
            if kijker in self.http_kijkers:
                self.http_kijkers.remove(kijker)
                if kijker.gemist:
                    self.kijker_gemist += kijker.gemist
                if kijker.bijgesprongen:
                    self.kijker_bijgesprongen += kijker.bijgesprongen
                aantal = self.aantal_kijkers
            else:
                aantal = None
        if aantal is not None:
            meld(f"{self.naam}: kijker via HTTP weg "
                 f"({kijker.verzonden / 1e6:.1f} MB verzonden), nu {aantal}")

    @property
    def aantal_kijkers(self) -> int:
        return len(self.srt_kijkers) + len(self.http_kijkers)

    # --- gegevens ----------------------------------------------------------
    def verwerk(self, brok: bytes) -> None:
        """Verdeel binnengekomen MPEG-TS meteen en zet het ontleden in de rij."""
        if not brok:
            return
        self.bytes_in += len(brok)
        # Snel wegzetten: als de werkthread achterloopt mag dat de ontvangst
        # niet ophouden. Loopt de rij vol, dan gooien we het oudste weg — dan
        # verliest de segmenter even een stuk, maar blijft de uitzending lopen.
        try:
            self._werk_rij.put_nowait(brok)
        except queue.Full:
            try:
                self._werk_rij.get_nowait()
                self._werk_rij.put_nowait(brok)
            except (queue.Empty, queue.Full):
                pass

        # meteen doorgeven aan de kijkers (goedkoop: één schrijfactie per kijker)
        with SLOT:
            srt_kijkers = list(self.srt_kijkers)
            http_kijkers = list(self.http_kijkers)
        for kijker in srt_kijkers:
            try:
                kijker.stuur(brok)
            except Exception as fout:                        # noqa: BLE001
                meld(f"{self.naam}: SRT-kijker weg ({fout})")
                with SLOT:
                    if kijker in self.srt_kijkers:
                        self.srt_kijkers.remove(kijker)
                kijker.sluit()
        for kijker in http_kijkers:
            if not kijker.stuur(brok):
                # De kijker houdt het niet bij: laat hem hele beeldgroepen
                # overslaan in plaats van de stroom te beschadigen.
                kijker.spring_naar_live(self._aanloop_bytes())

    def _werk_lus(self) -> None:
        """Ontleedt de stroom: programmatabel, keyframes, segmenten en aanloop."""
        while not self._werk_gestopt:
            try:
                brok = self._werk_rij.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._ontleed(brok)
            except Exception as fout:                         # noqa: BLE001
                meld(f"{self.naam}: ontleden mislukte ({fout})")

    def _ontleed(self, brok: bytes) -> None:
        """Pakket voor pakket verwerken, zodat we op een keyframe kunnen knippen."""
        self._rest += brok
        while len(self._rest) >= tssplit.PAKKET:
            blok = bytes(self._rest[:tssplit.PAKKET])
            if blok[0] != tssplit.SYNC:
                # uit de pas: zoek de volgende sync
                volgende = bytes(self._rest).find(bytes([tssplit.SYNC]))
                if volgende <= 0:
                    self._rest = bytearray()
                    break
                self._rest = bytearray(self._rest[volgende:])
                continue
            self._rest = bytearray(self._rest[tssplit.PAKKET:])
            try:
                pakket = tssplit.TsPakket(blok)
            except ValueError:
                continue
            keyframe, mag_knippen, tabel = self.ts.verwerk(pakket)
            if self.ts.tijd is not None:
                self.stroomtijd = self.ts.tijd
            if tabel:
                self.hls.onthou_tabel(blok)
                if len(self.tabellen) >= 4:
                    self.tabellen.pop(0)
                self.tabellen.append(blok)
            if pakket.begin and pakket.pid in self.ts.programma.beeld:
                self._pes_start = len(self.aanloop)
            if not self.aanloop_vol:
                self.aanloop.append(blok)
                if len(self.aanloop) > 20000:          # ruim 3 MB
                    self.aanloop_vol = True            # beeld te lang: niet opsparen
            if mag_knippen and keyframe:
                if not self.aanloop_vol:
                    # alleen het laatste keyframe bewaren, vanaf het begin van
                    # de bijbehorende PES-eenheid
                    if self._aanloop_lengte() > 4 * 1024 * 1024:
                        self.aanloop = []
                    else:
                        self.aanloop = self.aanloop[self._pes_start:]
                    self._pes_start = 0
                self.hls.markeer_knip(stroomtijd=self.stroomtijd)
            self.hls.voeg_toe(blok)

    def sluit(self) -> None:
        self.gestopped = True
        self._werk_gestopt = True
        if self.bron_kanaal is not None:
            self.bron_kanaal.sluit()
            self.bron_kanaal = None
        self.hls.rond_af()
        with SLOT:
            kijkers = list(self.srt_kijkers)
            self.srt_kijkers.clear()
            for kijker in self.http_kijkers:
                kijker.sluit()
            self.http_kijkers.clear()
        for kijker in kijkers:
            kijker.sluit()

    def gegevens(self) -> dict:
        return {
            "naam": self.naam,
            "online": True,
            "bron": self.bron,
            "peer": self.bron_peer,
            "sinds": round(time.time() - self.gestart, 1),
            "bytes": self.bytes_in,
            "kijkers": self.aantal_kijkers,
            "srt_kijkers": len(self.srt_kijkers),
            "http_kijkers": len(self.http_kijkers),
            "gemist": self.kijker_gemist,
            "bijgesprongen": self.kijker_bijgesprongen,
            "segmenten": self.hls.teller,
            "sporen": self._sporen(),
            # Het aantal échte sporen uit de programmatabel. "_sporen" geeft de
            # codenamen (dus "H264" en "AAC"), en dat is iets anders dan vier
            # audiosets: een bord dat die twee door elkaar haalt, denkt dat er
            # sporen verdwijnen.
            "sporen_aantal": len(self.ts.programma.beeld) + len(self.ts.programma.geluid),
            "beeldsporen": len(self.ts.programma.beeld),
            "geluidsporen": len(self.ts.programma.geluid),
        }

    def _sporen(self) -> list[str]:
        uit = []
        if self.ts.programma.beeld:
            uit.append("H264")
        if self.ts.programma.geluid:
            uit.append("AAC")
        return uit


# -------------------------------------------------------------------- hub -----

class Hub:
    def __init__(self) -> None:
        self.streams: dict[str, Stroom] = {}
        self.luisteraar: srtkanaal.SrtKanaal | None = None
        self.gestopped = threading.Event()
        self.rtmp_proces: subprocess.Popen | None = None

    # --- streams -----------------------------------------------------------
    def pak_stroom(self, naam: str) -> Stroom:
        with SLOT:
            if naam not in self.streams:
                self.streams[naam] = Stroom(naam)
                meld(f"nieuwe stroom '{naam}'")
            return self.streams[naam]

    def heeft_stroom(self, naam: str) -> bool:
        with SLOT:
            return naam in self.streams

    def alle_streams(self) -> list[dict]:
        with SLOT:
            return [s.gegevens() for s in self.streams.values()]

    def sluit_stroom_als(self, naam: str, stroom: "Stroom") -> None:
        """Ruim een stroom op, maar alleen als het nog dezelfde is.

        Zonder deze controle kan een oude uitzender die netjes afsluit de
        uitzending van zijn opvolger uit de lijst halen.
        """
        with SLOT:
            if self.streams.get(naam) is not stroom:
                return
        self.sluit_stroom(naam)

    def sluit_stroom(self, naam: str) -> None:
        with SLOT:
            stroom = self.streams.pop(naam, None)
        if stroom:
            stroom.sluit()
            meld(f"stroom '{naam}' gestopt")

    # --- SRT ---------------------------------------------------------------
    def start_srt(self) -> None:
        self.luisteraar = srtkanaal.SrtKanaal.luisteraar(
            CONFIG["srt_poort"], latency_ms=int(CONFIG["latency"]),
            wachtwoord=CONFIG["srt_encryptie"])
        meld(f"SRT luistert op poort {CONFIG['srt_poort']} "
             f"(libsrt {srtkanaal.controleer()['versie']})")
        threading.Thread(target=self._srt_aannemen, daemon=True,
                         name="srt-aannemen").start()

    def _srt_aannemen(self) -> None:
        luisteraar = self.luisteraar
        if luisteraar is None:
            return
        while not self.gestopped.is_set():
            try:
                kanaal = luisteraar.accepteer()
            except Exception as fout:                        # noqa: BLE001
                if not self.gestopped.is_set():
                    meld(f"aannemen van een SRT-verbinding mislukte: {fout}")
                return
            vraag = ontleed_streamid(kanaal.streamid)
            naam = veilige_naam(vraag["naam"])
            if not naam:
                # Geen streamid: spelers als VLC laten de query in de URL weg
                # (srt://host:8890?streamid=... wordt srt://host:8890), dus wie
                # zonder opgave binnenkomt mag de uitzending zien die loopt.
                naam = self._lopende_naam()
                if not naam:
                    meld("SRT-verbinding zonder streamid en er is nog geen "
                         "uitzending — geweigerd")
                    kanaal.sluit()
                    continue
                meld(f"SRT-verbinding zonder streamid: kijker krijgt '{naam}'")
            if vraag["doel"] == "publish":
                if not self._mag_publiceren(vraag):
                    meld(f"publiceren van '{naam}' geweigerd: verkeerde "
                         f"inloggegevens ({kanaal.peer})")
                    kanaal.sluit()
                    continue
                threading.Thread(target=self._ontvang_van,
                                 args=(kanaal, naam), daemon=True,
                                 name=f"bron-{naam}").start()
            else:
                if not self._mag_kijken(vraag):
                    meld(f"kijken naar '{naam}' geweigerd: verkeerd wachtwoord")
                    kanaal.sluit()
                    continue
                kanaal.zet("SNDSYN", False)
                self.pak_stroom(naam).voeg_srt_kijker_toe(kanaal)

    def _lopende_naam(self) -> str:
        """De naam van de uitzending die loopt (voor kijkers zonder streamid)."""
        with SLOT:
            lopend = [s for s in self.streams.values() if s.bytes_in > 0]
        if not lopend:
            return ""
        # de langst lopende uitzending wint
        return max(lopend, key=lambda s: s.gestart).naam

    def _mag_publiceren(self, vraag: dict) -> bool:
        if not CONFIG["pub_pass"]:
            return True
        return (vraag["gebruiker"] == CONFIG["pub_user"]
                and vraag["wachtwoord"] == CONFIG["pub_pass"])

    def _mag_kijken(self, vraag: dict) -> bool:
        if not CONFIG["read_pass"]:
            return True
        return vraag["wachtwoord"] in (CONFIG["read_pass"], CONFIG["pub_pass"])

    def _ontvang_van(self, kanaal: srtkanaal.SrtKanaal, naam: str) -> None:
        # Er kan maar één bron per naam zijn. Komt er een nieuwe uitzender voor
        # een naam die al loopt, dan neemt die het over: de oude bron en zijn
        # kijkers gaan weg. Zonder dit kan de oude verbinding bij het afsluiten
        # de nieuwe uitzending opruimen (en dan is de stroom onvindbaar terwijl
        # er wel beeld binnenkomt).
        with SLOT:
            oud = self.streams.get(naam)
        if oud is not None and (oud.bytes_in > 0 or oud.bron_kanaal is not None):
            meld(f"'{naam}': een nieuwe uitzender neemt het over")
            self.sluit_stroom(naam)
        stroom = self.pak_stroom(naam)
        stroom.bron = "srt"
        stroom.bron_peer = kanaal.peer
        stroom.bron_kanaal = kanaal
        meld(f"'{naam}': uitzending begonnen vanaf {kanaal.peer}")
        try:
            while not self.gestopped.is_set():
                brok = kanaal.ontvang()
                if not brok:
                    break
                stroom.verwerk(brok)
        except Exception as fout:                            # noqa: BLE001
            meld(f"'{naam}': fout bij ontvangen: {fout}")
        finally:
            kanaal.sluit()
            meld(f"'{naam}': uitzending gestopt "
                 f"({stroom.bytes_in / 1e6:.1f} MB ontvangen)")
            # alleen opruimen als dit nog steeds de stroom van deze uitzender is
            self.sluit_stroom_als(naam, stroom)

    # --- RTMP --------------------------------------------------------------
    def start_rtmp(self) -> None:
        """RTMP-uitzendingen (OBS) opvangen met ffmpeg en op de hub zetten.

        ffmpeg kan als RTMP-server maar één uitzending tegelijk aannemen, dus er
        is één RTMP-poort met één vaste naam (CONFIG['rtmp_naam']). Wie een andere
        naam wil, publiceert via SRT; daar kunnen er wel veel naast elkaar.
        """
        naam = veilige_naam(CONFIG["rtmp_naam"])
        adres = f"rtmp://0.0.0.0:{CONFIG['rtmp_poort']}/live/{naam}"
        opdracht = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-listen", "1", "-i", adres,
            "-map", "0:v", "-map", "0:a", "-c", "copy", "-f", "mpegts",
            f"srt://127.0.0.1:{CONFIG['srt_poort']}"
            f"?streamid=publish:{naam}:{CONFIG['pub_user']}:{CONFIG['pub_pass']}",
        ]

        def loop() -> None:
            while not self.gestopped.is_set():
                self.rtmp_proces = subprocess.Popen(
                    opdracht, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                self.rtmp_proces.wait()
                if self.gestopped.is_set():
                    return
                time.sleep(2)

        threading.Thread(target=loop, daemon=True, name="rtmp").start()
        meld(f"RTMP luistert op poort {CONFIG['rtmp_poort']} "
             f"(OBS: server rtmp://<dit-ip>:{CONFIG['rtmp_poort']}/live, "
             f"sleutel {naam})")

    # --- stoppen -----------------------------------------------------------
    def stop(self) -> None:
        self.gestopped.set()
        if self.rtmp_proces and self.rtmp_proces.poll() is None:
            self.rtmp_proces.terminate()
        with SLOT:
            namen = list(self.streams)
        for naam in namen:
            self.sluit_stroom(naam)
        if self.luisteraar:
            self.luisteraar.sluit()


HUB = Hub()


# ------------------------------------------------------------------- HTTP ----

class HttpKant(BaseHTTPRequestHandler):
    server_version = "streamhub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:       # stilte
        return

    def do_GET(self) -> None:
        deel = urllib.parse.urlsplit(self.path)
        pad = urllib.parse.unquote(deel.path)
        if pad in ("/", "/index.html"):
            return self._bord()
        if pad == "/api/streams" or pad == "/api/hub/streams":
            return self._json({"streams": HUB.alle_streams(),
                               "srt_poort": CONFIG["srt_poort"],
                               "geladen": True})
        if pad.startswith("/api/hub/stroom/"):
            naam = veilige_naam(pad.rsplit("/", 1)[-1])
            with SLOT:
                stroom = HUB.streams.get(naam)
            if stroom is None:
                return self._json({"naam": naam, "online": False}, 200)
            return self._json(stroom.gegevens())
        if pad == "/api/log":
            vanaf = 0
            query = urllib.parse.parse_qs(deel.query)
            if query.get("vanaf"):
                vanaf = int(query["vanaf"][0] or 0)
            with LOGSLO:
                regels = [r for r in LOG if r["n"] > vanaf]
                nummer = LOGNR[0]
            return self._json({"regels": regels, "nummer": nummer})
        if pad.startswith("/hls/"):
            return self._hls(pad)
        if pad.startswith("/ts/"):
            return self._ts(pad)
        if pad.startswith("/beeld/"):
            return self._beeld(pad)
        if pad.startswith("/kijk"):
            return self._kijk(pad)
        if pad.startswith("/beeldstroom/"):
            return self._beeldstroom(pad)
        if pad.startswith("/whep/"):
            return self._json({"fout": "WebRTC komt in de volgende stap"}, 501)
        return self._json({"fout": "onbekend pad"}, 404)

    # --- hulpjes -----------------------------------------------------------
    def _json(self, gegevens, code: int = 200) -> None:
        brok = json.dumps(gegevens, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(brok)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(brok)

    def _bestand(self, pad: str, soort: str) -> None:
        if not os.path.exists(pad):
            return self._json({"fout": "niet gevonden"}, 404)
        with open(pad, "rb") as f:
            brok = f.read()
        self.send_response(200)
        self.send_header("Content-Type", soort)
        self.send_header("Content-Length", str(len(brok)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(brok)

    def _bord(self) -> None:
        pad = os.path.normpath(BORD_HTML)
        if not os.path.exists(pad):
            return self._json({"fout": "bordpagina ontbreekt"}, 500)
        self._bestand(pad, "text/html; charset=utf-8")

    def _hls(self, pad: str) -> None:
        rest = pad[len("/hls/"):]
        delen = rest.split("/")
        if len(delen) < 2:
            return self._json({"fout": "gebruik /hls/<naam>/index.m3u8"}, 404)
        naam = veilige_naam(delen[0])
        bestand = os.path.basename(delen[-1])
        map_ = os.path.join(CONFIG["hls_map"], naam)
        if bestand.endswith(".m3u8"):
            return self._bestand(os.path.join(map_, "index.m3u8"),
                                 "application/vnd.apple.mpegurl")
        if bestand.endswith(".ts"):
            return self._bestand(os.path.join(map_, bestand), "video/mp2t")
        return self._json({"fout": "onbekend bestand"}, 404)

    def _kijk(self, pad: str) -> None:
        """Kijkpagina die op elke telefoon werkt (losse beelden achter elkaar)."""
        rest = pad[len("/kijk"):].lstrip("/")
        naam = veilige_naam(rest.split("/")[0]) or self._eerste_naam()
        if not naam or not HUB.heeft_stroom(naam):
            return self._json({"fout": "geen uitzending om te bekijken"}, 404)
        host = (self.headers.get("Host") or f"127.0.0.1:{CONFIG['http_poort']}").split(":")[0]
        pagina = f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kijken — {naam}</title>
<style>
 :root {{ --bg:#f4f7fb; --kaart:#fff; --lijn:#e3eaf4; --tekst:#0f172a;
          --accent:#2ea3f2; --diep:#1e73be; }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; background:var(--bg); color:var(--tekst);
        font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }}
 .kop {{ background:linear-gradient(90deg,#5ff0e8,#2ea3f2 45%,#1e73be);
        color:#fff; padding:14px 18px }}
 .kop h1 {{ margin:0; font-size:18px; letter-spacing:.02em }}
 .doos {{ max-width:900px; margin:18px auto; padding:0 14px }}
 .kaart {{ background:var(--kaart); border:1px solid var(--lijn);
          border-radius:12px; padding:12px; margin-bottom:14px }}
 img {{ width:100%; height:auto; border-radius:8px; background:#0d1424;
        display:block }}
 code {{ background:#eef4fb; padding:2px 6px; border-radius:6px;
        word-break:break-all; font-size:14px }}
 p {{ margin:8px 0 }}
</style></head>
<body>
<div class="kop"><h1>Werdens Media · live: {naam}</h1></div>
<div class="doos">
  <div class="kaart"><img src="/beeldstroom/{naam}" alt="live beeld"></div>
  <div class="kaart">
    <p><strong>Dit is een levende beeldstroom</strong> (een paar beelden per
       seconde). Zo kan elke telefoon meekijken zonder extra app.</p>
    <p>Vloeiend beeld met minder vertraging? Open in
       <strong>VLC</strong>: <code>http://{host}:{CONFIG['http_poort']}/ts/{naam}</code></p>
    <p>Op iPhone/iPad speelt ook: <code>http://{host}:{CONFIG['http_poort']}/hls/{naam}/index.m3u8</code></p>
    <p>Voor spelers met SRT: <code>srt://{host}:{CONFIG['srt_poort']}</code></p>
  </div>
</div></body></html>"""
        brok = pagina.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(brok)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(brok)

    def _eerste_naam(self) -> str:
        with SLOT:
            lopend = [s for s in HUB.streams.values() if s.bytes_in > 0]
        return lopend[0].naam if lopend else ""

    def _beeldstroom(self, pad: str) -> None:
        """Losse beelden achter elkaar (multipart) — werkt in elke browser."""
        naam = veilige_naam(pad[len("/beeldstroom/"):].split("/")[0])
        with SLOT:
            stroom = HUB.streams.get(naam)
        if stroom is None or stroom.bytes_in == 0:
            return self._json({"fout": f"stroom '{naam}' is niet in de lucht"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=raam")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        stroom.voeg_mjpeg_kijker_toe(self.connection)
        try:
            while not stroom.gestopped and HUB.heeft_stroom(naam):
                time.sleep(0.5)
                if self.connection.fileno() < 0:
                    break
        finally:
            stroom.verwijder_mjpeg_kijker(self.connection)

    def _beeld(self, pad: str) -> None:
        """Eén beeld uit de uitzending als JPEG (voor het bord, 1×/s vernieuwd).

        De browsers kunnen MPEG-TS of HLS niet zonder hulpbibliotheek afspelen;
        tot de WebRTC-route er is, laat het bord zo toch zien wat er in de lucht
        is. ffmpeg doet het rekenwerk, het opvragen en bewaren is eigen code.
        """
        naam = veilige_naam(pad[len("/beeld/"):].split("/")[0].split(".")[0])
        with SLOT:
            stroom = HUB.streams.get(naam)
        if stroom is None or stroom.bytes_in == 0:
            return self._json({"fout": f"stroom '{naam}' is niet in de lucht"}, 404)
        nu = time.monotonic()
        with SLOT:
            vers = stroom.beeld_tijd and (nu - stroom.beeld_tijd) < 2.0
            bestaand = stroom.beeld_pad
        if not vers:
            # Beeld uit het nieuwste segment halen: dat staat al op schijf en is
            # hooguit één segment oud. Een momentopname van de *live* stroom
            # kost ffmpeg seconden aan opstarten en is dan vaak half gedecodeerd;
            # dit duurt twee tienden van een seconde.
            map_ = os.path.join(CONFIG["hls_map"], naam)
            kandidaten = [p for p in Stroom._kandidaten(map_) if os.path.exists(p)]
            nieuwste = kandidaten[0] if kandidaten else None
            if nieuwste is None:
                return self._json({"fout": "nog geen beeld beschikbaar"}, 503)
            nieuw = os.path.join(map_, "beeld.jpg")
            tijdelijk = nieuw + ".nieuw"
            opdracht = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", nieuwste, "-frames:v", "1",
                        "-vf", "yadif=deint=interlaced", "-q:v", "4",
                        "-strict", "unofficial", "-update", "1",
                        "-f", "image2", tijdelijk]
            try:
                klaar = subprocess.run(opdracht, timeout=15, capture_output=True)
            except subprocess.TimeoutExpired:
                return self._json({"fout": "geen beeld binnen 15 s"}, 504)
            if klaar.returncode != 0 or not os.path.exists(tijdelijk):
                return self._json({"fout": "geen beeld uit de uitzending"}, 503)
            os.replace(tijdelijk, nieuw)
            with SLOT:
                stroom.beeld_pad = nieuw
                stroom.beeld_tijd = time.monotonic()
            bestaand = nieuw
        return self._bestand(bestaand, "image/jpeg")

    def _ts(self, pad: str) -> None:
        """Live MPEG-TS naar een speler sturen (VLC, ffmpeg, tv-headends)."""
        naam = veilige_naam(pad[len("/ts/"):].split("/")[0])
        with SLOT:
            stroom = HUB.streams.get(naam)
        if stroom is None:
            return self._json({"fout": f"stroom '{naam}' is niet in de lucht"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        kijker = stroom.voeg_http_kijker_toe(self.connection)
        if kijker is None:
            try:
                self.connection.close()
            except OSError:
                pass
            return
        try:
            # wachten tot de kijker weggaat of de uitzending stopt
            while not kijker.gestopt.is_set():
                if not HUB.heeft_stroom(naam):
                    break
                time.sleep(0.5)
        finally:
            stroom.verwijder_http_kijker(kijker)


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
        omzetting = {"SRT_PORT": ("srt_poort", int), "RTMP_PORT": ("rtmp_poort", int),
                     "HTTP_PORT": ("http_poort", int), "PUB_USER": ("pub_user", str),
                     "PUB_PASS": ("pub_pass", str), "READ_PASS": ("read_pass", str),
                     "SRT_LATENCY": ("latency", int),
                     "SRT_ENCRYPTIE": ("srt_encryptie", str),
                     "HLS_DIR": ("hls_map", str),
                     "HLS_SEGMENT": ("segment_seconden", float),
                     "HLS_KEEP": ("segmenten_bewaren", int),
                     "RTMP_NAAM": ("rtmp_naam", str),
                     "MAX_KIJKERS": ("max_kijkers", int)}
        if sleutel in omzetting:
            naam, soort = omzetting[sleutel]
            try:
                CONFIG[naam] = soort(waarde)
            except ValueError:
                meld(f"instelling {sleutel}={waarde!r} snap ik niet — overgeslagen")


def main() -> None:
    p = argparse.ArgumentParser(description="Eigen SRT/HLS-streamrelay")
    p.add_argument("--env", default=os.environ.get("HUB_ENV", "/opt/srt-relay/hub.env"))
    p.add_argument("--srt-poort", type=int)
    p.add_argument("--http-poort", type=int)
    antwoorden = p.parse_args()

    lees_env(antwoorden.env)
    if antwoorden.srt_poort:
        CONFIG["srt_poort"] = antwoorden.srt_poort
    if antwoorden.http_poort:
        CONFIG["http_poort"] = antwoorden.http_poort
    os.makedirs(CONFIG["hls_map"], exist_ok=True)

    HUB.start_srt()
    HUB.start_rtmp()
    http = ThreadingHTTPServer(("0.0.0.0", CONFIG["http_poort"]), HttpKant)
    http.daemon_threads = True
    meld(f"HTTP (bord, HLS, TS en API) op poort {CONFIG['http_poort']}")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        meld("gestopt")
    finally:
        HUB.stop()


if __name__ == "__main__":
    main()
