# carv-mcp

MCP-server die je [Carv](https://getcarv.com) ski-data beschikbaar maakt in Claude Desktop. Je
kunt Claude dan vragen stellen als "hoe ontwikkelde mijn Ski:IQ zich dit seizoen?", "wat ging er
mis in mijn derde afdaling van gisteren?" of "op welke metric verlies ik de meeste punten bij
carving-bochten?".

Alle meetgegevens die Carv opslaat zijn bereikbaar: per dag, per afdaling, per lift en per bocht.
Denk aan Ski:IQ per bochttype, kantelhoek, edge build, edging similarity, early edging, g-kracht,
weight release, bochtvorm, parallelle ski's, balans, bochtradius, snelheid, sneeuw en terrein,
GPS-sporen, materiaal en sensorkalibratie.

## Installeren

Je hebt [uv](https://docs.astral.sh/uv/) nodig.

```bash
uv tool install /pad/naar/carvMCP
```

```bash
carv-mcp login
```

```bash
carv-mcp install-claude
```

Herstart daarna Claude Desktop. `install-claude` voegt de server toe aan
`claude_desktop_config.json` en maakt eerst een `.bak` van het bestaande bestand. Wil je het
liever zelf doen, dan is dit het blok:

```json
{
  "mcpServers": {
    "carv": { "command": "/Users/<jij>/.local/bin/carv-mcp", "args": [] }
  }
}
```

Is [carv-garmin-sync](https://github.com/DGTLMagician/carvGarminSync) al ingesteld, dan gebruikt
carv-mcp die Carv-login en de gedownloade sessies, en is `login` niet nodig. Inloggen kan ook via
de omgevingsvariabelen `CARV_EMAIL` en `CARV_PASSWORD` in het `env`-blok van de Claude
Desktop-config.

`carv-mcp status` laat zien of de login werkt en hoeveel sessies er zijn.

## Tools

| Tool | Wat |
|---|---|
| `list_sessions` | alle skidagen met totalen en Ski:IQ, optioneel per periode |
| `get_session` | één dag: elke afdaling en liftrit, skiërprofiel, materiaal |
| `get_run` | één afdaling: alle techniek-metrics, Ski:IQ per bochttype, gemiddelde/min/max over de bochten, sneeuw en terrein, sub-segmenten |
| `get_turns` | per bocht: type, Ski:IQ, radius, snelheid, alle techniek-metrics en hun bijdrage aan Ski:IQ |
| `metric_trend` | verloop van een metric over dagen of afdalingen, bijvoorbeeld `ski_iq`, `ski_iq.carving` of `edge_angle` |
| `season_summary` | totalen en records per seizoen (juli t/m juni) |
| `explore_session_data` | vrije toegang tot de volledige gedecodeerde sessie via een pad, zoals `runs.3.metrics` of `turns.10.left` |
| `describe_message` | welke velden Carv definieert voor een berichttype |

Tools met een `session`-parameter accepteren een sessie-id, een datum (`2026-04-25`) of `latest`.
Alle tools zijn read-only. Met Carv wordt alleen gecommuniceerd om in te loggen, de
sessielijst op te halen en sessies te downloaden.

Metrics onder `display` zijn de genormaliseerde scores die de Carv-app toont (meestal 0 tot 1,
hoger is beter). Uitzonderingen: `tempo`, `turn_speed` (m/s), `turn_radius` en `turn_corridor`
(m). `iq_impact` is het aantal Ski:IQ-punten dat een metric bijdraagt.

## Opslag

- `~/.config/carv-mcp/carv.json`: Carv-sessiecookie, plus optioneel je wachtwoord (rechten 0600)
- `~/.cache/carv-mcp/`: gedownloade sessies (3 tot 7 MB per dag), de sessielijst en een index
  met samenvattingen. Alles hier mag weg; het wordt opnieuw opgehaald.

De eerste vraag na installatie downloadt alle sessies die nog niet in de cache staan. Dat duurt
even. Nieuwe sessies haal je op met `list_sessions(refresh=true)`; vraag Claude gewoon om "nieuwe
Carv-sessies op te halen".

## Hoe het werkt

De verbinding met Carv (gRPC op `rpc.carv.ai`, sessiebestanden als zstd-gecomprimeerde protobuf)
komt uit carv-garmin-sync; zie de README daar.

Die tool las alleen het GPS-spoor. Voor deze server is het volledige sessieformaat
achterhaald: 130 berichttypes met 1689 velden. Carv levert geen `.proto`-bestanden mee, maar de
Rust-library in de Android-app (`libandroid.so`, Carv 10.0.0) bevat wel alle symbolen. De
scripts in [`tools/schema`](tools/schema) halen het schema daaruit:

1. [`schema.py`](tools/schema/schema.py) en [`crawl.py`](tools/schema/crawl.py) emuleren met
   [unicorn](https://www.unicorn-engine.org/) de prost-functie `merge_field` van elk bericht. Per
   veldnummer wordt een ongeldig wire type aangeboden. Bij een bestaand veld geeft prost dan de
   veldnaam door aan `DecodeError::push`. Welke helper wordt aangeroepen (`int32::merge`,
   `message::merge<T>` en zo verder) bepaalt het type. Vanaf `Session` wordt zo de hele boom
   doorlopen.
2. [`sertypes.py`](tools/schema/sertypes.py) haalt uit de serde-`Serialize`-implementaties welke
   velden lijsten zijn (`Vec<T>`).
3. [`enums.py`](tools/schema/enums.py) emuleert de `Debug`-implementaties van enums voor de
   namen van bochttypes, liftsoorten, pisteniveaus en zo verder.
4. [`gen.py`](tools/schema/gen.py) schrijft dat samen naar
   [`src/carv_mcp/schema.json`](src/carv_mcp/schema.json). Daarmee decodeert
   [`decode.py`](src/carv_mcp/decode.py) een sessie generiek.

Alle 56 testsessies (december 2024 t/m april 2026) decoderen zonder één onbekend veld. Welk
int-veld bij welke enum hoort, staat niet in de binary. Die koppeling is op veldnaam gemaakt
(`ENUM_FIELDS` in `decode.py`).

Opnieuw genereren na een Carv-update: zet `libandroid.so` uit de arm64-split-APK in
`tools/schema/`, maak `syms.txt` met `nm -C libandroid.so > syms.txt`, installeer `unicorn` en
draai `crawl.py`, `sertypes.py`, `enums.py` en `gen.py`.
