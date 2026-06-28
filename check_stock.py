import asyncio, hashlib, json, os, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_PATH = DATA_DIR / "state.json"
REPORT_PATH = DATA_DIR / "last_report.json"
SITES_PATH = ROOT / "sites.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FORCE_ALERTS = os.getenv("FORCE_ALERTS", "false").lower() in {"1", "true", "yes", "oui"}
TEST_TELEGRAM = os.getenv("TEST_TELEGRAM", "false").lower() in {"1", "true", "yes", "oui"}
REALERT_HOURS = int(os.getenv("REALERT_HOURS", "6"))
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "4"))

POSITIVE = [
    "ajouter au panier", "add to cart", "add to basket", "in den warenkorb", "au panier",
    "disponible", "en stock", "in stock", "auf lager", "lieferbar", "available",
    "acheter", "commander", "buy now", "sofort lieferbar",
    "comprar", "añadir al carrito", "agregar al carrito", "disponibile", "acquista",
    "winkelwagen", "in winkelwagen", "op voorraad"
]
NEGATIVE = [
    "rupture", "épuisé", "epuise", "indisponible", "non disponible", "out of stock",
    "not available", "nicht verfügbar", "nicht verfugbar", "ausverkauft", "currently unavailable",
    "temporairement indisponible", "momentanément indisponible", "prévenez-moi", "notify me",
    "agotado", "no disponible", "sin stock", "esaurito", "non disponibile",
    "niet beschikbaar", "uitverkocht", "niet op voorraad"
]
PRODUCT_TERMS = ["midea", "portasplit", "porta split", "porta-split", "12000 btu", "12.000 btu", "3,5 kw", "3.5 kw", "3500w", "3500 w"]
EXCLUDE_TERMS = ["télécommande", "telecommande", "remote", "accessoire", "accessory", "tuyau", "hose", "filtre", "filter"]
DELIVERY_POSITIVE = ["livraison", "livré", "livraison à domicile", "expédié", "expedition", "france", "versand", "lieferung", "delivery", "envío", "entrega", "spedizione", "consegna", "bezorging", "levering"]
DELIVERY_NEGATIVE = ["retrait uniquement", "click and collect only", "nur abholung", "abholung", "pas de livraison", "sin envío", "solo recogida", "solo ritiro", "alleen afhalen"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as e:
        print(f"Impossible de lire {path}: {e}")
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").lower()).strip()


def hits(text: str, terms: List[str]) -> List[str]:
    return [t for t in terms if t in text]


def price_from(text: str) -> str:
    # Prix plausible pour un Midea PortaSplit : on ignore les faux prix type 99€, 10€, 000€
    candidates = re.findall(r"(\d{1,4}(?:[\s.,]\d{2})?)\s?(?:€|eur)", text)
    plausible = []
    for raw in candidates:
        cleaned = raw.replace(" ", "").replace(",", ".")
        try:
            value = float(cleaned)
        except Exception:
            continue
        if 350 <= value <= 2500:
            plausible.append((value, raw.replace(" ", "") + "€"))
    if not plausible:
        return ""
    # On prend le premier prix plausible trouvé sur la page
    return plausible[0][1]


def fingerprint(result: Dict[str, Any]) -> str:
    raw = "|".join(str(result.get(k, "")) for k in ["status", "price", "product_ok", "delivery_status", "alertable"])
    return hashlib.sha256(raw.encode()).hexdigest()


def evaluate(site: Dict[str, Any], text: str, enabled_buttons: int, error: str = "") -> Dict[str, Any]:
    nt = normalize(text)
    exact_hits = hits(nt, ["midea"])
    model_hits = hits(nt, ["portasplit", "porta split", "porta-split", "12000 btu", "12.000 btu", "3,5 kw", "3.5 kw", "3500w", "3500 w"])
    excluded_hits = hits(nt, EXCLUDE_TERMS)
    positive_hits = hits(nt, POSITIVE)
    negative_hits = hits(nt, NEGATIVE)
    delivery_positive = hits(nt, DELIVERY_POSITIVE)
    delivery_negative = hits(nt, DELIVERY_NEGATIVE)
    price = price_from(nt)

    product_ok = bool(exact_hits) and ("portasplit" in model_hits or "porta split" in model_hits or "porta-split" in model_hits) and len(model_hits) >= 2
    if site.get("id", "").startswith(("idealo", "geizhals", "cdiscount", "rakuten", "ebay", "leboncoin")):
        # search/comparator pages: require stricter product signal
        product_ok = bool(exact_hits) and any(x in model_hits for x in ["portasplit", "porta split", "porta-split"]) and len(model_hits) >= 2

    delivery_status = "positive" if delivery_positive and not delivery_negative else ("negative" if delivery_negative else ("assumed_ok" if site.get("delivery_fr") == "yes" else "unknown"))

    score = 0
    if product_ok: score += 5
    if positive_hits: score += 2
    if enabled_buttons: score += 2
    if delivery_status in {"positive", "assumed_ok"}: score += 1
    if negative_hits: score -= 4
    if excluded_hits: score -= 6

    if error:
        status = "error"
    elif not product_ok:
        status = "wrong_product_or_unclear"
    elif negative_hits and not enabled_buttons:
        status = "unavailable"
    elif score >= 7 and (positive_hits or enabled_buttons):
        status = "available"
    else:
        status = "unknown"

    alertable = status == "available" and product_ok and delivery_status != "negative" and not excluded_hits
    reasons = []
    if product_ok: reasons.append("produit exact probable")
    else: reasons.append("produit exact non confirmé")
    if price: reasons.append(f"prix détecté: {price}")
    if positive_hits: reasons.append("signaux dispo: " + ", ".join(positive_hits[:5]))
    if negative_hits: reasons.append("signaux rupture: " + ", ".join(negative_hits[:5]))
    if delivery_positive: reasons.append("signaux livraison: " + ", ".join(delivery_positive[:5]))
    if delivery_negative: reasons.append("signaux livraison négatifs: " + ", ".join(delivery_negative[:5]))
    if enabled_buttons: reasons.append(f"boutons achat actifs: {enabled_buttons}")
    if error: reasons.append(error[:250])

    res = {
        "id": site["id"], "name": site["name"], "country": site.get("country", ""), "url": site["url"],
        "checked_at": now_iso(), "status": status, "alertable": alertable, "product_ok": product_ok,
        "delivery_status": delivery_status, "score": score, "price": price,
        "exact_hits": exact_hits, "model_hits": model_hits, "excluded_hits": excluded_hits,
        "available_hits": positive_hits, "unavailable_hits": negative_hits,
        "delivery_positive_hits": delivery_positive, "delivery_negative_hits": delivery_negative,
        "enabled_buy_buttons": enabled_buttons, "reasons": reasons, "error": error
    }
    res["fingerprint"] = fingerprint(res)
    return res


async def fetch_site(browser, site: Dict[str, Any]) -> Dict[str, Any]:
    context = await browser.new_context(locale="fr-FR", user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36")
    page = await context.new_page()
    try:
        await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_timeout(2500)
        except Exception:
            pass
        html = await page.content()
        text = BeautifulSoup(html, "lxml").get_text(" ")
        buttons = await page.locator("button:not([disabled]), a[href]").all_text_contents()
        enabled_buttons = sum(1 for b in buttons if normalize(b) and any(p in normalize(b) for p in POSITIVE))
        return evaluate(site, text, enabled_buttons)
    except Exception as e:
        return evaluate(site, "", 0, error=repr(e))
    finally:
        await context.close()


def telegram_send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("ERREUR: Telegram non configuré. Vérifie TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=25,
        )
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text[:500])
        return r.status_code == 200
    except Exception as e:
        print("Erreur Telegram:", repr(e))
        return False


def alert_message(r: Dict[str, Any]) -> str:
    price = r.get("price") or "prix non détecté"
    return (
        "🚨 <b>Midea PortaSplit disponible</b>\n\n"
        f"🏬 <b>{r['name']}</b> ({r.get('country','')})\n"
        f"💶 {price}\n"
        f"🚚 Livraison: {r.get('delivery_status')}\n"
        f"⭐ Score: {r.get('score')}\n"
        f"🔗 {r['url']}\n\n"
        f"⏱ {r.get('checked_at')}"
    )


def should_alert(r: Dict[str, Any], previous: Dict[str, Any]) -> bool:
    if not r.get("alertable"):
        return False
    if FORCE_ALERTS:
        return True
    if not previous:
        return True
    if previous.get("fingerprint") != r.get("fingerprint"):
        return True
    last_alert = previous.get("last_alert")
    if last_alert:
        try:
            dt = datetime.fromisoformat(last_alert)
            return datetime.now(timezone.utc) - dt >= timedelta(hours=REALERT_HOURS)
        except Exception:
            return True
    return True


async def main():
    sites = load_json(SITES_PATH, [])
    state = load_json(STATE_PATH, {})

    if TEST_TELEGRAM:
        ok = telegram_send("✅ Test Telegram OK - Midea Stock Alert V11 Pro Sites")
        print("Test Telegram:", ok)
        return 0 if ok else 1

    print(f"Démarrage surveillance Midea PortaSplit V11 Pro - {len(sites)} sites")
    if not BOT_TOKEN or not CHAT_ID:
        print("ATTENTION: Telegram non configuré. Le script vérifie les sites mais ne pourra pas envoyer d'alerte.")

    sem = asyncio.Semaphore(MAX_PARALLEL)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        async def guarded(site):
            async with sem:
                print(f"Check: {site['name']}")
                return await fetch_site(browser, site)
        results = await asyncio.gather(*(guarded(s) for s in sites))
        await browser.close()

    alerts_sent = []
    for r in results:
        prev = state.get(r["id"], {})
        if should_alert(r, prev):
            if telegram_send(alert_message(r)):
                alerts_sent.append(r["id"])
                r["last_alert"] = now_iso()
            else:
                print(f"Alerte non envoyée pour {r['id']}")
                r["last_alert"] = prev.get("last_alert")
        else:
            r["last_alert"] = prev.get("last_alert")
        state[r["id"]] = {k: r.get(k) for k in ["status", "product_ok", "delivery_status", "price", "score", "alertable", "fingerprint", "checked_at", "last_alert"]}

    report = {"checked_at": now_iso(), "sites_count": len(sites), "alerts_sent": alerts_sent, "results": results}
    save_json(REPORT_PATH, report)
    save_json(STATE_PATH, state)

    print("\nRésumé:")
    for r in results:
        print(f"- {r['name']}: {r['status']} | produit={r['product_ok']} | livraison={r['delivery_status']} | alertable={r['alertable']} | score={r['score']} | prix={r.get('price') or '-'} | erreur={r.get('error') or '-'}")
    print("\nAlertes envoyées:", alerts_sent)
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
