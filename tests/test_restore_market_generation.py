import sqlite3

import pytest

from tools import restore_market_generation as recovery


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://quant.custard.top/api/meta", "https://quant.custard.top.example.com/api/meta"]
)
def test_recovery_downloads_cannot_target_an_arbitrary_origin(url):
    with pytest.raises(ValueError, match="fixed website"):
        recovery.read_url(url)


def test_pointer_restore_is_atomic_and_checks_both_expected_current_and_manifest():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE generations (generation_id TEXT PRIMARY KEY, manifest_sha256 TEXT);"
        "CREATE TABLE current_generation (singleton INTEGER PRIMARY KEY, generation_id TEXT, updated_at TEXT);"
        "INSERT INTO generations VALUES ('old','approved'),('new','newhash'),('newer','newerhash');"
        "INSERT INTO current_generation VALUES (1,'new','before');"
    )
    assert db.execute(recovery.RESTORE_SQL, ["old", "now", "new", "old", "wrong"]).rowcount == 0
    assert db.execute(recovery.RESTORE_SQL, ["old", "now", "newer", "old", "approved"]).rowcount == 0
    assert db.execute(recovery.RESTORE_SQL, ["old", "now", "new", "old", "approved"]).rowcount == 1
    assert db.execute("SELECT generation_id FROM current_generation").fetchone() == ("old",)
    assert db.execute("SELECT COUNT(*) FROM generations").fetchone() == (3,)


def test_restore_refuses_an_unhealthy_target_before_any_write(monkeypatch):
    monkeypatch.setattr(recovery, "read_url", lambda _: b'{"generation_id":"old","ok":false}')
    monkeypatch.setattr(recovery, "d1_query", lambda *args: pytest.fail("Must not query or update D1"))
    with pytest.raises(ValueError, match="integrity"):
        recovery.restore("old", "new", "approved")


def test_restore_refuses_a_concurrent_publication(monkeypatch):
    monkeypatch.setattr(recovery, "check_health", lambda _: {})
    calls = []

    def query(sql, params):
        calls.append(sql)
        return {"results": [{"generation_id": "newer"}]}

    monkeypatch.setattr(recovery, "d1_query", query)
    with pytest.raises(ValueError, match="current generation changed"):
        recovery.restore("old", "new", "approved")
    assert len(calls) == 1


def test_restore_can_resume_after_the_pointer_has_already_been_restored(monkeypatch):
    monkeypatch.setattr(recovery, "check_health", lambda _: {"ok": True})
    monkeypatch.setattr(recovery, "read_url", lambda _: b'{"generation_id":"old","manifest_sha256":"approved"}')
    calls = []

    def query(sql, params):
        calls.append(sql)
        return {"results": [{"generation_id": "old"}]}

    monkeypatch.setattr(recovery, "d1_query", query)
    assert recovery.restore("old", "new", "approved")["already_restored"] is True
    assert len(calls) == 1


def test_manifest_requires_a_complete_single_generation():
    generation = "7ee4958f2e33242d"
    manifest = {
        "catalogue": {"filename": f"catalog-{generation}.json.gz"},
        "signals": {"filename": f"signals-{generation}.json.gz"},
        "signature": {"filename": f"manifest-{generation}.sig"},
        "company_details": {
            "shards": [{"filename": f"company-details-{generation}-{i:02x}.json.gz"} for i in range(16)]
        },
    }
    assert len(recovery.manifest_assets(manifest, generation)) == 18
    manifest["company_details"]["shards"].pop()
    with pytest.raises(ValueError, match="complete generation"):
        recovery.manifest_assets(manifest, generation)
