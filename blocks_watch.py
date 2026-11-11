# Блокировки WB + Ozon → отдельный Telegram-бот. Логика проверки — как в 3_Скрипты/full_check.py (только чтение).
# Шлёт новые и снятые блокировки/скрытия; раз в день после summary_hour — сводку по кабинетам. Запуск: python3 -I blocks_watch.py
import collections, datetime, json, os, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import tg

WB = json.load(open(os.path.join(BASE, "tokens.json"), encoding="utf-8"))
OZ = json.load(open(os.path.join(BASE, "ozon_keys.json"), encoding="utf-8"))
STATE = os.path.join(BASE, "blocks_state.json")
BLOCK_IDS = {3, 127, 98}  # нельзя продавать / штраф / запрос госоргана
RKN = ("Роскомнадзор", "правообладател", "госорган")
SUMMARY_HOUR = 13
NOW = datetime.datetime.now()


def http(method, url, headers, body=None):
    for i in range(8):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                                                headers={**headers, "Content-Type": "application/json"}), timeout=120).read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 + 5 * i); continue
            return {"ERR": e.code, "body": e.read()[:200].decode("utf-8", "replace")}
        except Exception:
            time.sleep(3)
    return {"ERR": "retry"}


def ozon(name):
    k = OZ[name]; h = {"Client-Id": k["client_id"], "Api-Key": k["api_key"]}; out = []; items = []; last = ""
    while True:
        r = http("POST", "https://api-seller.ozon.ru/v3/product/list", h, {"filter": {"visibility": "ALL"}, "last_id": last, "limit": 1000})
        if "result" not in r:
            return None
        items += r["result"]["items"]; last = r["result"].get("last_id")
        if not last or len(r["result"]["items"]) < 1000:
            break
    act = [i for i in items if not i.get("archived")]
    for i in range(0, len(act), 100):
        info = http("POST", "https://api-seller.ozon.ru/v3/product/info/list", h, {"product_id": [x["product_id"] for x in act[i:i + 100]]})
        for x in info.get("items") or []:
            av = x.get("availabilities") or []; st = x.get("statuses") or {}
            reasons = [(rr["id"], rr["human_text"]["text"]) for a in av if a.get("availability") != "AVAILABLE" for rr in a.get("reasons") or []]
            hidden = any(a.get("availability") != "AVAILABLE" for a in av)
            declined = st.get("moderate_status") == "declined"
            if not (hidden or declined):
                continue
            why = "; ".join(sorted(set(r[1] for r in reasons))) or st.get("status_description") or ""
            kind = "Блокировка" if (declined or any(r[0] in BLOCK_IDS for r in reasons) or any(s in why for s in RKN)) else "Скрыта"
            out.append({"mp": "Ozon", "cab": name, "art": x["offer_id"], "id": str(x.get("sku")), "kind": kind, "why": why[:200]})
        time.sleep(0.3)
    return out


def wb(cab):
    h = {"Authorization": WB[cab]}; out = []
    bl = http("GET", "https://seller-analytics-api.wildberries.ru/api/v1/analytics/banned-products/blocked?sort=nmId&order=desc", h)
    if bl.get("ERR"):
        return None
    d1 = (datetime.date.today() - datetime.timedelta(days=1)).isoformat(); d0 = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    sh = http("POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v2/item-rating", h,
              {"currentPeriod": {"start": d0, "end": d1}, "onlyShadowedNms": True, "orderBy": {"field": "feedbackCount", "mode": "desc"}, "limit": 1000, "offset": 0})
    seen = collections.defaultdict(list)
    for x in bl.get("report") or []:
        seen[x["nmId"]].append(x)
    for nm, xs in seen.items():
        out.append({"mp": "WB", "cab": cab, "art": xs[0]["vendorCode"], "id": str(nm), "kind": "Блокировка", "why": "; ".join(x["reason"][:160] for x in xs)})
    for it in ((sh.get("data") or {}).get("items") or []):
        nm = it.get("nmId") or it.get("nmID")
        out.append({"mp": "WB", "cab": cab, "art": it.get("vendorCode"), "id": str(nm), "kind": "Скрыта", "why": "скрыта из каталога"})
    return out


def main():
    with ThreadPoolExecutor(8) as ex:
        jobs = {("Ozon", n): ex.submit(ozon, n) for n in OZ}
        jobs.update({("WB", c): ex.submit(wb, c) for c in WB})
    res = {k: f.result() for k, f in jobs.items()}
    failed = [f"{mp} {c}" for (mp, c), v in res.items() if v is None]
    rows = [r for v in res.values() if v for r in v]
    cur = {f"{r['mp']}|{r['cab']}|{r['id']}|{r['art']}": r for r in rows}
    st = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    prev = st.get("items")
    ok_cabs = {(mp, c) for (mp, c), v in res.items() if v is not None}
    if prev is None:
        cnt = collections.Counter((r["mp"], r["cab"], r["kind"]) for r in rows)
        txt = ["🛡 <b>Слежу за блокировками.</b> Сейчас:"] + [f"{mp} · {c}: {k.lower()} — {n}" for (mp, c, k), n in sorted(cnt.items())]
        tg.send("blocks", "\n".join(txt))
    else:
        new = [r for k, r in cur.items() if k not in prev]
        gone = [r for k, r in prev.items() if k not in cur and (r["mp"], r["cab"]) in ok_cabs]
        changed = [r for k, r in cur.items() if k in prev and prev[k]["kind"] != r["kind"]]
        for r in new:
            tg.send("blocks", f"{'🚫' if r['kind'] == 'Блокировка' else '👁'} <b>{r['kind']}</b> · {r['mp']} · {r['cab']}\n{r['art']} ({r['id']})\n{r['why']}")
        for r in changed:
            tg.send("blocks", f"🔄 {r['mp']} · {r['cab']} · {r['art']} ({r['id']}): теперь <b>{r['kind'].lower()}</b>\n{r['why']}")
        for r in gone:
            tg.send("blocks", f"✅ <b>Снято</b> · {r['mp']} · {r['cab']}\n{r['art']} ({r['id']}) — больше не {r['kind'].lower()}")
    if failed:
        tg.send("blocks", "⚠️ Не удалось проверить: " + ", ".join(failed))
    if NOW.hour >= SUMMARY_HOUR and st.get("summary_date") != str(datetime.date.today()) and prev is not None:
        cnt = collections.Counter((r["mp"], r["cab"]) for r in rows if r["kind"] == "Блокировка")
        hid = collections.Counter((r["mp"], r["cab"]) for r in rows if r["kind"] == "Скрыта")
        keys = sorted(set(cnt) | set(hid))
        txt = [f"🛡 <b>Блокировки на {NOW:%d.%m %H:%M}</b>"] + [f"{mp} · {c}: {cnt[(mp, c)]} блок., {hid[(mp, c)]} скрыто" for mp, c in keys]
        if tg.send("blocks", "\n".join(txt)):
            st["summary_date"] = str(datetime.date.today())
    # кабинеты, которые не ответили, держим в прежнем состоянии, чтобы не прислать ложное «снято»
    keep = {k: v for k, v in (prev or {}).items() if (v["mp"], v["cab"]) not in ok_cabs}
    st["items"] = {**keep, **cur}; st["time"] = NOW.isoformat(timespec="seconds")
    json.dump(st, open(STATE + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1); os.replace(STATE + ".tmp", STATE)
    os.makedirs(os.path.join(BASE, "out"), exist_ok=True)
    json.dump(rows, open(os.path.join(BASE, "out", "blocks_latest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{NOW:%Y-%m-%d %H:%M:%S} блокировки: {len(rows)} строк, не ответили: {failed or 'все ответили'}")


if __name__ == "__main__":
    main()
