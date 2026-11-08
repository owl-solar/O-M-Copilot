"""Command line: ``om-copilot ingest``."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

from design_ingest.llm import ExternalCallBlocked
from design_ingest.llm import summarize as llm_summary

from . import feedback as fb
from .answer import answer, grade
from .answer import summarize as summarize_grades
from .baseline import PRICE_IN, PRICE_OUT, ask_baseline, build_context, cost_usd
from .corpus import load_documents, summarize
from .evaluation import evaluate, latency_ms, load_questions
from .events import group, read_log
from .export import load_orders
from .export import write_csv as write_orders_csv
from .export import write_json as write_orders_json
from .manuals import add_manual, format_report, inspect, manifest
from .orders import approve
from .retrieval import Retriever
from .synth import code_pool, generate, write_csv, write_jsonl
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
    print(f"latency per query (search only, model already loaded): median {res['latency_ms']['median']} ms, "
          f"p95 {res['latency_ms']['p95']} ms")
    if args.save:
        _save_result(f"retrieval_{args.mode}{'' if not args.no_code_lookup else '_nolookup'}",
                     {**res["overall"], "latency_ms": res["latency_ms"]})
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
    if args.csv:
        print(f"{write_csv(events, args.csv)} events as a SCADA-style CSV -> {args.csv}")
    print("top codes: " + ", ".join(f"{m} {c} x{k}" for (m, c), k in top))
    return 0


def cmd_workorder(args: argparse.Namespace) -> int:
    docs = load_documents(args.folder)
    retriever = Retriever(docs, mode=args.mode)
    report = read_log(args.log, args.model)
    events = report.events if args.no_group else group(report.events)
    if args.asset:
        events = [e for e in events if e["asset_id"] == args.asset]
    if args.repeated:
        events = [e for e in events if e.get("repeated")]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for event in events[args.skip : args.skip + args.n]:
        wo = build(docs, event, retriever)
        stem = wo.id
        (out / f"{stem}.json").write_text(wo.model_dump_json(indent=2), encoding="utf-8")
        (out / f"{stem}.md").write_text(to_markdown(wo), encoding="utf-8")
        what = f"{len(wo.steps)} steps" if wo.found else "code not found"
        print(f"{stem}: {what}, priority {wo.priority}")
    return 0


def cmd_incidents(args: argparse.Namespace) -> int:
    """Read a fault log (CSV or JSONL), merge alarm storms into incidents, mark repeats."""
    report = read_log(args.log, args.model)
    print(f"read {len(report.events)} events, skipped {len(report.skipped)}; columns: {report.columns or 'jsonl'}")
    for msg in report.skipped[:5]:
        print(f"  skipped {msg}")
    incidents = group(report.events)
    repeated = [i for i in incidents if i["repeated"]]
    print(f"{len(report.events)} events -> {len(incidents)} incidents ({len(repeated)} repeats)")
    worst = Counter((i["model"], i["asset_id"], i["fault_code"]) for i in incidents).most_common(3)
    print("most incidents: " + ", ".join(f"{a} code {c} x{k}" for (_m, a, c), k in worst))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(i, ensure_ascii=False) for i in incidents]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"-> {out}")
    return 0 if report.events else 1


def cmd_export(args: argparse.Namespace) -> int:
    """Work orders (JSON files) -> one CSV and one JSON for a maintenance system."""
    orders, skipped = load_orders(args.folder)
    if skipped:
        print(f"skipped {len(skipped)} files that are not valid work orders (older format?): {', '.join(skipped[:3])} ...")
    if not orders:
        print(f"no work orders in {args.folder}")
        return 1
    out = Path(args.out)
    n = write_orders_csv(orders, out.with_suffix(".csv"), delimiter=args.delimiter)
    write_orders_json(orders, out.with_suffix(".json"))
    print(f"{n} work orders -> {out.with_suffix('.csv')} and {out.with_suffix('.json')}")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """Record one verdict: a step of a work order helped or did not work."""
    orders, _ = load_orders(args.folder)
    wo = next((o for o in orders if o.id == args.wo_id), None)
    if wo is None:
        print(f"work order {args.wo_id} not found in {args.folder}")
        return 1
    try:
        rec = fb.record(wo, args.step, "helped" if args.helped else "failed", args.note, args.who)
    except ValueError as e:
        print(f"cannot record: {e}")
        return 1
    print(f"recorded: {rec['wo_id']} step {rec['step_n']} (p.{rec['page']}) -> {rec['verdict']}")
    return 0


def cmd_feedback_report(args: argparse.Namespace) -> int:
    """Helped rate overall and per fault code, from the feedback log."""
    path = Path(args.file) if args.file else None
    s = fb.summarize(fb.read(path))
    if not s["verdicts"]:
        print(f"no feedback yet in {path or fb.log_path()}. Record some: om-copilot feedback WO_ID --step 1 --helped")
        return 1
    print(f"{s['verdicts']} verdicts: {s['helped']} helped, {s['failed']} did not work, "
          f"helped rate {s['rate']:.0%}")
    for r in s["codes"][: args.top]:
        flag = "  <- review the steps" if r["review"] else ""
        print(f"  {r['model']} code {r['fault_code']}: {r['helped']}/{r['n']} helped ({r['rate']:.0%}){flag}")
    print(f"{len(s['to_review'])} codes to review (at least {fb.REVIEW_MIN_VERDICTS} verdicts, "
          f"helped rate below {fb.REVIEW_BELOW_RATE:.0%})")
    return 0


def cmd_synth_feedback(args: argparse.Namespace) -> int:
    """DEMO ONLY: invented verdicts for existing work orders, written to a separate file."""
    orders, _ = load_orders(args.folder)
    if not orders:
        print(f"no work orders in {args.folder}")
        return 1
    recs = fb.synthetic(orders, seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in recs]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(recs)} SYNTHETIC verdicts -> {out} (demo only; report with: feedback-report --file {out})")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Open the demo page in the browser (needs: pip install -e .[ui])."""
    import subprocess
    import sys

    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("streamlit is not installed: pip install -e .[ui]")
        return 1
    page = Path(__file__).with_name("ui.py")
    print(f"open http://localhost:{args.port} in the browser (Ctrl+C to stop)")
    # headless: no e-mail prompt on the first start, no usage statistics
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(page), "--server.port", str(args.port),
                            "--server.headless", "true", "--browser.gatherUsageStats", "false"])


def cmd_eval_agent(args: argparse.Namespace) -> int:
    """Scripted agent (no LLM, free) on a fresh synthetic log whose storms and chronic repeats are known."""
    import tempfile

    from .agent_eval import passed, run_checks, scripted_agent
    from .mcp_server import Tools

    docs = load_documents(args.folder)
    pool = code_pool(docs)
    truth: list[dict] = []
    events = generate(pool, n=args.n, seed=args.seed, truth=truth)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "log.jsonl"
        write_jsonl(events, log)
        tools = Tools(docs=docs, retriever=Retriever(docs, mode="bm25"), log_path=str(log), orders_dir=str(Path(tmp) / "orders"))
        run = scripted_agent(tools, truth)
        res = run_checks(tools, truth, pool, run)
    kinds = Counter(t["kind"] for t in truth)
    print(f"{len(events)} events (seed {args.seed}); known to the generator: {kinds['storm']} storms, "
          f"{kinds['chronic']} chronic repeats, {kinds['cluster']} clusters")
    print(f"storms merged into one incident: {res['storms']}")
    print(f"chronic repeats flagged:         {res['chronic']}")
    print(f"priority and deadline follow the rules: {res['priority']}")
    print(f"writes: {res['writes']}")
    print(f"numbers in the report missing from tool output: {res['ungrounded_numbers']}")
    for name, items in res["failures"].items():
        for item in items[:5]:
            print(f"  FAIL {name}: {item}")
    print("\nreport:\n" + run["report"])
    return 0 if passed(res) else 1


def _save_result(name: str, data: dict) -> None:
    out = Path("data/compare")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def cmd_eval_agent_llm(args: argparse.Namespace) -> int:
    """PAID: Claude calls the MCP tools in a loop (step limit, spending cap). Two tasks: the 25 questions or
    the chronic-repeat task on the synthetic log."""
    import asyncio
    import tempfile

    import anthropic

    from . import agent_eval as ae
    from .agent import BudgetStop, Spend, run_agent
    from .mcp_server import Tools

    os.environ.setdefault("DESIGN_INGEST_TOKEN_BUDGET", "1000000")
    client, spend = anthropic.Anthropic(), Spend(cap_usd=args.max_usd)
    docs = load_documents(args.folder)
    allow = args.allow_external or None
    print(f"task {args.task}; at most {args.max_steps} tool rounds; spending cap ${args.max_usd:.2f}")
    if args.task == "log":
        pool = code_pool(docs)
        truth: list[dict] = []
        events = generate(pool, n=1000, seed=42, truth=truth)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log.jsonl"
            write_jsonl(events, log)
            tools = Tools(docs=docs, retriever=Retriever(docs, mode="bm25"), log_path=str(log),
                          orders_dir=str(Path(tmp) / "orders"))
            try:
                run = asyncio.run(run_agent(
                    tools, client=client, spend=spend, system=ae.LOG_SYSTEM,
                    user=ae.LOG_TASK.format(min_repeats=ae.MIN_REPEATS), allowed=ae.LOG_TOOLS, final_name="report",
                    final_description="Final report.", final_schema=ae.LOG_REPORT, max_steps=args.max_steps,
                    allow_external=allow))
            except BudgetStop as e:
                print(f"STOP: {e}")
                return 1
            res = ae.check_log_run(tools, truth, run.outputs, run.result)
        summary = {k: v for k, v in res.items() if k != "failures"}
        summary.update(rounds=run.rounds, forced=run.forced, seconds=round(run.seconds, 1), usd=round(run.usd, 4),
                       input_tokens=run.input_tokens, output_tokens=run.output_tokens)
        print(json.dumps(summary, indent=1))
        for name, items in res["failures"].items():
            for item in items[:6]:
                print(f"  FAIL {name}: {item}")
        _save_result(f"agent_log_{args.tag}", {**summary, "failures": res["failures"]})
        return 0
    questions = load_questions(args.questions)
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids.split(",")]
    questions = questions[: args.limit] if args.limit else questions
    _ctx, page_text = build_context(docs)
    tools = Tools(docs=docs, retriever=Retriever(docs, mode="hybrid"))
    grades, runs = [], []
    for q in questions:
        try:
            run = asyncio.run(run_agent(
                tools, client=client, spend=spend, system=ae.QA_SYSTEM, user=f"Question: {q['question']}",
                allowed=ae.QA_TOOLS, final_name="reply", final_description="Give the final answer with citations.",
                final_schema=ae.QA_REPLY, max_steps=args.max_steps, allow_external=allow))
        except BudgetStop as e:
            print(f"STOP: {e}")
            break
        a = ae.answer_from_reply(q["question"], run.result, page_text, None)
        g = grade(q, a)
        grades.append(g)
        runs.append(run)
        print(f"{q['id']} {'PASS' if g['ok'] else 'FAIL'} rounds={run.rounds}{' FORCED' if run.forced else ''} "
              f"{run.seconds:.1f}s est. ${run.usd:.4f} {({k: v for k, v in g.items() if k not in ('id', 'ok')})}")
        if args.show or not g["ok"]:
            _print_answer(a)
    lat = sorted(r.seconds for r in runs)
    stats = {**summarize_grades(grades), "questions": len(runs),
             "median_seconds": round(lat[len(lat) // 2], 1) if lat else None,
             "p95_seconds": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1) if lat else None,
             "forced_replies": sum(r.forced for r in runs), "mean_rounds": round(sum(r.rounds for r in runs) / max(1, len(runs)), 2),
             "usd_total": round(spend.usd, 4), "usd_per_question": round(spend.usd / max(1, len(runs)), 4),
             "input_tokens": spend.input_tokens, "output_tokens": spend.output_tokens}
    print(json.dumps(stats, indent=1))
    _save_result(f"agent_questions_{args.tag}", stats)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """A person approves a saved draft work order (the agent has no such tool)."""
    try:
        wo = approve(args.order_id, args.by, Path(args.folder) if args.folder else None)
    except ValueError as e:
        print(f"cannot approve: {e}")
        return 1
    print(f"{wo.id}: {wo.status} by {wo.approved_by}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """MCP server on stdio for Claude Desktop / Claude Code (needs: pip install -e .[mcp])."""
    try:
        from .mcp_server import build_server
    except ImportError:
        print("mcp is not installed: pip install -e .[mcp]", file=sys.stderr)
        return 1
    build_server().run("stdio")
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
    grades, seconds = [], []
    before = llm_summary()
    for q in questions:
        started = time.perf_counter()
        a = answer(q["question"], retriever, allow_external=args.allow_external or None)
        seconds.append(time.perf_counter() - started)
        g = grade(q, a)
        grades.append(g)
        print(f"{q['id']} {'PASS' if g['ok'] else 'FAIL'} {({k: v for k, v in g.items() if k not in ('id', 'ok')})}")
        if args.show or not g["ok"]:
            _print_answer(a)
    stats = summarize_grades(grades)
    print(json.dumps(stats, indent=1))
    after = llm_summary()
    tin, tout = after["input_tokens"] - before["input_tokens"], after["output_tokens"] - before["output_tokens"]
    lat = latency_ms(seconds)
    usd = (tin * PRICE_IN + tout * PRICE_OUT) / 1e6  # upper bound: the price above 100K prompt tokens
    print(f"this run: {len(seconds)} questions, latency median {lat['median']} ms, p95 {lat['p95']} ms (retrieval + model), "
          f"{tin} input / {tout} output tokens, est. ${usd:.4f} ({usd / max(1, len(seconds)):.4f} per question)")
    if args.save:
        _save_result(f"pipeline_{args.save}", {**stats, "latency_ms": lat, "questions": len(seconds), "input_tokens": tin,
                                               "output_tokens": tout, "usd_upper_bound": round(usd, 4)})
    return 0


def cmd_eval_baseline(args: argparse.Namespace) -> int:
    """Paid: the same questions, but the model gets all manuals in its context (no retrieval)."""
    import anthropic

    questions = load_questions(args.questions)
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids.split(",")]
    questions = questions[: args.limit] if args.limit else questions
    context, page_text = build_context(load_documents(args.folder))
    print(f"context: {len(context):,} characters, {len(page_text)} pages; spending cap ${args.max_usd:.2f}")
    client = anthropic.Anthropic()
    grades, spent = [], 0.0
    for i, q in enumerate(questions):
        a, usage = ask_baseline(q["question"], context, page_text, client=client,
                                allow_external=args.allow_external or None)
        spent += cost_usd(usage)
        g = grade(q, a)
        grades.append(g)
        print(f"{q['id']} {'PASS' if g['ok'] else 'FAIL'} cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
              f"est. spent ${spent:.3f} {({k: v for k, v in g.items() if k not in ('id', 'ok')})}")
        if args.show or not g["ok"]:
            _print_answer(a)
        if i == 1 and not getattr(usage, "cache_read_input_tokens", 0):
            print("STOP: the prompt cache is not being hit, every call would pay the full price")
            break
        if spent > args.max_usd:
            print(f"STOP: spending cap ${args.max_usd:.2f} reached")
            break
    print(json.dumps(summarize_grades(grades), indent=1))
    print(f"estimated spend ${spent:.3f} (cache prices applied)")
    return 0


def cmd_manuals(args: argparse.Namespace) -> int:
    """List the manuals in the folder with model label, fault codes and what needs attention."""
    docs = load_documents(args.folder)
    if not docs:
        print("no supported files found")
        return 1
    if args.json:
        print(json.dumps(manifest(docs), ensure_ascii=False, indent=2))
        return 0
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
    m.add_argument("--json", action="store_true", help="the manifest as JSON (same as the MCP resource)")
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
    e.add_argument("--save", action="store_true", help="write the numbers to data/compare/ for the comparison table")
    e.set_defaults(func=cmd_eval)
    s = sub.add_parser("synth", help="write a synthetic fault log (jsonl) using codes from the manuals")
    s.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    s.add_argument("-n", type=int, default=1000, help="number of events")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--out", default="data/synth/faults.jsonl")
    s.add_argument("--csv", help="also write a SCADA-style CSV here, e.g. data/synth/faults.csv")
    s.set_defaults(func=cmd_synth)
    w = sub.add_parser("workorder", help="draft work orders (json + md) from a fault log, no LLM")
    w.add_argument("folder", nargs="?", help="folder with PDFs (default: data/raw)")
    w.add_argument("--log", default="data/synth/faults.jsonl", help="CSV or JSONL fault log")
    w.add_argument("--model", help="model label when the CSV has no model column")
    w.add_argument("--no-group", action="store_true", help="one work order per raw event")
    w.add_argument("--repeated", action="store_true", help="only incidents that are repeats")
    w.add_argument("--asset", help="only this asset_id")
    w.add_argument("-n", type=int, default=5, help="how many records")
    w.add_argument("--skip", type=int, default=0)
    w.add_argument("--out", default="data/workorders")
    w.add_argument("--mode", choices=["bm25", "hybrid"], default="bm25")
    w.set_defaults(func=cmd_workorder)
    f = sub.add_parser("feedback", help="record that a step of a work order helped or did not work")
    f.add_argument("wo_id", help="work order id (file name without extension)")
    f.add_argument("--step", type=int, required=True)
    verdict = f.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--helped", action="store_true")
    verdict.add_argument("--failed", action="store_true")
    f.add_argument("--note")
    f.add_argument("--who", help="technician name or id")
    f.add_argument("--folder", default="data/workorders")
    f.set_defaults(func=cmd_feedback)
    r = sub.add_parser("feedback-report", help="helped rate overall and per fault code")
    r.add_argument("--file", help="feedback log (default: data/feedback.jsonl)")
    r.add_argument("--top", type=int, default=10, help="how many codes to list, worst first")
    r.set_defaults(func=cmd_feedback_report)
    sf = sub.add_parser("synth-feedback", help="DEMO ONLY: invented verdicts for existing work orders")
    sf.add_argument("--folder", default="data/workorders")
    sf.add_argument("--out", default="data/synth/feedback.jsonl")
    sf.add_argument("--seed", type=int, default=42)
    sf.set_defaults(func=cmd_synth_feedback)
    u = sub.add_parser("ui", help="open the demo page in the browser (extra: ui)")
    u.add_argument("--port", type=int, default=8501)
    u.set_defaults(func=cmd_ui)
    ea = sub.add_parser("eval-agent", help="scripted agent on the synthetic log with known storms and repeats (free)")
    ea.add_argument("--folder", help="folder with PDFs (default: data/raw)")
    ea.add_argument("--n", type=int, default=1000)
    ea.add_argument("--seed", type=int, default=42)
    ea.set_defaults(func=cmd_eval_agent)
    al = sub.add_parser("eval-agent-llm", help="PAID: Claude uses the MCP tools in a loop; task: questions | log")
    al.add_argument("task", choices=["questions", "log"])
    al.add_argument("--folder", help="folder with PDFs (default: data/raw)")
    al.add_argument("--questions", default="eval/questions.json")
    al.add_argument("--ids")
    al.add_argument("--limit", type=int)
    al.add_argument("--max-steps", type=int, default=5, help="rounds of tool calls before the reply is forced")
    al.add_argument("--max-usd", type=float, default=0.2, help="stop when the estimated spend passes this")
    al.add_argument("--tag", default="run1", help="name of the result file in data/compare/")
    al.add_argument("--show", action="store_true")
    al.add_argument("--allow-external", action="store_true")
    al.set_defaults(func=cmd_eval_agent_llm)
    ap = sub.add_parser("approve", help="a person approves a saved draft work order")
    ap.add_argument("order_id")
    ap.add_argument("--by", required=True, help="name of the person who approves")
    ap.add_argument("--folder", help="work orders folder (default: data/workorders)")
    ap.set_defaults(func=cmd_approve)
    sub.add_parser("mcp", help="MCP server on stdio for an agent (extra: mcp)").set_defaults(func=cmd_mcp)
    x = sub.add_parser("export", help="work orders -> CSV and JSON for a maintenance system (CMMS)")
    x.add_argument("folder", nargs="?", default="data/workorders", help="folder with work order JSON files")
    x.add_argument("--out", default="data/export/workorders", help="path without extension")
    x.add_argument("--delimiter", default=",", help="CSV delimiter, ';' for Excel with a regional setting")
    x.set_defaults(func=cmd_export)
    i = sub.add_parser("incidents", help="fault log (CSV/JSONL) -> incidents: merge alarm storms, mark repeats")
    i.add_argument("log", help="CSV or JSONL fault log")
    i.add_argument("--model", help="model label when the CSV has no model column")
    i.add_argument("--out", help="write incidents as jsonl")
    i.set_defaults(func=cmd_incidents)
    b = sub.add_parser("eval-baseline", help="comparison: all manuals in the model context, no retrieval (paid)")
    b.add_argument("--questions", default="eval/questions.json")
    b.add_argument("--ids")
    b.add_argument("--limit", type=int)
    b.add_argument("--folder")
    b.add_argument("--show", action="store_true")
    b.add_argument("--max-usd", type=float, default=1.0, help="stop when the estimated spend exceeds this")
    b.add_argument("--allow-external", action="store_true")
    b.set_defaults(func=cmd_eval_baseline)
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
            p2.add_argument("--save", help="name of the result file in data/compare/ (pipeline_NAME.json)")
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
