from decimal import Decimal

import pandas as pd

from analyst.charts import choose_chart, clean, facts


def test_time_series_is_line():
    df = pd.DataFrame({"month": ["2018-01", "2018-02", "2018-03"], "revenue": [100.0, 120.0, 180.0]})
    ch = choose_chart(df)
    assert ch["kind"] == "line" and ch["x"] == "month"
    f = facts(df, ch)
    assert "2018-03" in f and "+80%" in f


def test_decimal_year_is_line():
    df = clean(pd.DataFrame({"year": [Decimal("2016"), Decimal("2017")], "orders": [Decimal("26"), Decimal("265")]}))
    assert choose_chart(df)["kind"] == "line"


def test_year_month_columns_merged():
    df = clean(pd.DataFrame({"year": [2017, 2017, 2018], "month": [11, 12, 1], "orders": [5, 7, 9]}))
    assert list(df.columns) == ["period", "orders"] and df["period"].tolist() == ["2017-11", "2017-12", "2018-01"]
    assert choose_chart(df)["kind"] == "line"


def test_categories_is_bar():
    df = pd.DataFrame({"category": ["toys", "pets", "books"], "orders": [50, 30, 20]})
    ch = choose_chart(df)
    assert ch["kind"] == "bar"
    assert "Top item share of total: 50%" in facts(df, ch)


def test_single_value_is_metric():
    assert choose_chart(pd.DataFrame({"total": [42]}))["kind"] == "metric"


def test_empty_and_decimal():
    assert choose_chart(pd.DataFrame())["kind"] == "none"
    df = clean(pd.DataFrame({"x": ["a"], "v": [Decimal("1.5")]}))
    assert df["v"].dtype.kind == "f"
