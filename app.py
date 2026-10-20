import json, re, os, hashlib, unicodedata, threading, datetime as dt
from contextlib import contextmanager
from flask import Flask, jsonify, send_from_directory, Response
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder="static")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_FILE", os.path.join(HERE, "data.json"))
REFERENCE = os.path.join(HERE, "reference.json")
# Fallback competition name, used only when a game row has no competition line of its own.
TEAMS = {
    "Senior": {"url": "https://www.kzs.si/ekipa/18662?tekmovanje=609&tab=tekme", "comp": "2. SKL"},
    "U20": {"url": "https://www.kzs.si/ekipa/18612?tekmovanje=624&tab=tekme", "comp": ""},
}
FIELDS = ["date", "time", "score", "hall", "ha"]
# kzs.si turns the kick-off into the BROWSER's timezone. A server browser runs in UTC, which made every game 1-2 h early
# (and no-time games the day before). The browser must behave like one in Slovenia.
TZ = "Europe/Ljubljana"
LOCK = threading.Lock()

# ---------- small helpers ----------
def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def is_focus(name): return "portoroz" in norm(name).replace(" ", "")
def clean(s): return re.sub(r"\s+", " ", str(s or "")).strip(" -–|,")

ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
SLO = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
TIME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])[:.]([0-5]\d)(?::([0-5]\d))?(?![\d])")
SCORE = re.compile(r"^\s*(\d{1,3})\s*(?:-|–|:)\s*(\d{1,3})\s*$")

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
    m = re.match(r"^\s*(\d+)\.?\s*(?:kolo|krog)\b", str(s or ""), re.IGNORECASE)
    return m.group(1) if m else ""

def build(team, rnd, date, time, home, away, score, hall, issues, comp=None):
    """The ONLY place a game is created. Home = first-listed (host) team. Portorož must be exactly one side."""
    home, away = clean(home), clean(away)
    if not date or not home or not away:
        issues.append(f"incomplete row: {date or '?'} | {home or '?'} | {away or '?'}"); return None
    if is_focus(home) == is_focus(away):
        issues.append(f"{date}: '{home} – {away}' skipped (Portorož must be exactly one of the two teams)"); return None
    ha = "H" if is_focus(home) else "A"
    return {"team": team, "comp": clean(comp) or TEAMS[team]["comp"], "round": rnd or "", "date": date, "time": time,
            "home": home, "away": away, "ha": ha, "opp": away if ha == "H" else home,
            "title": f"{home} – {away}", "score": score, "hall": clean(hall)}

# ---------- scraping: the method from kzs-calendar.py ----------
# Render the team page, take the visible text of <body>, keep the lines between the "Razpored" heading
# and the first sponsor heading, then split them into games at every date line.
LINE_DATE = re.compile(r"^\d{1,2}\.\s*\d{1,2}\.\s*\d{4}$")
LINE_TIME = re.compile(r"^\d{1,2}[.:]\d{2}$")
LINE_ROUND = re.compile(r"^\d+\.\s*kolo$", re.IGNORECASE)
SPONSOR = re.compile(r"^(?:Platinasti sponzor(?:ji)?|Generalni sponzor|Zlati sponzorji|"
                     r"Srebrni sponzorji|Bronasti sponzorji)$", re.IGNORECASE)

@contextmanager
def browser_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            yield browser.new_context(timezone_id=TZ, locale="sl-SI").new_page()
        finally:
            browser.close()

def fetch_text(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.locator("body").wait_for()
    page.wait_for_timeout(2000)
    return page.locator("body").inner_text()

def page_lines(text):
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if l.lower() == "razpored"), None)
    if start is None: raise ValueError("'Razpored' not found on the page")
    end = next((i for i in range(start + 1, len(lines)) if SPONSOR.match(lines[i])), len(lines))
    return lines[start + 1:end]

def parse_games(lines):
    """Each game starts at a date line: date, competition, hall, 'N. kolo', host, [time or score], guest."""
    starts = [i for i, l in enumerate(lines) if LINE_DATE.fullmatch(l)]
    games = []
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(lines)
        date, fields = lines[s], [f for f in lines[s + 1:e] if f not in {"-", "VS", "vs"}]
        rounds = [i for i, f in enumerate(fields) if LINE_ROUND.fullmatch(f)]
        if len(rounds) != 1:
            games.append({"date": date, "error": "Could not identify exactly one round", "raw": fields}); continue
        if rounds[0] != 2 or len(fields) < 5:
            games.append({"date": date, "error": "Unexpected number/order of fields", "raw": fields}); continue
        rem = fields[3:]
        if len(rem) == 3 and (LINE_TIME.fullmatch(rem[1]) or SCORE.match(rem[1])): host, mid, guest = rem
        elif len(rem) == 4 and rem[1].isdigit() and rem[2].isdigit(): host, mid, guest = rem[0], f"{rem[1]}:{rem[2]}", rem[3]
        elif len(rem) == 2: host, guest = rem; mid = ""
        else:
            games.append({"date": date, "error": "Unexpected host/time/guest fields", "raw": fields}); continue
        games.append({"date": date, "competition": fields[0], "hall": fields[1], "round": fields[2],
                      "host": host, "mid": mid, "guest": guest})
    return games

def from_lines(team, lines, issues):
    out = []
    for g in parse_games(lines):
        if "error" in g:
            issues.append(f"{g['date']}: {g['error']} {g['raw']}"); continue
        score = get_score(g["mid"])
        game = build(team, round_of(g["round"]), get_date(g["date"]), "" if score else get_time(g["mid"]),
                     g["host"], g["guest"], score, g["hall"], issues, comp=g["competition"])
        if game: out.append(game)
    return out

def from_text(team, text, issues):
    return from_lines(team, page_lines(text), issues)

def number(games):
    """Sort games and give each a stable key (team + competition + round), so a moved game shows as 'changed', not removed+added."""
    games = sorted(games, key=lambda g: (g["date"], g["time"] or "99:99", g["team"]))
    seen = {}
    for g in games:
        base = "|".join([g["team"], norm(g["comp"]), g["round"] or f"{g['date']}-{norm(g['opp'])}"])
        seen[base] = seen.get(base, 0) + 1
        g["key"] = base if seen[base] == 1 else f"{base}#{seen[base]}"
    return games

# ---------- reference check ----------
def load_reference():
    try:
        with open(REFERENCE, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return []

def check(team, games, today=None):
    """Compare the scraped games with reference.json. Information only: the website is always the source of truth."""
    res = {"count": len(games), "home": sum(g["ha"] == "H" for g in games), "away": sum(g["ha"] == "A" for g in games)}
    comp = TEAMS[team]["comp"]
    ref = [r for r in load_reference() if comp and norm(r["comp"]) == norm(comp)]
    if not ref: return res
    today = today or dt.date.today().isoformat()
    site = [g for g in games if norm(g["comp"]) == norm(comp)]
    by_round = {g["round"]: g for g in site if g["round"]}
    used, matched, diffs, missing, past = set(), 0, [], [], []
    for r in ref:
        label = f"{r['date']} {'vs' if r['ha'] == 'H' else '@'} {r['opp']}"
        g = by_round.get(str(r["round"])) or next((x for x in site if x["date"] == r["date"] and norm(x["opp"]) == norm(r["opp"])), None)
        if not g:
            (past if r["date"] < today else missing).append(f"round {r['round']}: {label}"); continue
        used.add(g["key"]); d = []
        if norm(g["opp"]) != norm(r["opp"]): d.append(f"opponent is {g['opp']} on site")
        if g["date"] != r["date"]: d.append(f"moved to {g['date']}")
        if g["ha"] != r["ha"]: d.append(f"home/away is {g['ha']} on site, {r['ha']} in reference")
        if r["time"] and g["time"] != r["time"]: d.append(f"time is {g['time'] or '–'} on site, {r['time']} in reference")
        if r["hall"] and norm(g["hall"]) != norm(r["hall"]): d.append(f"hall is '{g['hall'] or '–'}' on site")
        if d: diffs.append(f"round {r['round']}: {label}: " + "; ".join(d))
        else: matched += 1
    extra = [f"round {g['round']}: {g['date']} {'vs' if g['ha'] == 'H' else '@'} {g['opp']}" for g in site if g["key"] not in used]
    res.update(reference=len(ref), matched=matched, differences=diffs, missing=missing, past=past, extra=extra)
    return res

# ---------- storage + refresh ----------
def load():
    try:
        with open(DATA, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError):
        return {"games": [], "last_refresh": None, "last_changes": None, "checks": {}, "issues": {}}

def save(state):
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA)

def diff(old, new):
    o, n = {g["key"]: g for g in old}, {g["key"]: g for g in new}
    changed = []
    for k in n:
        if k in o:
            ch = {f: [o[k].get(f, ""), n[k].get(f, "")] for f in FIELDS if o[k].get(f, "") != n[k].get(f, "")}
            if ch: changed.append({"game": n[k], "changes": ch})
    return [n[k] for k in n if k not in o], [o[k] for k in o if k not in n], changed

def refresh():
    with LOCK:
        state = load()
        old = state.get("games", [])
        fresh, errors = {}, []
        checks, issues = dict(state.get("checks") or {}), dict(state.get("issues") or {})
        with browser_page() as page:
            for team, cfg in TEAMS.items():
                iss = []
                try:
                    gs = from_text(team, fetch_text(page, cfg["url"]), iss)
                    if not gs: raise ValueError("no games found" + (f" ({iss[0]})" if iss else ""))
                    fresh[team] = number(gs); checks[team] = check(team, fresh[team]); issues[team] = iss
                except Exception as exc:
                    errors.append(f"{team}: {exc}")
        if not fresh: return {"error": "; ".join(errors) or "nothing could be loaded"}
        new = sum(fresh.values(), [])
        new_keys, today = {g["key"] for g in new}, dt.date.today().isoformat()
        # the schedule page only lists upcoming games: a game that was known, is now gone and lies in the past was played -> keep it
        played = [g for g in old if g["team"] in fresh and g["key"] not in new_keys and g["date"] < today]
        pk = {g["key"] for g in played}
        added, removed, changed = diff([g for g in old if g["team"] in fresh and g["key"] not in pk], new)
        kept = [g for g in old if g["team"] not in fresh]  # a team that failed keeps its previous games
        state.update(games=number(kept + played + new), checks=checks, issues=issues,
                     last_refresh=dt.datetime.now(dt.timezone.utc).isoformat(),
                     last_changes={"first_run": not old, "added": added, "removed": removed, "changed": changed, "errors": errors})
        save(state)
        return state

# ---------- calendar export ----------
def esc(s): return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

def fold(line):
    out, cur, n = [], "", 0
    for ch in line:
        w = len(ch.encode("utf-8"))
        if n + w > (74 if out else 75): out.append(cur); cur, n = "", 0
        cur += ch; n += w
    return "\r\n ".join(out + [cur])

def make_ics(games):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Portorož basketball//KZS//EN", "CALSCALE:GREGORIAN",
         "X-WR-CALNAME:Portorož basketball"]
    for g in games:
        d = dt.date.fromisoformat(g["date"])
        label = "ČLANI" if g["team"] == "Senior" else g["team"]
        title = f"{label}: {g['title']}" + (f" ({g['score']})" if g.get("score") else "")
        L += ["BEGIN:VEVENT", f"UID:{hashlib.sha1(g['key'].encode()).hexdigest()}@portoroz-basketball", f"DTSTAMP:{stamp}"]
        if g.get("time"):
            start = dt.datetime.combine(d, dt.time.fromisoformat(g["time"]))
            L += [f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{start + dt.timedelta(hours=2):%Y%m%dT%H%M%S}"]
        else:
            L += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}"]
        L.append(f"SUMMARY:{esc(title)}")
        if g.get("hall"): L.append(f"LOCATION:{esc(g['hall'])}")
        desc = " • ".join(x for x in [g.get("comp"), f"{g['round']}. kolo" if g.get("round") else "",
                                      "HOME" if g["ha"] == "H" else "AWAY"] if x)
        L += [f"DESCRIPTION:{esc(desc)}", "END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"

# ---------- routes ----------
@app.get("/")
def index(): return send_from_directory(HERE, "index.html")

@app.get("/api/games")
def api_games(): return jsonify(load())

@app.post("/api/refresh")
def api_refresh():
    try: res = refresh()
    except Exception as exc: return jsonify({"error": str(exc)}), 500
    return (jsonify(res), 502) if "error" in res else jsonify(res)

@app.get("/api/debug")
def api_debug():
    """What the scraper's browser actually sees (timezone + the schedule lines) - for troubleshooting."""
    out = {}
    with LOCK, browser_page() as page:
        for team, cfg in TEAMS.items():
            try:
                text = fetch_text(page, cfg["url"])
                out[team] = {"browser_timezone": page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone"),
                             "schedule_lines": page_lines(text)[:400]}
            except Exception as exc:
                out[team] = {"error": str(exc)}
    return jsonify(out)

@app.get("/calendar.ics")
def calendar_ics():
    return Response(make_ics(load().get("games", [])), mimetype="text/calendar",
                    headers={"Content-Disposition": "attachment; filename=portoroz-basketball.ics"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
