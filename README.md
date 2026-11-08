# O&M Copilot

Copilot for solar plant operations: an inverter fault goes in, the matching instruction from the manufacturer's manual and a draft work order come out. Every statement carries a citation (file and page); if the manuals do not answer, it says so.

Status: working CLI, evaluated on 25 questions over three manuals. See [PLAN.md](PLAN.md) for the plan and [CLAUDE.md](CLAUDE.md) for decisions with numbers.

## How it works

Manuals are read with [design-ingest](https://github.com/owl-solar/design-ingest) (pinned to a git tag), searched offline (BM25 + small local embeddings, plus an exact fault-code lookup), and the best passages go to an LLM (Claude Haiku) that must answer from them only. Every quote is verified word for word in code. Details and the evaluation plan are in [PLAN.md](PLAN.md).

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

## Use

```bash
om-copilot ingest                         # parse the manuals, count pages and fault codes
om-copilot eval --mode hybrid             # retrieval metrics (free, offline)
om-copilot synth -n 1000 --seed 42        # synthetic fault log in data/synth/ (codes from the manuals)
om-copilot workorder -n 5                 # draft work orders (JSON + markdown), no LLM, free
om-copilot ask "SG350HX shows fault 39. What should I check?" --allow-external      # one paid call
om-copilot eval-answers --allow-external  # 25 paid calls, about 1.5 cents with claude-haiku-5-5
```

## Results (three manuals: Sungrow string, Huawei alarm reference, SMA utility inverter)

| Step | Measure | Result |
|---|---|---|
| Retrieval, BM25 only | hit@5 / MRR on 22 answerable questions | 0.77 / 0.76 |
| Retrieval, hybrid + exact code lookup | hit@5 / MRR | 1.00 / 0.83 |
| Answers (Claude Haiku) | citations verified word for word | 100% |
| | answers with an expected page cited | 22 of 22 |
| | questions without an answer refused | 3 of 3 |

The question set and expected pages are in `eval/questions.json`; expected pages were checked by hand against the PDFs.

## Examples (real runs, retold in my own words; the tool itself prints the manual's quotes)

**Question with a fault code** (`om-copilot ask "SG350HX shows fault 39. What should I check?"`):

```
Fault 39 (Low System Insulation Resistance): check the ISO resistance protection value, the resistance
to ground of the string and DC cable, and the battery cables; contact the manufacturer if nothing helps.
  [SG305HX_SG320HX-20_SG350HX-20 User Manual.pdf, p.131] quote found in the passage: yes
  (one citation per step; the quotes are shown by the tool, not reproduced here)
```

**Procedure question** ("How do I disconnect the inverter for maintenance?"): four steps (open the external AC breaker and secure it, turn the DC switch off, wait about 25 minutes for the capacitors to discharge, confirm with a clamp that the DC cable carries no current). Each step has a citation to p.136 of the same manual and every quote was verified in code.

**Question the manuals do not answer** ("... Fronius Symo error E306 ..."): `found=False`, no citations. The model says the passages contain no such code and names what is missing instead of guessing.

**Work order** (`om-copilot workorder -n 5`): for a Sungrow record with fault 1600 the order has priority P3 (the manual gives no severity), two safety excerpts (pp. 137 and 15) and four steps from p.134, each in the manual's own words with its page. Files: one JSON and one markdown per record.

## Draft work order (format)

A work order is built from a fault-log record without an LLM. Its steps are the manual's corrective measures, each with file and page; safety items are the DANGER/WARNING passages of the same manual; priority comes from the severity printed in the manual (table in `workorder.py`). Shape of the markdown view (text paraphrased here):

```
# Work order (DRAFT): INV-07, fault 1600
- Asset: INV-07 (SG350HX-20), site Site C
- Fault: 1600 PV Grounding Fault
- Priority: P3
## Safety      1. Power off and wait 25 minutes before maintenance [User Manual, p.137]
## Steps       1. Do not open the DC switch under load >0.5 A [User Manual, p.134]
               2. ... (4 steps, each with its page)
```

A code that is not in the manual of that model gives an order with no steps and a note, not a guess.

## Licence

MIT, see [LICENSE](LICENSE). The manufacturer manuals are not covered by it and are not part of this repository.
