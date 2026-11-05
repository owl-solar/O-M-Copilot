"""Command line: ``om-copilot ingest``."""
from __future__ import annotations

import argparse
import logging

from pathlib import Path

from design_ingest.search import build_index

from .corpus import load_documents, summarize
from .evaluation import evaluate, load_questions


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
    index = build_index(load_documents(args.folder))
    questions = load_questions(args.questions)
    res = evaluate(index, questions)

    def line(name: str, m: dict) -> str:
        return (f"{name:<48} n={int(m['n']):<3} hit@1={m['hit@1']:.2f} hit@3={m['hit@3']:.2f} "
                f"hit@5={m['hit@5']:.2f} MRR={m['mrr']:.2f}")

    print(line("overall", res["overall"]))
    for k, m in res["by_type"].items():
        print(line(f"  type: {k}", m))
    for k, m in res["by_file"].items():
        print(line(f"  file: {Path(k).name[:40]}", m))
    by_id = {q["id"]: q for q in questions}
    for qid in res["misses"]:
        print(f"  miss {qid} (rank {res['ranks'][qid]}): {by_id[qid]['question']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="om-copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="parse the manuals and print what was found")
    p.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    p.add_argument("--warnings", action="store_true", help="print parser warnings")
    p.set_defaults(func=cmd_ingest)
    e = sub.add_parser("eval", help="retrieval metrics (hit@k, MRR) on eval/questions.json")
    e.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    e.add_argument("--questions", default="eval/questions.json")
    e.set_defaults(func=cmd_eval)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
