#!/usr/bin/env python3
"""tssplit — eigen MPEG-TS-ontleder en HLS-segmenter.

De relay krijgt beeld binnen als MPEG-TS (over SRT of RTMP). Om daar HLS van te
maken moet je de pakketjes zelf lezen: waar begint een nieuw beeld, waar zit de
programmatabel, en hoe knip je netjes op een keyframe.

Deze module doet dat zonder hulpmiddelen van buiten:
  * TsPakket    — leest de 188-byte pakketjes, met de programma- en PID-tabel
  * TsKijker    — ontleedt een stroom pakketjes en vertelt waar de keyframes zitten
  * HlsKnipper  — schrijft segmenten op keyframes en houdt de m3u8 bij

Werking van het knippen: een segment begint bij een PAT/PMT en een keyframe, en
eindigt bij het volgende keyframe (of bij de maximumlengte). Zo kan elke speler
elk segment los beginnen — dat is de eis die HLS stelt.
"""

from __future__ import annotations

import glob
import os
import time

PAKKET = 188
SYNC = 0x47

# PID's met een vaste betekenis in MPEG-TS
PID_PAT = 0x0000
PID_NUL = 0x1FFF


class TsPakket:
    """Eén MPEG-TS-pakket van 188 bytes."""

    __slots__ = ("ruw", "pid", "begin", "vervolg", "scramble", "adaptatie", "nuttig")

    def __init__(self, ruw: bytes):
        if len(ruw) != PAKKET or ruw[0] != SYNC:
            raise ValueError("geen MPEG-TS-pakket")
        self.ruw = ruw
        self.pid = ((ruw[1] & 0x1F) << 8) | ruw[2]
        self.begin = bool(ruw[1] & 0x40)          # payload_unit_start_indicator
        self.vervolg = bool(ruw[3] & 0x10)
        self.scramble = (ruw[3] >> 6) & 0x03
        aanpassing = (ruw[3] >> 4) & 0x03
        inhoud = 4
        if aanpassing == 2:
            self.adaptatie = ruw[4:4 + ruw[4]] if ruw[4] else b""
            inhoud = 4 + 1 + (ruw[4] if ruw[4] else 0)
        elif aanpassing == 3:
            lengte = ruw[4]
            self.adaptatie = ruw[4:5 + lengte]
            inhoud = 5 + lengte
        else:
            self.adaptatie = b""
        self.nuttig = ruw[inhoud:] if inhoud < PAKKET else b""

    @property
    def heeft_payload(self) -> bool:
        return len(self.nuttig) > 0

    @property
    def pcr(self) -> int | None:
        """Program Clock Reference in 27 MHz-tikken, of None."""
        a = self.adaptatie
        if len(a) >= 7 and (a[0] & 0x10):
            basis = (a[1] << 25) | (a[2] << 17) | (a[3] << 9) | (a[4] << 1) | (a[5] >> 7)
            return basis * 300 + ((a[5] & 0x01) << 8) + a[6]
        return None


class Programma:
    """Welke PID's horen bij dit programma: beeld, geluid en de klok."""

    def __init__(self) -> None:
        self.pmt_pid: int | None = None
        self.pcr_pid: int | None = None
        self.beeld: list[int] = []
        self.geluid: list[int] = []

    @property
    def bekend(self) -> bool:
        return bool(self.beeld or self.geluid)


def lees_pat(ruw: bytes) -> dict[int, int]:
    """Programma Association Table: programmanummer -> PID van de PMT.

    Let op de indeling van de sectie: byte 0 is het `pointer_field` (meestal 0),
    byte 1 is het table_id, daarna de sectielengte. De tabellen zelf beginnen dus
    bij byte 9, en de laatste vier bytes zijn het controlegetal. Wie hier één byte
    naast zit, vindt nooit een PMT — en dan blijft de hele stroom onbekend.
    """
    if len(ruw) < 12 or ruw[1] != 0x00:
        return {}
    lengte = ((ruw[2] & 0x0F) << 8) | ruw[3]
    # De sectie loopt van byte 4 tot en met de CRC; de CRC is de laatste 4 bytes.
    # De programma's staan vanaf byte 9 tot het einde van de sectie min de CRC.
    tabel = ruw[9:lengte]
    uit: dict[int, int] = {}
    for i in range(0, len(tabel) - 3, 4):
        nummer = (tabel[i] << 8) | tabel[i + 1]
        pid = ((tabel[i + 2] & 0x1F) << 8) | tabel[i + 3]
        if nummer:
            uit[nummer] = pid
    return uit


def lees_pmt(ruw: bytes) -> tuple[int, list[tuple[int, int]]]:
    """Program Map Table: (PCR-PID, lijst van (stroomtype, PID))."""
    if len(ruw) < 16 or ruw[1] != 0x02:
        return 0, []
    lengte = ((ruw[2] & 0x0F) << 8) | ruw[3]
    pcr_pid = ((ruw[9] & 0x1F) << 8) | ruw[10]
    info_lengte = ((ruw[11] & 0x0F) << 8) | ruw[12]
    p = 13 + info_lengte
    eind = lengte                       # sectie-einde, CRC niet meegerekend
    uit: list[tuple[int, int]] = []
    while p + 4 < eind:
        soort = ruw[p]
        pid = ((ruw[p + 1] & 0x1F) << 8) | ruw[p + 2]
        info = ((ruw[p + 3] & 0x0F) << 8) | ruw[p + 4]
        uit.append((soort, pid))
        p += 5 + info
    return pcr_pid, uit


class TsKijker:
    """Houdt bij waar de keyframes zitten en waar een segment mag beginnen."""

    def __init__(self) -> None:
        self.programma = Programma()
        self._pes: dict[int, bytearray] = {}
        self._pat_gezien = False
        # Klok van de stroom in seconden: bij voorkeur uit de presentatietijd
        # (PTS) van het beeld, anders uit de programmaklok (PCR).
        self.tijd: float | None = None

    def verwerk(self, pakket: TsPakket) -> tuple[bool, bool, bool]:
        """Geef (keyframe, mag_knippen, tabel) terug voor dit pakket.

        keyframe      : hier begint een nieuw beeld dat zelfstandig te tonen is
        mag_knippen   : dit is een goed moment om een nieuw segment te beginnen
        tabel         : dit pakket is een programma- of PMT-pakket; de segmenter
                        moet het onthouden om elk segment mee te laten beginnen
        """
        if pakket.scramble:
            return False, False, False

        klok = pakket.pcr
        if klok is not None:
            self.tijd = klok / 90000.0            # 27 MHz / 300 = 90 kHz

        if pakket.pid == PID_PAT:
            if pakket.begin and pakket.heeft_payload:
                tabel = lees_pat(pakket.nuttig)
                if tabel:
                    nieuw = next(iter(tabel.values()))
                    if nieuw != self.programma.pmt_pid:
                        self.programma = Programma()
                        self.programma.pmt_pid = nieuw
                    self._pat_gezien = True
            return False, False, True

        if pakket.pid == self.programma.pmt_pid:
            if pakket.begin and pakket.heeft_payload:
                pcr_pid, sporen = lees_pmt(pakket.nuttig)
                if sporen:
                    self.programma.pcr_pid = pcr_pid
                    self.programma.beeld = [p for s, p in sporen if s in (0x01, 0x02, 0x1B)]
                    self.programma.geluid = [p for s, p in sporen if s in (0x03, 0x04, 0x0F, 0x11)]
            return False, False, True

        if pakket.pid in self.programma.beeld:
            keyframe = self._ontleed_beeld(pakket)
            # Zodra de tabellen bekend zijn is een keyframe genoeg om te knippen;
            # de tabellen zelf gaan mee aan het begin van elk segment.
            return keyframe, keyframe, False

        if pakket.pid in self.programma.geluid:
            return False, False, False

        return False, False, False

    @staticmethod
    def _pes_tijd(ruw: bytes) -> float | None:
        """Presentatietijd (PTS) uit de PES-kop, in seconden."""
        if len(ruw) < 14 or ruw[0] or ruw[1] or ruw[2] != 1:
            return None
        vlaggen = ruw[7] >> 6
        if vlaggen not in (2, 3):                 # geen PTS aanwezig
            return None
        b = ruw[9:14]
        tikken = (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) \
            | (((b[2] >> 1) & 0x7F) << 15) | (b[3] << 7) | ((b[4] >> 1) & 0x7F)
        return tikken / 90000.0

    def _ontleed_beeld(self, pakket: TsPakket) -> bool:
        """Zoek in de PES-payload naar een IDR-beeld (NAL-type 5)."""
        brok = self._pes.setdefault(pakket.pid, bytearray())
        if pakket.begin:
            brok.clear()
            # PES-kop overslaan
            if len(pakket.nuttig) >= 9 and pakket.nuttig[0] == 0 and pakket.nuttig[1] == 0 \
                    and pakket.nuttig[2] == 1:
                tijd = self._pes_tijd(pakket.nuttig)
                if tijd is not None:
                    self.tijd = tijd
                kop = 9 + pakket.nuttig[8]
                brok += pakket.nuttig[kop:]
            else:
                brok += pakket.nuttig
        else:
            brok += pakket.nuttig
        gevonden = self._zoek_idr(bytes(brok))
        if gevonden:
            brok.clear()
        return gevonden

    @staticmethod
    def _zoek_idr(ruw: bytes) -> bool:
        """IDR-slice in een H.264 NAL-stroom (Annex-B) — dat is een keyframe."""
        i = 0
        eind = len(ruw)
        while i < eind - 3:
            if ruw[i] == 0 and ruw[i + 1] == 0 and ruw[i + 2] in (0, 1):
                begin = i + 3 if ruw[i + 2] == 1 else i + 4
                if begin < eind:
                    soort = ruw[begin] & 0x1F
                    if soort == 5:                         # IDR-beeld
                        return True
                    if soort == 1:                         # gewoon beeld: geen keyframe
                        return False
                    if soort in (6, 7, 8, 9):              # SEI, SPS, PPS, scheiding
                        pass                               # verder zoeken
                    else:
                        return False
                i = begin
            else:
                i += 1
        return False


class HlsKnipper:
    """Schrijft MPEG-TS-segmenten en houdt de afspeellijst bij.

    Knippen gebeurt op een keyframe: de hub meldt met `markeer_knip()` waar in de
    buffer een nieuw segment mag beginnen (namelijk bij een keyframe, en pas
    nadat de programmatabel langs is gekomen). Zo kan elke speler elk segment
    los beginnen — dat is de eis die HLS stelt.
    """

    def __init__(self, map_: str, naam: str, segment_seconden: float = 2.0,
                 bewaar: int = 6) -> None:
        self.map = map_
        self.naam = naam
        self.segment_seconden = segment_seconden
        self.bewaar = bewaar
        self.teller = 0
        self.reeds_geschreven: list[tuple[str, float]] = []
        self._buffer = bytearray()
        self._knip_punt: int | None = None
        self._knip_tijd: float | None = None      # streamtijd (PCR) bij de knip
        self._knip_wand = time.monotonic()        # terugvaloptie: wandkloktijd
        self.tabel_pakketten: list[bytes] = []
        os.makedirs(map_, exist_ok=True)
        # oude segmenten en beelden van een vorige uitzending opruimen: anders
        # pakt een kijker (of het bord) een beeld uit de vorige uitzending
        for oud in glob.glob(os.path.join(map_, "seg*.ts")) + \
                glob.glob(os.path.join(map_, "beeld.jpg")):
            try:
                os.remove(oud)
            except OSError:
                pass
        self._schrijf_index()

    @property
    def index_pad(self) -> str:
        return os.path.join(self.map, "index.m3u8")

    def onthou_tabel(self, ruw: bytes) -> None:
        """Onthoud een programma- of PMT-pakket voor het begin van elk segment.

        Zonder de programmatabel vooraan kan een speler een segment niet zelf
        beginnen: hij weet dan niet welke PID's bij het programma horen.
        """
        if len(self.tabel_pakketten) >= 4:
            self.tabel_pakketten.pop(0)
        self.tabel_pakketten.append(ruw)

    def voeg_toe(self, ruw: bytes) -> None:
        """Voeg één MPEG-TS-pakket toe aan de buffer."""
        self._buffer += ruw
        if self._knip_punt is None and len(self._buffer) > 8 * 1024 * 1024:
            # Vangnet: komt er nooit een keyframe (kapotte bron), dan mag de
            # buffer niet onbeperkt groeien.
            self._knip_punt = 0
            self._knip_tijd = None
            self._knip_wand = time.monotonic()

    def markeer_knip(self, stroomtijd: float | None = None) -> str | None:
        """Een keyframe: hier mag een nieuw segment beginnen.

        Er wordt geknipt op de klok van de stroom zelf (de PCR, in seconden) en
        niet op de wandklok: bij het lezen van een bestand loopt de wandklok niet
        mee en zou er nooit een segment volgen.
        """
        punt = len(self._buffer)
        if self._knip_punt is None:
            self._knip_punt = punt
            self._knip_tijd = stroomtijd
            self._knip_wand = time.monotonic()
            return None
        if stroomtijd is not None and self._knip_tijd is not None:
            verstreken = stroomtijd - self._knip_tijd
        else:
            verstreken = time.monotonic() - self._knip_wand
        if verstreken < -1.0:
            # De bron begint opnieuw (een lusbestand dat met -c copy wordt
            # uitgezonden springt terug in de tijd). Dan meteen knippen, anders
            # loopt het segment door tot de tijd ooit weer voorbij het oude punt
            # is — en dat kan minuten duren.
            verstreken = self.segment_seconden
        # Kleine marge: een keyframe dat een fractie te vroeg komt mag niet
        # betekenen dat we een hele segmentduur overslaan.
        if verstreken < self.segment_seconden * 0.9:
            return None
        # Het segment loopt van het begin van de buffer tot dit keyframe; het
        # volgende segment begint precies bij dit keyframe (dus op 0 in de rest).
        begin = bytes(self._buffer[:punt])
        self._buffer = bytearray(self._buffer[punt:])
        self._knip_punt = 0
        self._knip_tijd = stroomtijd
        self._knip_wand = time.monotonic()
        return self._bewaar(begin, round(min(verstreken, 30.0), 2))

    def rond_af(self) -> str | None:
        """Slotsegment schrijven (als de bron stopt)."""
        if not self._buffer:
            return None
        brok = bytes(self._buffer)
        self._buffer = bytearray()
        self._knip_punt = None
        duur = time.monotonic() - self._knip_wand
        return self._bewaar(brok, round(min(max(duur, 0.1), 30.0), 2))

    def _bewaar(self, inhoud: bytes, duur: float) -> str:
        self.teller += 1
        if self.tabel_pakketten:
            inhoud = b"".join(self.tabel_pakketten) + inhoud
        naam = f"seg{self.teller:05d}.ts"
        pad = os.path.join(self.map, naam)
        with open(pad, "wb") as f:
            f.write(inhoud)
        self._laatste_knip = time.monotonic()
        self.reeds_geschreven.append((naam, round(duur, 2)))
        while len(self.reeds_geschreven) > self.bewaar:
            oud, _ = self.reeds_geschreven.pop(0)
            try:
                os.remove(os.path.join(self.map, oud))
            except OSError:
                pass
        self._schrijf_index()
        return pad

    def _schrijf_index(self) -> None:
        regels = ["#EXTM3U", "#EXT-X-VERSION:3",
                  f"#EXT-X-TARGETDURATION:{int(self.segment_seconden) + 1}",
                  f"#EXT-X-MEDIA-SEQUENCE:"
                  f"{max(0, self.teller - len(self.reeds_geschreven))}"]
        for naam, duur in self.reeds_geschreven:
            regels.append(f"#EXTINF:{duur:.2f},")
            regels.append(naam)
        tijdelijk = self.index_pad + ".nieuw"
        with open(tijdelijk, "w") as f:
            f.write("\n".join(regels) + "\n")
        os.replace(tijdelijk, self.index_pad)
