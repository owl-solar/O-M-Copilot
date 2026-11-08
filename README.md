# O&M Copilot

Copilot for solar plant operations: an inverter fault goes in, the matching instruction from the manufacturer's manual and a draft work order come out. Every statement carries a citation (file and page); if the manuals do not answer, it says so.

Status: working CLI, evaluated on 25 questions over three manuals. See [PLAN.md](PLAN.md) for the plan and [CLAUDE.md](CLAUDE.md) for decisions with numbers.

## Why it is useful

- **Every statement has a source.** Answers and work orders cite the manual and the page. The model
  must quote the manual word for word, and the code checks each quote against the passage it came
  from: 100% of citations were found in the cited passage in the evaluation.
- **It says "not found" instead of guessing.** For questions the manuals do not answer it refuses
  (3 of 3 in the evaluation) and says what is missing.
- **Fault codes are looked up exactly.** "Fault 39" goes straight to its table entry, not through
  fuzzy search: the right page is in the top 5 for all 22 answerable questions, and for fault codes
  it is first in 11 of 12 cases.
- **Work orders need no AI.** Steps are the manual's own corrective measures with their pages and
  the priority comes from a plain table: same input, same order, no cost, nothing invented.

## Where AI is used, and where it is not

| Step | Method | Needs a model call? | What it gives |
|---|---|---|---|
| Read the PDFs, extract fault-code tables | Deterministic parsing (design-ingest) | No | Same result every run; 212 fault-code records from three manuals |
| Find a fault code named in the question | Exact lookup by number | No | Numbers are matched exactly, never "close enough" |
| Find passages by words | BM25 keyword search | No (offline) | Fast, explainable; strong on part names and exact terms |
| Find passages by meaning | Small local embedding model (bge-small, 64 MB) | No (runs on your machine) | Finds a passage when the question uses different words than the manual |
| Write the answer | Claude (Haiku), fed only the top passages | **Yes, the only paid step** | Short readable answer with quotes instead of a list of pages |
| Check the answer | Code compares every quote with its passage | No | Invented or reworded quotes are flagged |
| Work order | Template over the manual's own steps | No | Free, reproducible, no hallucinated steps |

Why the mix: keyword search alone put the right page in the top 5 for 77% of questions;
embeddings alone reached 95% but blurred numbers (fault codes first in only 58% of cases);
embeddings plus the exact code lookup reached 100% (92% first for codes). The model is used
once, at the end, and is never trusted without the quote check.

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
om-copilot add-manual FILE.pdf --model ABC-1000   # add your own manual (see below)
om-copilot manuals                        # list the manuals with model label, fault codes, warnings
om-copilot ingest                         # parse the manuals, count pages and fault codes
om-copilot eval --mode hybrid             # retrieval metrics (free, offline)
om-copilot synth -n 1000 --seed 42        # synthetic fault log in data/synth/ (codes from the manuals)
om-copilot workorder -n 5                 # draft work orders (JSON + markdown), no LLM, free
om-copilot ask "SG350HX shows fault 39. What should I check?" --allow-external      # one paid call
om-copilot eval-answers --allow-external  # 25 paid calls, about 1.5 cents with claude-haiku-5-5
```

## Add your own manual

Manuals of other manufacturers or models work the same way as the three used for the evaluation.

1. **Add it:** `om-copilot add-manual "C:path	oManual.pdf" --model ABC-1000`.
   The PDF is copied to `data/raw/`, read, and checked; the model label (`ABC-1000`, the value your fault
   log has in `model`) is saved in `data/config.local.json` (git-ignored). Without `--model` the file name is used.
2. **Read the report.** It shows pages, fault codes found and what needs attention:
   - fault codes > 0: questions, exact code lookup and work orders all work for this manual;
   - severity words the priority table does not know: add them under `"priority"` in `data/config.local.json`,
     for example `{"priority": {"severe": "P1"}}` (unknown words get P3);
   - 0 fault codes: see below.
3. **See everything at once:** `om-copilot manuals` lists all manuals with label, codes and warnings.
4. **Note the source** in `docs/sources.md` (the command prints the line with the sha256). Do not commit the PDF.

Then use it: `om-copilot ask "..."`, `om-copilot workorder` with a fault log whose `model` is the label.
One manual that covers a whole series (for example three inverter sizes) gets one label; a different series
with its own manual gets its own. The same code number in two manuals is fine: work orders look it up in the
manual of the record's model.

**If no fault codes were found.** Free questions still work through text search, but the exact code lookup
and work orders do not. Fault-code tables are recognised by headings such as Fault Code / Fault Name /
Corrective Measures (also Error, Alarm, Remedy, Action) and by ruled rows. A different layout needs a reader in
[design-ingest](https://github.com/owl-solar/design-ingest) (add it there with a test, release a tag, raise the
pin in `pyproject.toml`); it is deliberately not worked around here.

Limits: English manuals only for now, PDFs with a text layer (no scans).

## Results (three manuals: Sungrow string, Huawei alarm reference, SMA utility inverter)

| Step | Measure | Result |
|---|---|---|
| Retrieval, BM25 only | hit@5 / MRR on 22 answerable questions | 0.77 / 0.76 |
| Retrieval, hybrid + exact code lookup | hit@5 / MRR | 1.00 / 0.83 |
| Answers (Claude Haiku) | citations verified word for word | 100% |
| | answers with an expected page cited | 22 of 22 |
| | questions without an answer refused | 3 of 3 |

The questions and expected pages are in `eval/questions.json` (checked by hand against the PDFs). The check that answers contain key words from the manual reads them from a local file, `data/must_contain.json` (`{"q01": ["word", ...]}`, git-ignored because the words come from the manuals); without it that one check is skipped and the others run as usual.

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
