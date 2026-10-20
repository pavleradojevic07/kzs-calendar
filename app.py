import json, re, os, datetime as dt
from flask import Flask, jsonify, send_from_directory, Response
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder="static")
DATA = os.environ.get("DATA_FILE", "data.json")
TEAMS = {
    "Senior": "https://www.kzs.si/ekipa/18662?tekmovanje=609&tab=tekme",
    "U20": "https://www.kzs.si/ekipa/18612?tekmovanje=624&tab=tekme",
}
DATE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
SCORE = re.compile(r"\b(\d{2,3})\s*[:\-–]\s*(\d{2,3})\b")
FIELDS = ["date", "time", "score", "details"]
FOCUS = re.compile(r"portoro[žz]", re.I)          # focus team: Portorož
VENUE = re.compile(r"dvoran|športn|sportn|hala|center|\bOŠ\b|šola", re.I)

def load():
    try:
        return json.load(open(DATA, encoding="utf-8"))
    except Exception:
        return {"games": [], "last_refresh": None, "last_changes": None}

def save(s):
    json.dump(s, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def render(url):
    """Load the JS-rendered page; return visible text + any JSON the page fetched."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(locale="sl-SI")
        blobs = []
        def grab(r):
            try:
                if "json" in (r.headers.get("content-type") or ""):
                    blobs.append({"url": r.url, "json": r.json()})
            except Exception:
                pass
        pg.on("response", grab)
        pg.goto(url, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2000)
        text = pg.inner_text("body")
        b.close()
    return text, blobs

def parse(team, text):
    """Best-effort: every line holding a date starts a game; the next few lines belong to it."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    blocks, cur = [], None
    for l in lines:
        if DATE.search(l):
            cur = [l]; blocks.append(cur)
        elif cur is not None and len(cur) < 7:
            cur.append(l)
    games, seen = [], {}
    for blk in blocks:
        joined = " | ".join(blk)
        d, m, y = DATE.search(blk[0]).groups()
        date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        t = TIME.search(joined)
        time = f"{int(t.group(1)):02d}:{t.group(2)}" if t else ""
        rest = TIME.sub("", DATE.sub("", joined))
        s = SCORE.search(rest)
        score = f"{s.group(1)}:{s.group(2)}" if s else ""
        title = next((x for x in blk if re.search(r"\s[-–:]\s", DATE.sub("", x)) and not DATE.fullmatch(x)), blk[-1])
        title = re.sub(r"\s+", " ", SCORE.sub("", TIME.sub("", DATE.sub("", title)))).strip(" -|,")
        parts = re.split(r"\s+[-–:]\s+", title, maxsplit=1)
        ha, opp = "", title
        if len(parts) == 2:
            if FOCUS.search(parts[0]): ha, opp = "H", parts[1]
            elif FOCUS.search(parts[1]): ha, opp = "A", parts[0]
        venue = next((x for x in blk[1:] if VENUE.search(x)), "")
        base = f"{team}|{title}"
        seen[base] = seen.get(base, 0) + 1
        games.append({"key": f"{base}|{seen[base]}", "team": team, "title": title,
                      "date": date, "time": time, "score": score, "details": joined,
                      "ha": ha, "opp": opp.strip(), "venue": venue})
    return games

def diff(old, new):
    o = {g["key"]: g for g in old}; n = {g["key"]: g for g in new}
    added = [n[k] for k in n if k not in o]
    removed = [o[k] for k in o if k not in n]
    changed = []
    for k in n:
        if k in o:
            ch = {f: [o[k][f], n[k][f]] for f in FIELDS[:3] if o[k][f] != n[k][f]}
            if ch:
                changed.append({"game": n[k], "changes": ch})
    return {"added": added, "removed": removed, "changed": changed}

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/api/games")
def games():
    return jsonify(load())

@app.post("/api/refresh")
def refresh():
    state, new, errors = load(), [], []
    for team, url in TEAMS.items():
        try:
            text, _ = render(url)
            new += parse(team, text)
        except Exception as e:
            errors.append(f"{team}: {e}")
    if errors and not new:
        return jsonify({"error": "; ".join(errors)}), 502
    first = state["last_refresh"] is None
    ch = diff(state["games"], new)
    ch["first_run"] = first
    ch["errors"] = errors
    ch["at"] = dt.datetime.now().isoformat(timespec="seconds")
    save({"games": new, "last_refresh": ch["at"], "last_changes": ch})
    return jsonify(load())

@app.get("/debug")
def debug():
    """Shows what the scraper sees, so the parser can be tuned if games don't appear."""
    out = {}
    for team, url in TEAMS.items():
        text, blobs = render(url)
        out[team] = {"text": text[:6000], "json_urls": [b["url"] for b in blobs],
                     "parsed": parse(team, text)}
    return jsonify(out)

@app.get("/calendar.ics")
def ics():
    def stamp(g, add=0):
        h, m = (g["time"] or "00:00").split(":")
        t = dt.datetime.fromisoformat(g["date"]) + dt.timedelta(hours=int(h) + add, minutes=int(m))
        return t.strftime("%Y%m%dT%H%M%S")
    ev = []
    for i, g in enumerate(load()["games"]):
        ev += ["BEGIN:VEVENT", f"UID:{abs(hash(g['key']))}@kzs-calendar",
               f"DTSTAMP:{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}",
               f"DTSTART;TZID=Europe/Ljubljana:{stamp(g)}", f"DTEND;TZID=Europe/Ljubljana:{stamp(g, 2)}",
               f"SUMMARY:[{'Člani' if g['team']=='Senior' else 'U20'}] {g['title']}" + (f" ({g['score']})" if g["score"] else ""),
               f"LOCATION:{g.get('venue') or ('Portorož' if g.get('ha')=='H' else g.get('opp',''))}",
               "END:VEVENT"]
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//kzs-calendar//EN"] + ev + ["END:VCALENDAR"])
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": "attachment; filename=basketball.ics"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
