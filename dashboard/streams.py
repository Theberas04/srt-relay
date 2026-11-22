#!/usr/bin/env python3
"""streams.py — bouw de ffmpeg-opdrachten voor het uitzendbord.

Eén plek waar de filtergraph, de toontrack en de uitzendopdracht worden gemaakt,
zodat het bord en de losse scripts niet uit elkaar lopen.

Beeldopbouw van het testbeeld (testkaart, balken, titel, tijdcode):

    bron(nen) -> schalen + passend maken -> fps -> concat -> [tinterlace]
              -> logo laten stuiteren -> bovenbalk -> titel -> tijdcode
              -> kanaalvak -> crawl

Geluid: één meerkanaals wav waarin elk kanaal alleen in zijn eigen tijdslot
klinkt, opgesplitst in stereosporen (`pan`), want de AAC-encoder en de resampler
lopen stuk op de kanaallabels die `channelsplit`+`amerge` opleveren.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import wave

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

KANAALTONEN = [440, 554, 659, 880, 1047, 1319, 1760, 2093]   # A4, C#5, E5, A5, ...
SAMPLE_RATE = 48000

# Huisstijl Werdens Media
ACCENT = "0x2ea3f2"
CRAWL_KLEUR = "#5ff0e8"

BEELDFORMATEN = [
    {"id": "bron", "naam": "Zelfde als bron", "breedte": 0, "hoogte": 0},
    {"id": "1080", "naam": "1920x1080 (full hd)", "breedte": 1920, "hoogte": 1080},
    {"id": "720", "naam": "1280x720 (hd)", "breedte": 1280, "hoogte": 720},
    {"id": "576", "naam": "720x576 (sd, pal)", "breedte": 720, "hoogte": 576},
]

MEDIA_EXTENSIES = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".mxf",
                   ".avi", ".mpg", ".mpeg", ".mts", ".flv", ".wmv")


def peil(pad: str) -> dict:
    """Duur, formaat en fps van een mediabestand (via ffprobe)."""
    uit = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", pad],
        capture_output=True, text=True)
    if uit.returncode != 0:
        raise ValueError(f"niet te lezen: {pad}: {uit.stderr.strip()[:200]}")
    gegevens = json.loads(uit.stdout)
    beeld = next((s for s in gegevens.get("streams", [])
                  if s.get("codec_type") == "video"), None)
    geluid = next((s for s in gegevens.get("streams", [])
                   if s.get("codec_type") == "audio"), None)
    if beeld is None:
        raise ValueError(f"geen videospoor in {os.path.basename(pad)}")
    fps = 0.0
    for sleutel in ("avg_frame_rate", "r_frame_rate"):
        ruw = beeld.get(sleutel) or ""
        if "/" in ruw:
            teller, noemer = ruw.split("/", 1)
            if float(noemer or 0) > 0:
                fps = float(teller) / float(noemer)
                break
    duur = float(gegevens.get("format", {}).get("duration") or
                 beeld.get("duration") or 0.0)
    return {
        "bestand": os.path.basename(pad),
        "pad": pad,
        "veldorde": beeld.get("field_order") or "progressive",
        "duur": round(duur, 3),
        "breedte": int(beeld.get("width") or 0),
        "hoogte": int(beeld.get("height") or 0),
        "fps": round(fps, 3),
        "geluid": geluid is not None,
        "bytes": os.path.getsize(pad),
    }


def maak_tonen(pad: str, slot: float, aantal: int, volume: float = 0.35,
               duur: float | None = None) -> float:
    """Meerkanaals wav waarin elk kanaal alleen in zijn eigen tijdslot klinkt."""
    if duur is None:
        duur = slot * aantal
    frames = int(duur * SAMPLE_RATE)
    with wave.open(pad, "wb") as w:
        w.setnchannels(aantal)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        blok = bytearray()
        for i in range(frames):
            t = i / SAMPLE_RATE
            slot_nr = int(t / slot) % aantal
            for k in range(aantal):
                if k == slot_nr:
                    in_slot = t - int(t / slot) * slot
                    fade = min(1.0, in_slot / 0.02, (slot - in_slot) / 0.02)
                    waar = int(volume * fade * 32767 *
                               math.sin(2 * math.pi * KANAALTONEN[k] * t))
                else:
                    waar = 0
                blok += int(waar).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(blok))
    return duur


def _schoon(tekst: str) -> str:
    """Een dubbele punt in drawtext-tekst moet een backslash krijgen."""
    return (tekst or "").replace("\\", "").replace(":", r"\:").replace("'", "")


def bouw_filter(bronnen: list[str], opties: dict, aantal_kanalen: int,
                logo_pad: str | None) -> tuple[str, str, list[str]]:
    """Filtergraph, het laatste videolabel en de audioklabels."""
    breedte = int(opties["breedte"])
    hoogte = int(opties["hoogte"])
    fps = float(opties["fps"])
    aantal_bronnen = len(bronnen)

    # Bij 50i is de gekozen snelheid de VELDsnelheid: wekken op 50 Hz en dan
    # vouwt tinterlace twee opeenvolgende beelden tot één interlaced frame, dus
    # 25 frames per seconde met elk twee velden — dat is pas echt 50i. Alleen de
    # vlag zetten levert 25p in een interlaced jasje op.
    veld_fps = float(opties["fps"])
    if opties.get("interlaced") and veld_fps < 50:
        # Interlaced werkt in velden: met 25 komt de frameteller op 12,5 beelden
        # per seconde uit (elk frame bevat twee velden) en dat is geen 50i maar
        # een schokkerig, versmeerd beeld. Interlaced houdt daarom 50 velden aan.
        veld_fps = 50.0
    tussen_fps = veld_fps
    uit_fps = veld_fps / 2 if opties.get("interlaced") else veld_fps
    opties["uit_fps"] = uit_fps
    k = hoogte / 1080.0

    def px(waarde: float) -> int:
        return max(8, int(round(waarde * k)))

    stap: list[str] = []
    for i in range(aantal_bronnen):
        stap.append(
            f"[{i}:v]scale={breedte}:{hoogte}:force_original_aspect_ratio=decrease,"
            f"pad={breedte}:{hoogte}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={tussen_fps},setsar=1,format=yuv420p[v{i}]")
    if aantal_bronnen == 1:
        stap.append("[v0]null[beeld]")
    else:
        stap.append("".join(f"[v{i}]" for i in range(aantal_bronnen)) +
                    f"concat=n={aantal_bronnen}:v=1:a=0[beeld]")
    vorige = "[beeld]"

    if opties.get("interlaced"):
        # Eerst ontdubbelen (yadif), dan pas velden vlechten. Gaat een bron die
        # al interlaced is rechtstreeks door de vlechter, dan wordt het dubbel
        # interlaced en versmeert de onderste helft van het beeld.
        # setfield=tff in plaats van -top: dat laatste is vervallen in ffmpeg 7
        stap.append(f"{vorige}yadif=deint=interlaced,"
                    "tinterlace=mode=interleave_top:flags=vlpf,"
                    "setfield=tff[beeldi]")
        vorige = "[beeldi]"

    if logo_pad:
        logo_index = aantal_bronnen
        stap.append(f"[{logo_index}:v]scale={px(170)}:-1[logo]")
        stap.append(
            f"{vorige}[logo]overlay="
            "x='(W-w)/2+(W-w)/2*sin(2*PI*t/11)':"
            "y='(H-h)/2+(H-h)/2*cos(2*PI*t/7)':format=auto[metlogo]")
        vorige = "[metlogo]"

    bovenbalk = bool(opties.get("titel") or opties.get("tijdcode") or
                     opties.get("kanaalvak"))
    if bovenbalk:
        stap.append(f"{vorige}drawbox=x=0:y=0:w={breedte}:h={px(96)}:"
                    "color=black@0.72:t=fill[balk]")
        vorige = "[balk]"

    if opties.get("titel"):
        stap.append(f"{vorige}drawtext=fontfile={FONT}:"
                    f"text='{_schoon(opties['titel'])}':"
                    f"x={px(44)}:y={px(30)}:fontsize={px(44)}:fontcolor=white[vb]")
        vorige = "[vb]"

    if tijdcode_modus(opties) == "uitzending":
        stap.append(f"{vorige}drawtext=fontfile={FONT_MONO}:"
                    "text='%{pts\\:hms}':"
                    f"x=w-tw-{px(306)}:y={px(26)}:fontsize={px(40)}:fontcolor=white:"
                    f"box=1:boxcolor=black@0.85:boxborderw={px(14)}[vtijd]")
        vorige = "[vtijd]"

    if opties.get("kanaalvak"):
        buiten = px(268)
        stap.append(f"{vorige}drawbox=x=w-{buiten}:y=0:w={buiten}:h={px(96)}:"
                    "color=black@0.72:t=fill[kbalk]")
        vorige = "[kbalk]"
        bx = breedte - px(252)
        by = px(14)
        bw = px(236)
        bh = px(68)
        stap.append(f"{vorige}drawbox=x={bx}:y={by}:w={bw}:h={bh}:"
                    f"color={ACCENT}@0.95:t=fill[kgroen]")
        vorige = "[kgroen]"
        slot = float(opties["slot"])
        stap.append(f"{vorige}drawtext=fontfile={FONT}:"
                    rf"text='CH %{{eif\:mod(floor(t/{slot})\,{aantal_kanalen})+1\:d}}':"
                    f"x={bx}+{bw}/2-tw/2:y={by}+{bh}/2-th/2:"
                    f"fontsize={px(52)}:fontcolor=white[knr]")
        vorige = "[knr]"

    if opties.get("crawl"):
        crawl = (_schoon(opties["crawl"]) + "   ") * 6
        stap.append(f"{vorige}drawtext=fontfile={FONT}:text='{crawl}':"
                    rf"x='w-mod(t*180\,w+tw)':y=h-{px(92)}:fontsize={px(42)}:"
                    f"fontcolor={CRAWL_KLEUR}:"
                    f"box=1:boxcolor=black@0.8:boxborderw={px(16)}[uit]")
        vorige = "[uit]"

    if vorige == "[beeld]":
        # geen enkele overlay: het beeld is al klaar
        stap.append("[beeld]null[uit]")
        vorige = "[uit]"

    geluid_index = aantal_bronnen + (1 if logo_pad else 0)
    audioklabels = []
    for paar in range(aantal_kanalen // 2):
        label = f"[audio{paar}]"
        stap.append(f"[{geluid_index}:a]pan=stereo|c0=c{paar * 2}"
                    f"|c1=c{paar * 2 + 1}{label}")
        audioklabels.append(label)

    return ";\n".join(stap) + "\n", vorige, audioklabels


TIJDCODE_STANDEN = ("uit", "uitzending", "serverklok")


def tijdcode_modus(opties: dict) -> str:
    """Hoe de tijdcode in beeld komt.

    * "uit"        — geen tijdcode
    * "uitzending" — een teller die met de uitzending meeloopt (00:00:14)
    * "serverklok" — de klok van de server, in de tijdzone van de server
    """
    waarde = opties.get("tijdcode", False)
    if waarde in (False, None, "", "uit", "nee", 0):
        return "uit"
    if str(waarde) in ("serverklok", "server", "klok"):
        return "serverklok"
    return "uitzending"


def schaal_van(hoogte: int):
    """Zelfde maatvoering als in de filtergraph: alles rekent vanaf 1080 hoog."""
    k = hoogte / 1080.0

    def px(waarde: float) -> int:
        return max(8, int(round(waarde * k)))

    return px


def klok_filter(breedte: int, hoogte: int) -> str:
    """Tijdcode die de klok van de server volgt, op de plek van de gewone tijdcode.

    De escapes zijn hier het gevoelige punt: binnen een filterbeschrijving moet
    elke dubbele punt in tekst een backslash krijgen, en die backslash moet op
    zijn beurt weer ontsnapt worden. Deze vorm is met de hand getest — met minder
    backslashes klaagt ffmpeg met "No option name near".
    """
    px = schaal_van(hoogte)
    tekst = r"%{localtime\:%H\\\:%M\\\:%S}"
    return (f"drawtext=fontfile={FONT_MONO}:text='{tekst}'"
            f":x=w-tw-{px(306)}:y={px(26)}:fontsize={px(40)}:fontcolor=white:"
            f"box=1:boxcolor=black@0.85:boxborderw={px(14)}")


def bouw_render_opdracht(bronnen: list[str], opties: dict, uitpad: str,
                         filterpad: str, tonenpad: str,
                         logo_pad: str | None) -> list[str]:
    """ffmpeg-opdracht die één ronde van de afspeellijst naar een lusbestand rendert."""
    duur = float(opties["duur"])
    aantal = int(opties["kanalen"])

    filtergraph, laatste, audioklabels = bouw_filter(bronnen, opties, aantal, logo_pad)
    with open(filterpad, "w") as f:
        f.write(filtergraph)

    fps = float(opties["fps"])
    uit_fps = float(opties.get("uit_fps") or fps)
    gop = max(1, int(round(uit_fps * 2)))

    opdracht = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-nostats", "-progress", "pipe:1"]
    for bron in bronnen:
        opdracht += ["-i", bron]
    if logo_pad:
        opdracht += ["-i", logo_pad]
    opdracht += ["-i", tonenpad]
    opdracht += ["-filter_complex_script", filterpad, "-map", laatste]
    for label in audioklabels:
        opdracht += ["-map", label]
    opdracht += [
        "-t", f"{duur:.3f}",
        "-c:v", "libx264", "-preset", opties["preset"], "-b:v", opties["bitrate"],
        "-profile:v", "high",
        # geen B-frames: MediaMTX weigert die voor WebRTC en dan blijft het
        # beeld in de browser leeg terwijl de stream wel online staat
        "-bf", "0",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        # SPS/PPS voor elke keyframe, anders kan een decoder die later binnenkomt
        # nooit beginnen ("non-existing PPS") en herstelt ook niet bij de volgende
        # weightp=0: x264 kan geen gewogen voorspelling combineren met interlaced
        "-x264-params", "repeat_headers=1:weightp=0",
        "-pix_fmt", "yuv420p",
    ]
    if opties.get("interlaced"):
        # tinterlace + setfield doen het veldwerk; -top is vervallen in ffmpeg 7
        opdracht += ["-flags", "+ilme+ildct", "-r", f"{uit_fps:g}"]
    else:
        opdracht += ["-r", f"{uit_fps:g}"]
    opdracht += [
        "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2",
        "-movflags", "+faststart", "-f", "mp4", uitpad,
    ]
    return opdracht


def bouw_uitzend_opdracht(looppad: str, host: str, poort: int, naam: str,
                          gebruiker: str, wachtwoord: str,
                          opties: dict | None = None) -> list[str]:
    """Zend de lus eindeloos uit naar de relay.

    -map 0:v -map 0:a is niet optioneel: zonder die twee schrijft de
    MPEG-TS-muxer standaard één video- en één audiospoor weg en verdwijnen de
    andere stereosporen stil uit de stream.

    Staat de tijdcode op de klok van de server, dan wordt het beeld bij het
    uitzenden opnieuw gecodeerd met de klok erin: een klok kan niet in een lus
    worden ingebakken (hij zou bij elke herhaling terugspringen) en met `-c copy`
    kan er niets overheen. Dat kost rekenkracht; de andere standen blijven
    werken met `-c copy`.
    """
    doel = (f"srt://{host}:{poort}"
            f"?streamid=publish:{naam}:{gebruiker}:{wachtwoord}")
    opdracht = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
                # machineleesbare voortgang op de uitvoer: daarmee kan het bord
                # zien of deze machine het formaat bijhoudt (speed=) en hoeveel
                # het beeld achterloopt ten opzichte van de klok
                "-progress", "pipe:1", "-nostats",
                "-re", "-stream_loop", "-1", "-i", looppad,
                "-map", "0:v", "-map", "0:a"]
    if opties and tijdcode_modus(opties) == "serverklok":
        breedte = int(opties.get("breedte") or 1920)
        hoogte = int(opties.get("hoogte") or 1080)
        veld_fps = float(opties.get("fps") or 25)
        if opties.get("interlaced") and veld_fps < 50:
            veld_fps = 50.0                 # zie bouw_filter: 25 zou 12,5 worden
        uit_fps = veld_fps / 2 if opties.get("interlaced") else veld_fps
        # keyframe elke twee seconden: zonder deze grens kiest x264 250 beelden
        # (tien seconden) en dan wacht een late kijker tien seconden op beeld en
        # worden de HLS-segmenten tien seconden lang
        gop = max(1, int(round(uit_fps * 2)))
        filter_beeld = klok_filter(breedte, hoogte)
        if opties.get("interlaced"):
            # Ontdubbelen, de klok tekenen en dan pas weer velden vlechten: de
            # lus is immers al interlaced en mag niet nóg een keer door de
            # vlechter (dat gaf de versmering halverwege het beeld).
            filter_beeld = ("yadif=deint=interlaced," + filter_beeld
                            + ",tinterlace=mode=interleave_top:flags=vlpf,"
                              "setfield=tff")
        opdracht += [
            "-vf", filter_beeld,
            "-c:v", "libx264", "-preset", str(opties.get("preset") or "veryfast"),
            "-b:v", str(opties.get("bitrate") or "6M"),
            "-profile:v", "high", "-bf", "0",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-x264-params", "repeat_headers=1:weightp=0",
            "-pix_fmt", "yuv420p",
        ]
        if opties.get("interlaced"):
            # de lus is al interlaced; dit vertelt x264 dat het zo moet blijven
            opdracht += ["-flags", "+ilme+ildct"]
        opdracht += ["-c:a", "copy"]
    else:
        opdracht += ["-c", "copy"]
    opdracht += ["-f", "mpegts", doel]
    return opdracht


def lees_relay_gegevens(env_paden: list[str], yml_paden: list[str]) -> dict:
    """Publiceerhost, poort, gebruiker en wachtwoord van de relay.

    Eerst de env-bestanden die de installatie schrijft, dan als terugval het
    wachtwoord uit de MediaMTX-config (daar staat het in de `pass:`-regel).
    """
    gegevens = {"host": "127.0.0.1", "poort": 8890, "gebruiker": "tom",
                "wachtwoord": ""}
    for pad in env_paden:
        if not pad or not os.path.exists(pad):
            continue
        for regel in open(pad, encoding="utf-8", errors="replace"):
            regel = regel.strip()
            if not regel or regel.startswith("#") or "=" not in regel:
                continue
            sleutel, waarde = regel.split("=", 1)
            sleutel = sleutel.strip()
            waarde = waarde.strip().strip('"').strip("'")
            if sleutel == "SRT_PUB_HOST" and waarde:
                gegevens["host"] = waarde
            elif sleutel == "SRT_PORT" and waarde:
                gegevens["poort"] = int(waarde)
            elif sleutel == "PUB_USER" and waarde:
                gegevens["gebruiker"] = waarde
            elif sleutel == "SRT_PUB_PASS" and waarde:
                gegevens["wachtwoord"] = waarde
    if gegevens["wachtwoord"]:
        return gegevens
    for pad in yml_paden:
        if not pad or not os.path.exists(pad):
            continue
        for regel in open(pad, encoding="utf-8", errors="replace"):
            s = regel.strip()
            if s.startswith("pass:") and "wachtwoord" not in s:
                gegevens["wachtwoord"] = s.split(":", 1)[1].strip()
                break
        if gegevens["wachtwoord"]:
            break
    return gegevens


def noem_formaat(breedte: int, hoogte: int, opties: dict) -> str:
    """Korte aanduiding zoals 1080p50 of 1080i50."""
    fps = int(round(float(opties["fps"])))
    teken = "i" if opties.get("interlaced") else "p"
    return f"{hoogte}{teken}{fps}" if hoogte else f"{breedte}x{hoogte}"
