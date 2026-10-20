import json, re, os, hashlib, unicodedata, difflib, datetime as dt
from flask import Flask, jsonify, send_from_directory, Response
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder="static")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_FILE", os.path.join(HERE, "data.json"))
# Competition names are configured here, NOT guessed from page text (page menus contain "Pokal Spar" etc.)
TEAMS = {
    "Senior": {"url": "https://www.kzs.si/ekipa/18662?tekmovanje=609&tab=tekme", "comp": "2. SKL"},
    "U20": {"url": "https://www.kzs.si/ekipa/18612?tekmovanje=624&tab=tekme", "comp": ""},
}
FIELDS = ["date", "time", "score", "hall", "ha"]

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def is_focus(name): return "portoroz" in norm(name).replace(" ", "")
def clean(s): return re.sub(r"\s+", " ", str(s or "")).strip(" -–|,")

ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
SLO = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?![\d])")
SCORE = re.compile(r"^\s*(\d{1,3})\s*(?:-|–|:)\s*(\d{1,3})\s*$")
ORD = {"prvi":1,"drugi":2,"tretji":3,"cetrti":4,"peti":5,"sesti":6,"sedmi":7,"osmi":8,"deveti":9,"deseti":10,
       "enajsti":11,"dvanajsti":12,"trinajsti":13,"stirinajsti":14,"petnajsti":15,"sestnajsti":16,
       "sedemnajsti":17,"osemnajsti":18,"devetnajsti":19,"dvajseti":20,"enaindvajseti":21,"dvaindvajseti":22}

def get_date(s):
    m = ISO.search(s)
    if m: y, mo, d = map(int, m.groups())
    else:
        m = SLO.search(s)
        if not m: return ""
        d, mo, y = map(int, m.groups())
    try: return dt.date(y, mo, d).isoformat()
    except ValueError: return ""

def get_time(s):
    s = SLO.sub("", ISO.sub("", s))
    m = TIME.search(s)
    t = f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""
    return "" if t == "00:00" else t

def get_score(s):
    m = SCORE.match(s or "")
    if not m or (":" in s and TIME.fullmatch(s.strip()) and int(m.group(1)) <= 23): return ""
    return f"{int(m.group(1))}:{int(m.group(2))}"

def round_of(s):
    n = norm(s)
    m = re.match(r"^(\d+) (?:kolo|krog)\b", n) or re.match(r"^(?:kolo|krog) (\d+)\b", n)
    if m: return m.group(1)
    w = n.split()
    return str(ORD[w[0]]) if len(w) >= 2 and w[0] in ORD and w[1] in ("kolo", "krog") else None

def build(team, rnd, date, time, home, away, score, hall, issues):
    """The ONLY place a game is created. Home = first-listed (Domov) team. Portorož must be exactly one side."""
    home, away = clean(home), clean(away)
    if not date or not home or not away:
        issues.append(f"incomplete row: {date or '?'} | {home or '?'} | {away or '?'}"); return None
    if is_focus(home) == is_focus(away):
        issues.append(f"{date}: '{home} – {away}' skipped (Portorož must be exactly one of the two teams)"); return None
    ha = "H" if is_focus(home) else "A"
    return {"team": team, "comp": TEAMS[team]["comp"], "round": rnd or "", "date": date, "time": time,
            "home": home, "away": away, "ha": ha, "opp": away if ha == "H" else home,
            "title": f"{home} – {away}", "score": score, "hall": clean(hall)}

# ---------- extractors (tried in order; first one that yields valid games wins) ----------
def from_items(team, items, issues):
    """items = headings + table rows in document order."""
    out, rnd, col = [], None, None
    for it in items:
        if "h" in it:
            rnd = round_of(it["h"]) or rnd; continue
        cells = it["r"]; names = [norm(c) for c in cells]
        if any(n == "datum" for n in names):
            col = {}
            for i, n in enumerate(names):
                if n == "datum": col["date"] = i
                elif n in ("domov", "home", "domaci"): col["home"] = i
                elif n in ("gost", "gostje", "away", "guest"): col["away"] = i
                elif "rezultat" in n or n in ("cas", "result"): col["mid"] = i
                elif "igrisc" in n or "dvoran" in n or n == "hall": col["hall"] = i
            continue
        c = col or {"date": 0, "home": 1, "mid": 2, "away": 3, "hall": 4}
        if not (len(cells) > max(c.values(), default=0) and get_date(cells[c["date"]])): continue
        mid = cells[c["mid"]] if "mid" in c else ""
        g = build(team, rnd, get_date(cells[c["date"]]), get_time(cells[c["date"]]) or get_time(mid),
                  cells[c["home"]], cells[c["away"]], get_score(mid), cells[c["hall"]] if "hall" in c else "", issues)
        if g: out.append(g)
    return out

def name_of(v):
    if isinstance(v, dict):
        for k in ("name", "title", "teamName", "shortName", "naziv"):
            if isinstance(v.get(k), str): return v[k]
        return ""
    return v if isinstance(v, str) else ""

def from_json(team, blobs, issues):
    out = []
    def pick(d, pat):
        for k, v in d.items():
            if re.search(pat, k, re.I) and v not in (None, ""): return v
    def walk(o, rnd=None):
        if isinstance(o, list): [walk(x, rnd) for x in o]
        elif isinstance(o, dict):
            h = pick(o, r"^(home|domov|host|team_?1|home_?team|domaci)"); a = pick(o, r"^(away|gost|guest|visitor|team_?2|away_?team)")
            d = pick(o, r"date|datum|start|termin")
            if h and a and d and name_of(h) and name_of(a):
                ds = str(d); date = get_date(ds) or (dt.datetime.fromtimestamp(d/1000).date().isoformat() if isinstance(d, (int, float)) and d > 1e11 else "")
                r = pick(o, r"round|kolo|krog"); hs = pick(o, r"(home|domov).*(score|result|points)"); as_ = pick(o, r"(away|gost).*(score|result|points)")
                hall = name_of(pick(o, r"hall|dvoran|venue|igrisc|location|court") or "")
                g = build(team, str(r) if r else rnd, date, get_time(ds) if "T" in ds or " " in ds else "", name_of(h), name_of(a),
                          f"{hs}:{as_}" if hs is not None and as_ is not None else "", hall, issues)
                if g: out.append(g)
            else:
                for v in o.values(): walk(v, rnd)
    for b in blobs: walk(b["json"])
    return out

def from_text(team, text, issues):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out, rnd, i = [], None, 0
    starts = [k for k, l in enumerate(lines) if get_date(l)]
    for n, s in enumerate(starts):
        for l in lines[(starts[n-1] if n else 0):s]:
            rnd = round_of(l) or rnd
        end = starts[n+1] if n+1 < len(starts) else len(lines)
        blk = lines[s:end][:8]; body = blk[1:]
        mi = next((k for k, l in enumerate(body) if get_score(l) or (TIME.fullmatch(l) is not None)), None)
        if mi is not None and 1 <= mi and mi + 1 < len(body):
            home, away, hall = body[mi-1], body[mi+1], (body[mi+2] if mi + 2 < len(body) else "")
            mid = body[mi]
        else:
            sep = next((l for l in body if re.search(r"\s[-–]\s", l) and not get_score(l)), None)
            if not sep: issues.append(f"{get_date(blk[0])}: could not read teams from {blk[:4]}"); continue
            home, away = re.split(r"\s+[-–]\s+", sep, maxsplit=1); mid = " ".join(body)
            hall = next((l for l in body if l != sep and not TIME.fullmatch(l) and not get_score(l)), "")
        g = build(team, rnd, get_date(blk[0]), get_time(blk[0]) or get_time(mid), home, away, get_score(mid), hall, issues)
        if g: out.append(g)
    return out

def extract(team, page):
    issues, res = [], {}
    for name, fn, arg in (("table", from_items, page["items"]), ("json", from_json, page["blobs"]), ("text", from_text, page["text"])):
        iss = []; games = fn(team, arg, iss); res[name] = (games, iss)
        if games: return games, name, iss
    return [], "none", [x for _, i in res.values() for x in i]

def number(games):  # stable keys: team|opponent|home-or-away|occurrence
    seen = {}
    for g in games:
        b = f"{g['team']}|{norm(g['opp'])}|{g['ha']}"; seen[b] = seen.get(b, 0) + 1; g["key"] = f"{b}|{seen[b]}"
    return games

# ---------- reference check (your spreadsheet) ----------
def similar(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() > .8)

def check(team, games):
    info = {"count": len(games), "home": sum(g["ha"] == "H" for g in games), "away": sum(g["ha"] == "A" for g in games)}
    if team != "Senior": return info
    try: ref = [r for r in json.load(open(os.path.join(HERE, "reference.json"), encoding="utf-8")) if r["comp"] == "2. SKL"]
    except Exception: return info
    used, missing, diffs = set(), [], []
    for r in ref:
        c = [g for g in games if g["key"] not in used and g["ha"] == r["ha"] and similar(g["opp"], r["opp"])]
        if not c: missing.append(f"round {r['round']}: {'vs' if r['ha']=='H' else '@'} {r['opp']} ({r['date']})"); continue
        g = c[0]; used.add(g["key"])
        if g["date"] != r["date"]: diffs.append(f"{g['opp']}: date site {g['date']} vs reference {r['date']}")
        if r["time"] and g["time"] and g["time"] != r["time"]: diffs.append(f"{g['opp']}: time site {g['time']} vs reference {r['time']}")
    extra = [f"{g['title']} ({g['date']})" for g in games if g["key"] not in used]
    info.update(reference=len(ref), matched=len(used), missing=missing, extra=extra, differences=diffs)
    return info

# ---------- state, diff, scraping ----------
def load():
    try: return json.load(open(DATA, encoding="utf-8"))
    except Exception: return {"games": [], "last_refresh": None, "last_changes": None, "checks": {}, "issues": {}}

def diff(old, new):
    o = {g["key"]: g for g in old}; n = {g["key"]: g for g in new}
    ch = [{"game": n[k], "changes": {f: [o[k].get(f, ""), n[k].get(f, "")] for f in FIELDS if o[k].get(f, "") != n[k].get(f, "")}} for k in n if k in o]
    return {"added": [n[k] for k in n if k not in o], "removed": [o[k] for k in o if k not in n], "changed": [c for c in ch if c["changes"]]}

def render(url):
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(locale="sl-SI"); blobs = []
        def grab(r):
            try:
                if "json" in (r.headers.get("content-type") or ""): blobs.append({"url": r.url, "json": r.json()})
            except Exception: pass
        pg.on("response", grab)
        pg.goto(url, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2500); pg.mouse.wheel(0, 20000); pg.wait_for_timeout(1500)
        items = pg.evaluate("""[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,tr')].map(e=>e.tagName==='TR'?{r:[...e.querySelectorAll('th,td')].map(c=>c.innerText.trim())}:{h:e.innerText.trim()})""")
        text = pg.inner_text("body"); b.close()
    return {"items": items, "blobs": blobs, "text": text}

@app.get("/")
def index(): return send_from_directory("static", "index.html")

@app.get("/api/games")
def games(): return jsonify(load())

@app.post("/api/refresh")
def refresh():
    st = load(); keep, new, ok, errors, checks, issues = {}, [], [], [], {}, {}
    for team, cfg in TEAMS.items():
        try:
            gs, how, iss = extract(team, render(cfg["url"]))
        except Exception as e:
            gs, how, iss = [], "error", [str(e)]
        issues[team] = iss
        if gs:
            gs = number(gs); new += gs; ok.append(team); checks[team] = dict(check(team, gs), source=how)
        else:   # never wipe a team's calendar because one scrape failed
            new += [g for g in st["games"] if g["team"] == team]; errors.append(f"{team}: no valid games found ({'; '.join(iss[:2]) or 'page had no readable matches'})")
    if not ok: return jsonify({"error": "; ".join(errors)}), 502
    ch = diff([g for g in st["games"] if g["team"] in ok], [g for g in new if g["team"] in ok])
    ch.update(first_run=st["last_refresh"] is None, errors=errors, at=dt.datetime.now().isoformat(timespec="seconds"))
    json.dump({"games": new, "last_refresh": ch["at"], "last_changes": ch, "checks": checks, "issues": issues}, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return jsonify(load())

@app.get("/debug")
def debug():
    out = {}
    for team, cfg in TEAMS.items():
        pg = render(cfg["url"]); gs, how, iss = extract(team, pg)
        out[team] = {"extractor": how, "games_found": len(gs), "issues": iss[:20], "sample": gs[:3], "first_items": pg["items"][:25],
                     "json_urls": [b["url"] for b in pg["blobs"]], "json_head": [json.dumps(b["json"], ensure_ascii=False)[:500] for b in pg["blobs"][:4]],
                     "text_head": pg["text"][:2500]}
    return jsonify(out)

@app.get("/calendar.ics")
def ics():
    ev = []
    for g in load()["games"]:
        d = g["date"].replace("-", ""); end = (dt.date.fromisoformat(g["date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
        if g["time"]:
            s = f"{d}T{g['time'].replace(':', '')}00"; e = (dt.datetime.fromisoformat(g["date"] + "T" + g["time"]) + dt.timedelta(hours=2)).strftime("%Y%m%dT%H%M%S")
            times = [f"DTSTART;TZID=Europe/Ljubljana:{s}", f"DTEND;TZID=Europe/Ljubljana:{e}"]
        else: times = [f"DTSTART;VALUE=DATE:{d}", f"DTEND;VALUE=DATE:{end}"]
        ev += ["BEGIN:VEVENT", "UID:" + hashlib.md5(g["key"].encode()).hexdigest() + "@kzs-calendar",
               f"DTSTAMP:{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}", *times,
               f"SUMMARY:{'🏠' if g['ha']=='H' else '✈️'} [{'Člani' if g['team']=='Senior' else 'U20'}] {g['title']}" + (f" ({g['score']})" if g["score"] else ""),
               f"LOCATION:{g['hall']}", f"DESCRIPTION:{' • '.join(x for x in [g['comp'], (g['round'] + '. kolo') if g['round'] else ''] if x)}", "END:VEVENT"]
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//kzs-calendar//EN"] + ev + ["END:VCALENDAR"])
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": "attachment; filename=basketball.ics"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
