"""Pick only the tables that matter for a question (a large schema does not fit in a prompt).

Score per table = embedding similarity(question, table card)
                + bonus if a value hint from the question lives in that table
                + small bonus for words shared with table/column names.
Then add 'bridge' tables that join two selected tables, so the SQL can connect them.
"""
import hashlib
import re

import numpy as np

from . import config, llm
from .profiler import table_card


def _words(s: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if len(w) > 2}


class SchemaIndex:
    def __init__(self, profiles: list[dict]):
        self.profiles = {p["source"]: p for p in profiles}
        self.items = []  # (source, table, card)
        for p in profiles:
            for tname, t in p["tables"].items():
                self.items.append((p["source"], tname, table_card(tname, t)))
        self.vectors = self._embed_cards() if self.items else None

    def _embed_cards(self):
        cards = [c for _, _, c in self.items]
        key = hashlib.sha1(("\n".join(cards) + config.EMBED_MODEL).encode()).hexdigest()[:16]
        path = config.CACHE_DIR / "embeddings" / f"{key}.npy"
        if path.exists():
            return np.load(path)
        try:
            vecs = np.vstack([llm.embed(cards[i:i + 100]) for i in range(0, len(cards), 100)])
        except Exception:
            return None  # no API: lexical scoring only
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, vecs)
        return vecs

    def select(self, question: str, hints: list, extra: list[tuple] = (), k: int = config.TOP_TABLES):
        qwords = _words(question)
        sem = np.zeros(len(self.items))
        if self.vectors is not None:
            try:
                sem = self.vectors @ llm.embed([question])[0]
            except Exception:
                pass
        hint_tables = {}
        for _, where in hints:
            for w in where:
                src, rest = w.split(":", 1)
                hint_tables[(src, rest.rsplit(".", 1)[0])] = hint_tables.get((src, rest.rsplit(".", 1)[0]), 0) + 1
        scores = []
        for i, (src, tname, card) in enumerate(self.items):
            names = _words(tname) | {w for c in self.profiles[src]["tables"][tname]["columns"] for w in _words(c["name"])}
            s = sem[i] + 0.25 * min(hint_tables.get((src, tname), 0), 2) + 0.05 * len(qwords & names)
            scores.append(s)
        order = np.argsort(scores)[::-1]
        chosen = [(self.items[i][0], self.items[i][1]) for i in order[:k]]
        for e in extra:
            if e not in chosen and any(e == (s, t) for s, t, _ in self.items):
                chosen.append(e)
        chosen += self._bridges(chosen)
        return chosen

    def _bridges(self, chosen):
        """Tables that join to two or more chosen tables of the same source."""
        out = []
        for src, prof in self.profiles.items():
            sel = {t for s, t in chosen if s == src}
            if not sel:
                continue
            for tname, t in prof["tables"].items():
                if tname in sel:
                    continue
                linked = {f["ref_table"] for f in t["fks"]} | {
                    o for o, ot in prof["tables"].items() if any(f["ref_table"] == tname for f in ot["fks"])}
                if len(linked & sel) >= 2:
                    out.append((src, tname))
        return out

    def render(self, chosen) -> str:
        parts = []
        for src, prof in self.profiles.items():
            tabs = [t for s, t in chosen if s == src]
            if not tabs:
                continue
            others = [t for t in prof["tables"] if t not in tabs]
            parts.append(f"=== SOURCE {src} (dialect: {prof['dialect']}) ===")
            parts += [table_card(t, prof["tables"][t]) for t in tabs]
            if others:
                parts.append(f"(other tables in {src}, not shown: {', '.join(others[:30])})")
        return "\n".join(parts)

    def render_source(self, src: str, tables: list[str]) -> str:
        prof = self.profiles[src]
        return "\n".join(table_card(t, prof["tables"][t]) for t in tables if t in prof["tables"])
