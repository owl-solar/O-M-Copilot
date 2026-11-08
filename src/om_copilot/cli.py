"""Command line: ``om-copilot ingest``."""
from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from datetime import date
from pathlib import Path

from design_ingest.llm import ExternalCallBlocked
from design_ingest.llm import summarize as llm_summary

from .answer import answer, grade
from .answer import summarize as summarize_grades
from .corpus import load_documents, summarize
from .evaluation import evaluate, load_questions
from .manuals import add_manual, format_report, inspect
from .retrieval import Retriever
from .synth import code_pool, generate, write_jsonl
from .workorder import build, to_markdown


def cmd_ingest(args: argparse.Namespace) -> int:
    docs = load_documents(args.folder)
    if not docs:
        print("no supported files found")
        return 1
    for doc in docs:
        s = summarize(doc)
        print(
            f"{s['file']}: {s['pages']} pages, {s['chunks']} chunks, {s['tables']} tables, "
            f"{s['fault_codes']} fault codes (pp. {s['fault_pages']}), {s['warnings']} warnings"
        )
        if args.warnings:
            for w in doc.warnings:
                print(f"    warning: {w}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    index = Retriever(load_documents(args.folder), mode=args.mode, code_lookup=not args.no_code_lookup)
    questions = load_questions(args.questions)
    res = evaluate(index, questions)

    def line(name: str, m: dict) -> str:
        return (f"{name:<48} n={int(m['n']):<3} hit@1={m['hit@1']:.2f} hit@3={m['hit@3']:.2f} "
                f"hit@5={m['hit@5']:.2f} MRR={m['mrr']:.2f}")

    print(f"mode: {args.mode}, code lookup: {not args.no_code_lookup}")
    print(line("overall", res["overall"]))
    for k, m in res["by_type"].items():
        print(line(f"  type: {k}", m))
    for k, m in res["by_file"].items():
        print(line(f"  file: {Path(k).name[:40]}", m))
    by_id = {q["id"]: q for q in questions}
    for qid in res["misses"]:
        print(f"  miss {qid} (rank {res['ranks'][qid]}): {by_id[qid]['question']}")
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    pool = code_pool(load_documents(args.folder))
    events = generate(pool, n=args.n, seed=args.seed)
    count = write_jsonl(events, args.out)
    top = Counter((e.model, e.fault_code) for e in events).most_common(5)
    print(f"{count} events from {len(pool)} codes -> {args.out} (seed {args.seed})")
    print("top codes: " + ", ".join(f"{m} {c} x{k}" for (m, c), k in top))
    return 0


def cmd_workorder(args: argparse.Namespace) -> int:
    docs = load_documents(args.folder)
    retriever = Retriever(docs, mode=args.mode)
    lines = Path(args.log).read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    if args.asset:
        events = [e for e in events if e["asset_id"] == args.asset]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for event in events[args.skip : args.skip + args.n]:
        wo = build(docs, event, retriever)
        stem = f"{wo.ts.replace(':', '')}_{wo.asset_id}_{wo.fault_code}"
        (out / f"{stem}.json").write_text(wo.model_dump_json(indent=2), encoding="utf-8")
        (out / f"{stem}.md").write_text(to_markdown(wo), encoding="utf-8")
        what = f"{len(wo.steps)} steps" if wo.found else "code not found"
        print(f"{stem}: {what}, priority {wo.priority}")
    return 0


def _retriever(args: argparse.Namespace) -> Retriever:
    return Retriever(load_documents(args.folder), mode=args.mode)


def _print_answer(a) -> None:
    print(f"found={a.found} verified={a.verified} model={a.model}")
    print(a.answer)
    for c in a.citations:
        print(f"  [{c.file}, p.{c.page}] {'ok ' if c.verified else 'NOT FOUND '}\"{c.quote}\"")


def cmd_ask(args: argparse.Namespace) -> int:
    """One question, one paid call (needs --allow-external)."""
    a = answer(args.question, _retriever(args), allow_external=args.allow_external or None)
    _print_answer(a)
    return 0 if a.verified else 1


def cmd_eval_answers(args: argparse.Namespace) -> int:
    """Paid: one call per answerable question and per unanswerable one. Limit with --ids / --limit."""
    os.environ.setdefault("DESIGN_INGEST_TOKEN_BUDGET", str(args.budget))  # design-ingest stops at this many tokens/day
    questions = load_questions(args.questions)
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids.split(",")]
    questions = questions[: args.limit] if args.limit else questions
    retriever = _retriever(args)
    grades = []
    for q in questions:
        a = answer(q["question"], retriever, allow_external=args.allow_external or None)
        g = grade(q, a)
        grades.append(g)
        print(f"{q['id']} {'PASS' if g['ok'] else 'FAIL'} {({k: v for k, v in g.items() if k not in ('id', 'ok')})}")
        if args.show or not g["ok"]:
            _print_answer(a)
    print(json.dumps(summarize_grades(grades), indent=1))
    today = llm_summary()["per_day"].get(date.today().isoformat(), {})
    print(f"llm today (all runs): {today}")
    return 0


def cmd_manuals(args: argparse.Namespace) -> int:
    """List the manuals in the folder with model label, fault codes and what needs attention."""
    docs = load_documents(args.folder)
    if not docs:
        print("no supported files found")
        return 1
    for doc in docs:
        print(format_report(inspect(doc)))
    return 0


def cmd_add_manual(args: argparse.Namespace) -> int:
    """Add a manual: copy it to the manuals folder, read it, remember its model label."""
    try:
        doc, info, dest = add_manual(args.file, args.model)
    except (FileNotFoundError, ValueError) as e:
        print(f"cannot add: {e}")
        return 1
    print(format_report(info))
    print(f"\nstored: {dest}")
    print(f"model label saved for '{dest.stem.lower()}' -> {info['model']}")
    print(f"for docs/sources.md: {doc.filename} | version: ? | where downloaded: ? | sha256 {doc.sha256}")
    return 0 if info["fault_codes"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="om-copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("manuals", help="list the manuals with model label, fault codes and warnings")
    m.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    m.set_defaults(func=cmd_manuals)
    a = sub.add_parser("add-manual", help="add a manual: copy, read, check, save its model label")
    a.add_argument("file", help="path to the PDF")
    a.add_argument("--model", help="model label used in fault logs, e.g. ABC-1000 (default: file name)")
    a.set_defaults(func=cmd_add_manual)
    p = sub.add_parser("ingest", help="parse the manuals and print what was found")
    p.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    p.add_argument("--warnings", action="store_true", help="print parser warnings")
    p.set_defaults(func=cmd_ingest)
    e = sub.add_parser("eval", help="retrieval metrics (hit@k, MRR) on eval/questions.json")
    e.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    e.add_argument("--questions", default="eval/questions.json")
    e.add_argument("--mode", choices=["bm25", "hybrid"], default="bm25", help="retrieval mode (hybrid needs the embeddings extra)")
    e.add_argument("--no-code-lookup", action="store_true", help="disable the exact fault-code lookup")
    e.set_defaults(func=cmd_eval)
    s = sub.add_parser("synth", help="write a synthetic fault log (jsonl) using codes from the manuals")
    s.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    s.add_argument("-n", type=int, default=1000, help="number of events")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--out", default="data/synth/faults.jsonl")
    s.set_defaults(func=cmd_synth)
    w = sub.add_parser("workorder", help="draft work orders (json + md) from a fault log, no LLM")
    w.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    w.add_argument("--log", default="data/synth/faults.jsonl")
    w.add_argument("--asset", help="only this asset_id")
    w.add_argument("-n", type=int, default=5, help="how many records")
    w.add_argument("--skip", type=int, default=0)
    w.add_argument("--out", default="data/workorders")
    w.add_argument("--mode", choices=["bm25", "hybrid"], default="bm25")
    w.set_defaults(func=cmd_workorder)
    for name, helptext, fn in (("ask", "answer one question with checked citations (paid call)", cmd_ask),
                               ("eval-answers", "answer eval questions and grade them (paid calls)", cmd_eval_answers)):
        p2 = sub.add_parser(name, help=helptext)
        if name == "ask":
            p2.add_argument("question")
        else:
            p2.add_argument("--questions", default="eval/questions.json")
            p2.add_argument("--ids", help="comma separated question ids, e.g. q01,q02")
            p2.add_argument("--limit", type=int, help="only the first N questions")
            p2.add_argument("--budget", type=int, default=200_000, help="token cap for the day (design-ingest)")
            p2.add_argument("--show", action="store_true", help="print passing answers too")
        p2.add_argument("--folder", help="folder with PDFs (default: data/raw)")
        p2.add_argument("--mode", choices=["bm25", "hybrid"], default="hybrid")
        p2.add_argument("--allow-external", action="store_true", help="allow the paid call to the model")
        p2.set_defaults(func=fn)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except ExternalCallBlocked as e:
        print(f"blocked: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
