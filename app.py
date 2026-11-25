"""Voice Data Analyst - Streamlit user interface.   Run:  streamlit run app.py"""
import re
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from analyst import config, speech
from analyst.agent import Analyst
from analyst.sources import load_csvs_into_upload_db

st.set_page_config(page_title="Voice Data Analyst", page_icon="🎙️", layout="wide")


@st.cache_resource(show_spinner="Loading local speech models (first time only)...")
def load_speech():
    speech.warm_up()
    return True


def default_sources() -> dict:
    srcs = config.default_sources()
    if not srcs:  # fall back to the local SQLite copies made by scripts/load_data.py
        for name in ("olist", "marketing"):
            if (config.ROOT / "data" / f"{name}.db").exists():
                srcs[name] = f"sqlite:///data/{name}.db"
    return srcs


def get_analyst() -> Analyst:
    if "analyst" not in st.session_state:
        a = Analyst()
        for name, url in default_sources().items():
            try:
                with st.spinner(f"Profiling {name}..."):
                    a.add_source(name, url)
            except Exception as e:
                st.warning(f"Could not connect '{name}': {e}")
        st.session_state.analyst = a
        st.session_state.mic = 0
        st.session_state.played = set()
    return st.session_state.analyst


load_speech()
analyst = get_analyst()

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Data sources")
    for name, src in list(analyst.sources.items()):
        prof = analyst.profiles[name]
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**{name}** · `{src.dialect}` · {len(prof['tables'])} tables")
        if c2.button("✕", key=f"rm_{name}", help=f"Disconnect {name}"):
            analyst.remove_source(name)
            st.rerun()
    if not analyst.sources:
        st.info("No data source connected yet.")

    with st.expander("➕ Connect a database (connection string)"):
        with st.form("connect", clear_on_submit=True):
            name = st.text_input("Name", placeholder="sales")
            url = st.text_input("Connection string", placeholder="postgresql://user:pass@localhost:5432/db")
            if st.form_submit_button("Connect") and name and url:
                try:
                    with st.spinner("Profiling database..."):
                        analyst.add_source(re.sub(r"\W+", "_", name), url.strip())
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not connect: {e}")

    with st.expander("➕ Upload a SQLite file or CSV files"):
        db = st.file_uploader("SQLite database", type=["db", "sqlite", "sqlite3"])
        if db and st.button("Add SQLite file"):
            path = config.ROOT / "data" / "uploaded" / db.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(db.getvalue())
            analyst.add_source(re.sub(r"\W+", "_", Path(db.name).stem), f"sqlite:///{path.as_posix()}", refresh=True)
            st.rerun()
        csvs = st.file_uploader("CSV files", type=["csv"], accept_multiple_files=True)
        if csvs and st.button("Add CSV files"):
            tables = load_csvs_into_upload_db([(f.name, f.getvalue()) for f in csvs])
            analyst.add_source("uploads", f"sqlite:///{config.UPLOAD_DB.as_posix()}", refresh=True)
            st.success(f"Loaded tables: {', '.join(tables)}")
            st.rerun()

    with st.expander("Schema overview"):
        for name, prof in analyst.profiles.items():
            st.markdown(f"**{name}**")
            for t, info in prof["tables"].items():
                st.caption(f"{t} — {info['rows']:,} rows, {len(info['columns'])} columns")

    st.divider()
    speak = st.toggle("Speak answers", value=True)
    if st.button("🧹 New conversation"):
        analyst.reset()
        st.rerun()
    st.caption(f"LLM: {config.OPENAI_MODEL} (OpenAI API)  \nSpeech recognition: faster-whisper "
               f"{config.WHISPER_MODEL} (local)  \nVoice: Piper (local)")


# ------------------------------------------------------------------ rendering
def draw_chart(df: pd.DataFrame, ch: dict, title: str):
    kind = ch.get("kind")
    if kind == "metric":
        cols = st.columns(len(ch["y"]))
        for c, y in zip(cols, ch["y"]):
            c.metric(str(y).replace("_", " ").title(), f"{df[y].iloc[0]:,.2f}".rstrip("0").rstrip("."))
        return
    if kind not in ("line", "bar"):
        return
    data = df.head(ch.get("head", len(df))).copy()
    x, ys = ch["x"], ch["y"]
    if kind == "line":
        xt = "O"  # integer years / 'YYYY-MM' / 'YYYY-Q1' stay ordinal labels; full dates become a time axis
        if not pd.api.types.is_integer_dtype(data[x]) and data[x].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}").all():
            data[x] = pd.to_datetime(data[x].astype(str))
            xt = "T"
        if ch.get("color"):
            y, col = ys[0], ch["color"]
            top = data.groupby(col)[y].sum().nlargest(8).index
            data = data[data[col].isin(top)]
            enc = dict(color=alt.Color(f"{col}:N", title=str(col)))
        else:
            data = data.melt(id_vars=[x], value_vars=ys, var_name="series", value_name="value")
            y, enc = "value", (dict(color="series:N") if len(ys) > 1 else {})
        chart = alt.Chart(data).mark_line(point=True).encode(
            x=alt.X(f"{x}:{xt}", title=str(x)), y=alt.Y(f"{y}:Q", title=str(ys[0]) if len(ys) == 1 else ""),
            tooltip=list(data.columns), **enc)
    else:
        y = ys[0]
        data[x] = data[x].astype(str)
        chart = alt.Chart(data).mark_bar().encode(
            y=alt.Y(f"{x}:N", sort="-x", title=str(x), axis=alt.Axis(labelOverlap=False, labelLimit=220)),
            x=alt.X(f"{y}:Q", title=str(y)), tooltip=list(data.columns))
    chart = chart.properties(title=title or "", height=alt.Step(28) if kind == "bar" else 360)
    st.altair_chart(chart, width="stretch")


def render(turn, i: int, latest: bool):
    with st.chat_message("user"):
        st.write(turn.heard or turn.question)
        if turn.corrections:
            fixes = ", ".join(f"“{c.heard}” → **{c.value}**" for c in turn.corrections)
            st.caption(f"🔧 Domain-word correction: {fixes}")
        if turn.question != turn.heard and "(clarification:" in turn.question:
            st.caption(f"Understood as: {turn.question}")
    with st.chat_message("assistant"):
        icon = {"clarify": "❓ ", "refuse": "⛔ ", "error": "⚠️ "}.get(turn.action, "")
        st.markdown(f"{icon}{turn.message}")
        if turn.audio:
            autoplay = latest and i not in st.session_state.played
            st.audio(turn.audio, format="audio/wav", autoplay=autoplay)
            st.session_state.played.add(i)
        if turn.df is not None and turn.action == "sql":
            if turn.chart.get("kind") in ("line", "bar", "metric"):
                draw_chart(turn.df, turn.chart, turn.title)
            st.dataframe(turn.df, width="stretch", hide_index=True, height=min(300, 38 + 35 * len(turn.df)))
        if turn.sql:
            with st.expander(f"SQL ran on **{turn.source}**" + (" · follow-up (edited previous query)"
                                                               if turn.is_follow_up else "")):
                st.code(turn.sql, language="sql")
                if turn.assumptions:
                    st.caption(f"Assumptions: {turn.assumptions}")
                if len(turn.attempts) > 1 or any(a["problem"] for a in turn.attempts):
                    st.markdown("**Self-repair attempts**")
                    for n, a in enumerate(turn.attempts, 1):
                        st.caption(f"Attempt {n}: {a['problem'] or 'OK'}")
                        st.code(a["sql"], language="sql")
                st.caption("Tables considered: " + ", ".join(f"{s}.{t}" for s, t in turn.tables))
        if turn.timings:
            with st.expander(f"⏱️ {turn.timings.get('total_to_speech', 0):.2f}s from question to spoken answer"):
                st.dataframe(pd.DataFrame(list(turn.timings.items()), columns=["stage", "seconds"]),
                             hide_index=True, width="stretch")


# ------------------------------------------------------------------ main
st.title("🎙️ Voice Data Analyst")
st.caption("Ask a business question out loud. You get a chart, a table, the SQL, and a short spoken answer. "
           "Try: *“Which product categories brought in the most revenue last quarter?”*, then *“now break it down by month”*.")

for i, turn in enumerate(analyst.history):
    render(turn, i, latest=(i == len(analyst.history) - 1))

if analyst.pending:
    st.info(f"🎧 Waiting for your answer: *{analyst.history[-1].message}*")

audio = st.audio_input("🎤 Record your question (click, speak, click again)", key=f"mic_{st.session_state.mic}")
typed = st.chat_input("…or type a question")

if audio is not None or typed:
    with st.spinner("Thinking..."):
        analyst.ask(text=typed, audio=audio.getvalue() if audio is not None else None, speak=speak)
    st.session_state.mic += 1  # fresh recorder for the next question
    st.rerun()
