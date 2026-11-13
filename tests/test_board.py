"""Lab board: store (sglab.board) and the pages / JSON API (sglab.web.board) in both auth modes."""
import itertools
import shutil
from pathlib import Path

import pytest

from sglab import board as board_mod
from sglab.board import BoardError, BoardStore, clean_tags

REPO = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://testserver"}
PW = "correct horse battery"


@pytest.fixture
def clock(monkeypatch):
    """Every store timestamp one second after the previous one, so ordering is deterministic."""
    tick = itertools.count()
    monkeypatch.setattr(board_mod, "_now", lambda: (lambda n: f"2026-11-13T10:{n // 60:02d}:{n % 60:02d}.000+00:00")(next(tick)))


@pytest.fixture
def store(tmp_path, clock):
    return BoardStore(tmp_path / "catalog.sqlite")


# ---------------------------------------------------------------- store
def test_create_get_and_validation(store):
    p = store.create("olga", "question", "Does GLS03 need re-flashing?", "before Friday\nplease", author_name="Olga",
                     session_id="S001_rest_1", device="GLS03", tags="Firmware, #GLS03 firmware")
    assert p["id"] and p["kind"] == "question" and p["state"] == "open" and p["pinned"] is False
    assert p["author"] == "olga" and p["author_name"] == "Olga" and p["tags"] == ["firmware", "gls03"]
    assert p["session_id"] == "S001_rest_1" and p["device"] == "GLS03"
    assert p["replies"] == [] and p["n_replies"] == 0 and p["edited"] is False
    assert p["created_at"] == p["updated_at"] == p["activity_at"]
    assert store.get(p["id"]) == p and store.get(999) is None

    assert store.create("ev", "note", "", "first line is the title\nmore")["title"] == "first line is the title"
    for bad in (dict(kind="rant", title="x"), dict(kind="note", title="", body="  "),
                dict(kind="note", title="x" * 201), dict(kind="note", title="x", body="y" * 20001)):
        with pytest.raises(BoardError):
            store.create("ev", **bad)
    with pytest.raises(BoardError):
        store.create("  ", "note", "x")
    assert clean_tags(["A b", "c"]) == ["a", "b", "c"] and clean_tags(None) == []


def test_store_reopens_existing_tables(tmp_path):
    a = BoardStore(tmp_path / "c.sqlite")
    pid = a.create("ev", "note", "kept")["id"]
    assert BoardStore(tmp_path / "c.sqlite").get(pid)["title"] == "kept"  # schema creation is idempotent


def test_edit_delete_and_replies(store):
    p = store.create("ev", "request", "Charge glasses", "both pairs")
    r1 = store.add_reply(p["id"], "ada", "on it", author_name="Ada")
    store.add_reply(p["id"], "ev", "thanks")
    got = store.get(p["id"])
    assert [r["body"] for r in got["replies"]] == ["on it", "thanks"] and got["n_replies"] == 2
    assert got["replies"][0]["author_name"] == "Ada" and got["activity_at"] > got["updated_at"]

    e = store.edit(p["id"], title="Charge all glasses", tags="power")
    assert e["title"] == "Charge all glasses" and e["body"] == "both pairs" and e["tags"] == ["power"] and e["edited"]
    with pytest.raises(BoardError):
        store.edit(p["id"], author="mallory")
    with pytest.raises(KeyError):
        store.edit(999, title="x")
    with pytest.raises(BoardError):
        store.add_reply(p["id"], "ev", "   ")
    with pytest.raises(KeyError):
        store.add_reply(999, "ev", "hello")

    assert store.delete_reply(r1["id"]) and not store.delete_reply(r1["id"])
    assert store.get(p["id"])["n_replies"] == 1
    assert store.delete(p["id"]) and not store.delete(p["id"])
    assert store.get(p["id"]) is None and store.list() == []
    with store._con() as con:  # replies go with their post
        assert con.execute("SELECT COUNT(*) FROM board_replies").fetchone()[0] == 0


def test_state_and_counts(store):
    q = store.create("ev", "question", "q1")
    store.create("ev", "question", "q2")
    store.create("ev", "request", "r1")
    i = store.create("ev", "issue", "i1")
    n = store.create("ev", "note", "n1")
    store.create("ev", "status", "s1")
    assert store.counts() == {"question": 2, "request": 1, "issue": 1, "open": 4}

    d = store.set_state(q["id"], "done", by="Ada")
    assert d["state"] == "done" and d["closed_by"] == "Ada" and d["closed_at"]
    assert d["updated_at"] == q["updated_at"]  # marking done is not an edit
    store.set_state(i["id"], "done")
    assert store.counts() == {"question": 1, "request": 1, "issue": 0, "open": 2}
    assert store.kind_counts()["all"] == 6 and store.kind_counts("done") == {
        "announcement": 0, "note": 0, "question": 1, "request": 0, "issue": 1, "status": 0, "all": 2}

    o = store.set_state(q["id"], "open")
    assert o["state"] == "open" and o["closed_by"] is None and o["closed_at"] is None
    with pytest.raises(BoardError):
        store.set_state(n["id"], "done")  # notes have no done state
    with pytest.raises(BoardError):
        store.set_state(q["id"], "maybe")
    store.set_state(q["id"], "done", by="Ada")
    assert store.edit(q["id"], kind="note")["state"] == "open"  # no longer something that can be done


def test_ordering_pins_and_reply_activity(store):
    a = store.create("ev", "note", "A")["id"]
    b = store.create("ev", "note", "B")["id"]
    c = store.create("ev", "note", "C")["id"]
    ids = lambda **k: [p["id"] for p in store.list(**k)]  # noqa: E731
    assert ids() == [c, b, a]
    store.add_reply(a, "ada", "bump")            # a reply is activity
    assert ids() == [a, c, b]
    store.set_pinned(b, True)                    # pins beat activity
    assert ids() == [b, a, c] and store.get(b)["pinned"] is True
    store.edit(c, body="more")                   # so is an edit
    assert ids() == [b, c, a]
    assert [p["id"] for p in store.recent(2)] == [c, a]  # recent() ignores pins
    assert "replies" not in store.recent(1)[0]
    store.set_pinned(b, False)
    assert ids() == [c, a, b]
    assert ids(limit=2) == [c, a] and ids(limit=2, offset=2) == [b]
    with pytest.raises(KeyError):
        store.set_pinned(999, True)


def test_filters_and_search(store):
    q = store.create("ev", "question", "Reflash GLS03?", "the 100% battery_level thing", session_id="S1", device="GLS03")["id"]
    n = store.create("olga", "note", "Cuff drift", "seen on S2", session_id="S2", tags="cuff")["id"]
    i = store.create("ev", "issue", "Broker offline", device="gls03")["id"]
    store.add_reply(n, "ada", "recalibrated the monitor")
    store.set_state(i, "done")
    ids = lambda **k: sorted(p["id"] for p in store.list(**k))  # noqa: E731
    assert ids(kind="question") == [q] and ids(state="done") == [i] and ids(state="open") == [q, n]
    assert ids(session_id="S1") == [q] and ids(session_id="S") == []       # exact match
    assert ids(device="GLS03") == [q, i]                                     # case-insensitive
    assert ids(q="cuff") == [n] and ids(q="RECALIBRATED") == [n]             # tags, replies, any case
    assert ids(q="olga") == [n] and ids(q="gls03 reflash") == [q]            # author; every word must match
    assert ids(q="100%") == [q] and ids(q="%") == [q] and ids(q="battery_level") == [q] and ids(q="batteryXlevel") == []
    assert ids(kind="issue", state="open") == []
    assert store.count() == 3 and store.count(device="gls03") == 2 and store.count(q="cuff") == 1


# ---------------------------------------------------------------- web
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from sglab.settings import load_settings  # noqa: E402


@pytest.fixture
def server(tmp_path, clock):
    """App with accounts: ada (admin), olga + otto (operators), vic (viewer)."""
    from sglab.auth import AuthStore
    from sglab.web.app import create_app

    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(f'[auth]\nenabled = true\n[pki]\ndir = "{tmp_path / "pki"}"\n')
    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True, exist_ok=True)
    a = AuthStore(s.db_path)
    for u, role in (("ada", "admin"), ("olga", "operator"), ("otto", "operator"), ("vic", "viewer")):
        a.add_user(u, PW, role, display=u.capitalize())
    app = create_app(s)
    return app, {u: login(app, u) for u in ("ada", "olga", "otto", "vic")}


@pytest.fixture
def local(tmp_path, clock):
    """Single-user app (no accounts)."""
    from sglab.web.app import create_app

    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").unlink(missing_ok=True)
    s = load_settings(tmp_path)
    return TestClient(create_app(s))


def login(app, user):
    c = TestClient(app, headers=ORIGIN)
    r = c.post("/login", data={"username": user, "password": PW, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def post(c, **data):
    r = c.post("/api/board", json={"kind": "note", "title": "hello", **data})
    assert r.status_code == 201, r.text
    return r.json()


def test_api_login_and_roles(server):
    app, u = server
    anon = TestClient(app)
    assert anon.get("/api/board").status_code == 401 and anon.get("/api/board/summary").status_code == 401
    assert anon.get("/board", follow_redirects=False).status_code == 303

    # viewers read, operators and above write
    assert u["vic"].get("/board").status_code == 200 and u["vic"].get("/api/board").json()["can_post"] is False
    assert u["vic"].post("/api/board", json={"kind": "note", "title": "x"}).status_code == 403
    p = post(u["olga"], kind="question", title="Who has the cuff?", body="need it at 3")
    assert p["author"] == "olga" and p["author_name"] == "Olga" and p["url"] == f"/board/{p['id']}"
    assert p["kind_label"] == "Question" and p["state_label"] == "needs an answer"
    assert p["can"] == {"edit": True, "delete": True, "pin": False, "state": True, "reply": True}
    assert u["vic"].post(f"/api/board/{p['id']}/replies", json={"body": "me"}).status_code == 403
    assert u["vic"].get(f"/api/board/{p['id']}").json()["can"] == {
        "edit": False, "delete": False, "pin": False, "state": False, "reply": False}
    assert "needs an operator account" in u["vic"].get("/board").text.replace("<b>", "").replace("</b>", "")
    assert 'id="composer"' not in u["vic"].get("/board").text and 'id="composer"' in u["olga"].get("/board").text

    # the typed author is ignored when there are accounts
    assert post(u["otto"], author="ada")["author"] == "otto"
    # cookie-authenticated writes must come from our own pages
    assert u["olga"].post("/api/board", json={"kind": "note", "title": "x"},
                          headers={"Origin": "http://evil.example"}).status_code == 403
    # bad input -> a readable message, not a stack trace
    for bad in ({"kind": "rant", "title": "x"}, {"kind": "note", "title": "", "body": ""},
                {"kind": "note", "title": "x", "session_id": "no_such_session"}):
        r = u["olga"].post("/api/board", json=bad)
        assert r.status_code == 400 and r.json()["error"], bad
    assert u["olga"].post("/api/board", content="not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert u["olga"].get("/api/board/999").status_code == 404 and u["olga"].get("/board/999").status_code == 404
    assert u["olga"].get("/api/board?kind=rant").status_code == 400


def test_api_author_vs_other_vs_admin(server):
    app, u = server
    p = post(u["olga"], kind="request", title="Charge glasses")
    pid = p["id"]
    url = f"/api/board/{pid}"

    # someone else: no edit / delete / pin, but an operator may mark it done and reply
    assert u["otto"].patch(url, json={"title": "hacked"}).status_code == 403
    assert u["otto"].delete(url).status_code == 403
    assert u["otto"].patch(url, json={"pinned": True}).status_code == 403
    assert u["olga"].patch(url, json={"pinned": True}).status_code == 403   # not even the author
    assert u["vic"].patch(url, json={"state": "done"}).status_code == 403
    # a mixed change with one forbidden part changes nothing
    assert u["otto"].patch(url, json={"state": "done", "title": "hacked"}).status_code == 403
    assert u["olga"].get(url).json()["state"] == "open"
    d = u["otto"].patch(url, json={"state": "done"}).json()
    assert d["state"] == "done" and d["closed_by"] == "Otto" and d["state_label"] == "done"
    assert u["olga"].patch(url, json={"state": "open"}).json()["closed_by"] is None

    r = u["otto"].post(f"{url}/replies", json={"body": "done by noon"})
    assert r.status_code == 201
    rid = r.json()["replies"][0]["id"]
    assert r.json()["replies"][0]["can_delete"] is True and r.json()["replies"][0]["author_name"] == "Otto"
    assert u["olga"].get(url).json()["replies"][0]["can_delete"] is False
    assert u["olga"].delete(f"/api/board/replies/{rid}").status_code == 403      # the post's author is not the reply's
    assert u["otto"].post(f"{url}/replies", json={"body": "  "}).status_code == 400
    assert u["otto"].post("/api/board/999/replies", json={"body": "x"}).status_code == 404
    assert u["otto"].delete(f"/api/board/replies/{rid}").json()["n_replies"] == 0
    assert u["otto"].delete(f"/api/board/replies/{rid}").status_code == 404

    # the author edits their own post
    e = u["olga"].patch(url, json={"title": "Charge both glasses", "body": "before 9", "tags": "power", "kind": "note"})
    assert e.status_code == 200 and e.json()["title"] == "Charge both glasses" and e.json()["edited"] is True
    assert e.json()["kind"] == "note" and e.json()["state_label"] is None and e.json()["can"]["state"] is False
    assert u["olga"].patch(url, json={"state": "done"}).status_code == 400       # notes cannot be done

    # the admin can do everything to anyone's post
    rid = u["otto"].post(f"{url}/replies", json={"body": "noted"}).json()["replies"][0]["id"]
    a = u["ada"].patch(url, json={"pinned": True, "title": "Charge ALL glasses"}).json()
    assert a["pinned"] is True and a["title"] == "Charge ALL glasses" and a["author"] == "olga"
    assert a["can"] == {"edit": True, "delete": True, "pin": True, "state": False, "reply": True}
    assert u["ada"].delete(f"/api/board/replies/{rid}").status_code == 200
    assert u["ada"].delete(url).json() == {"ok": True}
    assert u["olga"].get(url).status_code == 404 and u["ada"].delete(url).status_code == 404

    # the author deletes their own; everything was audited
    mine = post(u["otto"])["id"]
    assert u["otto"].delete(f"/api/board/{mine}").status_code == 200
    actions = {a["action"] for a in app.state.ctx.auth.audit_log()}
    assert {"board.post", "board.edit", "board.state", "board.pin", "board.reply", "board.reply_delete",
            "board.delete"} <= actions


def test_api_list_filters_ordering_and_summary(server):
    app, u = server
    from sglab.web.board import store as board_store
    b = board_store(app.state.ctx)
    a = post(u["olga"], kind="question", title="Reflash GLS03?", device="GLS03", tags="firmware")["id"]
    n = post(u["otto"], kind="note", title="Cuff drift", body="saw 5 mmHg offset")["id"]
    i = post(u["olga"], kind="issue", title="Broker offline")["id"]
    # (the API only links recordings that exist; put one in directly)
    s = b.create("olga", "note", "About that recording", session_id="S001_rest_1", author_name="Olga")["id"]

    def ids(query=""):
        r = u["vic"].get("/api/board" + query)
        assert r.status_code == 200, r.text
        return [p["id"] for p in r.json()["posts"]]

    assert ids() == [s, i, n, a]
    u["otto"].post(f"/api/board/{a}/replies", json={"body": "yes, v0.9 is out"})
    assert ids() == [a, s, i, n]                                  # reply activity
    u["ada"].patch(f"/api/board/{n}", json={"pinned": True})
    assert ids() == [n, a, s, i]                                  # pinned first
    u["olga"].patch(f"/api/board/{i}", json={"state": "done"})

    assert ids("?kind=question") == [a] and ids("?state=open") == [n, a, s] and ids("?state=done") == [i]
    assert ids("?session=S001_rest_1") == [s] and ids("?session=nope") == []
    assert ids("?device=gls03") == [a] and ids("?q=mmHg") == [n] and ids("?q=v0.9") == [a]
    assert ids("?limit=2") == [n, a] and ids("?limit=2&offset=2") == [s, i]

    d = u["vic"].get("/api/board?session=S001_rest_1").json()
    assert d["total"] == 1 and d["limit"] == 50 and d["offset"] == 0 and d["can_post"] is False
    assert d["counts"] == {"question": 1, "request": 0, "issue": 0, "open": 1}
    item = d["posts"][0]
    assert set(item) >= {"id", "url", "kind", "kind_label", "title", "body", "author", "author_name", "created_at",
                         "updated_at", "edited", "activity_at", "session_id", "device", "tags", "pinned", "state",
                         "state_label", "closed_by", "closed_at", "n_replies", "replies", "can"}
    assert item["session_id"] == "S001_rest_1" and item["replies"] == []

    sm = u["vic"].get("/api/board/summary").json()
    assert set(sm) == {"counts", "recent"} and sm["counts"] == {"question": 1, "request": 0, "issue": 0, "open": 1}
    assert [p["id"] for p in sm["recent"]] == [a, s, i, n]        # newest activity, pins ignored
    assert "replies" not in sm["recent"][0] and sm["recent"][0]["n_replies"] == 1
    for k in range(3):
        post(u["olga"], title=f"more {k}")
    assert len(u["vic"].get("/api/board/summary").json()["recent"]) == 5


def test_page_escapes_and_links(server):
    app, u = server
    evil = '<script>alert("x")</script>'
    p = post(u["olga"], kind="issue", title=f"title {evil}", device='GLS"03',
             body=f"line one {evil}\nsee https://example.org/a?b=1&c=2. and <b>bold</b>\njavascript:alert(1)")
    u["otto"].post(f"/api/board/{p['id']}/replies", json={"body": f"reply {evil} http://x.test/<img>"})
    for url in ("/board", f"/board/{p['id']}"):
        html = u["olga"].get(url).text
        assert "<script>alert" not in html and "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in html, url
        assert "<b>bold</b>" not in html and "<img>" not in html
        assert '<a href="https://example.org/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">' in html
        assert 'href="javascript:' not in html and '<a href="http://x.test/"' in html
        assert 'data-device="GLS&#34;03"' in html
        assert f'id="post-{p["id"]}"' in html and "not fixed yet" in html and 'data-act="state"' in html
    assert u["vic"].get("/board").status_code == 200 and 'data-act="state"' not in u["vic"].get("/board").text

    # filters on the page; finished posts are hidden until asked for
    u["olga"].patch(f"/api/board/{p['id']}", json={"state": "done"})
    note = post(u["olga"], title="plain note")
    assert f'id="post-{p["id"]}"' not in u["vic"].get("/board").text
    assert f'id="post-{p["id"]}"' in u["vic"].get("/board?state=all").text
    assert f'id="post-{note["id"]}"' not in u["vic"].get("/board?kind=issue&state=all").text
    assert "No posts match" in u["vic"].get("/board?q=zzzz").text
    assert u["vic"].get("/board?kind=rant&state=weird&page=-3").status_code == 200


def test_page_session_filter_and_empty_state(server):
    app, u = server
    from sglab.web.board import store as board_store
    assert "Nothing here yet" in u["olga"].get("/board").text
    b = board_store(app.state.ctx)
    s = b.create("olga", "note", "About that recording", session_id="S001_rest_1")["id"]
    other = post(u["olga"], title="unrelated")["id"]
    html = u["vic"].get("/board?session=S001_rest_1").text
    assert f'id="post-{s}"' in html and f'id="post-{other}"' not in html
    assert 'href="/sessions/S001_rest_1"' in html and "Nothing here yet" not in html


def test_single_user_mode(local):
    c = local
    assert c.get("/board").status_code == 200 and "Your name or initials" in c.get("/board").text
    r = c.post("/api/board", json={"kind": "note", "title": "no name"})
    assert r.status_code == 400 and "name" in r.json()["error"]
    p = c.post("/api/board", json={"kind": "question", "title": "Where is the charger?", "author": "EV"}).json()
    assert p["author"] == p["author_name"] == "EV"
    assert p["can"] == {"edit": True, "delete": True, "pin": True, "state": True, "reply": True}  # local admin
    url = f"/api/board/{p['id']}"
    assert c.post(f"{url}/replies", json={"body": "top drawer"}).status_code == 400            # name needed here too
    r = c.post(f"{url}/replies", json={"body": "top drawer", "author": "MK"}).json()
    assert r["replies"][0]["author_name"] == "MK" and r["replies"][0]["can_delete"] is True
    d = c.patch(url, json={"state": "done", "pinned": True, "author": "MK"}).json()
    assert d["state"] == "done" and d["closed_by"] == "MK" and d["pinned"] is True
    assert c.get("/api/board/summary").json()["counts"]["open"] == 0
    html = c.get(f"/board/{p['id']}").text
    assert "Where is the charger?" in html and "top drawer" in html and "answered" in html
    assert c.delete(url).status_code == 200 and c.get("/api/board").json()["posts"] == []
