# Addons für Strom & Wärme

Addons sind Module für die Home-Assistant-Integration [Strom & Wärme](https://github.com/Maddog3010/ha-fenster-heizung), die du direkt in der Integration unter **Einstellungen → Addons** installierst, aktualisierst und wieder entfernst. Dieses Repository ist der **Katalog** dafür.

| Modul | Addon | Version | Stufe |
|---|---|---|---|
| 21 | **Saugroboter** – Räume, Karte, Status und Wartung deiner Saugroboter, zuerst für Roborock | 0.1.1 Build 1 | In Entwicklung |

## So kommen Addons zu dir

1. Die Integration lädt einmal am Tag `catalog.json` und `catalog.sig` aus diesem Repository.
2. Sie prüft die **Unterschrift** (Ed25519, Schlüssel von Maddog3010). Ohne gültige Unterschrift wird nichts angeboten.
3. Beim Installieren oder Aktualisieren lädt sie die ZIP-Datei aus dem passenden GitHub-Release, prüft **Größe und SHA-256** und entpackt sie nach `config/strom_waerme_addons/<id>/`. Dieser Ordner übersteht HACS-Updates.
4. Die bisherige Version bleibt als `<id>.old` liegen. Startet die neue nicht, wird automatisch zurückgewechselt. Unter Einstellungen → Addons geht es auch von Hand („Zurück zu …“).

Deine Daten (Räume, Ziele, Verlauf) liegen nicht im Addon-Ordner, sondern im Speicher von Home Assistant, und bleiben bei Updates erhalten.

## Aufbau

```
catalog.json          Liste aller Addons mit Version, Download, Größe, SHA-256 und Änderungen
catalog.sig           Unterschrift über catalog.json
addons/<id>/          Quelltext je Addon: addon.json, __init__.py, Python-Teil, www/panel.js, CHANGELOG.md
tools/build.py        baut ZIPs und Katalog und unterschreibt
```

## Neue Version veröffentlichen

### Mit dem Tool (empfohlen)

Doppelklick auf **Veröffentlichen.command**. Im Browser öffnet sich „Addon veröffentlichen“: Addon wählen, Version antippen (Fehlerbehebung, Neue Funktionen, Großer Schritt oder nur neuer Build), Stufe wählen, kurz schreiben, was neu ist, **Veröffentlichen**. Das Tool trägt Version und Änderungen ein, baut die ZIP, signiert den Katalog, lädt den Quelltext hoch, legt das Release an, veröffentlicht den Katalog und prüft am Ende die Prüfsumme der ZIP auf GitHub.

- Beim ersten Start: ein GitHub-Token (Classic, Recht `public_repo`) und dein privater Schlüssel `FHPRIV1-…`. Beides liegt danach im Schlüsselbund des Mac.
- Gibt es das Repository noch nicht, legt das Tool es mit einem Tipp an und lädt alle Dateien hoch.
- **Probelauf**: baut alles, lädt aber nichts hoch.
- `python3 tools/veroeffentlichen.py --demo` zeigt den ganzen Ablauf, ohne GitHub zu berühren.
- Der Katalog kommt immer als letzter Schritt. Bricht vorher etwas ab, sehen die Integrationen nichts Halbes.

### Von Hand

1. In `addons/<id>/addon.json` Version oder Build erhöhen und oben in `addons/<id>/CHANGELOG.md` die Änderungen eintragen.
2. `FH_PRIV=FHPRIV1-… python3 tools/build.py` ausführen. Es entstehen `dist/<id>-<version>-b<build>.zip`, `catalog.json` und `catalog.sig`.
3. Auf GitHub ein Release mit dem ausgegebenen Tag anlegen (z. B. `saugroboter-0.1.1-b1`) und die ZIP-Datei anhängen.
4. `catalog.json` und `catalog.sig` committen und pushen. Ab jetzt sehen alle Integrationen das Update.

`python3 tools/build.py --check` prüft Katalog, Unterschrift und Prüfsummen.

Made with ❤ by **MADDOG3010**
