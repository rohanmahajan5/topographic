# The wordmark's typeface

- `Overpass.woff2`: Overpass, by Delve Withrington, Dave Bailey and Thomas Jockin for Red Hat (Copyright 2021 The Overpass
  Project Authors, https://github.com/RedHatOfficial/Overpass); licence SIL Open Font License 1.1, full text in
  `OFL-Overpass.txt` beside this file. No Reserved Font Name is declared.
- Fetched 2026-11-21 from Google Fonts' repository: https://raw.githubusercontent.com/google/fonts/main/ofl/overpass/Overpass%5Bwght%5D.ttf
  (the variable font, weights 100 to 900) and `ofl/overpass/OFL.txt`.
- Changed for the app: cut down to Basic Latin plus a no-break space, the ellipsis, the en and em dashes and the right single
  quote, with the weight axis kept, and stored as WOFF2 (fontTools' subsetter). The licence allows this; the file stays
  under the OFL and is not sold on its own.
- Why Overpass: it descends from Highway Gothic, the lettering of road signs, the typeface family of wayfinding; calm,
  even and a little narrow, it reads as precise at title size. Space Grotesk and Jost were tried beside it (2026-11-21).

Used by `wordmark.css`: the page's title band in the app (`live.html`, served by `server.py` from `brand/`) and the app's start screen
(`agent-ide/app/start.html`, which reads it from the workspace beside the app). Nothing is fetched at run time.
