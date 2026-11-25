"""Domain word correction: fix misheard names / places / categories using the real database values.

The vocabulary comes from the profiler (distinct values of categorical text columns). For every
word n-gram of the transcript we look for a close match by spelling (rapidfuzz) and by sound
(metaphone). Ordinary English words are never replaced.
"""
import re
import unicodedata
from dataclasses import dataclass, field

import jellyfish
from rapidfuzz import fuzz, process
from wordfreq import zipf_frequency

COMMON_ZIPF = 3.5  # words at least this frequent in English are treated as ordinary words
EDGE_WORDS = set("a an the of in on at to for by from with and or per than vs".split())  # never start/end a match

STOPWORDS = set("""a an the of in on at to for by from with and or not no is are was were be been
what which who whom whose how many much show me give list tell find get top bottom best worst most
least highest lowest total sum average avg mean count number per each every all any some than then
this that these those it its their there here do does did did have has had i we you they my our
year years month months week weeks day days quarter quarters last first next previous same now break
down by over under between during before after since until order orders customer customers product
products seller sellers sale sales revenue price prices value values payment payments review reviews
city cities state states category categories type types status time date share percent percentage
ratio rate growth compare versus vs only also just about around up out into as if so but can could
would should will please let see what's whats where when why name names item items delivered""".split())


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    s = re.sub(r"[_\-/]+", " ", s.lower())
    return re.sub(r"[^a-z0-9 ]+", "", s).strip()


@dataclass
class Correction:
    heard: str
    value: str
    where: list
    score: float
    method: str


@dataclass
class CorrectionResult:
    original: str
    corrected: str
    corrections: list = field(default_factory=list)   # Correction objects that changed the text
    hints: list = field(default_factory=list)         # exact matches: (value, [table.column, ...])


class DomainCorrector:
    def __init__(self, profiles: list[dict]):
        self.where: dict[str, set] = {}      # normalized -> {"source:table.column"}
        self.display: dict[str, str] = {}    # normalized -> original database value
        self.rank: dict[str, int] = {}       # normalized -> frequency rank in its column (0 = most common)
        self.protected: set[str] = set()     # words used in table/column names (freight, price ...)
        for prof in profiles:
            for tname, t in prof["tables"].items():
                self.protected |= set(normalize(re.sub(r"([a-z])([A-Z])", r"\1 \2", tname)).split())
                for c in t["columns"]:
                    self.protected |= set(normalize(re.sub(r"([a-z])([A-Z])", r"\1 \2", c["name"])).split())
                    if c.get("kind") != "category":
                        continue
                    for rank, v in enumerate(c.get("values", [])):
                        if not isinstance(v, str):
                            continue
                        n = normalize(v)
                        if len(n) < 3 or n.isdigit() or n in STOPWORDS:
                            continue
                        self.where.setdefault(n, set()).add(f"{prof['source']}:{tname}.{c['name']}")
                        self.display.setdefault(n, v)
                        self.rank[n] = min(rank, self.rank.get(n, rank))
        self.vocab = list(self.where)
        self.nospace = [v.replace(" ", "") for v in self.vocab]
        self.max_words = max((len(v.split()) for v in self.vocab), default=1)
        self.max_words = min(self.max_words, 5)
        self.sound = {}
        for v in self.vocab:
            self.sound.setdefault(self._code(v), []).append(v)

    @staticmethod
    def _code(s: str) -> str:
        return " ".join(jellyfish.metaphone(w) for w in s.split())

    def _match(self, gram: str):
        if gram in self.where:
            return gram, 100.0, "exact"
        flat = gram.replace(" ", "")
        cutoff = 85 if len(flat) < 8 else 80
        cands = {}
        for query, choices in ((gram, self.vocab), (flat, self.nospace)):   # "perfume area" ~ "perfumaria"
            for _, score, idx in process.extract(query, choices, scorer=fuzz.ratio, score_cutoff=cutoff, limit=5):
                v = self.vocab[idx]
                if abs(len(v.replace(" ", "")) - len(flat)) <= max(2, len(flat) // 4):
                    cands[v] = max(score, cands.get(v, 0))
        if cands:
            top = max(cands.values())
            # among near-equal matches prefer the most frequent spelling in the data (guarulhos, not garulhos)
            best = min((v for v, s in cands.items() if s >= top - 6), key=lambda v: (self.rank[v], -cands[v]))
            return best, cands[best], "spelling"
        same_sound = self.sound.get(self._code(gram))
        if same_sound:
            cand = max(same_sound, key=lambda v: fuzz.ratio(gram, v))
            if fuzz.ratio(gram, cand) >= 60:
                return cand, fuzz.ratio(gram, cand), "sound"
        return None

    def correct(self, text: str) -> CorrectionResult:
        words = text.split()
        norm = [normalize(w) for w in words]
        out, res, i = [], CorrectionResult(original=text, corrected=text), 0
        while i < len(words):
            done = False
            for n in range(min(self.max_words, len(words) - i), 0, -1):
                parts = [p for p in norm[i:i + n] if p]
                if not parts:
                    continue
                gram = " ".join(parts)
                if (n == 1 and (gram in STOPWORDS or gram.isdigit() or len(gram) < 4)) \
                        or parts[0] in EDGE_WORDS or parts[-1] in EDGE_WORDS \
                        or all(p in STOPWORDS or p in self.protected for p in parts):
                    continue
                m = self._match(gram)
                if not m:
                    continue
                value, score, method = m
                common = all(zipf_frequency(p, "en") >= COMMON_ZIPF for p in parts)
                if method != "exact" and common and (n == 1 or method == "sound"):
                    continue  # never "correct" ordinary English words (score, sold, came ...)
                where = sorted(self.where[value])
                shown = self.display[value]
                if method == "exact":
                    res.hints.append((shown, where))
                    out.extend(words[i:i + n])
                else:
                    trail = re.sub(r"^.*?([^\w]*)$", r"\1", words[i + n - 1])  # keep "?" etc.
                    out.append(shown + trail)
                    res.corrections.append(Correction(" ".join(words[i:i + n]), shown, where, score, method))
                    res.hints.append((shown, where))
                i += n
                done = True
                break
            if not done:
                out.append(words[i])
                i += 1
        res.corrected = " ".join(out)
        return res
