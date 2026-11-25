import pytest

from analyst.correction import DomainCorrector
from analyst.profiler import profile_source, table_card
from analyst.sources import DataSource, load_csvs_into_upload_db


def test_profile_unseen_db(shop_db):
    prof = profile_source(DataSource("shop", shop_db), use_cache=False)
    t = prof["tables"]
    assert set(t) == {"customers", "orders"}
    assert t["orders"]["rows"] == 300
    cols = {c["name"]: c for c in t["customers"]["columns"]}
    assert cols["city"]["kind"] == "category" and "Curitiba" in cols["city"]["values"]
    assert {c["name"]: c for c in t["orders"]["columns"]}["order_date"]["kind"] == "date"
    # relationship inferred from names only (no FOREIGN KEY declared)
    links = [(f["col"], f["ref_table"], f["ref_col"]) for f in t["customers"]["fks"] + t["orders"]["fks"]]
    assert ("customer_id", "customers", "id") in links or ("id", "orders", "customer_id") in links
    assert "TABLE orders (300 rows)" in table_card("orders", t["orders"])


def test_connection_is_read_only(shop_db):
    src = DataSource("shop", shop_db)
    assert len(src.run("SELECT * FROM customers")) == 40
    with pytest.raises(Exception):
        src.run("DELETE FROM customers")
    assert len(src.run("SELECT * FROM customers")) == 40


def test_correction_on_unseen_db(shop_db):
    c = DomainCorrector([profile_source(DataSource("shop", shop_db), use_cache=False)])
    r = c.correct("total amount for customers in kuritiba and florian opolis")
    assert "Curitiba" in r.corrected and "Florianopolis" in r.corrected
    # ordinary English words are never changed
    assert c.correct("what was the average score sold last month").corrections == []


def test_csv_upload(tmp_path, monkeypatch):
    from analyst import config
    monkeypatch.setattr(config, "UPLOAD_DB", tmp_path / "uploads.db")
    names = load_csvs_into_upload_db([("Brazil States.csv", b"State Code,Region\nSP,Southeast\nRJ,Southeast\n")])
    assert names == ["brazil_states"]
    src = DataSource("uploads", f"sqlite:///{(tmp_path / 'uploads.db').as_posix()}")
    assert list(src.run("SELECT * FROM brazil_states").columns) == ["state_code", "region"]
