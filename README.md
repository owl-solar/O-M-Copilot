# O&M Copilot

Copilot for solar plant operations: an inverter fault goes in, the matching instruction from the manufacturer's manual and a draft work order come out. Every statement carries a citation (file and page); if the manuals do not answer, it says so.

Status: early work in progress (planning and scaffold). See [PLAN.md](PLAN.md).

## How it works

Manuals are read with [design-ingest](https://github.com/owl-solar/design-ingest) (pinned to a git tag), searched offline with BM25, and the best passages go to an LLM that must answer from them only. Quotes are verified in code. Details and the evaluation plan are in [PLAN.md](PLAN.md).

## Data

Manufacturer manuals are copyrighted and **not** in this repository. Download them yourself and put them into `data/raw/` (git-ignored). Sources, versions and checksums are in [docs/sources.md](docs/sources.md). Only synthetic fault logs are used; there is no real plant data here.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
copy .env.example .env          # then fill in ANTHROPIC_API_KEY if you want LLM answers
pytest
```

External model calls are off by default (design-ingest guard); they need an explicit opt-in and cost money.

## Licence

To be added before the repository is made public.
