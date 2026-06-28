import os, re, json, time, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
SITES_FILE = ROOT / "sites.json"
STATE_FILE = DATA / "state.json"
REPORT_FILE = DATA / "last_report.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TEST_TELEGRAM = os.getenv("TEST_TELEGRAM", "false").lower() == "true"
REMIND_HOURS = int(os.getenv("REMIND_HOURS", "6"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7,de;q=0.6,it;q=0.5,es;q=0.5",
}

PRODUCT_WORDS = ["midea", "portasplit", "porta split", "porta-split", "12000", "12.000", "12000 btu", "3,5 kw", "3.5 kw", "3500w", "3500 w"]
BAD_WORDS = ["tuyau", "hose", "window kit", "kit fenêtre", "accessoire", "telecommande", "télécommande", "remote", "filtre", "support"]
AVAILABLE_WORDS = [
    "ajouter au panier", "add to basket", "add to cart", "in den warenkorb", "aggiungi al carrello", "añadir al carrito",
    "en stock", "disponible", "auf lager", "lieferbar", "available", "in stock", "prêt à expédier", "expédié"
]
UNAVAILABLE_WORDS = [
    "rupture", "épuisé", "indisponible", "non disponible", "out of stock", "sold out", "nicht verfügbar", "ausverkauft",
    "derzeit nicht", "momentanément indisponible", "bientôt disponible", "me prévenir", "notify me"
]
DELIVERY_FR_WORDS = ["livraison", "livraison à domicile", "france", "expédié", "versand", "lieferung", "shipping", "delivery", "lieferbar"]
DELIVERY_BAD_WORDS = ["pas de livraison", "ne livre pas", "not deliver", "no delivery", "pickup only", "retrait uniquement"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def norm(txt):
    txt = txt.lower()
    txt = txt.replace("\xa0", " ")
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def hits(text, words):
    return [w for w in words if w in text]


def price_from(text):
    m = re.search(r"(\d{2,4}(?:[\s.,]\d{2})?)\s?€", text)
    return m.group(0).replace(" ", "") if m else ""


def fingerprint(r):
    base = f"{r['status']}|{r['price']}|{r['alertable']}|{r['product_ok']}|{r['delivery_status']}"
    return hashlib.sha256(base.encode()).hexdigest()


def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("ERREUR: Telegram non configuré. Ajoute TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans GitHub Secrets.")
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        res = requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": False}, timeout=20)
        print(f"Telegram status: {res.status_code}")
        print(res.text[:800])
        return res.status_code == 200
    except Exception as e:
        print(f"Erreur Telegram: {e}")
        return False


def alert_message(r):
    return (
        "🚨 <b>Midea PortaSplit disponible possible</b>\n\n"
        f"🏬 <b>{r['name']}</b> ({r['country']})\n"
        f"💶 Prix: {r.get('price') or 'à vérifier'}\n"
        f"🚚 Livraison: {r.get('delivery_status')}\n"
        f"📊 Score: {r.get('score')}\n"
        f"🔗 {r['url']}\n\n"
        f"⏱ {now_iso()}"
    )


def check_site(site):
    r = {
        "id": site["id"], "name": site["name"], "country": site.get("country", ""), "url": site["url"],
        "checked_at": now_iso(), "status": "error", "alertable": False, "product_ok": False,
        "delivery_status": "unknown", "score": 0, "price": "", "hits": {}, "error": ""
    }
    try:
        resp = requests.get(site["url"], headers=HEADERS, timeout=25, allow_redirects=True)
        r["http_status"] = resp.status_code
        html = resp.text[:900000]
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = norm(soup.get_text(" "))
        title = norm(soup.title.get_text(" ") if soup.title else "")
        combined = f"{title} {text}"

        product_hits = hits(combined, PRODUCT_WORDS)
        bad_hits = hits(combined, BAD_WORDS)
        available_hits = hits(combined, AVAILABLE_WORDS)
        unavailable_hits = hits(combined, UNAVAILABLE_WORDS)
        delivery_hits = hits(combined, DELIVERY_FR_WORDS)
        delivery_bad_hits = hits(combined, DELIVERY_BAD_WORDS)
        price = price_from(combined)

        # Product OK needs Midea + PortaSplit/porta split + power hint OR exact known ASIN/product page.
        product_ok = ("midea" in product_hits and any(x in product_hits for x in ["portasplit", "porta split", "porta-split"]) and (len(product_hits) >= 3))
        if "B0CY2YW8BT" in site["url"]:
            product_ok = True if ("midea" in combined or "portasplit" in combined or resp.status_code in (200, 301, 302)) else product_ok

        score = 0
        score += 4 if product_ok else -10
        score += 3 if available_hits else 0
        score -= 6 if unavailable_hits else 0
        score += 1 if delivery_hits else 0
        score -= 4 if delivery_bad_hits else 0
        score -= 8 if bad_hits and not product_ok else 0
        score += 1 if price else 0

        if delivery_bad_hits:
            delivery_status = "negative"
        elif delivery_hits:
            delivery_status = "positive"
        elif site.get("country") == "FR":
            delivery_status = "assumed_ok"
        else:
            delivery_status = "unknown"

        if product_ok and available_hits and not unavailable_hits and delivery_status != "negative":
            status = "available"
        elif product_ok and unavailable_hits:
            status = "unavailable"
        elif product_ok:
            status = "unknown"
        else:
            status = "wrong_product_or_unclear"

        alertable = status == "available" and product_ok and delivery_status in ["positive", "assumed_ok", "unknown"]

        r.update({
            "status": status, "alertable": alertable, "product_ok": product_ok,
            "delivery_status": delivery_status, "score": score, "price": price,
            "hits": {"product": product_hits, "available": available_hits, "unavailable": unavailable_hits, "delivery": delivery_hits, "bad": bad_hits},
        })
    except Exception as e:
        r["error"] = repr(e)
    r["fingerprint"] = fingerprint(r)
    return r


def should_alert(result, previous):
    if not result.get("alertable"):
        return False, "not_alertable"
    prev = previous.get(result["id"], {})
    if prev.get("fingerprint") != result.get("fingerprint"):
        return True, "new_or_changed"
    last_alert = prev.get("last_alert")
    if last_alert:
        try:
            t = datetime.fromisoformat(last_alert)
            if datetime.now(timezone.utc) - t >= timedelta(hours=REMIND_HOURS):
                return True, "reminder"
        except Exception:
            return True, "bad_previous_date"
    else:
        return True, "no_previous_alert_date"
    return False, "already_alerted_recently"


def main():
    if TEST_TELEGRAM:
        ok = send_telegram("✅ Test Telegram OK - Midea Stock Alert")
        raise SystemExit(0 if ok else 1)

    sites = load_json(SITES_FILE, [])
    state = load_json(STATE_FILE, {})
    results, alerts_sent = [], []

    print(f"Début vérification Midea PortaSplit - {len(sites)} sites")
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(check_site, s) for s in sites]
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            print(f"- {res['name']}: {res['status']} | produit={res['product_ok']} | livraison={res['delivery_status']} | alertable={res['alertable']} | prix={res.get('price') or '-'} | score={res['score']} | erreur={res.get('error','')[:120]}")

    results.sort(key=lambda x: x["id"])
    for r in results:
        do_alert, reason = should_alert(r, state)
        r["alert_decision"] = reason
        if do_alert:
            if send_telegram(alert_message(r)):
                alerts_sent.append(r["id"])
                state.setdefault(r["id"], {})["last_alert"] = now_iso()
        state[r["id"]] = {**state.get(r["id"], {}), "status": r["status"], "product_ok": r["product_ok"], "delivery_status": r["delivery_status"], "price": r["price"], "score": r["score"], "alertable": r["alertable"], "fingerprint": r["fingerprint"], "last_check": r["checked_at"]}

    report = {"checked_at": now_iso(), "sites_count": len(sites), "alerts_sent": alerts_sent, "results": results}
    save_json(STATE_FILE, state)
    save_json(REPORT_FILE, report)
    print(f"Alertes envoyées: {alerts_sent}")

if __name__ == "__main__":
    main()
