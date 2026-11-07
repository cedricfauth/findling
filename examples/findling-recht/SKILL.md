---
name: findling-recht
description: Deutsches Recht mit Findling (search/read/browse) belegen – Normen, Urteile, Fristen, Miete, Arbeit, Vertrag, Familie, Erbe, Strafe; auch englisch („German law“), auch ohne Findling-Erwähnung.
---

# Deutsches Recht mit Findling belegen

Aus dem Gedächtnis zitierte Normen sind oft veraltet oder ungenau, erfundene
Aktenzeichen sind schlimmer als keine. Stütze rechtliche Aussagen deshalb auf die
indexierten Texte und verlinke sie, damit sich jede Aussage mit einem Klick prüfen lässt.

1. **Bestand prüfen:** `browse()` zeigt die Ordner; erkenne Gesetze und
   Entscheidungen am Namen. `browse(<Gesetze-Ordner>)` zeigt, welche Gesetze da
   sind: eine Datei je Gesetz, benannt nach der Abkürzung (`BGB.html`, `SGB 5.html`).
   Fehlt ein Ordner oder das nötige Gesetz, oder läuft Findling nicht, sag das klar
   und kennzeichne die Aussagen dazu als unbelegt.
2. **Normen wörtlich lesen:** bekannte Norm direkt mit
   `read(file=<Gesetzesdatei>, section="§ 573")` öffnen, nur die Nummer, ohne Absatz
   und Gesetz („§ 573 BGB“ und „§ 573 Abs. 2“ finden nichts). Artikel stehen ohne
   Punkt: `"Art 3"`, nicht `"Art. 3"`. Absätze stehen als (1), (2), Nummern als „1.“
   im Text; zitiere sie daraus. Unbekannte Norm: erst
   `search(…, within=<voller Pfad des Gesetze-Ordners>)`. Verweise auf andere Normen
   nachlesen, wenn sie zählen. Die Dateien nennen keinen Gesetzesstand: bei kürzlich
   geänderten Vorschriften darauf hinweisen.
3. **Entscheidungen:** je Gericht ein Unterordner (`bgh`, `olgk`, `lg-berlin`,
   `lsgnrw` …); für ein bestimmtes Gericht `within` auf diesen Ordner setzen.
   Dateinamen: `JJJJ-MM-TT Urteil|Beschluss <Az>`, mit `-` statt `/` im Aktenzeichen.
   Gericht, Art, Datum und Aktenzeichen aus der ersten Überschrift übernehmen
   („Amtsgericht Aachen, Urteil vom 26.07.2004, 4 C 608/03“), nie aus dem Gedächtnis.
   Vor dem Stützen Tenor und die tragende Stelle der Entscheidungsgründe (bei
   Beschlüssen „Gründe“) lesen. Die alleinstehenden Zahlen im Text sind Randnummern:
   als „Rn. 18“ zitieren.
4. **Lückenhafte Sammlung:** Welche Entscheidungen indexiert sind, ändert sich; es ist
   nie die gesamte Rechtsprechung. Kein Treffer heißt
   nicht, dass es keine Rechtsprechung gibt; eine einzelne Entscheidung ist keine
   „ständige Rechtsprechung“ oder „herrschende Meinung“. Treffer mit
   `match: "meaning"` prüfen oder mit anderen Fachbegriffen neu suchen. Findest du
   nichts Belastbares, sag das.
5. **Web nur nach Rückfrage:** Lokale Quellen gehen vor. Reichen sie nicht (Gesetz oder
   Entscheidung fehlt, Stand fraglich, nichts Belastbares), sag, was fehlt, und frag,
   ob du im Web suchen sollst. Ohne Zustimmung keine Websuche. Web-Fundstellen mit
   Quelle verlinken und als solche kennzeichnen.

**Antwort** in der Sprache der Frage: Kurzantwort, dann Begründung (Voraussetzungen
der Norm einzeln am Sachverhalt), dann offene Punkte. Gefragte Normen und tragende
Stellen (Absatz, Satz, Leitsatz, Randnummer) wörtlich als Zitat (`>`) mit Link, nur
den nötigen Teil, Kürzungen mit […] markieren. Jede Norm und jede Entscheidung mit
Link (`url` des Treffers oder Link-Zeile aus `read`), z. B.
[§ 573 Abs. 2 Nr. 2 BGB](url) oder [AG Aachen, Urt. v. 26.07.2004 – 4 C 608/03, Rn. 18](url).
Kennzeichne, was nicht aus den indexierten Quellen stammt. Bei einem konkreten
Einzelfall mit Folgen genügt ein Satz, dass das keine Rechtsberatung ersetzt.
