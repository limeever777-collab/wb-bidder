# Бидер рекламы WB: ДРР по кластерам (Джем) и карточкам → план ставок, пауза по дневному лимиту.
# python3 -I bidder.py                     — только план, ничего не меняет (out/plan_*.md + .json)
# python3 -I bidder.py --cab "Swiss Health" — один кабинет
# python3 -I bidder.py --apply             — записать план (только кабинеты из config.apply.cabinets)
# python3 -I bidder.py --rollback ФАЙЛ     — вернуть ставки из бэкапа
# ДРР = расход ÷ все заказы ₽ (statistics orders); ДРР кластера = расход кластера ÷ (заказы Джема × ср. чек).
import argparse, collections, datetime, fcntl, json, os, re, sys, time, urllib.error, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)  # python -I не видит соседние модули
import tg
A = "https://advert-api.wildberries.ru"
JAM = "https://seller-analytics-api.wildberries.ru/api/v2/search-report/product/search-texts"
ORDERS = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
NOW = datetime.datetime.now()
TODAY = NOW.date()


def rel(p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(BASE, p))


def load(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(obj, open(p + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def http(method, url, tok, body=None, tries=8):
    err = ""
    for i in range(tries):
        r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Authorization": tok, "Content-Type": "application/json"})
        try:
            raw = urllib.request.urlopen(r, timeout=180).read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (408, 429) or e.code >= 500:
                err = f"{e.code}"; time.sleep(min(5 + 5 * i, 30)); continue
            return {"ERR": e.code, "body": e.read()[:300].decode("utf-8", "replace")}
        except Exception as e:
            err = str(e); time.sleep(3)
    return {"ERR": "retry", "body": err}


def exact(s):
    # точное имя кластера: регистр и ё не важны (главные запросы и тест сравниваем так, без основ слов)
    return " ".join(s.lower().replace("ё", "е").split())


def norm(s):
    w = re.sub(r"[^a-zа-я0-9 ]", " ", s.lower().replace("ё", "е")).split()
    return " ".join(sorted(x[:5] if len(x) > 5 else x for x in w))


def chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def fmt(x):
    return f"{x:,.0f}".replace(",", " ")


# ---------- сбор данных ----------

def gather(cab, tok, cfg, d0, d1, log):
    g = {"camps": {}, "orders": {}, "arts": {}, "spend": collections.defaultdict(float),
         "spend_today": collections.defaultdict(float), "spend_cday": collections.defaultdict(float), "clusters": {}, "own": {}, "minb": {}, "jam": {}, "pos_recent": {}, "today_raw": {}, "minus": {}}
    cnt = http("GET", A + "/adv/v1/promotion/count", tok)
    if cnt.get("ERR"):
        log(f"{cab}: promotion/count {cnt}"); return None
    ids = [a["advertId"] for grp in cnt.get("adverts") or [] if grp["status"] in (9, 11) for a in grp["advert_list"]]
    ids = [i for i in ids if i not in cfg["skip_campaigns"]]
    for part in chunks(ids, 50):
        info = http("GET", A + "/api/advert/v2/adverts?ids=" + ",".join(map(str, part)), tok)
        for a in info.get("adverts") or []:
            g["camps"][a["id"]] = {"id": a["id"], "name": a["settings"]["name"], "status": a["status"],
                                   "pay": a["settings"]["payment_type"], "bid_type": a["bid_type"],
                                   "place": a["settings"]["placements"],
                                   "nms": {n["nm_id"]: n["bids_kopecks"] for n in a["nm_settings"]}}

    # заказы карточек за окно
    o = collections.defaultdict(lambda: [0, 0.0])
    od = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0]))
    for x in http("GET", f"{ORDERS}?dateFrom={d0}", tok) or []:
        if isinstance(x, dict) and x["date"][:10] >= d0:
            od[x["nmId"]][x["date"][:10]][0] += 1; od[x["nmId"]][x["date"][:10]][1] += x["priceWithDisc"]
            if x["date"][:10] <= d1:
                o[x["nmId"]][0] += 1; o[x["nmId"]][1] += x["priceWithDisc"]
            g["arts"][x["nmId"]] = x.get("supplierArticle")
    g["orders"] = dict(o)
    g["orders_daily"] = od

    # расход по кампаниям/карточкам: окно + сегодня
    for i, part in enumerate(chunks(list(g["camps"]), 50)):
        if i: time.sleep(21)
        r = http("GET", f"{A}/adv/v3/fullstats?ids={','.join(map(str, part))}&beginDate={d0}&endDate={TODAY}", tok)
        if isinstance(r, dict) and r.get("ERR"):
            log(f"{cab}: fullstats {r}"); continue
        for c in r or []:
            for day in c.get("days") or []:
                dt = day["date"][:10]
                for app in day.get("apps") or []:
                    for n in app.get("nms") or []:
                        g["spend_cday"][(c["advertId"], dt)] += n["sum"]
                        vc = g.setdefault("nmday", {}).setdefault((n["nmId"], dt), [0, 0])
                        vc[0] += n.get("views") or 0; vc[1] += n.get("clicks") or 0
                        if dt == str(TODAY):
                            g["spend_today"][(c["advertId"], n["nmId"])] += n["sum"]
                        elif dt <= d1:
                            g["spend"][(c["advertId"], n["nmId"])] += n["sum"]

    # в работу — активные и те, что на паузе, но тратили за окно (кончился бюджет и т.п.)
    active = [c for c in g["camps"].values() if c["status"] == 9 or any(g["spend"].get((c["id"], nm)) for nm in c["nms"])]
    log(f"{cab}: кампаний в работе {len(active)} (на паузе из них {sum(c['status'] == 11 for c in active)}), всего {len(g['camps'])}")

    # кластеры: статистика за окно, свои ставки (ручная CPM), минимальные ставки
    items = [{"advert_id": c["id"], "nm_id": nm} for c in active for nm in c["nms"]]
    for part in chunks(items, 100):
        r = http("POST", A + "/adv/v0/normquery/stats", tok, {"from": d0, "to": d1, "items": part})
        if r.get("ERR"):
            log(f"{cab}: normquery/stats {r}"); continue
        for s in r.get("stats") or []:
            g["clusters"][(s["advert_id"], s["nm_id"])] = s.get("stats") or []
        time.sleep(6)
    manual = [x for x in items if is_cluster_camp(g["camps"][x["advert_id"]])]
    for part in chunks(manual, 100):
        r = http("POST", A + "/adv/v0/normquery/get-bids", tok, {"items": part})
        if r.get("ERR"):
            log(f"{cab}: normquery/get-bids {r}"); continue
        for b in r.get("bids") or []:
            g["own"][(b["advert_id"], b["nm_id"], b["norm_query"])] = b["bid"]
    # минус-фразы: отключённые кластеры не трогаем
    for part in chunks(manual, 100):
        r = http("POST", A + "/adv/v0/normquery/get-minus", tok, {"items": part})
        if r.get("ERR"):
            log(f"{cab}: normquery/get-minus {r}"); continue
        for m in r.get("items") or []:
            g["minus"][(m["advert_id"], m["nm_id"])] = set(m.get("norm_queries") or [])
    # свежая позиция в рекламе: сегодня (если ≥ 100 показов), иначе вчера, иначе среднее за окно
    for dt in (d1, str(TODAY)):
        for part in chunks(manual, 100):
            time.sleep(6)
            r = http("POST", A + "/adv/v0/normquery/stats", tok, {"from": dt, "to": dt, "items": part})
            if r.get("ERR"):
                log(f"{cab}: normquery/stats {dt} {r}"); continue
            for s in r.get("stats") or []:
                for x in s.get("stats") or []:
                    if dt == str(TODAY):
                        g["today_raw"][(s["advert_id"], s["nm_id"], x["norm_query"])] = (x.get("views") or 0, x.get("avg_pos"))
                    if (x.get("views") or 0) >= 100 and x.get("avg_pos"):
                        g["pos_recent"][(s["advert_id"], s["nm_id"], x["norm_query"])] = (x["avg_pos"], "сегодня" if dt == str(TODAY) else "вчера")
    for c in active:
        types = ["combined"] if c["bid_type"] == "unified" else ["search", "recommendation"]
        r = http("POST", A + "/api/advert/v1/bids/min", tok,
                 {"advert_id": c["id"], "nm_ids": list(c["nms"])[:100], "payment_type": c["pay"], "placement_types": types})
        if r.get("ERR"):
            log(f"{cab}: bids/min {c['id']} {r}"); continue
        for n in r.get("bids") or []:
            for b in n["bids"]:
                g["minb"][(c["id"], n["nm_id"], b["type"])] = b["value"] / 100
        time.sleep(3)

    # Джем по карточкам, где кластеры тратили (кэш на окно)
    nms = sorted({nm for (cid, nm), st in g["clusters"].items() if sum(x["spend"] for x in st) > 0})
    lim = cfg["cabinets"].get(cab, {}).get("jam_limit", 30)
    p1 = (datetime.date.fromisoformat(d0) - datetime.timedelta(days=cfg["lookback_days"])).isoformat()
    p2 = (datetime.date.fromisoformat(d0) - datetime.timedelta(days=1)).isoformat()
    calls = 0
    for nm in nms:
        cp = os.path.join(BASE, "cache", f"jam_{nm}_{d0}_{d1}.json")
        cached = load(cp)
        if cached is None:
            merged = {}
            for f in ("openCard", "orders"):
                if calls: time.sleep(21)
                calls += 1
                r = http("POST", JAM, tok, {"currentPeriod": {"start": d0, "end": d1}, "pastPeriod": {"start": p1, "end": p2},
                                            "nmIds": [nm], "topOrderBy": f, "includeSubstitutedSKUs": True, "includeSearchTexts": True,
                                            "orderBy": {"field": f, "mode": "desc"}, "limit": lim})
                if r.get("ERR"):
                    log(f"{cab}: Джем {nm} {f} {r}"); merged = None; break
                for it in (r.get("data") or {}).get("items") or []:
                    merged[it["text"]] = {"ord": it["orders"]["current"], "open": it["openCard"]["current"],
                                          "pos": it["avgPosition"]["current"], "freq": it["frequency"]["current"]}
            if merged is None:
                continue
            cached = merged; save(cp, cached)
        idx = {}
        for text, v in cached.items():
            j = idx.setdefault(norm(text), {"ord": 0, "open": 0, "pos": [], "freq": 0})
            j["ord"] += v["ord"]; j["open"] += v["open"]; j["pos"].append(v["pos"]); j["freq"] = max(j["freq"], v.get("freq", 0))
        g["jam"][nm] = idx
    log(f"{cab}: кластеров с расходом по {len(nms)} карточкам, Джем загружен по {len(g['jam'])}")
    return g


def is_cluster_camp(c):
    # ставки по кластерам — только ручная ставка + оплата за показы + поиск
    return c["bid_type"] == "manual" and c["pay"] == "cpm" and c["place"].get("search")


# ---------- решения ----------

def decide(cab, g, cfg, market, state):
    acts, notes, card_rows, cl_rows = [], [], [], []
    cards = cfg["cards"]
    raise_ok = cfg.get("_week_drr") is None or cfg["_week_drr"] <= cfg["weekly_drr_limit"]
    spend_card = collections.defaultdict(float)
    for (cid, nm), s in g["spend"].items():
        spend_card[nm] += s
    today_card = collections.defaultdict(float)
    for (cid, nm), s in g["spend_today"].items():
        today_card[nm] += s

    def card_cfg(nm, key, default=None):
        return cards.get(str(nm), {}).get(key, cfg.get(key, default))

    def name(nm):
        return cards.get(str(nm), {}).get("name") or g["arts"].get(nm) or str(nm)

    def stock_low(nm):
        st = (market.get("products") or {}).get(str(nm), {}).get("stock")
        return st is not None and st < cfg["min_stock"]

    def held(nm):
        # cards.<nm>.hold_lower_until: до этой даты включительно ставки карточки не снижаются (тест, например новой инфографики)
        h = cards.get(str(nm), {}).get("hold_lower_until")
        return h if h and str(TODAY) <= h else None

    def up_factor(d):
        # подъём ставки: +raise_step, при ДРР ниже raise_strong_below × цели — +raise_step_strong
        return 1 + (cfg["raise_step_strong"] if d < T * cfg["raise_strong_below"] else cfg["raise_step"])

    def cooled(key, hours=None):
        t = state["changed"].get(key)
        return not t or (NOW - datetime.datetime.fromisoformat(t)).total_seconds() > (hours or cfg["cooldown_hours"]) * 3600

    def clamp(old, factor, lo, hi, label):
        new = int(round(old * factor))
        new = max(int(-(-lo // 1)), min(int(hi), new))
        if factor > 1 and old >= hi:
            notes.append(f"{label}: поднял бы, но ставка {fmt(old)} уже на потолке {fmt(hi)}"); return None
        if factor < 1 and old <= lo:
            notes.append(f"{label}: снизил бы, но ставка {fmt(old)} уже на минимуме WB {fmt(lo)}"); return None
        if abs(new - old) < (1 if old < 100 else max(2, old * 0.02)): return None
        return new

    for nm in sorted(set(spend_card) | set(today_card)):
        cnt, rev = g["orders"].get(nm, [0, 0.0])
        T = card_cfg(nm, "target_drr") / 100
        drr = spend_card[nm] / rev if rev else None
        price = rev / cnt if cnt else None
        camps = [c for c in g["camps"].values() if nm in c["nms"]]
        low = stock_low(nm)
        if low:
            notes.append(f"{name(nm)}: остаток ниже {cfg['min_stock']} шт — подъёмы ставок отключены")
        pos_sm = {norm(q): v["pos"] for q, v in ((market.get("products") or {}).get(str(nm), {}).get("positions") or {}).items()}
        if spend_card[nm] >= 1 or today_card[nm] >= 1:
            card_rows.append({"cab": cab, "art": name(nm), "nm": nm, "spend": spend_card[nm], "rev": rev, "cnt": cnt,
                              "drr": drr, "target": T, "today": today_card[nm], "cap": cards.get(str(nm), {}).get("daily_cap")})

        # 1) дневной лимит расхода → пауза кампаний, где все карточки упёрлись в лимит
        cap = cards.get(str(nm), {}).get("daily_cap")
        if cap and today_card[nm] >= cap:
            for c in camps:
                if c["status"] == 9 and not any(a["type"] == "pause" and a["advert_id"] == c["id"] for a in acts) and all(cards.get(str(n), {}).get("daily_cap") and today_card[n] >= cards[str(n)]["daily_cap"] for n in c["nms"]):
                    acts.append({"cab": cab, "type": "pause", "advert_id": c["id"], "camp": c["name"], "nm_id": nm, "art": name(nm),
                                 "reason": f"расход сегодня {fmt(today_card[nm])} ₽ ≥ лимита {fmt(cap)} ₽", "spend": today_card[nm]})

        # 2) кластеры в ручных CPM-кампаниях
        for c in camps:
            if c["status"] not in (9, 11):
                continue
            if is_cluster_camp(c):
                base = c["nms"][nm]["search"] / 100
                lo = g["minb"].get((c["id"], nm, "search"), 0)
                hi = card_cfg(nm, "max_bid_cpm")
                jam = g["jam"].get(nm)
                top = {exact(t) for t in cards.get(str(nm), {}).get("top", [])}
                tp_card = card_cfg(nm, "target_pos")
                top_pos = {exact(k): v for k, v in cards.get(str(nm), {}).get("top_pos", {}).items()}
                # кластеры за окно + главные, у которых показы есть только сегодня (например, только что вернули из минус-фраз)
                clusters = list(g["clusters"].get((c["id"], nm), []))
                seen = {x["norm_query"] for x in clusters}
                for (cid2, nm2, q2), (tv2, tpos2) in g["today_raw"].items():
                    if cid2 == c["id"] and nm2 == nm and q2 not in seen and exact(q2) in top and tv2:
                        clusters.append({"norm_query": q2, "spend": 0, "avg_pos": tpos2, "orders": 0, "atbs": 0})
                for x in clusters:
                    q, sp = x["norm_query"], x["spend"]
                    if q in g["minus"].get((c["id"], nm), ()):
                        continue
                    j = jam.get(norm(q)) if jam is not None else None
                    at = cfg.get("auto_top") or {}
                    is_top = exact(q) in top or bool(at and j and j.get("freq", 0) >= at["min_week_freq"] and j["ord"] >= at["min_orders"])
                    if sp <= 0 and not is_top:
                        continue
                    tp = top_pos.get(exact(q), tp_card)
                    own = g["own"].get((c["id"], nm, q))
                    old = own if own is not None else base
                    rp = g["pos_recent"].get((c["id"], nm, q))
                    pos = rp[0] if rp else x.get("avg_pos")
                    row = {"cab": cab, "type": "cluster_bid", "advert_id": c["id"], "camp": c["name"], "nm_id": nm, "art": name(nm),
                           "query": q, "old": old, "own": own is not None, "spend": round(sp), "ad_pos": x.get("avg_pos"),
                           "pos": pos, "pos_date": rp[1] if rp else "за окно", "top": is_top,
                           "sm_pos": pos_sm.get(norm(q)), "jam_ord": j["ord"] if j else None, "wb_ord": x.get("orders"), "freq": j.get("freq") if j else None}
                    if j and j["ord"] and price:
                        row["drr"] = round(sp / (j["ord"] * price) * 100, 1)
                    cl_rows.append(row)
                    if sp < cfg["min_spend_rub"] and not is_top:
                        continue
                    factor, why, breach = None, "", False
                    key = f"{c['id']}|{nm}|{q}"
                    if is_top:
                        # главный запрос: держим позицию ≤ tp, пока ДРР запроса не выше потолка; выше — снижаем, как по ДРР
                        cap = card_cfg(nm, "top_drr_cap") / 100
                        d = sp / (j["ord"] * price) if j and j["ord"] and price else None
                        breach = bool((not cnt and sp >= cfg["min_spend_rub"])
                                      or (d is not None and d > cap and sp >= price * T * cfg["min_expected_orders"])
                                      or (j is not None and j["ord"] == 0 and price and sp >= price * cap * 2))
                    if is_top and breach:
                        factor = max(1 - cfg["max_step_down"], cap / d) if d else 1 - cfg["max_step_down"]
                        why = (f"главный запрос, но ДРР {d * 100:.0f}% > потолка {cap * 100:.0f}%" if d
                               else f"главный запрос без заказов при расходе {fmt(sp)} ₽")
                    elif is_top:
                        tv, tpos = g["today_raw"].get((c["id"], nm, q), (0, None))
                        snap = state.get("snap", {}).get(key)
                        row["snap"] = {"date": str(TODAY), "views": tv, "pos_sum": tv * (tpos or 0)}
                        if snap and snap["date"] == str(TODAY):
                            dv = tv - snap["views"]
                            if dv < cfg["min_views_after_change"]:
                                notes.append(f"{name(nm)} «{q}»: после изменения {dv} показов из {cfg['min_views_after_change']} — жду, позиция ещё не ясна")
                                continue
                            pos = (tv * tpos - snap["pos_sum"]) / dv
                            row["pos"], row["pos_date"] = round(pos, 1), f"после изменения ({dv} пок.)"
                        if pos and pos > tp:
                            if not raise_ok:
                                notes.append(f"{name(nm)} «{q}»: позиция {pos:.1f} > {tp}, но общий ДРР за неделю {cfg['_week_drr']:.1f}% > {cfg['weekly_drr_limit']}% — подъём заморожен"); continue
                            if d is not None and d > cap * 0.9:
                                notes.append(f"{name(nm)} «{q}»: позиция {pos:.1f} > {tp}, но ДРР {d * 100:.0f}% у потолка {cap * 100:.0f}% — не поднимаю"); continue
                            factor, why = 1 + cfg["max_step_up_top"], f"главный запрос: позиция {pos:.1f} > {tp}" + (f", ДРР {d * 100:.0f}%" if d else "")
                        elif pos and pos <= tp - cfg["top_margin"]:
                            factor, why = 1 - cfg["max_step_down_top"], f"главный запрос на {pos:.1f} месте — ищу ставку дешевле, держа ≤ {tp}"
                    elif not cnt:
                        factor, why = 1 - cfg["max_step_down"], "у карточки нет заказов за период"
                    elif j is None:
                        if sp >= price * T * 2 and not x.get("orders") and not x.get("atbs"):
                            factor, why = 1 - cfg["max_step_down"], "нет в Джеме, нет корзин и заказов"
                    elif j["ord"] == 0:
                        am = cfg.get("auto_minus") or {}
                        if am.get("mode") in ("auto", "confirm") and sp >= price * am["spend_checks"]:
                            acts.append({**row, "type": "minus", "key": key,
                                         "reason": f"0 заказов по Джему при расходе {fmt(sp)} ₽ (≥ {am['spend_checks']} ср. чеков по {fmt(price)} ₽)"})
                            continue
                        if sp >= price * T * 1.5:
                            factor, why = 1 - cfg["max_step_down"], "0 заказов по Джему"
                            if sp >= price * T * 3:
                                acts.append({**row, "type": "minus_hint", "reason": f"0 заказов при расходе {fmt(sp)} ₽ — кандидат в минус-фразы"})
                    else:
                        d = sp / (j["ord"] * price)
                        row["drr"] = round(d * 100, 1)
                        if d > T * (1 + cfg["tolerance"]):
                            if sp < price * T * cfg["min_expected_orders"]:
                                notes.append(f"{name(nm)} «{q}»: ДРР {d * 100:.0f}% при {j['ord']} зак. и расходе {fmt(sp)} ₽ — мало данных, не трогаю")
                                continue
                            factor, why = max(1 - cfg["max_step_down"], T / d), f"ДРР {d * 100:.0f}% > цели {T * 100:.0f}%"
                        elif d < T * cfg["raise_below"] and j["ord"] >= cfg["min_jam_orders_to_raise"] and not low and raise_ok \
                                and (x.get("avg_pos") or 99) > cfg["target_ad_pos"]:
                            factor = up_factor(d)
                            why = f"ДРР {d * 100:.0f}% < цели, {j['ord']} зак. — беру больше трафика (+{(factor - 1) * 100:.0f}%)"
                    if factor is None:
                        continue
                    if factor < 1 and held(nm):
                        notes.append(f"{name(nm)} «{q}»: снизил бы ({why}), но тест до {held(nm)} — не снижаю"); continue
                    new = clamp(old, factor, lo, hi, f"{name(nm)} «{q}»")
                    if new is None:
                        continue
                    hours = (cfg["cooldown_hours_top"] if is_top and not breach
                             else cfg["cooldown_raise_hours"] if factor > 1 else cfg["cooldown_hours"])
                    if not cooled(key, hours):
                        notes.append(f"{name(nm)} «{q}»: хотел {fmt(old)}→{fmt(new)}, но ставку меняли < {hours} ч назад"); continue
                    acts.append({**row, "new": new, "reason": why, "key": key})

            # 3) базовые ставки по ДРР карточки: единая ставка, CPC, ручная CPM (в поиске действует на кластеры без своей ставки)
            if c["bid_type"] == "unified":
                places = [("combined", "combined", c["nms"][nm]["search"])]
            else:
                places = [(p, p.rstrip("s"), c["nms"][nm][p]) for p in ("search", "recommendations") if c["place"].get(p)]
            sp = g["spend"].get((c["id"], nm), 0)
            if not places or sp < cfg["min_spend_rub"]:
                continue
            factor, why = None, ""
            if not cnt:
                factor, why = 1 - cfg["max_step_down"], "у карточки нет заказов за период"
            elif drr > T * (1 + cfg["tolerance"]) and cfg["base_bids"]["lower"]:
                factor, why = max(1 - cfg["max_step_down"], T / drr), f"ДРР карточки {drr * 100:.0f}% > цели {T * 100:.0f}%"
            elif drr < T * cfg["raise_below"] and cfg["base_bids"]["raise"] and not low and raise_ok \
                    and cnt >= cfg["min_orders_to_raise_base"]:
                factor = up_factor(drr)
                why = f"ДРР карточки {drr * 100:.0f}% < цели, {cnt} зак. — беру больше трафика (+{(factor - 1) * 100:.0f}%)"
            if factor is None:
                continue
            if factor < 1 and held(nm):
                notes.append(f"{name(nm)} РК {c['id']} базовая: снизил бы ({why}), но тест до {held(nm)} — не снижаю"); continue
            hi = card_cfg(nm, "max_bid_cpc" if c["pay"] == "cpc" else "max_bid_cpm")
            for placement, mtype, kop in places:
                if factor > 1 and placement == "search" and is_cluster_camp(c):
                    continue  # в ручной CPM поиск поднимаем по кластерам, базовую не трогаем
                old = kop / 100
                new = clamp(old, factor, g["minb"].get((c["id"], nm, mtype), 0), hi, f"{name(nm)} РК {c['id']} базовая ({placement})")
                key = f"{c['id']}|{nm}|base:{placement}"
                if new is None:
                    continue
                hours = cfg["cooldown_raise_hours"] if factor > 1 else cfg["cooldown_hours"]
                if not cooled(key, hours):
                    notes.append(f"{name(nm)} РК {c['id']} базовая ({placement}): ставку меняли < {hours} ч назад"); continue
                acts.append({"cab": cab, "type": "base_bid", "advert_id": c["id"], "camp": c["name"], "nm_id": nm, "art": name(nm),
                             "placement": placement, "pay": c["pay"], "old": old, "new": new, "spend": round(sp),
                             "drr": round(drr * 100, 1) if drr else None, "reason": why, "key": key})
                # главные запросы без своей ставки живут на базовой — закрепляем их до снижения базовой
                if placement == "search" and is_cluster_camp(c) and new < old:
                    acted = {a.get("query") for a in acts if a["type"] == "cluster_bid" and a["advert_id"] == c["id"] and a["nm_id"] == nm}
                    for r in cl_rows:
                        if r["advert_id"] == c["id"] and r["nm_id"] == nm and r["top"] and not r["own"] and r["query"] not in acted:
                            acts.append({**r, "new": int(round(old)), "reason": "закрепляю ставку главного запроса перед снижением базовой",
                                         "key": f"{c['id']}|{nm}|{r['query']}"})

    # 4) снять паузу, которую ставил бидер, на следующий день
    for cid, day in list(state["paused"].get(cab, {}).items()):
        c = g["camps"].get(int(cid))
        if c and c["status"] == 11 and day < str(TODAY):
            acts.append({"cab": cab, "type": "start", "advert_id": c["id"], "camp": c["name"], "reason": f"пауза бидера от {day}, новый день"})
    return acts, notes, card_rows, cl_rows


# ---------- до и после изменений ----------

def effect(cab, tok, g, d0, log, days_back=10, span=3):
    # по каждому изменению ставки кластера: средние за день — до (span полных дней) и после (полные дни после; если их нет — сегодня)
    try:
        ch = [json.loads(l) for l in open(os.path.join(BASE, "out", "changes.jsonl"), encoding="utf-8") if l.strip()]
    except FileNotFoundError:
        return []
    since = str(TODAY - datetime.timedelta(days=days_back))
    ch = [c for c in ch if c["cab"] == cab and c["type"] == "cluster_bid" and c.get("ok") and c["time"][:10] >= since]
    for c in ch:
        if not c.get("nm_id"):
            nms = list((g["camps"].get(c["advert_id"]) or {}).get("nms") or [])
            c["nm_id"] = nms[0] if len(nms) == 1 else None
    ch = [c for c in ch if c["nm_id"]]
    if not ch:
        return []
    start = (datetime.date.fromisoformat(min(c["time"][:10] for c in ch)) - datetime.timedelta(days=span)).isoformat()
    items = [{"advertId": a, "nmId": n} for a, n in sorted({(c["advert_id"], c["nm_id"]) for c in ch})]
    r = http("POST", A + "/adv/v1/normquery/stats", tok, {"from": start, "to": str(TODAY), "items": items})
    if r.get("ERR"):
        log(f"{cab}: normquery v1 {r}"); return []
    daily = collections.defaultdict(dict)
    for it in r.get("items") or []:
        for ds in it.get("dailyStats") or []:
            st = ds.get("stat") or {}
            daily[(it["advertId"], it["nmId"], st.get("normQuery"))][ds["date"][:10]] = st

    def avg(stats, orders, days):
        v = [stats[d] for d in days if d in stats]
        views = sum(x.get("views") or 0 for x in v)
        return {"days": len(days), "views": views / max(len(days), 1), "clicks": sum(x["clicks"] for x in v) / max(len(days), 1),
                "spend": sum(x["spend"] for x in v) / max(len(days), 1),
                "pos": sum((x.get("avgPos") or 0) * (x.get("views") or 0) for x in v) / views if views else None,
                "orders": sum(orders.get(d, [0])[0] for d in days) / len(days) if days and min(days) >= d0 else None}

    rows = []
    for c in ch:
        day = datetime.date.fromisoformat(c["time"][:10])
        before = [str(day - datetime.timedelta(days=i)) for i in range(span, 0, -1)]
        after = [str(day + datetime.timedelta(days=i)) for i in range(1, (TODAY - day).days)]
        partial = not after
        if partial:
            after = [str(TODAY)] if day < TODAY else []
        st = daily.get((c["advert_id"], c["nm_id"], c["query"]), {})
        od = g["orders_daily"].get(c["nm_id"], {})
        rows.append({**c, "before": avg(st, od, before), "after": avg(st, od, after) if after else None,
                     "after_label": "сегодня, неполный день" if partial and after else (f"{len(after)} дн." if after else "ещё нет данных")})
    return rows


# ---------- минус-фразы по правилу ----------

def do_minus(cab, tok, items, cfg, log):
    # добавить кластеры в минус-фразы: бэкап списка → запись → перечитка; остальные минус-фразы не трогаем
    by = collections.defaultdict(list)
    for a in items:
        by[(a["advert_id"], a["nm_id"])].append(a)
    for (aid, nm), lst in by.items():
        it = [{"advert_id": aid, "nm_id": nm}]
        r = http("POST", A + "/adv/v0/normquery/get-minus", tok, {"items": it})
        if r.get("ERR"):
            log(f"{cab}: get-minus {aid} {r}"); continue
        old = ((r.get("items") or [{}])[0].get("norm_queries")) or []
        save(os.path.join(rel(cfg["backup_dir"]), f"minus_{cab.replace(' ', '_')}_{aid}_{NOW:%Y%m%d_%H%M%S}.json"),
             {"time": NOW.isoformat(), "cab": cab, "advert_id": aid, "nm_id": nm, "before": old, "add": [a["query"] for a in lst]})
        new = old + [a["query"] for a in lst if a["query"] not in old]
        w = http("POST", A + "/adv/v0/normquery/set-minus", tok, {"advert_id": aid, "nm_id": nm, "norm_queries": new})
        time.sleep(2)
        now = set(((http("POST", A + "/adv/v0/normquery/get-minus", tok, {"items": it}).get("items") or [{}])[0].get("norm_queries")) or [])
        ok = not (isinstance(w, dict) and w.get("ERR")) and set(old) <= now
        with open(os.path.join(BASE, "out", "changes.jsonl"), "a", encoding="utf-8") as f:
            for a in lst:
                a["applied"] = ok and a["query"] in now
                f.write(json.dumps({"time": NOW.isoformat(timespec="seconds"), "ok": a["applied"], "cab": cab, "type": "minus", "art": a.get("art"),
                                    "nm_id": nm, "advert_id": aid, "camp": a.get("camp"), "query": a["query"], "old": None, "new": None,
                                    "pos": a.get("pos"), "drr": None, "reason": a.get("reason")}, ensure_ascii=False) + "\n")
        log(f"{cab}: минус-фразы РК {aid}: +{len(lst)} {'ok' if ok else 'ОШИБКА ' + json.dumps(w, ensure_ascii=False)[:200]}")
    return items


def handle_minus(cab, tok, acts, cfg, state, apply_ok, log):
    # «minus» из decide: auto — отключаем сразу; confirm — спрашиваем в Telegram; без бота/записи — только подсказка
    mode = (cfg.get("auto_minus") or {}).get("mode")
    seen = state.setdefault("minus_seen", {})
    for a in [a for a in acts if a["type"] == "minus"]:
        recent = seen.get(a["key"]) and (TODAY - datetime.date.fromisoformat(seen[a["key"]])).days < 14
        a["type"] = "minus_hint"
        if recent:
            a["reason"] += " — уже предлагал, ждёт ответа или отклонено"
        elif apply_ok and mode == "auto":
            do_minus(cab, tok, [a], cfg, log); seen[a["key"]] = str(TODAY)
            a["reason"] += " — отключено автоматически" if a.get("applied") else " — не удалось отключить"
        elif apply_ok and mode == "confirm" and tg.conf("bidder"):
            pid = f"{abs(hash(a['key'])) % 10 ** 8:08d}"
            state.setdefault("minus_pending", {})[pid] = {k: a.get(k) for k in ("cab", "advert_id", "nm_id", "query", "art", "camp", "spend", "reason", "key")}
            tg.send("bidder", f"🧹 <b>Отключить запрос?</b>\n{a.get('art')} · РК {a['advert_id']}\n«{a['query']}»\n{a['reason']}",
                    [("Отключить", f"m:{pid}:y"), ("Оставить", f"m:{pid}:n")])
            seen[a["key"]] = str(TODAY)
            a["reason"] += " — отправлено на подтверждение в Telegram"
        else:
            a["reason"] += " — кандидат в минус-фразы"


def tg_callbacks(tokens, cfg, state, log):
    # нажатия кнопок в Telegram: m:<id>:y — отключить кластер, m:<id>:n — оставить
    for u in tg.updates("bidder", state.get("tg_offset", 0)):
        state["tg_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq or not str(cq.get("data", "")).startswith("m:"):
            continue
        _, pid, ans = cq["data"].split(":")
        p = state.get("minus_pending", {}).pop(pid, None)
        msg = cq.get("message") or {}
        if not p:
            tg.answer("bidder", cq["id"], "Уже обработано"); continue
        if ans == "y":
            do_minus(p["cab"], tokens[p["cab"]], [p], cfg, log)
            res = "✅ Отключено" if p.get("applied") else "⚠️ Не удалось отключить, попробую позже"
            if not p.get("applied"):
                state["minus_pending"][pid] = p
        else:
            res = "↩️ Оставлено, 14 дней не предлагаю"
        tg.answer("bidder", cq["id"], res)
        tg.edit("bidder", msg.get("chat", {}).get("id"), msg.get("message_id"), f"{res}: {p.get('art')} «{p['query']}»")


# ---------- сигналы и сводка в Telegram ----------

def notify(gs, tokens, cfg, state, d0, d1, log):
    if not tg.conf("bidder"):
        return
    al = state.setdefault("alerts", {})

    def once(key, text):
        if al.get(key) != str(TODAY):
            tg.send("bidder", text); al[key] = str(TODAY)

    yday = str(TODAY - datetime.timedelta(days=1))
    prev = [str(TODAY - datetime.timedelta(days=i)) for i in range(2, 8)]
    lines = []
    for cab, g in gs.items():
        names = lambda nm: cfg["cards"].get(str(nm), {}).get("name") or g["arts"].get(nm) or nm
        # ДРР карточки за неделю выше порога
        sp = collections.defaultdict(float)
        for (cid, nm), v in g["spend"].items():
            sp[nm] += v
        for nm, s in sp.items():
            rev = g["orders"].get(nm, [0, 0])[1]
            if s >= cfg["alert"]["min_spend"] and (not rev or s / rev * 100 > cfg["alert"]["card_drr"]):
                once(f"drr|{cab}|{nm}", f"⚠️ {cab} · <b>{names(nm)}</b>: ДРР за неделю {'∞' if not rev else f'{s / rev * 100:.0f}%'} "
                                        f"(реклама {fmt(s)} ₽, заказы {fmt(rev)} ₽)")
        # кампании: встала из-за бюджета / бюджет на исходе
        for cid, c in g["camps"].items():
            ysp = g["spend_cday"].get((cid, yday), 0)
            if c["status"] not in (9, 11) or (c["status"] == 11 and not any(g["spend"].get((cid, nm)) for nm in c["nms"])):
                continue
            b = http("GET", f"{A}/adv/v1/budget?id={cid}", tokens[cab])
            total = (b or {}).get("total") if isinstance(b, dict) and not b.get("ERR") else None
            if total is None:
                continue
            if c["status"] == 11 and total < cfg["alert"]["budget_min"] and not state.get("paused", {}).get(cab, {}).get(str(cid)):
                once(f"budget0|{cid}", f"⛔ {cab} · РК {cid} «{c['name']}» на паузе, бюджет {fmt(total)} ₽ — пополните")
            elif c["status"] == 9 and ysp > 0 and total < ysp * 0.3:
                once(f"budget|{cid}", f"⏳ {cab} · РК {cid} «{c['name']}»: бюджет {fmt(total)} ₽, вчера расход {fmt(ysp)} ₽ — хватит на несколько часов")
            time.sleep(0.3)
        # заказы вчера против среднего за 6 дней до этого
        od = g["orders_daily"]
        y = sum(v.get(yday, [0, 0])[1] for v in od.values())
        avg = sum(v.get(d, [0, 0])[1] for v in od.values() for d in prev) / len(prev)
        if avg and y < avg * (1 - cfg["alert"]["orders_drop"] / 100):
            once(f"orders|{cab}", f"📉 {cab}: заказы вчера {fmt(y)} ₽ — на {(1 - y / avg) * 100:.0f}% ниже среднего за 6 дней ({fmt(avg)} ₽)")
        ysum = sum(v for (cid, dt), v in g["spend_cday"].items() if dt == yday)
        lines.append(f"<b>{cab}</b>: реклама {fmt(ysum)} ₽, заказы {fmt(y)} ₽, ДРР {ysum / y * 100 if y else 0:.1f}%")
    # сводка раз в день после summary_hour
    if NOW.hour >= cfg["alert"]["summary_hour"] and state.get("summary_date") != str(TODAY):
        ch = []
        try:
            ch = [json.loads(l) for l in open(os.path.join(BASE, "out", "changes.jsonl"), encoding="utf-8") if l.strip()]
        except FileNotFoundError:
            pass
        ch = [c for c in ch if c["time"][:10] == yday and c.get("ok")]
        up = [c for c in ch if c["type"] in ("cluster_bid", "base_bid") and (c.get("new") or 0) > (c.get("old") or 0)]
        down = [c for c in ch if c["type"] in ("cluster_bid", "base_bid") and (c.get("new") or 0) < (c.get("old") or 0)]
        mn = [c for c in ch if c["type"] == "minus"]
        wd = cfg.get("_week_drr")
        text = [f"📊 <b>Сводка за {yday[8:]}.{yday[5:7]}</b>"] + lines + [
            f"ДРР за неделю: {'—' if wd is None else f'{wd:.1f}%'} (лимит {cfg['weekly_drr_limit']}%)",
            f"Бидер: ставок ↑ {len(up)}, ↓ {len(down)}, минус-фраз {len(mn)}"]
        for c in sorted(up + down, key=lambda c: -abs((c.get("new") or 0) - (c.get("old") or 0)))[:6]:
            text.append(f"• {c.get('art')} «{c.get('query') or c.get('placement')}»: {fmt(c.get('old') or 0)} → {fmt(c.get('new') or 0)} ₽")
        text += conv_watch(gs, tokens, cfg, state, log)
        if tg.send("bidder", "\n".join(text)):
            state["summary_date"] = str(TODAY)


FUNNEL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history"


def conv_watch(gs, tokens, cfg, state, log):
    # cards.<nm>.conv_watch = "ГГГГ-ММ-ДД" (дата смены инфографики): конверсии после неё против недели до неё
    out = []
    for cab, g in gs.items():
        for nm_s, cc in cfg["cards"].items():
            since = cc.get("conv_watch")
            nm = int(nm_s)
            if not since or not any(nm in c["nms"] for c in g["camps"].values()):
                continue
            s0 = datetime.date.fromisoformat(since)
            yday = TODAY - datetime.timedelta(days=1)
            if yday < s0:
                continue

            def funnel(a, b):
                r = http("POST", FUNNEL, tokens[cab], {"selectedPeriod": {"start": str(a), "end": str(b)}, "nmIds": [nm],
                                                       "skipDeletedNm": False, "aggregationLevel": "day"})
                if not isinstance(r, list) or not r:
                    log(f"{cab}: воронка {nm}: {str(r)[:200]}"); return None
                # дни, где заказов больше, чем корзин (оптовый/аномальный заказ), не считаем
                return [h for h in r[0]["history"] if h["orderCount"] <= h["cartCount"]]

            def agg(hs, days):
                o = sum(h["openCount"] for h in hs); ca = sum(h["cartCount"] for h in hs); od = sum(h["orderCount"] for h in hs)
                v = sum(g.get("nmday", {}).get((nm, d), [0, 0])[0] for d in days); k = sum(g.get("nmday", {}).get((nm, d), [0, 0])[1] for d in days)
                return {"days": len(hs), "cart": ca / o * 100 if o else 0, "c2o": od / ca * 100 if ca else 0, "o2o": od / o * 100 if o else 0,
                        "ord_day": od / len(hs) if hs else 0, "ctr": k / v * 100 if v else None}

            base = state.setdefault("conv_base", {}).get(nm_s)
            if not base or base.get("since") != since:
                b0, b1 = s0 - datetime.timedelta(days=7), s0 - datetime.timedelta(days=1)
                hs = funnel(b0, b1)
                if hs is None:
                    continue
                base = {"since": since, **agg(hs, [str(b0 + datetime.timedelta(days=i)) for i in range(7)])}
                state["conv_base"][nm_s] = base
            a = max(s0, yday - datetime.timedelta(days=6))
            hs = funnel(a, yday)
            if not hs:
                continue
            cur = agg(hs, [str(a + datetime.timedelta(days=i)) for i in range((yday - a).days + 1)])
            arrow = lambda x, y: "↑" if y > x * 1.05 else "↓" if y < x * 0.95 else "≈"
            ctr = (f", CTR рекламы {base['ctr']:.1f}% → {cur['ctr']:.1f}% {arrow(base['ctr'], cur['ctr'])}"
                   if base.get("ctr") and cur.get("ctr") else "")
            out.append(f"🖼 <b>{cc.get('name') or nm}</b>, инфографика с {since[8:]}.{since[5:7]} ({cur['days']} дн. против недели до):\n"
                       f"в корзину {base['cart']:.1f}% → {cur['cart']:.1f}% {arrow(base['cart'], cur['cart'])}, "
                       f"корзина→заказ {base['c2o']:.0f}% → {cur['c2o']:.0f}% {arrow(base['c2o'], cur['c2o'])}, "
                       f"заказов в день {base['ord_day']:.1f} → {cur['ord_day']:.1f} {arrow(base['ord_day'], cur['ord_day'])}{ctr}")
    return out


# ---------- запись ----------

def backup(cab, tok, acts, cfg):
    items = [{"advert_id": a["advert_id"], "nm_id": a["nm_id"]} for a in acts if a["type"] == "cluster_bid"]
    items = [dict(t) for t in {tuple(sorted(i.items())) for i in items}]
    bids = http("POST", A + "/adv/v0/normquery/get-bids", tok, {"items": items}) if items else {}
    ids = sorted({a["advert_id"] for a in acts})
    adverts = http("GET", A + "/api/advert/v2/adverts?ids=" + ",".join(map(str, ids)), tok) if ids else {}
    p = os.path.join(rel(cfg["backup_dir"]), f"backup_{cab.replace(' ', '_')}_{NOW:%Y%m%d_%H%M%S}.json")
    save(p, {"time": NOW.isoformat(), "cab": cab, "actions": acts, "bids": bids, "adverts": adverts})
    if bids.get("ERR") or adverts.get("ERR"):
        raise RuntimeError(f"бэкап не снят: {bids.get('ERR')} {adverts.get('ERR')}")
    return p


def apply(cab, tok, acts, state, log):
    res = []
    cl = [a for a in acts if a["type"] == "cluster_bid"]
    for part in chunks(cl, 100):
        r = http("POST", A + "/adv/v0/normquery/bids", tok,
                 {"bids": [{"advert_id": a["advert_id"], "nm_id": a["nm_id"], "norm_query": a["query"], "bid": a["new"]} for a in part]})
        res.append(("кластеры", len(part), r)); time.sleep(1)
        if isinstance(r, dict) and r.get("failed"):
            log(f"{cab}: WB отклонил ставки кластеров: {json.dumps(r['failed'], ensure_ascii=False)[:500]}")
    base = collections.defaultdict(list)
    for a in acts:
        if a["type"] == "base_bid":
            base[a["advert_id"]].append({"nm_id": a["nm_id"], "bid_kopecks": int(a["new"] * 100), "placement": a["placement"]})
    if base:
        r = http("PATCH", A + "/api/advert/v1/bids", tok, {"bids": [{"advert_id": k, "nm_bids": v} for k, v in base.items()]})
        res.append(("базовые", sum(map(len, base.values())), r))
    for a in acts:
        if a["type"] in ("pause", "start"):
            r = http("GET", f"{A}/adv/v0/{a['type']}?id={a['advert_id']}", tok)
            res.append((a["type"], a["advert_id"], r))
            a["applied"] = not r.get("ERR")
            if not r.get("ERR"):
                if a["type"] == "pause":
                    state["paused"].setdefault(cab, {})[str(a["advert_id"])] = str(TODAY)
                else:
                    state["paused"].get(cab, {}).pop(str(a["advert_id"]), None)
            time.sleep(1)
    for what, n, r in res:
        log(f"{cab}: запись {what} {n}: {'ОШИБКА ' + json.dumps(r, ensure_ascii=False) if r.get('ERR') else 'ok'}")
    # перечитка кластерных ставок
    if cl:
        time.sleep(3)
        items = [dict(t) for t in {(("advert_id", a["advert_id"]), ("nm_id", a["nm_id"])) for a in cl}]
        now = {(b["advert_id"], b["nm_id"], b["norm_query"]): b["bid"]
               for b in http("POST", A + "/adv/v0/normquery/get-bids", tok, {"items": items}).get("bids") or []}
        for a in cl:
            a["applied"] = now.get((a["advert_id"], a["nm_id"], a["query"])) == a["new"]
    base_ok = any(what == "базовые" and not r.get("ERR") for what, n, r in res)
    with open(os.path.join(BASE, "out", "changes.jsonl"), "a", encoding="utf-8") as f:
        for a in acts:
            if a["type"] == "base_bid":
                a["applied"] = base_ok
            if a.get("applied") and a["type"] in ("cluster_bid", "base_bid"):
                state["changed"][a["key"]] = NOW.isoformat(timespec="seconds")
                if a.get("snap") and a.get("top"):
                    state.setdefault("snap", {})[a["key"]] = a["snap"]
            f.write(json.dumps({"time": NOW.isoformat(timespec="seconds"), "ok": bool(a.get("applied")),
                                **{k: a.get(k) for k in ("cab", "type", "art", "nm_id", "advert_id", "camp", "query", "placement", "old", "new", "pos", "drr", "reason")}},
                               ensure_ascii=False) + "\n")
    bad = [a for a in cl if not a.get("applied")]
    if bad:
        log(f"{cab}: не подтвердились {len(bad)} кластерных ставок: " + "; ".join(f"«{a['query']}» {a['new']}" for a in bad[:10]))


def rollback(path, tokens, log):
    b = load(path)
    tok = tokens[b["cab"]]
    old = {(x["advert_id"], x["nm_id"], x["norm_query"]): x["bid"] for x in (b["bids"] or {}).get("bids") or []}
    put, drop = [], []
    for a in b["actions"]:
        if a["type"] != "cluster_bid":
            continue
        k = (a["advert_id"], a["nm_id"], a["query"])
        if k in old:
            put.append({"advert_id": k[0], "nm_id": k[1], "norm_query": k[2], "bid": old[k]})
        else:
            drop.append({"advert_id": k[0], "nm_id": k[1], "norm_query": k[2]})
    if put:
        log(f"возврат ставок кластеров {len(put)}: {http('POST', A + '/adv/v0/normquery/bids', tok, {'bids': put}) or 'ok'}")
    if drop:
        log(f"снятие своих ставок кластеров {len(drop)}: {http('DELETE', A + '/adv/v0/normquery/bids', tok, {'bids': drop}) or 'ok'}")
    base = collections.defaultdict(list)
    camps = {a["id"]: a for a in (b["adverts"] or {}).get("adverts") or []}
    for a in b["actions"]:
        if a["type"] == "base_bid" and a["advert_id"] in camps:
            kop = next(n["bids_kopecks"] for n in camps[a["advert_id"]]["nm_settings"] if n["nm_id"] == a["nm_id"])
            base[a["advert_id"]].append({"nm_id": a["nm_id"], "placement": a["placement"],
                                         "bid_kopecks": kop["search" if a["placement"] == "combined" else a["placement"]]})
    if base:
        log(f"возврат базовых ставок: {http('PATCH', A + '/api/advert/v1/bids', tok, {'bids': [{'advert_id': k, 'nm_bids': v} for k, v in base.items()]})}")
    for a in b["actions"]:
        if a["type"] == "pause":
            url = f"{A}/adv/v0/start?id={a['advert_id']}"
            log(f"запуск РК {a['advert_id']}: {http('GET', url, tok) or 'ok'}")


# ---------- отчёт ----------

def report(acts, notes, summary, cards, d0, d1, mode):
    L = [f"# Бидер WB — {NOW:%d.%m.%Y %H:%M} ({mode})", f"Окно: {d0} — {d1}. ДРР = расход ÷ все заказы ₽; кластеры — по заказам Джема.", ""]
    L += summary
    if cards:
        L += ["", "## Карточки", "| Кабинет | Артикул | Расход, ₽ | Заказы | Заказы, ₽ | ДРР / цель | Сегодня, ₽ |", "|---|---|---:|---:|---:|---:|---:|"]
        for r in sorted(cards, key=lambda r: -r["spend"]):
            drr = "—" if r["drr"] is None else f"{r['drr'] * 100:.1f}%"
            L.append(f"| {r['cab']} | {r['art']} | {fmt(r['spend'])} | {r['cnt']} | {fmt(r['rev'])} | {drr} / {r['target'] * 100:.0f}% | "
                     f"{fmt(r['today'])}{' из ' + fmt(r['cap']) if r['cap'] else ''} |")
    titles = {"pause": "Пауза по дневному лимиту", "start": "Снять паузу бидера", "cluster_bid": "Ставки кластеров (ручная CPM)",
              "base_bid": "Базовые ставки (единая / CPC / рекомендации)", "minus_hint": "Кандидаты в минус-фразы (вручную)"}
    for t, title in titles.items():
        rows = [a for a in acts if a["type"] == t]
        if not rows:
            continue
        L += ["", f"## {title} — {len(rows)}"]
        if t == "cluster_bid":
            L += ["| Кабинет | Артикул | РК | Кластер | Ставка | Расход, ₽ | Заказы Джем | ДРР | Поз. реклама | Поз. Sellmonitor | Причина |",
                  "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
            for a in sorted(rows, key=lambda a: -a["spend"]):
                L.append(f"| {a['cab']} | {a['art']} | {a['advert_id']} | {a['query']} | {fmt(a['old'])}{'' if a['own'] else ' (баз.)'} → **{fmt(a['new'])}** | "
                         f"{fmt(a['spend'])} | {'—' if a['jam_ord'] is None else a['jam_ord']} | {a.get('drr') or '—'}{'%' if a.get('drr') else ''} | "
                         f"{a['ad_pos'] or '—'} | {a['sm_pos'] or '—'} | {a['reason']} |")
        elif t == "base_bid":
            L += ["| Кабинет | Артикул | РК | Оплата / место | Ставка | Расход РК, ₽ | ДРР карточки | Причина |", "|---|---|---|---|---:|---:|---:|---|"]
            for a in sorted(rows, key=lambda a: -a["spend"]):
                L.append(f"| {a['cab']} | {a['art']} | {a['advert_id']} «{a['camp']}» | {a['pay']} / {a['placement']} | {fmt(a['old'])} → **{fmt(a['new'])}** | "
                         f"{fmt(a['spend'])} | {a['drr'] or '—'}% | {a['reason']} |")
        else:
            for a in rows:
                L.append(f"- {a['cab']}: {a.get('art', '')} РК {a['advert_id']} «{a['camp']}»{' — «' + a['query'] + '»' if a.get('query') else ''}: {a['reason']}")
    if notes:
        L += ["", "## Заметки"] + [f"- {n}" for n in notes]
    return "\n".join(L)


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e3e3e0;--ok:#1f7a3f;--okbg:#e5f4ea;--bad:#b42318;--badbg:#fdeceb}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--card:#202023;--fg:#ececee;--mut:#9a9aa1;--line:#323236;--ok:#5fd08a;--okbg:#17321f;--bad:#ff8a80;--badbg:#3a1d1b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.mut{color:var(--mut)}.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600}
.on{background:var(--okbg);color:var(--ok)}.off{background:var(--badbg);color:var(--bad)}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:12px;color:var(--mut);font-weight:600}td.n,th.n{text-align:right}tr:last-child td{border-bottom:0}
td.q{white-space:normal;min-width:200px}.good{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.star{color:#d4a017}
.grp td{background:var(--bg);font-weight:600}ul{margin:6px 0;padding-left:20px}.help{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
"""


def write_dashboard(cfg, writing, acts, notes, cards, cl, eff, d0, d1):
    from html import escape as e
    n = lambda x: "—" if x is None else fmt(x)
    pct = lambda x: "—" if x is None else f"{x}%"
    tp = cfg["target_pos"]

    def poscell(p):
        return "<td class='n'>—</td>" if not p else f"<td class='n {'good' if p <= tp else 'bad'}'>{p:.0f}</td>"

    planned = {(a["advert_id"], a.get("nm_id"), a.get("query")): a for a in acts if a["type"] == "cluster_bid"}
    nxt = NOW + datetime.timedelta(minutes=30)
    wd_txt = "—" if cfg.get("_week_drr") is None else f"{cfg['_week_drr']:.1f}%"
    H = ["<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<meta http-equiv='refresh' content='300'><title>Бидер WB</title><style>{CSS}</style></head><body><main><h1>Бидер WB</h1>",
         f"<div class='mut'>Обновлено {NOW:%d.%m.%Y %H:%M}, следующий запуск примерно в {nxt:%H:%M}. Окно статистики {d0[8:]}.{d0[5:7]}–{d1[8:]}.{d1[5:7]}. "
         f"Цель ДРР {cfg['target_drr']}%. Главные запросы (ВЧ с заказами) держим в топ-{tp}, пока их ДРР ≤ {cfg['top_drr_cap']}%. "
         f"Общий ДРР за неделю: {wd_txt} "
         f"(лимит {cfg['weekly_drr_limit']}%, выше — ставки не поднимаются).</div><p>"]
    for cab in cfg["cabinets"]:
        on = cab in writing
        H.append(f"<span class='badge {'on' if on else 'off'}'>{e(cab)}: {'меняет ставки' if on else 'только план'}</span> ")
    H.append("</p>")
    if cfg.get("test_scope"):
        def lbl(t, q):
            cc = cfg["cards"].get(str(t["nm_id"]), {})
            return f"«{e(q)}» ≤{cc.get('top_pos', {}).get(q, cc.get('target_pos', tp))}"
        H.append("<p><b>Тест:</b> ставки меняются только здесь (≤N — целевая позиция в рекламе) — " + "; ".join(
            f"{e(str(cfg['cards'].get(str(t['nm_id']), {}).get('name', t['nm_id'])))}, РК {t['advert_id']}: {', '.join(lbl(t, q) for q in t.get('queries') or [])}"
            for t in cfg["test_scope"]) + ". Остальное только в плане.</p>")

    H.append("<h2>Карточки</h2><div class='wrap'><table><tr><th>Кабинет</th><th>Артикул</th><th class='n'>Расход, ₽</th><th class='n'>Заказы</th>"
             "<th class='n'>Заказы, ₽</th><th class='n'>ДРР</th><th class='n'>Сегодня, ₽</th></tr>")
    for r in sorted(cards, key=lambda r: -r["spend"]):
        drr = r["drr"]
        dc = "" if drr is None else ("good" if drr <= r["target"] else "bad")
        cap = " из " + n(r["cap"]) if r["cap"] else ""
        H.append(f"<tr><td>{e(r['cab'])}</td><td>{e(str(r['art']))}</td><td class='n'>{n(r['spend'])}</td><td class='n'>{r['cnt']}</td>"
                 f"<td class='n'>{n(r['rev'])}</td><td class='n {dc}'>{'—' if drr is None else f'{drr * 100:.1f}%'}</td>"
                 f"<td class='n'>{n(r['today'])}{cap}</td></tr>")
    H.append("</table></div>")

    ch = []
    try:
        since = (NOW - datetime.timedelta(days=3)).isoformat()
        ch = [json.loads(l) for l in open(os.path.join(BASE, "out", "changes.jsonl"), encoding="utf-8") if l.strip()]
        ch = [c for c in ch if c["time"] >= since][::-1]
    except FileNotFoundError:
        pass
    H.append(f"<h2>Изменения за 3 дня — {len(ch)}</h2>")
    if ch:
        H.append("<div class='wrap'><table><tr><th>Когда</th><th>Артикул</th><th>Запрос / место</th><th class='n'>Было</th><th class='n'>Стало</th>"
                 "<th class='n'>Позиция</th><th class='n'>ДРР</th><th>Причина</th><th></th></tr>")
        for c in ch:
            what = c.get("query") or {"pause": "пауза РК", "start": "запуск РК"}.get(c["type"]) or f"базовая ({c.get('placement')})"
            t = c["time"]
            ok = "✓" if c.get("ok") else "<span class='bad'>ошибка</span>"
            H.append(f"<tr><td>{t[8:10]}.{t[5:7]} {t[11:16]}</td><td>{e(str(c.get('art') or ''))}</td><td class='q'>{e(what)}</td>"
                     f"<td class='n'>{n(c.get('old'))}</td><td class='n'>{n(c.get('new'))}</td>{poscell(c.get('pos'))}"
                     f"<td class='n'>{pct(c.get('drr'))}</td><td class='q'>{e(c.get('reason') or '')}</td><td>{ok}</td></tr>")
        H.append("</table></div>")
    else:
        H.append("<p class='mut'>Пока ничего не менялось.</p>")

    H.append(f"<h2>До и после изменений — {len(eff)}</h2>")
    if eff:
        H.append("<p class='mut'>Средние за день: «до» — 3 полных дня перед изменением, «после» — полные дни после него (если их ещё нет — сегодняшний неполный день). "
                 "Заказы — все заказы карточки за день, не только с этого запроса. Зелёным — изменение в нужную сторону.</p>"
                 "<div class='wrap'><table><tr><th>Когда</th><th>Артикул</th><th>Запрос</th><th class='n'>Ставка</th><th class='n'>Позиция</th>"
                 "<th class='n'>Показы/день</th><th class='n'>Клики/день</th><th class='n'>Расход/день, ₽</th><th class='n'>Заказы карточки/день</th><th>После</th></tr>")

        def ba(b, a, key, digits=0, lower_better=False):
            num = lambda v: f"{v:,.{digits}f}".replace(",", " ")
            x, y = b.get(key), (a or {}).get(key)
            if x is None or y is None:
                return f"<td class='n'>{'—' if x is None else num(x)} → —</td>"
            cls = "" if abs(y - x) < 1e-9 else ("good" if (y < x) == lower_better else "bad")
            return f"<td class='n'>{num(x)} → <span class='{cls}'>{num(y)}</span></td>"

        for r in sorted(eff, key=lambda r: r["time"], reverse=True):
            t, b, a = r["time"], r["before"], r["after"]
            H.append(f"<tr><td>{t[8:10]}.{t[5:7]} {t[11:16]}</td><td>{e(str(r.get('art') or ''))}</td><td class='q'>{e(r['query'])}</td>"
                     f"<td class='n'>{n(r['old'])} → {n(r['new'])}</td>{ba(b, a, 'pos', 1, True)}{ba(b, a, 'views')}{ba(b, a, 'clicks')}"
                     f"{ba(b, a, 'spend', 0, True)}{ba(b, a, 'orders', 1)}<td class='mut'>{r['after_label']}</td></tr>")
        H.append("</table></div>")
    else:
        H.append("<p class='mut'>Изменений ставок кластеров за 10 дней нет.</p>")

    H.append(f"<h2>Кластеры (ручная CPM)</h2><p class='mut'>★ — главный запрос, держим в топ-{tp}. Позиция — средняя в рекламе за сегодня, вчера или за окно; "
             f"зелёная — в топ-{tp}.</p><div class='wrap'><table><tr><th></th><th>Запрос</th><th class='n'>Ставка, ₽</th><th class='n'>Позиция</th><th>за</th>"
             "<th class='n'>Sellmonitor</th><th class='n'>Расход, ₽</th><th class='n'>Заказы Джем</th><th class='n'>ДРР</th><th>План</th></tr>")
    grp = collections.defaultdict(list)
    for r in cl:
        grp[(r["cab"], r["art"], r["advert_id"], r["camp"])].append(r)
    for (cab, art, cid, camp), rows in sorted(grp.items(), key=lambda kv: -sum(r["spend"] for r in kv[1])):
        H.append(f"<tr class='grp'><td></td><td colspan='9'>{e(cab)} · {e(str(art))} · РК {cid} «{e(camp)}»</td></tr>")
        for r in sorted(rows, key=lambda r: (not r["top"], -r["spend"])):
            a = planned.get((r["advert_id"], r["nm_id"], r["query"]))
            plan = f"→ <b>{fmt(a['new'])}</b> {e(a['reason'])}" if a else ""
            base = "" if r["own"] else " <span class='mut'>баз.</span>"
            H.append(f"<tr><td class='star'>{'★' if r['top'] else ''}</td><td class='q'>{e(r['query'])}</td><td class='n'>{n(r['old'])}{base}</td>"
                     f"{poscell(r['pos'])}<td class='mut'>{r['pos_date']}</td><td class='n'>{n(r['sm_pos'])}</td><td class='n'>{n(r['spend'])}</td>"
                     f"<td class='n'>{n(r['jam_ord'])}</td><td class='n'>{pct(r.get('drr'))}</td><td class='q'>{plan}</td></tr>")
    H.append("</table></div>")

    hints = [a for a in acts if a["type"] == "minus_hint"]
    if hints:
        H.append("<h2>Кандидаты в минус-фразы</h2><ul>" + "".join(
            f"<li>{e(str(a['art']))}, РК {a['advert_id']}: «{e(a['query'])}» — {e(a['reason'])}</li>" for a in hints) + "</ul>")
    if notes:
        H.append("<h2>Заметки</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in notes) + "</ul>")
    H.append("<h2>Как управлять</h2><div class='help'>Напишите Codex, например: «сделай главными для Osempic1 запросы оземпик и таблетки для похудения», "
             "«цель ДРР для Оземп-1 — 18%», «не трогай РК 37120723», «выключи запись по BioNature». "
             "Настройки лежат в <code>~/wb-bidder/config.json</code>, страница обновляется после каждого запуска.</div></main></body></html>")
    open(os.path.join(BASE, "out", "dashboard.html"), "w", encoding="utf-8").write("".join(H))


def in_scope(a, cfg):
    # test_scope: [{"advert_id", "nm_id", "queries": [...]}] — пишем только эти кластеры; пусто — пишем всё
    scope = cfg.get("test_scope")
    if not scope:
        return True
    for s in scope:
        if a.get("advert_id") == s["advert_id"] and a.get("nm_id") == s["nm_id"] and a["type"] == "cluster_bid":
            if not s.get("queries") or exact(a.get("query") or "") in {exact(q) for q in s["queries"]}:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cab"); ap.add_argument("--apply", action="store_true"); ap.add_argument("--rollback")
    ap.add_argument("--config", default=os.path.join(BASE, "config.json"))
    args = ap.parse_args()
    lock = open(os.path.join(BASE, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("бидер уже запущен — выхожу"); return
    cfg = load(args.config)
    tokens = load(rel(cfg["tokens_file"]))
    os.makedirs(os.path.join(BASE, "out"), exist_ok=True)
    logf = open(os.path.join(BASE, "out", "log.txt"), "a", encoding="utf-8")

    def log(s):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {s}"
        print(line, flush=True); logf.write(line + "\n"); logf.flush()

    if args.rollback:
        rollback(args.rollback, tokens, log); return
    state = load(os.path.join(BASE, "state.json"), {"changed": {}, "paused": {}})
    market = load(rel(cfg["market_file"]), {})
    if market.get("updated"):
        age = (TODAY - datetime.date.fromisoformat(market["updated"][:10])).days
        if age > 2:
            log(f"market.json (Sellmonitor) устарел на {age} дн. — позиции и остатки Sellmonitor не учитываются"); market = {}
    d1 = (TODAY - datetime.timedelta(days=1)).isoformat()
    d0 = (TODAY - datetime.timedelta(days=cfg["lookback_days"])).isoformat()
    mode = "ЗАПИСЬ" if args.apply else "только план"
    cabs = [args.cab] if args.cab else list(tokens)
    if args.apply:
        allowed = set(cfg["apply"]["cabinets"])
        if not allowed & set(cabs):
            log("--apply: ни один кабинет не разрешён в config.apply.cabinets — запись отменена"); return
    all_acts, all_notes, summary, all_cards, all_cl, all_eff = [], [], [], [], [], []
    gs = {}
    for cab in cabs:
        g = gather(cab, tokens[cab], cfg, d0, d1, log)
        if g is not None:
            gs[cab] = g
    # общий ДРР по всем кабинетам за окно: выше weekly_drr_limit — ставки не поднимаем
    wsp = sum(sum(g["spend"].values()) for g in gs.values()); wrv = sum(sum(v[1] for v in g["orders"].values()) for g in gs.values())
    cfg["_week_drr"] = wsp / wrv * 100 if wrv else None
    if cfg["_week_drr"] is not None:
        log(f"общий ДРР за {d0}–{d1}: {cfg['_week_drr']:.1f}% (лимит {cfg['weekly_drr_limit']}%) — подъёмы {'разрешены' if cfg['_week_drr'] <= cfg['weekly_drr_limit'] else 'заморожены'}")
    if args.apply:
        tg_callbacks(tokens, cfg, state, log)
    for cab, g in gs.items():
        acts, notes, card_rows, cl_rows = decide(cab, g, cfg, market, state)
        handle_minus(cab, tokens[cab], acts, cfg, state, args.apply and cab in cfg["apply"]["cabinets"], log)
        all_eff += effect(cab, tokens[cab], g, d0, log)
        all_cards += card_rows; all_cl += cl_rows
        changes = [a for a in acts if a["type"] in ("cluster_bid", "base_bid")]
        if len(changes) > cfg["max_changes_per_run"]:
            keep = set(map(id, sorted(changes, key=lambda a: -a["spend"])[:cfg["max_changes_per_run"]]))
            notes.append(f"изменений {len(changes)} > лимита {cfg['max_changes_per_run']}: оставлены самые затратные")
            acts = [a for a in acts if a["type"] not in ("cluster_bid", "base_bid") or id(a) in keep]
        sp = sum(g["spend"].values()); rv = sum(v[1] for v in g["orders"].values())
        summary.append(f"- **{cab}**: расход {fmt(sp)} ₽, заказы {fmt(rv)} ₽, ДРР {sp / rv * 100 if rv else 0:.1f}%; "
                       f"сегодня потрачено {fmt(sum(g['spend_today'].values()))} ₽; действий: {len([a for a in acts if a['type'] != 'minus_hint'])}")
        for a in acts:
            a["out_of_scope"] = not in_scope(a, cfg)
            if a["out_of_scope"] and a["type"] != "minus_hint":
                a["reason"] += " — вне теста, не применяется"
        if args.apply and cab in cfg["apply"]["cabinets"] and any(a["type"] != "minus_hint" and not a["out_of_scope"] for a in acts):
            todo = [a for a in acts if a["type"] != "minus_hint" and not a.get("out_of_scope")]
            log(f"{cab}: бэкап → {backup(cab, tokens[cab], todo, cfg)}")
            apply(cab, tokens[cab], todo, state, log)
        all_acts += acts; all_notes += [f"{cab}: {n}" for n in notes]
    # plan_latest — всегда; копия с датой — только когда набор действий изменился (запуск раз в 30 мин)
    sig = lambda acts: sorted(f"{a['type']}|{a['advert_id']}|{a.get('nm_id')}|{a.get('query') or a.get('placement')}|{a.get('new')}" for a in acts)
    prev = load(os.path.join(BASE, "out", "plan_latest.json"), {})
    changed = sig(prev.get("actions") or []) != sig(all_acts)
    plan = {"time": NOW.isoformat(), "mode": mode, "window": [d0, d1], "actions": all_acts, "notes": all_notes}
    md = report(all_acts, all_notes, summary, all_cards, d0, d1, mode)
    names = ["plan_latest"] + ([f"plan_{NOW:%Y-%m-%d_%H%M}"] if changed else [])
    for n in names:
        save(os.path.join(BASE, "out", n + ".json"), plan)
        open(os.path.join(BASE, "out", n + ".md"), "w", encoding="utf-8").write(md)
    if args.apply:
        notify(gs, tokens, cfg, state, d0, d1, log)
        save(os.path.join(BASE, "state.json"), state)
    write_dashboard(cfg, cfg["apply"]["cabinets"], all_acts, all_notes, all_cards, all_cl, all_eff, d0, d1)
    log(f"готово: out/{names[-1]}.md, действий {len(all_acts)}{'' if changed else ' (без изменений)'}")


if __name__ == "__main__":
    main()
