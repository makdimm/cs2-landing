from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import sqlite3
import os
import shutil
import asyncio

app = FastAPI(title="CS2 HiddenAI")

DB_PATH = os.getenv("DB_PATH", "/db/cs_bot.db")
WORK_DB = "/tmp/cs_bot_copy.db"

REFRESH_INTERVAL = int(os.getenv("DB_REFRESH_SEC", "30"))

def init_db():
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, WORK_DB)

init_db()


async def refresh_db_loop():
    """Фоновое обновление копии БД каждые REFRESH_INTERVAL секунд"""
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        try:
            if os.path.exists(DB_PATH):
                shutil.copy2(DB_PATH, WORK_DB)
        except Exception:
            pass


@app.on_event("startup")
async def startup():
    asyncio.create_task(refresh_db_loop())

templates = Jinja2Templates(directory="templates")

def get_db():
    conn = sqlite3.connect(WORK_DB, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ── Все игроки (без фильтра по дням) ──
ACTIVE_FILTER_SQL = ""


def get_daily_impact_for_player(conn, player_name):
    """Get a list of per-day impact values for the last 8 game dates."""
    days = conn.execute(
        "SELECT DISTINCT match_date FROM matches ORDER BY match_date DESC LIMIT 8"
    ).fetchall()
    if not days:
        return []
    day_dates = sorted([d[0] for d in days])
    result = []
    for day in day_dates:
        row = conn.execute("""
            SELECT ROUND(AVG(ps.kd), 2) AS kd,
                   ROUND(AVG(ps.fk - ps.fd), 1) AS entries,
                   ROUND(AVG(ps.clutch_1v5 + ps.clutch_1v4 + ps.clutch_1v3), 2) AS clutches,
                   ROUND(AVG(ps.multikill_3k + ps.multikill_4k + ps.multikill_5k), 2) AS multi,
                   ROUND(AVG(ps.adr), 1) AS adr
            FROM player_stats ps
            JOIN matches m ON ps.match_id = m.id
            WHERE ps.player_name = ? AND m.match_date = ?
        """, (player_name, day)).fetchone()
        if row and row["kd"] is not None:
            entries_val = row["entries"] if row["entries"] and row["entries"] > 0 else 0
            impact = ((row["kd"] * 1.0) + (entries_val * 0.15) + (row["clutches"] * 0.1) + (row["multi"] * 0.1) + (row["adr"] * 0.01)) / 2.0
            impact = min(impact, 2.0)
            result.append({"date": day, "impact": round(impact, 2)})
    return result


def get_impact_rating(limit=20):
    conn = get_db()
    sql = f"""
    WITH player_agg AS (
        SELECT ps.player_name,
               COUNT(DISTINCT m.match_date) AS games,
               ROUND(AVG(ps.kd), 2) AS kd,
               ROUND(AVG(ps.adr), 1) AS adr,
               ROUND(AVG(ps.hs_percent), 1) AS hs,
               ROUND(AVG(ps.fk - ps.fd), 1) AS entries,
               ROUND(AVG(ps.clutch_1v5 + ps.clutch_1v4 + ps.clutch_1v3), 2) AS clutches,
               ROUND(AVG(ps.multikill_3k + ps.multikill_4k + ps.multikill_5k), 2) AS multi
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE 1=1 {ACTIVE_FILTER_SQL}
        GROUP BY ps.player_name
    )
    SELECT *, games AS games,
        ROUND(MIN(((kd * 1.0) + (CASE WHEN entries > 0 THEN entries ELSE 0 END * 0.15) + (clutches * 0.1) + (multi * 0.1) + (ADR * 0.01)) / 2.0, 2.0), 2) AS impact
    FROM player_agg
    ORDER BY CASE WHEN games >= 3 THEN 0 ELSE 1 END, impact DESC LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    players = [dict(r) for r in rows]
    for p in players:
        p["daily_impacts"] = get_daily_impact_for_player(conn, p["player_name"])
    conn.close()
    return players


def get_kd_rating(limit=20):
    conn = get_db()
    sql = f"""
        SELECT ps.player_name,
               COUNT(DISTINCT m.match_date) AS games,
               ROUND(AVG(ps.kd), 2) AS kd,
               ROUND(AVG(ps.adr), 1) AS adr,
               SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE 1=1 {ACTIVE_FILTER_SQL}
        GROUP BY ps.player_name
        ORDER BY CASE WHEN games >= 3 THEN 0 ELSE 1 END, kd DESC LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_hs_rating(limit=20):
    conn = get_db()
    sql = f"""
        SELECT ps.player_name,
               COUNT(DISTINCT m.match_date) AS games,
               ROUND(AVG(ps.hs_percent), 1) AS hs,
               SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths,
               ROUND(AVG(ps.kd), 2) AS kd
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE 1=1 {ACTIVE_FILTER_SQL}
        GROUP BY ps.player_name
        ORDER BY CASE WHEN games >= 3 THEN 0 ELSE 1 END, hs DESC LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_winrate_stats(limit=20, min_days=1, last_n=8):
    conn = get_db()
    days = conn.execute(
        "SELECT DISTINCT match_date FROM matches ORDER BY match_date DESC LIMIT ?", (last_n,)
    ).fetchall()
    day_dates = [d[0] for d in days]
    if not day_dates:
        conn.close()
        return []

    day_dates_sorted = sorted(day_dates)
    placeholders = ','.join('?' * len(day_dates_sorted))

    # Матчи за эти дни
    matches = conn.execute(f"""
        SELECT m.id, m.match_date, m.winner, m.score_a, m.score_b
        FROM matches m
        WHERE m.match_date IN ({placeholders})
        ORDER BY m.match_date, m.id
    """, day_dates_sorted).fetchall()

    # Определяем победителя дня
    day_winners = {}
    for d in day_dates_sorted:
        day_matches = [r for r in matches if r["match_date"] == d]
        a_wins = sum(1 for r in day_matches if r["winner"] == "A")
        b_wins = sum(1 for r in day_matches if r["winner"] == "B")
        if a_wins > b_wins:
            day_winners[d] = "Team A"
        elif b_wins > a_wins:
            day_winners[d] = "Team B"
        else:
            # Ничья — считаем сумму раундов
            a_rounds = sum(r["score_a"] or 0 for r in day_matches)
            b_rounds = sum(r["score_b"] or 0 for r in day_matches)
            day_winners[d] = "Team A" if a_rounds >= b_rounds else "Team B"

    rows = conn.execute(f"""
        SELECT DISTINCT ps.player_name, ps.team, m.match_date
        FROM player_stats ps
        JOIN matches m ON ps.match_id=m.id
        WHERE m.match_date IN ({placeholders})
    """, day_dates_sorted).fetchall()
    conn.close()

    # Собираем по игрокам — один день = один результат
    players = {}
    for row in rows:
        name = row["player_name"]
        day = row["match_date"]
        if name not in players:
            players[name] = {"won": 0, "seq": {}, "day_count": 0}
        if day not in players[name]["seq"]:
            players[name]["day_count"] += 1
            won = row["team"] == day_winners[day]
            players[name]["seq"][day] = won
            if won:
                players[name]["won"] += 1

    out = []
    for name, s in players.items():
        if s["day_count"] < min_days:
            continue
        seq = [s["seq"].get(d) for d in day_dates_sorted]
        played = sum(1 for v in seq if v is not None)
        won = s["won"]
        lost = played - won
        wr = round(won * 100.0 / played, 1)
        bar = "".join("W" if v else "L" for v in seq if v is not None)
        out.append({"name": name, "played": played, "won": won, "lost": lost, "wr": wr, "bar": bar})
    out.sort(key=lambda x: (0 if x["played"] >= 3 else 1, -x["wr"], -x["played"]))
    return out[:limit]


def get_day_stats():
    conn = get_db()
    days = conn.execute(
        "SELECT DISTINCT match_date FROM matches ORDER BY match_date DESC"
    ).fetchall()
    result = []
    for d in days:
        day = d[0]
        players = conn.execute(f"""
            SELECT ps.player_name,
                   SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths, SUM(ps.assists) AS assists,
                   ROUND(CAST(SUM(ps.kills) AS REAL) / MAX(SUM(ps.deaths), 1), 2) AS kd,
                   ROUND(AVG(ps.adr), 1) AS adr, ROUND(AVG(ps.hs_percent), 1) AS hs,
                   SUM(ps.plus_minus) AS pm, COUNT(DISTINCT ps.match_id) AS matches
            FROM player_stats ps JOIN matches m ON ps.match_id=m.id
            WHERE m.match_date=?
            GROUP BY ps.player_name ORDER BY kd DESC
        """, (day,)).fetchall()
        match_count = conn.execute("SELECT COUNT(*) FROM matches WHERE match_date=?", (day,)).fetchone()[0]
        result.append({
            "date": day,
            "matches": match_count,
            "players": [dict(p) for p in players]
        })
    conn.close()
    return result


def get_mvp_evp():
    conn = get_db()
    days = conn.execute("SELECT DISTINCT match_date FROM matches ORDER BY match_date DESC").fetchall()
    result = []
    for d in days:
        day = d[0]
        matches_ = conn.execute(
            "SELECT winner, score_a, score_b FROM matches WHERE match_date=?", (day,)
        ).fetchall()
        if not matches_:
            continue

        wins_a = sum(1 for m in matches_ if m["winner"] == "A")
        wins_b = sum(1 for m in matches_ if m["winner"] == "B")
        score_diff = sum((m["score_a"] or 0) - (m["score_b"] or 0) for m in matches_)
        if wins_a > wins_b:
            winner_team = "Team A"
        elif wins_b > wins_a:
            winner_team = "Team B"
        elif score_diff > 0:
            winner_team = "Team A"
        elif score_diff < 0:
            winner_team = "Team B"
        else:
            ka = conn.execute("""
                SELECT COALESCE(SUM(ps.kills),0) FROM player_stats ps
                JOIN matches m ON ps.match_id=m.id WHERE m.match_date=? AND ps.team='Team A'
            """, (day,)).fetchone()[0]
            kb = conn.execute("""
                SELECT COALESCE(SUM(ps.kills),0) FROM player_stats ps
                JOIN matches m ON ps.match_id=m.id WHERE m.match_date=? AND ps.team='Team B'
            """, (day,)).fetchone()[0]
            winner_team = "Team A" if ka >= kb else "Team B"

        match_ids = [m["id"] for m in conn.execute("SELECT id FROM matches WHERE match_date=?", (day,)).fetchall()]
        total_maps = 0
        for mid in match_ids:
            tk = conn.execute("SELECT COALESCE(SUM(kills),0) FROM player_stats WHERE match_id=?", (mid,)).fetchone()[0]
            total_maps += max(1, round(tk / 150.0))

        players = conn.execute(f"""
            SELECT ps.player_name, ps.team,
                   SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths, SUM(ps.assists) AS assists,
                   ROUND(AVG(ps.adr),1) AS adr,
                   SUM(COALESCE(ps.fk - ps.fd,0)) AS entries,
                   SUM(COALESCE(ps.clutch_1v5,0)*5+COALESCE(ps.clutch_1v4,0)*4+COALESCE(ps.clutch_1v3,0)*3+COALESCE(ps.clutch_1v2,0)*2+COALESCE(ps.clutch_1v1,0)) AS clutch_score,
                   SUM(COALESCE(ps.multikill_5k,0)*5+COALESCE(ps.multikill_4k,0)*4+COALESCE(ps.multikill_3k,0)*3+COALESCE(ps.multikill_2k,0)*2) AS multi_score,
                   SUM(COALESCE(ps.plus_minus,0)) AS pm
            FROM player_stats ps JOIN matches m ON ps.match_id=m.id
            WHERE m.match_date=?
            GROUP BY ps.player_name
        """, (day,)).fetchall()

        if len(players) < 4:
            continue

        rounds = total_maps * 26
        rated = []
        for p in players:
            kpr = p["kills"] / rounds
            dpr = p["deaths"] / rounds
            apr = p["assists"] / rounds
            ar = p["adr"] or 70
            impact_val = (kpr * 1.5) - (dpr * 0.75) + (apr * 0.6) + (ar * 0.1) \
                         + ((p["entries"] or 0) / total_maps * 1.5) \
                         + ((p["clutch_score"] or 0) / total_maps * 1.5) \
                         + ((p["multi_score"] or 0) / total_maps * 0.75) \
                         + ((p["pm"] or 0) / total_maps * 0.15)
            impact_val /= 10
            kd = round(p["kills"] / max(p["deaths"], 1), 2)
            rated.append({
                "name": p["player_name"], "team": p["team"][-1],
                "kills": p["kills"], "deaths": p["deaths"], "kd": kd,
                "adr": p["adr"], "impact": round(impact_val, 2),
                "_iv": impact_val
            })

        winners = [p for p in rated if p["team"] == winner_team[-1]]
        if not winners:
            continue

        mvp = max(winners, key=lambda x: x["_iv"])
        rest = sorted([p for p in rated if p["name"] != mvp["name"]], key=lambda x: x["_iv"], reverse=True)
        for p in rated:
            if "_iv" in p:
                del p["_iv"]
        result.append({
            "date": day, "maps": total_maps, "winner": winner_team[-1],
            "mvp": mvp, "evp": rest[:3]
        })
    conn.close()
    return result


def get_clutch_rating(limit=20):
    conn = get_db()
    sql = f"""
        SELECT ps.player_name,
               COUNT(DISTINCT m.match_date) AS games,
               ROUND(AVG(ps.clutch_1v5), 2) AS c5,
               ROUND(AVG(ps.clutch_1v4), 2) AS c4,
               ROUND(AVG(ps.clutch_1v3), 2) AS c3,
               ROUND(AVG(ps.clutch_1v2), 2) AS c2,
               ROUND(AVG(ps.clutch_1v1), 2) AS c1,
               ROUND(AVG(ps.clutch_1v5*5 + ps.clutch_1v4*4 + ps.clutch_1v3*3 + ps.clutch_1v2*2 + ps.clutch_1v1), 2) AS score
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE 1=1 {ACTIVE_FILTER_SQL}
        GROUP BY ps.player_name
        HAVING ROUND(AVG(ps.clutch_1v5 + ps.clutch_1v4 + ps.clutch_1v3 + ps.clutch_1v2 + ps.clutch_1v1), 2) > 0
        ORDER BY CASE WHEN games >= 3 THEN 0 ELSE 1 END, score DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── API endpoints ──

@app.get("/api/impact")
def api_impact():
    return get_impact_rating()

@app.get("/api/kd")
def api_kd():
    return get_kd_rating()

@app.get("/api/hs")
def api_hs():
    return get_hs_rating()

@app.get("/api/winrate")
def api_winrate():
    return get_winrate_stats()

@app.get("/api/days")
def api_days():
    return get_day_stats()

@app.get("/api/mvp-evp")
def api_mvp_evp():
    return get_mvp_evp()

@app.get("/api/clutches")
def api_clutches():
    return get_clutch_rating()

@app.get("/api/matches")
def api_matches():
    conn = get_db()
    rows = conn.execute("""
        SELECT m.*,
               (SELECT COUNT(*) FROM player_stats ps WHERE ps.match_id = m.id) AS players
        FROM matches m ORDER BY m.id DESC LIMIT 10
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/last-mvp")
def api_last_mvp():
    """MVP/EVP только за последний игровой день"""
    all_mvp = get_mvp_evp()
    return all_mvp[0] if all_mvp else {}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    impact = get_impact_rating()
    mvp_data = get_mvp_evp()
    last_day = mvp_data[0] if mvp_data else None
    return templates.TemplateResponse("index.html", {
        "request": request,
        "impact": impact,
        "last_day": last_day
    })


def get_recent_matches():
    conn = get_db()
    rows = conn.execute("""
        SELECT m.*,
               (SELECT COUNT(*) FROM player_stats ps WHERE ps.match_id = m.id) AS players
        FROM matches m ORDER BY m.id DESC LIMIT 10
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
