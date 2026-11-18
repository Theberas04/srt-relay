# SRT-relay via Proxmox

Zet in één commando een **SRT-relay / livestream-hub** neer in een eigen
Proxmox-container: alles wat op je netwerk een stream maakt (OBS, ffmpeg, een
camera, een telefoon) kan er naartoe duwen, en alles wat kan kijken (VLC, OBS,
browser, encoder) kan er weer vanaf halen.

    srt://<container-ip>:8890?streamid=publish:mijnstream
    srt://<container-ip>:8890?streamid=read:mijnstream
    http://<container-ip>:8889/mijnstream/      (kijken in de browser)

![Speler in de browser, live via de relay](docs/img/speler.png)

## Installeren

Op de **Proxmox-host**, als root:

    bash <(curl -fsSL https://raw.githubusercontent.com/Theberas04/srt-relay/main/srt-relay.sh)

Het script vraagt niets, maakt een Debian-container, installeert ffmpeg en
[MediaMTX](https://github.com/bluenviron/mediamtx), zet er een systemd-dienst
in, test of publiceren én terugkijken via SRT echt werkt, en print daarna de
klaar-om-te-gebruiken URL's met een zelfgegenereerd wachtwoord.

Klaar? Dan zie je zoiets:

    == Klaar
      Relay:            192.168.1.50  (container 130, srt-relay)
      SRT-poort:        8890/udp
      Publiceren:       srt://192.168.1.50:8890?streamid=publish:mijnstream:tom:8f2c...
      Kijken:           srt://192.168.1.50:8890?streamid=read:mijnstream
      Browser:          http://192.168.1.50:8889/mijnstream/

Opruimen: `pct destroy 130`.

## Wat je krijgt

| Poort | Protocol | Waarvoor |
|---|---|---|
| 8890 | UDP (SRT) | het relay zelf — publiceren én kijken |
| 8554 | TCP (RTSP) | camera's, VLC |
| 1935 | TCP (RTMP) | OBS |
| 8888 | TCP (HTTP) | kijken als HLS in de browser |
| 8889 | TCP (HTTP) | kijken als WebRTC in de browser (laagste vertraging) |
| 9997 | TCP | status-API, alleen vanaf de container zelf |

## Publiceren

**OBS** — Instellingen → Uitgang → Aangepaste stream:

    Server:  rtmp://192.168.1.50:1935
    Sleutel: mijnstream?user=tom&pass=<wachtwoord>

of als SRT-uitgang:

    srt://192.168.1.50:8890?streamid=publish:mijnstream:tom:<wachtwoord>

**ffmpeg:**

    ffmpeg -re -i input.mp4 -c copy -f mpegts \
      "srt://192.168.1.50:8890?streamid=publish:mijnstream:tom:<wachtwoord>"

**Telefoon** (Larix Broadcaster, SRT Stream):

    srt://192.168.1.50:8890?streamid=publish:telefoon:tom:<wachtwoord>

De naam in de streamid (`mijnstream`) is de naam waaronder de stream te zien
is. Publiceren kan alleen met het wachtwoord; kijken mag iedereen met de link.
Wil je kijken ook achter een wachtwoord, zet dan de `read`-regel in
`/opt/srt-relay/mediamtx.yml` onder de gebruiker in plaats van onder `any`.

## Kijken

    srt://192.168.1.50:8890?streamid=read:mijnstream      # VLC, OBS, ffmpeg
    http://192.168.1.50:8889/mijnstream/                  # WebRTC in de browser
    http://192.168.1.50:8888/mijnstream/                  # HLS in de browser

## Bronnen op je netwerk binnenhalen

Iets dat kan *duwen* (OBS, telefoon) heeft niets nodig. Een bron die alleen
*uitlevert* (camera, bestaande stream) laat je de relay ophalen:

    pct exec <id> -- srt-feed voordeur rtsp://192.168.1.60:554/Streaming/Channels/101
    pct exec <id> -- srt-feed desktop  rtsp://192.168.1.61:8554/desktop --on-demand
    pct exec <id> -- srt-streams                 # wat staat er online
    pct exec <id> -- srt-feed --verwijder voordeur

`--on-demand` verbindt pas als er iemand kijkt (spaart bandbreedte). De relay
herlaadt zijn config automatisch, dus herstarten is nooit nodig.

## Van buitenaf bereikbaar

1. Zet in je router **UDP 8890** door naar het container-IP.
2. Geef je WAN-IP een naam (eigen domein of Dynamic DNS).
3. Gebruik dan `srt://<jouw-domein>:8890?streamid=read:mijnstream`.

Let op: zit je provider achter CGNAT (WAN-IP begint met 100.64–100.127 of je
router laat een ander adres zien dan je publieke IP), dan is een port forward
niet genoeg — dan is een VPN zoals Tailscale of een relay op een VPS de weg.
Controleer het met `ffprobe "srt://<wan-ip>:8890?streamid=read:mijnstream"` op
een telefoon met mobiel internet.

## Opties

Als omgevingsvariabele mee te geven aan de one-liner, bijvoorbeeld:

    CTID=140 SRT_PORT=9000 DISK=5 bash <(curl -fsSL .../srt-relay.sh)

| Variabele | Standaard | Betekenis |
|---|---|---|
| `CTID` | eerste vrije vanaf 130 | container-ID |
| `CTNAME` | `srt-relay` | hostnaam |
| `SRT_PORT` | `8890` | UDP-poort van het relay |
| `DEBIAN` | `12` | gastversie (13 vraagt Proxmox 8.2+) |
| `CORES` / `RAM` / `DISK` | `1` / `512` / `3` | formaat van de container |
| `BRIDGE` | `vmbr0` | netwerkbrug (DHCP) |
| `TEMPLATE_STORAGE` | `local` | opslag voor het template |
| `ROOTFS_STORAGE` | `local-lvm` | opslag voor de schijf |
| `MEDIAMTX_VERSION` | `1.21.4` | versie van MediaMTX in de container |
| `FETCH_ONLY` | `0` | `1` = alleen bestanden ophalen en controleren |

## Op een bestaande machine of container

Zonder Proxmox, op Debian of Ubuntu:

    curl -fsSLO https://raw.githubusercontent.com/Theberas04/srt-relay/main/guest-setup.sh
    sudo bash guest-setup.sh

Dat installeert de relay in `/opt/srt-relay` met een systemd-dienst
`srt-relay` en de hulpmiddelen `srt-feed` en `srt-streams`.

## Beheer

    pct exec <id> -- systemctl status srt-relay
    pct exec <id> -- journalctl -u srt-relay -f
    pct exec <id> -- srt-streams

De volledige config met alle mogelijkheden staat in de container als
`/opt/srt-relay/mediamtx-volledig-voorbeeld.yml`.

## Wat er getest is

* De installer doorloopt in de testbank (`tests/test-installer.sh`) zes
  scenario's met nep-`pct`: alleen ophalen, een gewone installatie, een
  container die niet start (met diagnose), een onbereikbare gast, een bezet
  container-ID en het kiezen van het eerste vrije ID.
* De containerinstallatie is los gedraaid: installeren, dienst starten,
  zelftest (publiceren via SRT met wachtwoord en het beeld terugkijken),
  hulpmiddelen, en een tweede run die het wachtwoord en de streams laat staan.
* Publiceren met een fout wachtwoord wordt geweigerd; publiceren via RTMP
  (OBS-manier) en terugkijken via SRT werkt; een bron uit het netwerk laten
  ophalen werkt.
* Kijken in de browser via WebRTC: 1280x720 H.264 loopt (zie de afbeelding).

## Licentie

Alle rechten voorbehouden. MediaMTX zelf valt onder zijn eigen licentie (MIT);
zie de LICENSE in de container naast de binary.
