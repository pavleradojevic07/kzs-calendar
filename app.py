
    
  
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
TIME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])[:.]([0-5]\d)(?::([0-5]\d))?(?![\d])")
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

def build(team, rnd, date, time, home, away, score, hall, issues, comp=None):
    """The ONLY place a game is created. Home = first-listed (Domov) team. Portorož must be exactly one side."""
    home, away = clean(home), clean(away)
    if not date or not home or not away:
        issues.append(f"incomplete row: {date or '?'} | {home or '?'} | {away or '?'}"); return None
    if is_focus(home) == is_focus(away):
        issues.append(f"{date}: '{home} – {away}' skipped (Portorož must be exactly one of the two teams)"); return None
    ha = "H" if is_focus(home) else "A"
    return {"team": team, "comp": clean(comp) or TEAMS[team]["comp"], "round": rnd or "", "date": date, "time": time,
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

