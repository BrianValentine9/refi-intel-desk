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

**Ingest (`src/data/`).** Scheduled pulls fetch the latest values, normalize them into
tidy records, and write them to SQLite. Raw responses are cached locally so a failed
source or rate limit doesn't break a run. (Built in Step 2.)

**Storage (SQLite, `data/`).** A single local database holds the historical rate series.
It is gitignored — the repo ships code, not data — so anyone can rebuild it from the
public sources.

**Analysis core (`src/core/`).** Pure functions model a synthetic pool of loans by
note-rate cohort and compute, at each candidate rate trigger, how many loans clear the
net-tangible-benefit and recoupment tests. This is the trigger ladder. (Built in Step 3.)

**UI (`src/app/`).** A Streamlit dashboard renders current rates and the trigger ladder
so a user can see, at a glance, where refinance opportunity opens up. (Built in Step 4.)

**AI + evals.** The Anthropic API turns the day's computed figures into a plain-English
morning brief. An eval harness (`evals/`) checks every number in the brief against the
source data before it ships, so the AI layer is grounded, not decorative. (Built in Step 5.)
