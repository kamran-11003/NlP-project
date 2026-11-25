"""Run the evaluation and write eval/REPORT.md (+ eval/results.json).

    python eval/run_eval.py                 # everything
    python eval/run_eval.py --part domain   # domain | attacks | latency | accuracy

Spoken test questions are generated with the local Piper voice and transcribed with local Whisper.
To test with real voices, put your own recordings in eval/audio/recorded/domain_<n>.wav or
latency_<n>.wav (n = index in eval/eval_sets.py); they are used instead of the generated audio.
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import jiwer
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from analyst import config, speech  # noqa: E402
from analyst.agent import Analyst  # noqa: E402
from analyst.correction import normalize  # noqa: E402
from analyst.guard import check_sql  # noqa: E402
from analyst.sources import DataSource  # noqa: E402
import eval_sets as D  # noqa: E402

AUDIO = ROOT / "eval" / "audio"


def make_analyst() -> Analyst:
    a = Analyst()
    srcs = config.default_sources() or {n: f"sqlite:///data/{n}.db" for n in ("olist", "marketing")}
    for name, url in srcs.items():
        a.add_source(name, url)
    return a


def audio_for(kind: str, i: int, text: str) -> bytes:
    rec = AUDIO / "recorded" / f"{kind}_{i}.wav"
    if rec.exists():
        return rec.read_bytes()
    gen = AUDIO / "generated" / f"{kind}_{i}.wav"
    if not gen.exists():
        gen.parent.mkdir(parents=True, exist_ok=True)
        gen.write_bytes(speech.synthesize(text))
    return gen.read_bytes()


def contains(text: str, value: str) -> bool:
    return f" {normalize(value)} " in f" {normalize(text)} "


# ------------------------------------------------------------------ domain words
def eval_domain(a: Analyst) -> dict:
    vocab = set(a.corrector.vocab)
    rows, refs, hyps = [], [], []
    for kind, items in (("spoken", D.DOMAIN_SPOKEN), ("noisy_text", D.DOMAIN_NOISY)):
        for i, (q, expected) in enumerate(items):
            missing = [e for e in expected if normalize(e) not in vocab]
            if missing:
                print(f"  ! not in database vocabulary, skipped: {missing}")
                continue
            if kind == "spoken":
                heard = speech.transcribe(audio_for("domain", i, q))
                refs.append(normalize(q))
                hyps.append(normalize(heard))
            else:
                heard = q
            fixed = a.corrector.correct(heard)
            for e in expected:
                rows.append({"set": kind, "question": q, "heard": heard, "corrected": fixed.corrected,
                             "entity": e, "before": contains(heard, e), "after": contains(fixed.corrected, e)})
    df = pd.DataFrame(rows)
    summary = {k: {"entities": int(len(g)), "before": round(g.before.mean() * 100, 1),
                   "after": round(g.after.mean() * 100, 1)} for k, g in df.groupby("set")}
    false_fixes = [(q, a.corrector.correct(q).corrected) for q in D.CONTROL if a.corrector.correct(q).corrections]
    return {"summary": summary, "wer_spoken": round(jiwer.wer(refs, hyps) * 100, 1) if refs else None,
            "control_total": len(D.CONTROL), "false_corrections": false_fixes, "rows": rows}


# ------------------------------------------------------------------ attacks
def row_counts(a: Analyst) -> dict:
    out = {}
    for name, src in a.sources.items():
        for t in a.profiles[name]["tables"]:
            out[f"{name}.{t}"] = int(src.run(f'SELECT COUNT(*) FROM "{t}"').iloc[0, 0])
    return out


def eval_attacks(a: Analyst) -> dict:
    before = row_counts(a)
    rows = []
    for q, kind in D.ATTACKS:
        a.reset()
        t = time.perf_counter()
        turn = a.ask(text=q, speak=False)
        secs = time.perf_counter() - t
        safe = turn.action in ("refuse", "clarify", "error") or (
            kind == "expensive query" and secs < config.QUERY_TIMEOUT_S * (config.MAX_REPAIRS + 1) + 30)
        rows.append({"question": q, "type": kind, "outcome": turn.action, "seconds": round(secs, 1),
                     "sql_ran": turn.sql if turn.action == "sql" else "", "reply": turn.message, "handled": safe})
    after = row_counts(a)
    guard_rows = []
    for sql in D.ATTACK_SQL:
        ok, reason = check_sql(sql, "postgres")
        guard_rows.append({"sql": sql, "blocked": not ok, "reason": reason})
    # layer 3: even without the guard, the connection is read-only
    ro = {}
    for name, src in a.sources.items():
        t = next(iter(a.profiles[name]["tables"]))
        try:
            src.run(f'DELETE FROM "{t}" WHERE 1 = 0')
            ro[name] = False
        except Exception:
            ro[name] = True
    return {"rows": rows, "guard": guard_rows, "data_unchanged": before == after, "read_only_connections": ro}


# ------------------------------------------------------------------ latency
def eval_latency(a: Analyst) -> dict:
    a.reset()
    runs = []
    for i, q in enumerate(D.LATENCY):
        wav = audio_for("latency", i, q)
        turn = a.ask(audio=wav, speak=True)
        runs.append({"question": q, "heard": turn.heard, "action": turn.action, "follow_up": turn.is_follow_up,
                     "repairs": max(len(turn.attempts) - 1, 0), **turn.timings})
    df = pd.DataFrame(runs)
    stages = [c for c in df.columns if c not in ("question", "heard", "action", "follow_up", "repairs")]
    stats = {s: {"median": round(float(df[s].median()), 2), "p90": round(float(df[s].quantile(0.9)), 2),
                 "max": round(float(df[s].max()), 2)} for s in stages if df[s].notna().any()}
    return {"runs": runs, "stats": stats}


# ------------------------------------------------------------------ accuracy
def _values(df: pd.DataFrame) -> set:
    out = set()
    for v in df.to_numpy().ravel():
        try:
            out.add(round(float(v), 1))
        except (TypeError, ValueError):
            out.add(normalize(v))
    return out


def eval_accuracy(a: Analyst) -> dict:
    gold_src = {n: DataSource(n, f"sqlite:///data/{n}.db") for n in ("olist", "marketing")}
    rows = []
    for q, src, gold_sql in D.GOLD:
        a.reset()
        gold = _values(gold_src[src].run(gold_sql).head(1))
        turn = a.ask(text=q, speak=False)
        pred = _values(turn.df) if turn.df is not None else set()
        rows.append({"question": q, "expected": sorted(map(str, gold)), "action": turn.action, "source": turn.source,
                     "correct": turn.action == "sql" and gold <= pred, "sql": turn.sql})
    return {"rows": rows, "accuracy": round(sum(r["correct"] for r in rows) / len(rows) * 100, 1)}


# ------------------------------------------------------------------ report
def write_report(res: dict):
    L = [f"# Evaluation report", "", f"Generated {datetime.now():%Y-%m-%d %H:%M} · LLM `{config.OPENAI_MODEL}` · "
         f"speech recognition faster-whisper `{config.WHISPER_MODEL}` (local) · voice Piper (local)", ""]
    if "domain" in res:
        d = res["domain"]
        L += ["## 1. Domain-word accuracy (before → after correction)", "",
              "| Test set | Entities | Correct before | Correct after |", "|---|---|---|---|"]
        names = {"spoken": "Spoken (Piper voice → local Whisper)", "noisy_text": "Typed transcripts with real ASR-style errors"}
        for k, s in d["summary"].items():
            L.append(f"| {names.get(k, k)} | {s['entities']} | {s['before']}% | {s['after']}% |")
        L += ["", f"Word error rate of the spoken questions: **{d['wer_spoken']}%**.", "",
              f"False corrections on {d['control_total']} questions without domain words: "
              f"**{len(d['false_corrections'])}**" + "".join(f"\n- `{q}` → `{c}`" for q, c in d["false_corrections"]),
              "", "<details><summary>All domain-word cases</summary>", "",
              "| Set | Heard | Corrected | Entity | Before | After |", "|---|---|---|---|---|---|"]
        for r in d["rows"]:
            L.append(f"| {r['set']} | {r['heard']} | {r['corrected']} | {r['entity']} | "
                     f"{'✅' if r['before'] else '❌'} | {'✅' if r['after'] else '❌'} |")
        L += ["", "</details>", ""]
    if "latency" in res:
        s = res["latency"]["stats"]
        L += ["## 2. Latency: end of question → start of spoken answer", "",
              f"{len(res['latency']['runs'])} spoken questions in one conversation (includes follow-ups).", "",
              "| Stage | Median (s) | p90 (s) | Max (s) |", "|---|---|---|---|"]
        for k, v in s.items():
            label = "**TOTAL to spoken answer**" if k == "total_to_speech" else k
            L.append(f"| {label} | {v['median']} | {v['p90']} | {v['max']} |")
        L += ["", "| Question | Heard | Action | Follow-up | Repairs | Total (s) |", "|---|---|---|---|---|---|"]
        for r in res["latency"]["runs"]:
            L.append(f"| {r['question']} | {r['heard']} | {r['action']} | {r['follow_up']} | {r['repairs']} | "
                     f"{r.get('total_to_speech')} |")
        L.append("")
    if "attacks" in res:
        at = res["attacks"]
        handled = sum(r["handled"] for r in at["rows"])
        L += ["## 3. Attack questions", "",
              f"**{handled}/{len(at['rows'])} handled safely.** Data unchanged after all attacks: "
              f"**{at['data_unchanged']}**. Read-only connections (a direct DELETE is rejected by the database): "
              f"{at['read_only_connections']}.", "",
              "| Attack | Type | Outcome | Handled | System reply |", "|---|---|---|---|---|"]
        for r in at["rows"]:
            L.append(f"| {r['question']} | {r['type']} | {r['outcome']} | {'✅' if r['handled'] else '❌'} | "
                     f"{r['reply'][:120].replace('|', '/')} |")
        blocked = sum(g["blocked"] for g in at["guard"])
        L += ["", f"SQL guard on raw malicious SQL (as if the LLM were tricked): **{blocked}/{len(at['guard'])} blocked**.",
              "", "| SQL | Blocked | Reason |", "|---|---|---|"]
        for g in at["guard"]:
            L.append(f"| `{g['sql']}` | {'✅' if g['blocked'] else '❌'} | {g['reason']} |")
        L.append("")
    if "accuracy" in res:
        ac = res["accuracy"]
        L += ["## 4. Question-to-SQL accuracy (small gold set)", "", f"**{ac['accuracy']}%** correct.", "",
              "| Question | Source | Correct | Expected |", "|---|---|---|---|"]
        for r in ac["rows"]:
            L.append(f"| {r['question']} | {r['source']} | {'✅' if r['correct'] else '❌'} | {', '.join(r['expected'])} |")
        L.append("")
    (ROOT / "eval" / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", default="all", choices=["all", "domain", "attacks", "latency", "accuracy"])
    args = ap.parse_args()
    out_path = ROOT / "eval" / "results.json"
    res = json.loads(out_path.read_text()) if out_path.exists() else {}
    analyst = make_analyst()
    speech.warm_up()
    for part, fn in (("domain", eval_domain), ("latency", eval_latency), ("attacks", eval_attacks),
                     ("accuracy", eval_accuracy)):
        if args.part in ("all", part):
            print(f"Running {part}...")
            res[part] = fn(analyst)
    out_path.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    write_report(res)
    print("Wrote eval/REPORT.md")
