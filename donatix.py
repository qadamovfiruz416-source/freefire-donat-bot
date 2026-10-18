import asyncio
import os

import httpx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://donatix.tj/api/v1"

# рамзи бастаҳои Free Fire (СНГ) дар Donatix
PRODUCTS = {
    "a100": "topup-82e660f5e5",  # 110 Diamonds
    "a310": "topup-c6258ebfb2",  # 341 Diamonds
    "a520": "topup-59f4f54730",  # 572 Diamonds
    "v0": "topup-15c5b26dba",    # Weekly Lite
    "v1": "topup-8e6e0598f8",    # Weekly Membership
    "v2": "topup-b77065f769",    # Monthly Membership
}
PLAYER_ERRORS = {"player_not_found", "account_not_found"}


def load_env():
    path = os.path.join(BASE_DIR, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(
                        k.strip(), v.strip().strip('"').strip("'")
                    )
    except FileNotFoundError:
        pass


load_env()
KEY = os.environ.get("DONATIX_KEY", "")


def _headers(extra=None):
    h = {"X-API-Key": KEY, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


async def check_player(pack_key, gid):
    """Бармегардонад: ("ok", ном) | ("bad", None) | ("unknown", None)."""
    pid = PRODUCTS.get(pack_key)
    if not pid or not KEY:
        return ("unknown", None)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                BASE_URL + "/accounts/check",
                headers=_headers(),
                json={"product_id": pid, "fields": {"player_id": gid}},
            )
        d = r.json()
    except Exception:
        return ("unknown", None)
    if not d.get("ok"):
        if d.get("code") in PLAYER_ERRORS:
            return ("bad", None)
        return ("unknown", None)
    if d.get("supported") is False:
        return ("unknown", None)
    if d.get("valid") is False:
        return ("bad", None)
    if d.get("valid") is True:
        return ("ok", d.get("player_name") or "")
    return ("unknown", None)


async def auto_donate(db, oid):
    """Бармегардонад: ("done", None) | ("player", хато) | ("manual", сабаб)."""
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    pid = PRODUCTS.get(o["pack_key"]) if o["pack_key"] else None
    if not pid:
        return ("manual", "рамзи Donatix барои ин баста нест")
    if not KEY:
        return ("manual", "калиди Donatix нест")

    did = o["donatix_id"]
    async with httpx.AsyncClient(timeout=20) as c:
        if not did:
            # калиди ягона: такрори дархост пулро ду маротиба намегирад
            idem = "ffbot-%s-%s" % (oid, o["attempt"])
            body = {
                "product_id": pid,
                "quantity": 1,
                "fields": {"player_id": o["gid"]},
            }
            data = None
            for _ in range(3):
                try:
                    r = await c.post(
                        BASE_URL + "/orders",
                        headers=_headers({"Idempotency-Key": idem}),
                        json=body,
                    )
                    if r.status_code >= 500:
                        raise RuntimeError("server error")
                    data = r.json()
                    break
                except Exception:
                    await asyncio.sleep(3)
            if data is None:
                return (
                    "manual",
                    "Donatix ҷавоб надод. Дар панели Donatix санҷед (%s)" % idem,
                )
            if not data.get("ok"):
                code = data.get("code") or ""
                err = data.get("error") or code or "хато"
                if code in PLAYER_ERRORS:
                    return ("player", err)
                db.execute(
                    "UPDATE orders SET attempt=attempt+1 WHERE id=?", (oid,)
                )
                db.commit()
                return ("manual", "Donatix: %s" % err)
            did = (data.get("order") or {}).get("order_id")
            if not did:
                return (
                    "manual",
                    "Donatix ҷавоби номаълум дод. Дар панел санҷед (%s)" % idem,
                )
            db.execute("UPDATE orders SET donatix_id=? WHERE id=?", (did, oid))
            db.commit()

        # интизори натиҷа (то тақрибан 8 дақиқа)
        for _ in range(60):
            await asyncio.sleep(8)
            try:
                r = await c.get(BASE_URL + "/orders/" + did, headers=_headers())
                d = r.json().get("order") or {}
            except Exception:
                continue
            st = d.get("status")
            if st == "completed":
                return ("done", None)
            if st == "failed":
                db.execute(
                    "UPDATE orders SET donatix_id=NULL, attempt=attempt+1 "
                    "WHERE id=?",
                    (oid,),
                )
                db.commit()
                return (
                    "manual",
                    "Donatix: иҷро нашуд (%s). Пул ба баланси Donatix баргашт."
                    % (d.get("error") or ""),
                )
    return (
        "manual",
        "Donatix ҳанӯз коркард мекунад (%s). Дар панел санҷед ва аз нав кӯшиш кунед."
        % did,
    )
