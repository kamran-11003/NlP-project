"""Choose a suitable chart for a result, and compute the facts the spoken explanation may use.

The facts are computed in code (not by the LLM), so the numbers that are spoken are correct.
"""
import re
from decimal import Decimal

import pandas as pd

TIME_NAME = re.compile(r"(month|date|year|day|week|quarter|period|time|hour)", re.I)
TIME_VALUE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?([ T].*)?$|^\d{4}-?Q\d$", re.I)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Decimals -> float, numeric strings -> numbers."""
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            s = df[c].dropna()
            if len(s) and s.map(lambda v: isinstance(v, (int, float, Decimal))).all():
                df[c] = pd.to_numeric(df[c], errors="coerce")
        if pd.api.types.is_float_dtype(df[c]) and df[c].notna().all() and (df[c] % 1 == 0).all():
            df[c] = df[c].astype("int64")  # 2017.0 -> 2017
    # separate year + month (or quarter) columns -> one "period" column, e.g. 2017-03
    lower = {str(c).lower(): c for c in df.columns}
    y = lower.get("year")
    for part, fmt in (("month", "{:d}-{:02d}"), ("quarter", "{:d}-Q{:d}")):
        p = lower.get(part)
        if y is not None and p is not None and pd.api.types.is_integer_dtype(df[y]) \
                and pd.api.types.is_integer_dtype(df[p]):
            df.insert(0, "period", [fmt.format(a, b) for a, b in zip(df[y], df[p])])
            df = df.drop(columns=[y, p])
            break
    return df


def _is_time(df, c) -> bool:
    if pd.api.types.is_datetime64_any_dtype(df[c]):
        return True
    s = df[c].dropna().astype(str)
    if not len(s):
        return False
    if TIME_NAME.search(str(c)) and s.str.match(TIME_VALUE).all():
        return True
    if str(c).lower() == "year" and pd.api.types.is_integer_dtype(df[c]) and df[c].between(1900, 2100).all():
        return True
    return bool(s.str.match(r"^\d{4}-\d{2}").all())


def _is_id(c) -> bool:
    c = str(c).lower()
    return c == "id" or c.endswith("_id")


def choose_chart(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"kind": "none"}
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and not _is_id(c)]
    time = next((c for c in df.columns if _is_time(df, c)), None)
    nums = [c for c in nums if c != time]
    cats = [c for c in df.columns if c not in nums and c != time]
    if len(df) == 1 and nums and not time:
        return {"kind": "metric", "y": nums[:3], "label": cats[0] if cats else None}
    if time and nums:
        color = cats[0] if cats else None   # one line per category (top 8 by total are drawn)
        return {"kind": "line", "x": time, "y": nums[:1] if color else nums[:3], "color": color}
    if cats and nums and len(df) <= 40:
        return {"kind": "bar", "x": cats[0], "y": nums[:1]}
    if cats and nums:
        return {"kind": "bar", "x": cats[0], "y": nums[:1], "head": 25}
    return {"kind": "table"}


def _fmt(v) -> str:
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def facts(df: pd.DataFrame, chart: dict) -> str:
    kind = chart.get("kind")
    if kind == "none":
        return "The query returned no rows."
    if kind in ("table",) or not chart.get("y"):
        return f"{len(df)} rows returned."
    y = chart["y"][0]
    s = df[y].astype(float)
    out = []
    if kind == "metric":
        return "; ".join(f"{c} = {_fmt(df[c].iloc[0])}" for c in chart["y"])
    x = chart.get("x")
    if chart.get("color"):
        g = df.groupby(chart["color"])[y].sum().sort_values(ascending=False)
        out.append(f"Totals by {chart['color']}: " + ", ".join(f"{k} {_fmt(v)}" for k, v in g.head(5).items()))
        per_time = df.groupby(x)[y].sum()
        out.append(f"Combined {y} per {x}: " + ", ".join(f"{k} {_fmt(v)}" for k, v in per_time.items())[:600])
        top = df.loc[df[y].astype(float).idxmax()]
        out.append(f"Single highest value: {top[chart['color']]} in {top[x]} with {_fmt(float(top[y]))}.")
        return "\n".join(out)
    mean = s.mean()
    imax, imin = s.idxmax(), s.idxmin()
    out.append(f"{y}: total {_fmt(s.sum())}, average {_fmt(mean)} over {len(df)} rows.")
    out.append(f"Highest: {df.loc[imax, x]} with {_fmt(s[imax])}"
               + (f" ({(s[imax] / mean - 1) * 100:.0f}% above average)" if mean else "") + ".")
    out.append(f"Lowest: {df.loc[imin, x]} with {_fmt(s[imin])}.")
    if kind == "line" and not chart.get("color") and len(s) >= 2 and s.iloc[0]:
        change = (s.iloc[-1] / s.iloc[0] - 1) * 100
        out.append(f"From first ({df[x].iloc[0]}) to last ({df[x].iloc[-1]}) period: {change:+.0f}%.")
    if kind == "bar" and s.sum() > 0 and (s >= 0).all():
        out.append(f"Top item share of total: {s[imax] / s.sum() * 100:.0f}%.")
        if len(s) > 1:
            second = s.drop(imax).idxmax()
            out.append(f"Second: {df.loc[second, x]} with {_fmt(s[second])}.")
    return "\n".join(out)
