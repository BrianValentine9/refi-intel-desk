# Architecture

```
 PUBLIC DATA SOURCES                INGEST                STORAGE
 ┌─────────────────┐        ┌──────────────────┐     ┌────────────┐
 │ FRED API        │        │ src/data/        │     │ SQLite     │
 │  10Y Treasury   │ ─────▶ │  fetch + clean   │ ──▶ │ data/*.db  │
 │ Freddie Mac     │        │  pulls           │     │ rate series│
 │  PMMS 30Y avg   │        └──────────────────┘     └─────┬──────┘
 └─────────────────┘                                       │
                                                           ▼
   ANALYSIS CORE                  UI                  AI + EVALS
 ┌──────────────────┐     ┌──────────────────┐     ┌────────────────┐
 │ src/core/        │     │ src/app/         │     │ Anthropic API  │
 │  NTB test        │ ──▶ │  Streamlit       │ ──▶ │  morning brief │
 │  recoupment      │     │  trigger ladder  │     │ evals/         │
 │  trigger ladder  │     │  dashboard       │     │  accuracy gate │
 └──────────────────┘     └──────────────────┘     └────────────────┘
```

## Layers

**Public data sources.** Two free, public feeds: the 10-Year Treasury yield from the
Federal Reserve (FRED) and the 30-year average mortgage rate from Freddie Mac's
Primary Mortgage Market Survey (PMMS). No private, employer, or borrower data is used.

**Ingest (`src/data/`).** A `series_registry` names the tracked series (one source of
truth). `fred_client` fetches observations from the FRED API — rate-limited, with retry
and backoff, and storing FRED's `"."` missing-value marker as NULL rather than 0. `db`
writes them to SQLite via `INSERT OR REPLACE` so re-running never duplicates rows, and
`ingest` is the runnable entry point (`python -m src.data.ingest`) supporting a full
backfill or an incremental pull from each series' latest stored date. (Built in Step 2.)

**Storage (SQLite, `data/`).** A single local database holds the historical rate series.
It is gitignored — the repo ships code, not data — so anyone can rebuild it from the
public sources.

**Analysis core (`src/core/`).** A seeded generator (`pool`) builds a synthetic book
of loans and persists it to SQLite. Pure functions then judge each loan through three
independent layers — `rules_va`/`rules_fha` (agency-clear), `economics`
(economically-clear), and `callclear` (call-clear) — backed by `amort` and a versioned
`mip` schedule. `ladder` sweeps candidate trigger rates and counts how many loans clear
agency + economic tests at each rung, carrying call-clear flags alongside; `report`
prints it. Rules implement the contract in [docs/domain-rules.md](domain-rules.md) and
never invent regulation. (Built in Step 3.)

**UI (`src/app/`).** A Streamlit dashboard (`dashboard.py`) renders current rates,
trend chart, and the interactive trigger ladder so a user can see, at a glance, where
refinance opportunity opens up. It is a thin presentation layer: every number comes
from the analysis core and from tested read-only helpers in `data_access.py` — no rule
logic, economics, or SQL lives in the UI. Sidebar assumptions recompute the ladder live
through the core, cached for responsiveness. (Built in Step 4.)

**AI + evals.** The Anthropic API turns the day's computed figures into a plain-English
morning brief. An eval harness (`evals/`) checks every number in the brief against the
source data before it ships, so the AI layer is grounded, not decorative. (Built in Step 5.)
