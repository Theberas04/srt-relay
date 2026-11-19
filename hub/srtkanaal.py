#!/usr/bin/env python3
"""srtkanaal — eigen koppeling met de SRT-protocolbibliotheek (libsrt).

SRT is een UDP-protocol met een eigen verbindingsopbouw, herstel van verloren
pakketten en (optioneel) versleuteling. Dat protocol zelf schrijf je niet even
na: de relay praat met de systeembibliotheek `libsrt` die Debian meelevert (de
zelfde bibliotheek die ffmpeg gebruikt). Alles wat daar omheen zit — luisteren,
uitzend- en leesrechten, de fan-out naar meerdere kijkers, het doorgeven van de
stroom — is hieronder eigen code.

De koppeling gebeurt met ctypes, dus zonder pip-pakketten en zonder compiler.
De gebruikte optienummers komen uit srt.h van versie 1.5; `controleer` proeft ze
bij het starten, zodat een afwijkende bibliotheek meteen opvalt in plaats van
stil verkeerd te werken.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import socket
import struct
import threading

# ---------------------------------------------------------------- constanten --

SRT_INVALID_SOCK = -1
SRTT_LIVE = 0
SRTT_FILE = 1

# SRT_SOCKOPT uit srt.h (1.5.x)
SRTO = {
    "MSS": 0,
    "SNDSYN": 1,
    "RCVSYN": 2,
    "ISN": 3,
    "FC": 4,                 # venster in pakketten
    "UDP_SNDBUF": 8,
    "UDP_RCVBUF": 9,
    "SNDBUF": 5,
    "RCVBUF": 6,
    "LINGER": 7,
    "SNDTIMEO": 13,
    "RCVTIMEO": 14,
    "REUSEADDR": 15,
    "MAXBW": 16,
    "STATE": 17,
    "SENDER": 21,
    "TSBPDMODE": 22,
    "LATENCY": 23,
    "PASSPHRASE": 26,
    "TLPKTDROP": 31,
    "CONNTIMEO": 36,
    "SNDKMSTATE": 40,
    "RCVKMSTATE": 41,
    "RCVLATENCY": 43,
    "PEERLATENCY": 44,
    "STREAMID": 46,
    "PAYLOADSIZE": 49,
    "TRANSTYPE": 50,
}

# SRT_SOCKSTATUS
TOESTANDEN = {
    1: "geopend", 2: "verbinden", 3: "verbonden", 4: "sluiten",
    5: "gesloten", 6: "niet-bestaand",
}

# MPEG-TS gaat het beste in brokken van 7 pakketjes van 188 byte
PAYLOAD_TS = 1316


class SrtFout(RuntimeError):
    """Fout uit libsrt, met de tekst die de bibliotheek er zelf bij geeft."""


class _Lib:
    """Laadt libsrt één keer en legt de functies vast."""

    _slot = threading.Lock()
    _lib = None
    _gestart = False

    @classmethod
    def haal(cls):
        with cls._slot:
            if cls._lib is None:
                cls._lib = cls._laad()
            if not cls._gestart:
                cls._lib.srt_startup()
                cls._gestart = True
            return cls._lib

    @staticmethod
    def _laad():
        kandidaten = []
        gevonden = ctypes.util.find_library("srt-gnutls") or ctypes.util.find_library("srt")
        if gevonden:
            kandidaten.append(gevonden)
        kandidaten += ["libsrt-gnutls.so.1.5", "libsrt.so.1.5", "libsrt.so.1",
                       "/usr/lib/x86_64-linux-gnu/libsrt-gnutls.so.1.5"]
        laatste = None
        for naam in kandidaten:
            try:
                lib = ctypes.CDLL(naam)
            except OSError as fout:
                laatste = fout
                continue
            _lib = lib
            _lib.srt_startup.restype = ctypes.c_int
            _lib.srt_startup.argtypes = []
            _lib.srt_cleanup.restype = None
            _lib.srt_create_socket.restype = ctypes.c_int
            _lib.srt_create_socket.argtypes = []
            _lib.srt_setsockflag.restype = ctypes.c_int
            _lib.srt_setsockflag.argtypes = [ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_int]
            _lib.srt_getsockflag.restype = ctypes.c_int
            _lib.srt_getsockflag.argtypes = [ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
            _lib.srt_bind.restype = ctypes.c_int
            _lib.srt_bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            _lib.srt_listen.restype = ctypes.c_int
            _lib.srt_listen.argtypes = [ctypes.c_int, ctypes.c_int]
            _lib.srt_accept.restype = ctypes.c_int
            _lib.srt_accept.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_int)]
            _lib.srt_recvmsg.restype = ctypes.c_int
            _lib.srt_recvmsg.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            _lib.srt_sendmsg.restype = ctypes.c_int
            _lib.srt_sendmsg.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int]
            _lib.srt_close.restype = ctypes.c_int
            _lib.srt_close.argtypes = [ctypes.c_int]
            _lib.srt_getlasterror_str.restype = ctypes.c_char_p
            _lib.srt_getlasterror_str.argtypes = []
            _lib.srt_getsockstate.restype = ctypes.c_int
            _lib.srt_getsockstate.argtypes = [ctypes.c_int]
            _lib.srt_getversion.restype = ctypes.c_uint32
            _lib.srt_getversion.argtypes = []
            return _lib
        raise SrtFout(f"libsrt niet gevonden ({laatste})")


def _lib():
    return _Lib.haal()


def _laatste_fout(voorvoegsel: str) -> SrtFout:
    tekst = _lib().srt_getlasterror_str()
    return SrtFout(f"{voorvoegsel}: {tekst.decode('utf-8', 'replace') if tekst else 'onbekend'}")


def controleer() -> dict:
    """Proef of de optienummers bij deze libsrt passen."""
    kanaal = SrtKanaal(_lib().srt_create_socket())
    try:
        # TRANSTYPE is alleen te zetten (teruglezen geeft "operation not
        # supported"), STREAMID en LATENCY zijn wel te lezen — die twee
        # bewijzen dat de nummering klopt.
        kanaal.zet("TRANSTYPE", SRTT_LIVE)
        kanaal.zet("STREAMID", "controle:optienummer")
        if kanaal.vraag("STREAMID", "tekst") != "controle:optienummer":
            raise SrtFout("SRTO_STREAMID klopt niet bij deze libsrt")
        kanaal.zet("LATENCY", 120)
        if kanaal.vraag("LATENCY") != 120:
            raise SrtFout("SRTO_LATENCY klopt niet bij deze libsrt")
        kanaal.zet("RCVBUF", 64 * 1024 * 1024)
        if kanaal.vraag("RCVBUF") < 16 * 1024 * 1024:
            raise SrtFout("SRTO_RCVBUF klopt niet bij deze libsrt")
        return {"versie": _versie(), "latency": kanaal.vraag("LATENCY"),
                "rcvbuf": kanaal.vraag("RCVBUF"), "fc": kanaal.vraag("FC")}
    finally:
        kanaal.sluit()


def _versie() -> str:
    """Versienummer van libsrt als major.minor.patch (elk 8 bits van het getal)."""
    try:
        getal = int(_lib().srt_getversion())
        return f"{getal >> 16 & 0xFF}.{getal >> 8 & 0xFF}.{getal & 0xFF}"
    except Exception:                                        # noqa: BLE001
        return "onbekend"


# ------------------------------------------------------------------- kanaal --

class SrtKanaal:
    """Eén SRT-verbinding, of een luisteraar waar verbindingen op binnenkomen."""

    def __init__(self, sock: int, streamid: str = "", peer: str = ""):
        self.sock = sock
        self.streamid = streamid
        self.peer = peer
        self._dicht = False
        self.verstuurd = 0
        self.ontvangen = 0
        self.weggegooid = 0

    # --- opties ------------------------------------------------------------
    def zet(self, naam: str, waarde) -> None:
        nummer = SRTO[naam]
        if isinstance(waarde, str):
            brok = waarde.encode("utf-8")
            klaar = _lib().srt_setsockflag(self.sock, nummer, brok, len(brok))
        elif isinstance(waarde, bool):
            klaar = _lib().srt_setsockflag(self.sock, nummer,
                                           ctypes.byref(ctypes.c_int(int(waarde))),
                                           ctypes.sizeof(ctypes.c_int))
        else:
            klaar = _lib().srt_setsockflag(self.sock, nummer,
                                           ctypes.byref(ctypes.c_int(int(waarde))),
                                           ctypes.sizeof(ctypes.c_int))
        if klaar == -1:
            raise _laatste_fout(f"optie {naam}={waarde!r} afwijzen")

    def vraag(self, naam: str, soort: str = "getal"):
        nummer = SRTO[naam]
        if soort == "tekst":
            ruimte = ctypes.create_string_buffer(512)
            lengte = ctypes.c_int(512)
            if _lib().srt_getsockflag(self.sock, nummer, ruimte, ctypes.byref(lengte)) == -1:
                raise _laatste_fout(f"optie {naam} lezen")
            return ruimte.value.decode("utf-8", "replace")
        waarde = ctypes.c_int(0)
        lengte = ctypes.c_int(ctypes.sizeof(ctypes.c_int))
        if _lib().srt_getsockflag(self.sock, nummer, ctypes.byref(waarde),
                                  ctypes.byref(lengte)) == -1:
            raise _laatste_fout(f"optie {naam} lezen")
        return waarde.value

    @property
    def toestand(self) -> str:
        try:
            return TOESTANDEN.get(_lib().srt_getsockstate(self.sock), "onbekend")
        except Exception:                                    # noqa: BLE001
            return "onbekend"

    # --- opzetten ----------------------------------------------------------
    @classmethod
    def luisteraar(cls, poort: int, latency_ms: int = 200, wachtwoord: str = "",
                   bind: str = "0.0.0.0", backlog: int = 32) -> "SrtKanaal":
        sock = _lib().srt_create_socket()
        if sock == SRT_INVALID_SOCK:
            raise _laatste_fout("socket maken")
        kanaal = cls(sock)
        try:
            kanaal.zet("TRANSTYPE", SRTT_LIVE)
            kanaal.zet("REUSEADDR", True)
            kanaal.zet("LATENCY", latency_ms)
            kanaal.zet("RCVLATENCY", latency_ms)
            kanaal.zet("PEERLATENCY", latency_ms)
            kanaal.zet("PAYLOADSIZE", PAYLOAD_TS)
            # Ruime buffers. Levert een uitzender even een stoot (bijvoorbeeld
            # een encoder die een seconde inhaalt), dan raakt de standaardbuffer
            # vol; SRT zegt dan "No room to store incoming packet" en breekt de
            # verbinding — de uitzending sterft terwijl er niets mis is met het
            # netwerk. Met een groter venster en grotere buffers loopt dat goed.
            kanaal.zet("FC", 256000)                      # venster in pakketten
            kanaal.zet("RCVBUF", 64 * 1024 * 1024)
            kanaal.zet("UDP_RCVBUF", 8 * 1024 * 1024)
            if wachtwoord:
                kanaal.zet("PASSPHRASE", wachtwoord)
            # sockaddr_in: familie in host-orde (dus 02 00 op een little-endian
            # machine), poort in netwerkorde, dan het adres en 8 lege bytes.
            # De familie met "!H" wegschrijven levert "bad parameters" op.
            adres = ctypes.create_string_buffer(16)
            struct.pack_into("=HH4s8x", adres, 0, socket.AF_INET,
                             socket.htons(poort), socket.inet_aton(bind))
            if _lib().srt_bind(sock, adres, 16) == -1:
                raise _laatste_fout(f"binden op poort {poort}")
            if _lib().srt_listen(sock, backlog) == -1:
                raise _laatste_fout("luisteren")
            return kanaal
        except Exception:
            kanaal.sluit()
            raise

    def accepteer(self) -> "SrtKanaal":
        """Wacht op een nieuwe verbinding (blokkeert)."""
        ruimte = ctypes.create_string_buffer(128)
        lengte = ctypes.c_int(128)
        nieuw = _lib().srt_accept(self.sock, ruimte, ctypes.byref(lengte))
        if nieuw == SRT_INVALID_SOCK:
            raise _laatste_fout("verbinding aannemen")
        kanaal = SrtKanaal(nieuw)
        kanaal.streamid = str(kanaal.vraag("STREAMID", "tekst")).strip()
        ruw = bytes(ruimte.raw[:max(0, lengte.value)])
        # de familie staat in host-orde in de struct (net als bij het binden)
        if len(ruw) >= 8 and struct.unpack_from("=H", ruw, 0)[0] == socket.AF_INET:
            ip = socket.inet_ntoa(ruw[4:8])
            poort = struct.unpack_from("!H", ruw, 2)[0]
            kanaal.peer = f"{ip}:{poort}"
        else:
            kanaal.peer = "(onbekend adres)"
        # niet blokkeren bij verzenden: een trage kijker mag het beeld van de
        # anderen niet ophouden; wat niet weg kan, gaat weg
        kanaal.zet("SNDSYN", False)
        kanaal.zet("RCVSYN", True)
        return kanaal

    # --- gegevens ----------------------------------------------------------
    def ontvang(self, max_bytes: int = PAYLOAD_TS) -> bytes:
        """Lees één brok; b'' betekent: de andere kant sloot af of er kwam niets."""
        ruimte = ctypes.create_string_buffer(max_bytes)
        gelezen = _lib().srt_recvmsg(self.sock, ruimte, max_bytes)
        if gelezen == -1:
            tekst = _lib().srt_getlasterror_str()
            tekst = tekst.decode("utf-8", "replace") if tekst else ""
            # een nette afsluiting, een time-out of een weggevallen verbinding is
            # voor de aanroeper hetzelfde: de stroom is klaar
            if any(w in tekst.lower() for w in ("timeout", "broken", "closed",
                                               "eof", "connection", "not connected")):
                return b""
            raise SrtFout(f"ontvangen mislukte: {tekst}")
        self.ontvangen += gelezen
        return ruimte.raw[:gelezen]

    def stuur(self, brok: bytes) -> bool:
        """Stuur een brok; geeft False terug als de kijker te langzaam is."""
        gezonden = _lib().srt_sendmsg(self.sock, brok, len(brok), -1, 1)
        if gezonden == -1:
            self.weggegooid += len(brok)
            return False
        self.verstuurd += gezonden
        return True

    def sluit(self) -> None:
        if self._dicht:
            return
        self._dicht = True
        try:
            _lib().srt_close(self.sock)
        except Exception:                                    # noqa: BLE001
            pass

    def __repr__(self) -> str:
        return (f"<SrtKanaal {self.sock} {self.toestand} "
                f"streamid={self.streamid!r} peer={self.peer}>")
