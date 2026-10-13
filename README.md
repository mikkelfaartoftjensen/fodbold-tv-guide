# Fodbold-tv-guide

Hvilke tv-kanaler sender hvilke fodboldkampe de næste dage?

Hver halve time henter `guide.py` kampprogrammet fra ESPN og offentlige tv-programoversigter (XMLTV fra epgshare01) for en række lande og finder ud af, hvilke kanaler der sender hvilke kampe. Resultatet ligger som en lille JSON-fil på grenen `data`:

`https://raw.githubusercontent.com/<bruger>/fodbold-tv-guide/data/guide.json`

| Felt | Indhold |
|---|---|
| `ch` | kanaler: `[nøgler adskilt af "|", land]` |
| `events` | kampe med tid, liga, hold, stilling og hvilke kanaler (indeks i `ch`) der sender dem |
| `more` | andre direkte kampe fra programoversigterne |

Kør lokalt: `python guide.py out/guide.json`
