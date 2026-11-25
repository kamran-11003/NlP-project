"""The pipeline: voice -> text -> correction -> schema selection -> SQL -> guard -> run/repair
-> chart + facts -> spoken explanation. Each stage is timed."""
import time
from dataclasses import dataclass, field

import pandas as pd
import sqlglot
from sqlglot import exp

from . import config, llm, prompts, speech
from .charts import choose_chart, clean, facts
from .correction import DomainCorrector
from .guard import check_sql, dangerous_request
from .profiler import profile_source
from .schema_index import SchemaIndex
from .sources import DataSource


@dataclass
class Turn:
    heard: str = ""                 # raw speech-recognition text (or typed text)
    question: str = ""              # after domain-word correction
    corrections: list = field(default_factory=list)
    hints: list = field(default_factory=list)
    action: str = "sql"             # sql | clarify | refuse | error
    message: str = ""               # spoken text
    source: str = ""
    sql: str = ""
    attempts: list = field(default_factory=list)   # [{"sql", "problem"}] for self-repair
    tables: list = field(default_factory=list)     # tables given to the LLM
    used_tables: list = field(default_factory=list)
    assumptions: str = ""
    is_follow_up: bool = False
    title: str = ""
    df: pd.DataFrame | None = None
    chart: dict = field(default_factory=dict)
    facts: str = ""
    audio: bytes | None = None
    timings: dict = field(default_factory=dict)


class Analyst:
    def __init__(self):
        self.sources: dict[str, DataSource] = {}
        self.profiles: dict[str, dict] = {}
        self.history: list[Turn] = []
        self.pending: Turn | None = None   # waiting for a clarification answer
        self.corrector = DomainCorrector([])
        self.index = None

    # ---------- data sources ----------
    def add_source(self, name: str, url: str, refresh: bool = False):
        src = DataSource(name, url)
        self.profiles[name] = profile_source(src, use_cache=not refresh)
        self.sources[name] = src
        self._rebuild()

    def remove_source(self, name: str):
        self.sources.pop(name, None)
        self.profiles.pop(name, None)
        self._rebuild()

    def _rebuild(self):
        profs = list(self.profiles.values())
        self.corrector = DomainCorrector(profs)
        self.index = SchemaIndex(profs)

    def reset(self):
        self.history, self.pending = [], None

    # ---------- main entry ----------
    def ask(self, text: str | None = None, audio: bytes | None = None, speak: bool = True) -> Turn:
        turn, t = Turn(), time.perf_counter()

        def lap(stage):
            nonlocal t
            now = time.perf_counter()
            turn.timings[stage] = round(now - t, 3)
            t = now

        try:
            turn.heard = speech.transcribe(audio) if audio is not None else (text or "")
            lap("speech_recognition")
            if not turn.heard.strip():
                return self._finish(turn, "error", "Sorry, I didn't catch that. Please try again.", speak, lap)

            refusal = dangerous_request(turn.heard)
            if refusal:
                turn.question = turn.heard
                return self._finish(turn, "refuse", refusal, speak, lap)

            fix = self.corrector.correct(turn.heard)
            turn.question, turn.corrections, turn.hints = fix.corrected, fix.corrections, fix.hints
            if self.pending:  # this utterance answers our clarification question
                turn.question = f"{self.pending.question} (clarification: {turn.question})"
                self.pending = None
            lap("domain_correction")

            if not self.sources:
                return self._finish(turn, "error", "Please connect a data source first.", speak, lap)
            prev = next((h for h in reversed(self.history) if h.action == "sql"), None)
            extra = [(prev.source, tb) for tb in prev.used_tables] if prev else []
            turn.tables = self.index.select(turn.question, turn.hints, extra)
            lap("schema_selection")

            plan = llm.chat_json(prompts.PLAN_SYSTEM, prompts.PLAN_USER.format(
                schema=self.index.render(turn.tables),
                hints=self._hints_text(turn.hints),
                history=self._history_text(),
                question=turn.question))
            lap("sql_generation")

            action = plan.get("action", "sql")
            if action == "clarify":
                self.pending = turn
                return self._finish(turn, "clarify", plan.get("clarification_question") or
                                    "Could you say a bit more about what you need?", speak, lap)
            if action == "refuse":
                return self._finish(turn, "refuse", plan.get("refusal_reason") or
                                    "I can't help with that request.", speak, lap)

            turn.source = plan.get("source") if plan.get("source") in self.sources else \
                max(self.sources, key=lambda s: sum(1 for x, _ in turn.tables if x == s))
            turn.assumptions, turn.is_follow_up = plan.get("assumptions", ""), bool(plan.get("is_follow_up"))
            turn.title = plan.get("title", "")
            ok = self._run_with_repair(turn, plan.get("sql", ""))
            lap("query_and_repair")
            if not ok:
                return self._finish(turn, turn.action, turn.message, speak, lap)

            turn.chart = choose_chart(turn.df)
            turn.facts = facts(turn.df, turn.chart)
            msg = llm.chat_text(prompts.EXPLAIN_SYSTEM, prompts.EXPLAIN_USER.format(
                question=turn.question, assumptions=turn.assumptions or "none", n_rows=len(turn.df),
                columns=", ".join(map(str, turn.df.columns)), facts=turn.facts,
                rows=turn.df.head(15).to_string(index=False, max_colwidth=40)))
            lap("explanation")
            return self._finish(turn, "sql", msg, speak, lap)
        except Exception as e:  # network / API problems etc. — never crash the app
            return self._finish(turn, "error", f"Something went wrong: {e}", speak, lap)

    # ---------- helpers ----------
    def _run_with_repair(self, turn: Turn, sql: str) -> bool:
        src = self.sources[turn.source]
        for attempt in range(config.MAX_REPAIRS + 1):
            ok, reason = check_sql(sql, src.dialect)
            problem = None
            if not ok and not reason.startswith("SQL could not be parsed"):
                turn.sql = sql
                turn.attempts.append({"sql": sql, "problem": "BLOCKED: " + reason})
                turn.action, turn.message = "refuse", "I won't run that query because it is not a safe read-only query."
                return False
            if not ok:
                problem = reason
            else:
                try:
                    df = clean(src.run(sql))
                    problem = self._suspicious(df)
                    turn.sql, turn.df = sql, df
                    if not problem or attempt == config.MAX_REPAIRS:
                        turn.attempts.append({"sql": sql, "problem": problem or ""})
                        turn.used_tables = self._tables_in(sql, src.dialect)
                        return True
                except Exception as e:
                    problem = "ERROR: " + str(e).split("\n[SQL")[0][:500]
            turn.attempts.append({"sql": sql, "problem": problem})
            if attempt == config.MAX_REPAIRS:
                break
            tables = [t for s, t in turn.tables if s == turn.source]
            fixed = llm.chat_json(prompts.REPAIR_SYSTEM, prompts.REPAIR_USER.format(
                dialect=src.dialect, schema=self.index.render_source(turn.source, tables),
                question=turn.question, sql=sql, problem=problem))
            sql = fixed.get("sql", sql)
        turn.sql = sql
        turn.action = "error"
        turn.message = "I couldn't get a working query for that question. Could you rephrase it?"
        return False

    @staticmethod
    def _suspicious(df: pd.DataFrame) -> str | None:
        if df.empty:
            return "The query returned no rows. Check text values (spelling/case) and date ranges."
        nums = df.select_dtypes("number")
        if len(nums.columns) and nums.isna().all().all():
            return "All numeric results are NULL. Check joins, filters and column choice."
        return None

    @staticmethod
    def _tables_in(sql: str, dialect: str) -> list[str]:
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
            ctes = {c.alias for c in tree.find_all(exp.CTE)}
            names = []
            for t in tree.find_all(exp.Table):
                full = f"{t.db}.{t.name}" if t.db and t.db not in ("public", "main", "dbo") else t.name
                if t.name not in ctes and full not in names:
                    names.append(full)
            return names
        except Exception:
            return []

    @staticmethod
    def _hints_text(hints) -> str:
        if not hints:
            return "none"
        return "\n".join(f"'{v}' found in {', '.join(w)}" for v, w in hints)

    def _history_text(self) -> str:
        lines = []
        for h in self.history[-3:]:
            if h.action == "sql":
                lines.append(f"Q: {h.question}\n   source: {h.source}\n   SQL: {h.sql}")
            elif h.action == "clarify":
                lines.append(f"Q: {h.question}\n   (we asked: {h.message})")
        return "\n".join(lines) or "none"

    def _finish(self, turn: Turn, action: str, message: str, speak: bool, lap) -> Turn:
        turn.action, turn.message = action, message
        if speak:
            try:
                turn.audio = speech.synthesize(message)
            except Exception:
                turn.audio = None
            lap("text_to_speech")
        turn.timings["total_to_speech"] = round(sum(turn.timings.values()), 3)
        self.history.append(turn)
        return turn
