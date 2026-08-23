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


def get_last_game_dates(conn, limit=8):
    rows = conn.execute(
        "SELECT DISTINCT match_date FROM matches ORDER BY match_date DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return sorted([r[0] for r in rows])


def estimate_day_maps(conn, match_date):
    match_ids = conn.execute(
        "SELECT id FROM matches WHERE match_date=?",
        (match_date,)
    ).fetchall()
    total_maps = 0
    for match in match_ids:
        total_kills = conn.execute(
            "SELECT COALESCE(SUM(kills),0) FROM player_stats WHERE match_id=?",
            (match["id"],)
        ).fetchone()[0]
        total_maps += max(1, round(total_kills / 150.0))
    return total_maps


def compute_day_impact(day_stats, maps):
    if not day_stats or maps <= 0:
        return 0.0

    rounds = maps * 26
    if rounds <= 0:
        return 0.0

    kills = float(day_stats["kills"] or 0)
    deaths = float(day_stats["deaths"] or 0)
    assists = float(day_stats["assists"] or 0)
    adr = float(day_stats["adr"] or 70)
    entries = float(day_stats["entries"] or 0) / maps
    clutch_score = float(day_stats["clutch_score"] or 0) / maps
    multi_score = float(day_stats["multi_score"] or 0) / maps
    pm = float(day_stats["pm"] or 0) / maps

    kpr = kills / rounds
    dpr = deaths / rounds
    apr = assists / rounds

    return (
        (kpr * 1.5)
        - (dpr * 0.75)
        + (apr * 0.6)
        + (adr * 0.1)
        + (entries * 1.5)
        + (clutch_score * 1.5)
        + (multi_score * 0.75)
        + (pm * 0.15)
    ) / 10


def get_player_last8_impact_summary(conn, player_name, day_dates=None, day_maps=None):
    if day_dates is None:
        day_dates = get_last_game_dates(conn, 8)
    if not day_dates:
        return {
            "player_name": player_name,
            "games": 0,
            "kd": 0.0,
            "adr": 0.0,
            "hs": 0.0,
            "impact": 0.0,
            "daily_impacts": [],
        }

    if day_maps is None:
        day_maps = {day: estimate_day_maps(conn, day) for day in day_dates}

    placeholders = ",".join("?" for _ in day_dates)
    rows = conn.execute(
        f"""
        SELECT m.match_date,
               SUM(ps.kills) AS kills,
               SUM(ps.deaths) AS deaths,
               SUM(ps.assists) AS assists,
               ROUND(AVG(ps.adr), 1) AS adr,
               ROUND(AVG(ps.hs_percent), 1) AS hs,
               SUM(COALESCE(ps.fk, 0) - COALESCE(ps.fd, 0)) AS entries,
               SUM(
                   COALESCE(ps.clutch_1v5, 0) * 5
                   + COALESCE(ps.clutch_1v4, 0) * 4
                   + COALESCE(ps.clutch_1v3, 0) * 3
                   + COALESCE(ps.clutch_1v2, 0) * 2
                   + COALESCE(ps.clutch_1v1, 0)
               ) AS clutch_score,
               SUM(
                   COALESCE(ps.multikill_5k, 0) * 5
                   + COALESCE(ps.multikill_4k, 0) * 4
                   + COALESCE(ps.multikill_3k, 0) * 3
                   + COALESCE(ps.multikill_2k, 0) * 2
               ) AS multi_score,
               SUM(COALESCE(ps.plus_minus, 0)) AS pm
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE ps.player_name = ? AND m.match_date IN ({placeholders})
        GROUP BY m.match_date
        ORDER BY m.match_date
        """,
        (player_name, *day_dates),
    ).fetchall()

    daily_impacts = []
    total_kills = total_deaths = 0
    total_adr = total_hs = 0.0
    impact_sum = 0.0

    for row in rows:
        maps = day_maps.get(row["match_date"], 0)
        impact = compute_day_impact(row, maps)
        impact_sum += impact
        daily_impacts.append({"date": row["match_date"], "impact": round(impact, 2)})
        total_kills += row["kills"] or 0
        total_deaths += row["deaths"] or 0
        total_adr += row["adr"] or 0
        total_hs += row["hs"] or 0

    games = len(rows)
    if games:
        avg_impact = round(impact_sum / games, 2)
        kd = round(total_kills / max(total_deaths, 1), 2)
        adr = round(total_adr / games, 1)
        hs = round(total_hs / games, 1)
    else:
        avg_impact = 0.0
        kd = 0.0
        adr = 0.0
        hs = 0.0

    return {
        "player_name": player_name,
        "games": games,
        "kd": kd,
        "adr": adr,
        "hs": hs,
        "impact": avg_impact,
        "daily_impacts": daily_impacts,
    }


# ── Все игроки (без фильтра по дням) ──
ACTIVE_FILTER_SQL = ""


def get_daily_impact_for_player(conn, player_name):
    """Get a list of per-day impact values for the last 8 game dates."""
    summary = get_player_last8_impact_summary(conn, player_name)
    return summary["daily_impacts"]


def get_impact_rating(limit=20):
    conn = get_db()
    day_dates = get_last_game_dates(conn, 8)
    day_maps = {day: estimate_day_maps(conn, day) for day in day_dates}
    rows = conn.execute(
        f"SELECT DISTINCT ps.player_name FROM player_stats ps JOIN matches m ON ps.match_id = m.id WHERE 1=1 {ACTIVE_FILTER_SQL} ORDER BY ps.player_name"
    ).fetchall()
    players = []
    for row in rows:
        summary = get_player_last8_impact_summary(conn, row["player_name"], day_dates, day_maps)
        players.append(summary)
    players.sort(key=lambda x: (0 if x["games"] >= 3 else 1, -x["impact"], x["player_name"]))
    players = players[:limit]
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

        total_maps = estimate_day_maps(conn, day)

        players = conn.execute(f"""
            SELECT ps.player_name, ps.team,
                   SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths, SUM(ps.assists) AS assists,
                   ROUND(AVG(ps.adr),1) AS adr,
                   SUM(COALESCE(ps.fk,0) - COALESCE(ps.fd,0)) AS entries,
                   SUM(COALESCE(ps.clutch_1v5,0)*5+COALESCE(ps.clutch_1v4,0)*4+COALESCE(ps.clutch_1v3,0)*3+COALESCE(ps.clutch_1v2,0)*2+COALESCE(ps.clutch_1v1,0)) AS clutch_score,
                   SUM(COALESCE(ps.multikill_5k,0)*5+COALESCE(ps.multikill_4k,0)*4+COALESCE(ps.multikill_3k,0)*3+COALESCE(ps.multikill_2k,0)*2) AS multi_score,
                   SUM(COALESCE(ps.plus_minus,0)) AS pm
            FROM player_stats ps JOIN matches m ON ps.match_id=m.id
            WHERE m.match_date=?
            GROUP BY ps.player_name
        """, (day,)).fetchall()

        if len(players) < 4:
            continue

        rated = []
        for p in players:
            impact_val = compute_day_impact(p, total_maps)
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
        worst = min(rated, key=lambda x: x["_iv"])
        rest = sorted([p for p in rated if p["name"] != mvp["name"]], key=lambda x: x["_iv"], reverse=True)
        for p in rated:
            if "_iv" in p:
                del p["_iv"]
        result.append({
            "date": day, "maps": total_maps, "winner": winner_team[-1],
            "mvp": mvp,
            "evp": rest[:3],
            "worst": {
                "name": worst["name"],
                "team": worst["team"],
                "kills": worst["kills"],
                "deaths": worst["deaths"],
                "kd": worst["kd"],
                "adr": worst["adr"],
                "impact": worst["impact"],
            }
        })
    conn.close()
    return result


def get_medals_stats():
    mvp_evp_days = get_mvp_evp()
    if not mvp_evp_days:
        return []

    medal_stats = {}

    for day in mvp_evp_days:
        mvp = day.get("mvp")
        if mvp and mvp.get("name"):
            name = mvp["name"]
            stats = medal_stats.setdefault(name, {
                "player_name": name,
                "mvp_count": 0,
                "evp_count": 0,
                "worst_count": 0,
                "medals_total": 0,
                "_impact_sum": 0.0,
                "_kd_sum": 0.0,
                "_adr_sum": 0.0,
                "_medal_days": 0,
            })
            stats["mvp_count"] += 1
            stats["medals_total"] += 1
            stats["_kd_sum"] += float(mvp.get("kd") or 0)
            stats["_adr_sum"] += float(mvp.get("adr") or 0)
            stats["_medal_days"] += 1

        for evp in day.get("evp", []):
            if not evp or not evp.get("name"):
                continue
            name = evp["name"]
            stats = medal_stats.setdefault(name, {
                "player_name": name,
                "mvp_count": 0,
                "evp_count": 0,
                "worst_count": 0,
                "medals_total": 0,
                "_impact_sum": 0.0,
                "_kd_sum": 0.0,
                "_adr_sum": 0.0,
                "_medal_days": 0,
            })
            stats["evp_count"] += 1
            stats["medals_total"] += 1
            stats["_kd_sum"] += float(evp.get("kd") or 0)
            stats["_adr_sum"] += float(evp.get("adr") or 0)
            stats["_medal_days"] += 1

        worst = day.get("worst")
        if worst and worst.get("name"):
            name = worst["name"]
            stats = medal_stats.setdefault(name, {
                "player_name": name,
                "mvp_count": 0,
                "evp_count": 0,
                "worst_count": 0,
                "medals_total": 0,
                "_impact_sum": 0.0,
                "_kd_sum": 0.0,
                "_adr_sum": 0.0,
                "_medal_days": 0,
            })
            stats["worst_count"] += 1

    if not medal_stats:
        return []

    conn = get_db()
    day_dates = get_last_game_dates(conn, 8)
    day_maps = {day: estimate_day_maps(conn, day) for day in day_dates}
    games_rows = conn.execute("""
        SELECT ps.player_name, COUNT(DISTINCT m.match_date) AS games
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        GROUP BY ps.player_name
    """).fetchall()

    games_map = {row["player_name"]: row["games"] for row in games_rows}

    out = []
    for name, stats in medal_stats.items():
        medal_days = stats["_medal_days"] or 1
        avg_impact = get_player_last8_impact_summary(conn, name, day_dates, day_maps)["impact"]
        out.append({
            "player_name": name,
            "mvp_count": stats["mvp_count"],
            "evp_count": stats["evp_count"],
            "worst_count": stats["worst_count"],
            "medals_total": stats["medals_total"],
            "avg_impact": avg_impact,
            "avg_kd": round(stats["_kd_sum"] / medal_days, 2),
            "avg_adr": round(stats["_adr_sum"] / medal_days, 1),
            "games": games_map.get(name, 0),
        })

    conn.close()

    out.sort(key=lambda x: (-x["medals_total"], -x["mvp_count"], -x["avg_impact"], x["player_name"]))
    return out


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

@app.get("/api/medals")
def api_medals():
    return get_medals_stats()

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
