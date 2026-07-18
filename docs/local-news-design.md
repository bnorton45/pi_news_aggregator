# Local-news ingestion — design note

Status: **wire-syndication collapse SHIPPED** (trust path, testable without hardware);
**ingester DEFERRED to post-0b** — gated on the Pi ANN-latency spike (PLAN §10 0b, §12).
Tracked as an open PLAN item.

## Why local news

Local TV/radio/newspaper RSS is the earliest *published* coverage of most physical
events — often hours ahead of national mainstream. That is exactly the gap signal this
project surfaces (§6.6): local velocity up, mainstream baseline still quiet. It also
adds genuine corroborating origins for local events where social + one authority is
all we otherwise see.

## Source-class decision

New tier `local_news` (shipped in `SourceClass` + the DB enum, dormant until the
ingester lands):

- **Content**: RSS titles + descriptions only — the design's "never content-scraped"
  rule applies to full-article scraping; RSS is the syndication channel outlets
  publish for this purpose.
- **Corroboration weight**: counts as an origin like mainstream outlets do, BUT
  wire-collapsed (below). Never `authoritative`.
- **Baseline**: local_news items are EXCLUDED from `mainstream_presence` — local
  coverage is the *early* side of the gap signal; folding it into the baseline would
  cancel the very signal it provides.

## Wire-syndication collapse (shipped)

The trap this feature must not fall into: most local stations carry AP/Reuters/AFP
wire copy and network content services (CNN Newsource, Gray, Scripps, Nexstar). Five
stations running one AP story are ONE reporting origin. Without collapse, syndication
would forge corroboration and hollow out the §6.5 gate.

What already collapsed before this work: verbatim copy (COPY edges, simhash ≤6 bits)
and N items from one station (`src:<source>` origin key). The gap was *localized* wire
copy — station prepends its own dateline/lede, simhash drifts past 6 bits, and the
copies falsely count as independent.

Mechanism (all pure libs, unit-tested):

- `libs/trust/wire.py` — `wire_ref(text)` detects credited wire services. Error
  asymmetry sets the posture: a MISS overcounts origins → premature corroboration
  (unsafe); a FALSE POSITIVE folds an original report into a wire origin →
  undercounts (safe). So full org names match anywhere; terse tags (`(AP)`) only in
  the 160-char dateline window; bare "AP" never.
- `ProvNode.wire_ref` + origin-key precedence (`libs/trust/edges.py`): items of class
  `local_news`/`mainstream` with a wire credit get origin key `wire:<service>` —
  graph.py's existing key-collapse then folds all carriers of that service into one
  origin per Story. Social is excluded: an account pasting AP copy is that account's
  own amplification (COPY edges still catch verbatim reposts).
- Computed in the cluster layer alongside simhash (same text, same layer, §3.3-clean:
  cluster already runs simhash over the in-flight bus text) and stored on
  `items.wire_ref`, so member recounts read the stored value.

## Ingester design (post-0b)

ONE service `services/ingest/localnews` — not one per station:

- **Feed list**: curated OPML/ConfigMap, ~100 feeds at rollout → ~1–2k mature. Each
  entry: feed URL, station slug, market/region (feeds the gazetteer so a burst of one
  metro's stations registers as local velocity).
- **Polling**: staggered async loop, conditional GETs (ETag/Last-Modified); ~5-min
  interval ⇒ ~3 req/s of mostly-304s at 1k feeds. Dead-feed detection (N consecutive
  failures → log + skip; report in health beat).
- **Normalization**: title + description → `Item(source=<station>, source_class=
  LOCAL_NEWS, author_ref=<station domain>, urls=[entry link])`. Standard per-source
  wiring: own NATS user/subject, netpol, helm template, compose svc, tests, e2e.
- **Netpol**: rides the accepted §3 gap 11 (`0.0.0.0/0:443`) — but 1–2k upstream
  hosts pushes the 0b FQDN remediation toward an egress *proxy* rather than a
  per-host allowlist. Flag this in the gap-11 remediation decision.

## Capacity (why post-0b)

~1k feeds × 20–50 items/day ≈ 20–50k items/day.

| Stage | Added load | Budget | Verdict |
|---|---|---|---|
| poll/parse | ~3 req/s | I/O-bound | negligible |
| classify | +~0.5/s | sized for the social firehose | negligible |
| embed | +20–50k/day at a high survival rate | ≤400k/day cap (§6.3a governor squeezes social to compensate) | fits, eats 5–12% of cap |
| ANN clustering | +1 HNSW query per survivor | **unmeasured on Pi — the §10 0b gating spike** | the real gate |
| llm.heavy | more corroborated stories → more claim-extract | bounded queue, conc 2 | degrades gracefully |

**Rollout gates**: (1) 0b ANN-latency spike shows headroom → start with ~100
major-market feeds; (2) watch the embed governor + llm.heavy queue depth; (3) scale
the feed list. If the governor squeezes social below its floor, stop scaling.

## Open questions

- Wire-service list curation: the shipped detector covers AP/Reuters/AFP/CNN/Gray/
  Scripps/Nexstar; syndication brands drift — revisit when real feed data flows.
- Sinclair "The National Desk" and other group-owned shared content: currently only
  collapsed if credited; group-level collapse by station ownership map is a possible
  follow-up (ownership data is external and changes — deliberately not shipped).
- Whether corroboration should require ≥1 non-local origin for `corroborated` when
  all origins are local_news (wire-collapse already prevents the syndication case;
  this would guard against regional pack journalism). Decision deferred until real
  data shows whether it matters.
