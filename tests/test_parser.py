"""Run: python tests/test_parser.py   (no browser needed). Builds fake KZS pages from reference.json in 3 layouts."""
import json, os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
m = types.ModuleType("playwright.sync_api"); m.sync_playwright = None; sys.modules["playwright.sync_api"] = m
import app

REF = [r for r in json.load(open(os.path.join(app.HERE, "reference.json"), encoding="utf-8")) if r["comp"] == "2. SKL"]
def pair(r): return ("Portorož", r["opp"]) if r["ha"] == "H" else (r["opp"], "Portorož")
def sl(d): y, mo, da = d.split("-"); return f"{int(da)}. {int(mo)}. {y}"

# layout 1: table rows (Domov | Čas/Rezultat | Gost | Igrišče), with "N. KROG" headings
items = []
for r in REF:
    h, a = pair(r); items.append({"h": f"{r['round']}. KROG"})
    items.append({"r": ["Datum", "Domov", "Čas/Rezultat", "Gost", "Igrišče-dvorana"]})
    items.append({"r": [f"{r['date']} 19:00:00{sl(r['date'])}", h, r["time"] or "19:00", a, r["hall"]]})
# layout 2: plain text lines  date / home / time / away / hall
text = "\n".join(f"{r['round']}. kolo\nsob., {sl(r['date'])}\n{pair(r)[0]}\n{r['time'] or '19:00'}\n{pair(r)[1]}\n{r['hall']}" for r in REF)
# layout 3: JSON api style
blobs = [{"url": "x", "json": {"data": [{"round": r["round"], "date": r["date"] + "T19:00:00", "homeTeam": {"name": pair(r)[0]}, "awayTeam": {"name": pair(r)[1]}, "hall": {"name": r["hall"]}} for r in REF]}}]

# layout 4 (the REAL kzs.si layout from your error message): home / away / date / competition / hall / "N. kolo"
text2 = "Tekme\nLestvica\n" + "\n".join(f"{pair(r)[0]}\n{pair(r)[1]}\n{sl(r['date'])}\n2. SKL\n{r['hall']}\n{r['round']}. kolo" for r in REF)
fails = 0
def ok(cond, msg):
    global fails; fails += (not cond); print(("PASS " if cond else "FAIL ") + msg)

for name, fn, arg in (("table", app.from_items, items), ("json", app.from_json, blobs), ("real-layout", app.from_text, text2)):
    iss = []; gs = app.number(fn("Senior", arg, iss)); c = app.check("Senior", gs)
    ok(len(gs) == 22 and c["matched"] == 22 and not c["missing"], f"{name}: 22/22 games match the reference ({c.get('matched')}/{c.get('reference')})")
    ok(all(not app.is_focus(g["opp"]) for g in gs), f"{name}: opponent is never Portorož")
    ok(all((g["ha"] == "H") == app.is_focus(g["home"]) for g in gs), f"{name}: home = first-listed team, Portorož home => H")
    ok(sum(g["ha"] == "H" for g in gs) == 11, f"{name}: 11 home / 11 away")
    ok(not iss, f"{name}: no rejected rows")

iss = []
gs = app.from_text("Senior", "Celje\nPortorož\n24. 10. 2026\n2. SKL\n1. OŠ Celje\n5. kolo\nPortorož\nTerme Olimia Podčetrtek\n30. 10. 2026\n2. SKL\nŠD Lucija\n6. kolo", iss)
ok(len(gs) == 2 and gs[0]["ha"] == "A" and gs[0]["opp"] == "Celje" and gs[0]["round"] == "5" and gs[0]["hall"] == "1. OŠ Celje" and gs[0]["comp"] == "2. SKL", "your Oct 24 game: AWAY @ Celje, 1. OŠ Celje, 2. SKL, round 5")
ok(gs[1]["ha"] == "H" and gs[1]["opp"] == "Terme Olimia Podčetrtek" and gs[1]["date"] == "2026-10-30", "your Oct 30 game: HOME vs Terme Olimia")
gs = app.from_text("U20", "Portorož\nKoper\n25. 10. 2026\nFantje U20 1.B SKL\nŠD Lucija\n4. kolo", [])
ok(len(gs) == 1 and gs[0]["comp"] == "Fantje U20 1.B SKL" and gs[0]["ha"] == "H", "U20 competition name is read from the game row")
sw = app.number([dict(g, ha="A" if g["ha"] == "H" else "H") for g in app.number(app.from_text("Senior", text2, []))])
ok(len(app.check("Senior", sw)["differences"]) >= 20, "reversed home/away is reported clearly by the reference check")
g = app.build("Senior", "1", "2026-09-26", "", "Portorož", "Celje", "", "", [])
ok(g["ha"] == "H" and g["opp"] == "Celje", "Portorož–Celje is HOME vs Celje")
g = app.build("Senior", "1", "2026-09-26", "", "Slovan", "Portorož", "", "", [])
ok(g["ha"] == "A" and g["opp"] == "Slovan", "Slovan–Portorož is AWAY, opponent Slovan")
iss = []; ok(app.build("Senior", "1", "2026-09-26", "", "Slovan", "Celje", "", "", iss) is None and iss, "game without Portorož is rejected, not shown")
ok(app.build("Senior", "1", "2026-09-26", "", "KK Portoroz", "Portorož 2", "", "", []) is None, "both sides Portorož is rejected")
ok(app.get_score("78 - 68") == "78:68" and app.get_score("19:00") == "" and app.get_time("2026-09-26 00:00:00") == "", "score / time / midnight parsing")
print("\nALL PASSED" if not fails else f"\n{fails} FAILED"); sys.exit(bool(fails))
