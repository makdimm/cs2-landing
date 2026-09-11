from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3
import os
import shutil
import asyncio
from urllib.parse import quote

app = FastAPI(title="CS2 HiddenAI")

DB_PATH = os.getenv("DB_PATH", "/db/cs_bot.db")
WORK_DB = "/tmp/cs_bot_copy.db"

REFRESH_INTERVAL = int(os.getenv("DB_REFRESH_SEC", "30"))

PHOTOS_ROOT = "photos"
PHOTOS_PLAYERS = os.path.join(PHOTOS_ROOT, "players")
os.makedirs(PHOTOS_PLAYERS, exist_ok=True)

app.mount("/photos", StaticFiles(directory=PHOTOS_ROOT), name="photos")


def find_player_photo(player_name):
    """Ищет фото игрока в photos/players/<ник>.<ext>, затем в photos/<ник>.<ext>.
    Возвращает URL для отдачи или None."""
    exts = (".jpg", ".jpeg", ".png", ".webp", ".JPG", ".JPEG", ".PNG", ".WEBP")
    for rel in (PHOTOS_PLAYERS, PHOTOS_ROOT):
        for ext in exts:
            path = os.path.join(rel, player_name + ext)
            if os.path.isfile(path):
                prefix = "/photos/players/" if rel == PHOTOS_PLAYERS else "/photos/"
                return prefix + quote(player_name + ext, safe="")
    return None

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


def get_player_last_dates(conn, player_name, limit=8):
    """Последние N игровых дней КОНКРЕТНОГО игрока (персонально)."""
    rows = conn.execute(
        "SELECT DISTINCT m.match_date FROM player_stats ps JOIN matches m ON ps.match_id=m.id "
        "WHERE ps.player_name = ? ORDER BY m.match_date DESC LIMIT ?",
        (player_name, limit)
    ).fetchall()
    return sorted([r[0] for r in rows])


PLAYER_LAST8_IN = (
    "m.match_date IN ("
    "SELECT DISTINCT m2.match_date FROM player_stats ps2 JOIN matches m2 ON ps2.match_id=m2.id "
    "WHERE ps2.player_name = ps.player_name ORDER BY m2.match_date DESC LIMIT 8)"
)


def estimate_day_maps(conn, match_date):
    match_ids = conn.execute(
        "SELECT id, score_a, score_b FROM matches WHERE match_date=?",
        (match_date,)
    ).fetchall()
    total_maps = 0
    for match in match_ids:
        sa, sb = match["score_a"], match["score_b"]
        if sa is not None and sb is not None and sa <= 3 and sb <= 3 and sa + sb > 0:
            total_maps += sa + sb
            continue
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
        day_dates = get_player_last_dates(conn, player_name, 8)
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
    rows = conn.execute(
        f"SELECT DISTINCT ps.player_name FROM player_stats ps JOIN matches m ON ps.match_id = m.id WHERE 1=1 {ACTIVE_FILTER_SQL} ORDER BY ps.player_name"
    ).fetchall()
    players = []
    for row in rows:
        name = row["player_name"]
        p_dates = get_player_last_dates(conn, name, 8)
        p_maps = {d: estimate_day_maps(conn, d) for d in p_dates}
        players.append(get_player_last8_impact_summary(conn, name, p_dates, p_maps))
    players.sort(key=lambda x: (0 if x["games"] >= 3 else 1, -x["impact"], x["player_name"]))
    players = players[:limit]
    conn.close()
    return players


def get_kd_rating(limit=20):
    conn = get_db()
    sql = f"""
        SELECT ps.player_name,
               COUNT(DISTINCT m.match_date) AS games,
               ROUND(CAST(SUM(ps.kills) AS REAL) / MAX(SUM(ps.deaths), 1), 2) AS kd,
               ROUND(AVG(ps.adr), 1) AS adr,
               SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE {PLAYER_LAST8_IN}
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
        WHERE {PLAYER_LAST8_IN}
        GROUP BY ps.player_name
        ORDER BY CASE WHEN games >= 3 THEN 0 ELSE 1 END, hs DESC LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_winrate_stats(limit=20, min_days=1, last_n=8):
    """Винрейт по последним N игровым дням КАЖДОГО игрока (персонально)."""
    conn = get_db()
    player_rows = conn.execute("SELECT DISTINCT player_name FROM player_stats").fetchall()
    out = []
    for pr in player_rows:
        name = pr["player_name"]
        dates = get_player_last_dates(conn, name, last_n)
        if not dates:
            continue
        placeholders = ','.join('?' * len(dates))

        matches = conn.execute(f"""
            SELECT m.id, m.match_date, m.winner, m.score_a, m.score_b
            FROM matches m
            WHERE m.match_date IN ({placeholders})
            ORDER BY m.match_date, m.id
        """, dates).fetchall()

        day_winners = {}
        for d in dates:
            day_matches = [r for r in matches if r["match_date"] == d]
            a_wins = sum(1 for r in day_matches if r["winner"] == "A")
            b_wins = sum(1 for r in day_matches if r["winner"] == "B")
            if a_wins > b_wins:
                day_winners[d] = "Team A"
            elif b_wins > a_wins:
                day_winners[d] = "Team B"
            else:
                a_rounds = sum(r["score_a"] or 0 for r in day_matches)
                b_rounds = sum(r["score_b"] or 0 for r in day_matches)
                day_winners[d] = "Team A" if a_rounds >= b_rounds else "Team B"

        rows = conn.execute(f"""
            SELECT DISTINCT ps.team, m.match_date
            FROM player_stats ps
            JOIN matches m ON ps.match_id=m.id
            WHERE ps.player_name = ? AND m.match_date IN ({placeholders})
        """, (name, *dates)).fetchall()

        seq = []
        played = won = 0
        for d in dates:
            row = next((r for r in rows if r["match_date"] == d), None)
            if row is None:
                continue
            w = (row["team"] == day_winners[d])
            seq.append(w)
            played += 1
            if w:
                won += 1

        if played < min_days:
            continue
        lost = played - won
        wr = round(won * 100.0 / played, 1)
        bar = "".join("W" if v else "L" for v in seq)
        out.append({"name": name, "played": played, "won": won, "lost": lost, "wr": wr, "bar": bar})

    conn.close()
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
        avg_impact = get_player_last8_impact_summary(conn, name)["impact"]
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
    """Клатчи за последние 8 игровых дней КАЖДОГО игрока.

    Отдаём и СУММЫ (за весь период), и СРЕДНЕЕ ЗА ИГРОВОЙ ДЕНЬ:
    сумма ÷ число игровых дней игрока. Сортировка — по очкам в день.
    """
    conn = get_db()
    sql = f"""
        SELECT ps.player_name,
               COUNT(DISTINCT m.match_date) AS games,
               SUM(COALESCE(ps.clutch_1v5, 0)) AS c5,
               SUM(COALESCE(ps.clutch_1v4, 0)) AS c4,
               SUM(COALESCE(ps.clutch_1v3, 0)) AS c3,
               SUM(COALESCE(ps.clutch_1v2, 0)) AS c2,
               SUM(COALESCE(ps.clutch_1v1, 0)) AS c1,
               SUM(COALESCE(ps.clutch_1v5,0) + COALESCE(ps.clutch_1v4,0) + COALESCE(ps.clutch_1v3,0) + COALESCE(ps.clutch_1v2,0) + COALESCE(ps.clutch_1v1,0)) AS total,
               SUM(COALESCE(ps.clutch_1v5,0)*5 + COALESCE(ps.clutch_1v4,0)*4 + COALESCE(ps.clutch_1v3,0)*3 + COALESCE(ps.clutch_1v2,0)*2 + COALESCE(ps.clutch_1v1,0)) AS score
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE {PLAYER_LAST8_IN}
        GROUP BY ps.player_name
        HAVING total > 0
        ORDER BY CASE WHEN games >= 3 THEN 0 ELSE 1 END, (CAST(score AS REAL) / games) DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        days = d["games"] or 1
        d["score_per_day"] = round(d["score"] / days, 2)
        d["total_per_day"] = round(d["total"] / days, 2)
        out.append(d)
    return out


def get_entry_rating(limit=20):
    """Статистика по энтри за последние 8 игровых дней КАЖДОГО игрока: first kills / first deaths / разница."""
    conn = get_db()
    sql = f"""
        SELECT ps.player_name,
               COUNT(DISTINCT m.match_date) AS games,
               SUM(COALESCE(ps.fk, 0)) AS fk,
               SUM(COALESCE(ps.fd, 0)) AS fd,
               SUM(COALESCE(ps.fk, 0)) - SUM(COALESCE(ps.fd, 0)) AS diff,
               ROUND(CAST(SUM(COALESCE(ps.fk, 0)) AS REAL) / MAX(COUNT(DISTINCT m.match_date), 1), 2) AS fk_avg,
               ROUND(CAST(SUM(COALESCE(ps.fd, 0)) AS REAL) / MAX(COUNT(DISTINCT m.match_date), 1), 2) AS fd_avg
        FROM player_stats ps
        JOIN matches m ON ps.match_id = m.id
        WHERE {PLAYER_LAST8_IN}
        GROUP BY ps.player_name
        ORDER BY CASE WHEN games >= 3 THEN 0 ELSE 1 END, diff DESC, ps.player_name
        LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_player_card(player_name):
    """Данные карточки игрока: агрегаты ЗА ПОСЛЕДНИЕ 8 ИГРОВЫХ ДНЕЙ ИГРОКА, разбивка по дням, медали (за всё время)."""
    conn = get_db()
    dates = get_player_last_dates(conn, player_name, 8)
    if not dates:
        conn.close()
        return None
    placeholders = ",".join("?" for _ in dates)
    row = conn.execute(f"""
        SELECT COUNT(DISTINCT m.match_date) AS days,
               COUNT(*) AS matches,
               SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths, SUM(ps.assists) AS assists,
               ROUND(AVG(ps.adr), 1) AS adr, ROUND(AVG(ps.hs_percent), 1) AS hs,
               SUM(COALESCE(ps.fk, 0)) AS fk, SUM(COALESCE(ps.fd, 0)) AS fd,
               SUM(COALESCE(ps.plus_minus, 0)) AS pm,
               SUM(COALESCE(ps.clutch_1v5,0)) AS c5, SUM(COALESCE(ps.clutch_1v4,0)) AS c4,
               SUM(COALESCE(ps.clutch_1v3,0)) AS c3, SUM(COALESCE(ps.clutch_1v2,0)) AS c2,
               SUM(COALESCE(ps.clutch_1v1,0)) AS c1,
               SUM(COALESCE(ps.multikill_5k,0)) AS mk5, SUM(COALESCE(ps.multikill_4k,0)) AS mk4,
               SUM(COALESCE(ps.multikill_3k,0)) AS mk3, SUM(COALESCE(ps.multikill_2k,0)) AS mk2
        FROM player_stats ps JOIN matches m ON ps.match_id = m.id
        WHERE ps.player_name = ? AND m.match_date IN ({placeholders})
    """, (player_name, *dates)).fetchone()

    if not row or not row["matches"]:
        conn.close()
        return None

    totals = dict(row)
    totals["kd"] = round((totals["kills"] or 0) / max(totals["deaths"] or 0, 1), 2)
    totals["clutch_score"] = (totals["c5"] or 0) * 5 + (totals["c4"] or 0) * 4 + (totals["c3"] or 0) * 3 + (totals["c2"] or 0) * 2 + (totals["c1"] or 0)

    days = conn.execute("""
        SELECT m.match_date AS date,
               SUM(ps.kills) AS kills, SUM(ps.deaths) AS deaths,
               ROUND(AVG(ps.adr), 1) AS adr, ROUND(AVG(ps.hs_percent), 1) AS hs,
               SUM(COALESCE(ps.fk,0)) AS fk, SUM(COALESCE(ps.fd,0)) AS fd,
               SUM(COALESCE(ps.plus_minus,0)) AS pm,
               SUM(COALESCE(ps.clutch_1v5,0)*5 + COALESCE(ps.clutch_1v4,0)*4 + COALESCE(ps.clutch_1v3,0)*3 + COALESCE(ps.clutch_1v2,0)*2 + COALESCE(ps.clutch_1v1,0)) AS clutch_score
        FROM player_stats ps JOIN matches m ON ps.match_id = m.id
        WHERE ps.player_name = ?
        GROUP BY m.match_date ORDER BY m.match_date DESC LIMIT 8
    """, (player_name,)).fetchall()
    by_day = []
    for d in days:
        dd = dict(d)
        dd["kd"] = round((dd["kills"] or 0) / max(dd["deaths"] or 0, 1), 2)
        by_day.append(dd)
    conn.close()

    medals = {"mvp": 0, "evp": 0, "worst": 0}
    for day in get_mvp_evp():
        mvp = day.get("mvp") or {}
        if mvp.get("name") == player_name:
            medals["mvp"] += 1
        for e in day.get("evp", []):
            if (e or {}).get("name") == player_name:
                medals["evp"] += 1
        w = day.get("worst") or {}
        if w.get("name") == player_name:
            medals["worst"] += 1

    return {"name": player_name, "totals": totals, "by_day": by_day, "medals": medals,
            "photo": find_player_photo(player_name)}


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

@app.get("/api/entry")
def api_entry():
    return get_entry_rating()

@app.get("/api/player/{player_name}")
def api_player(player_name: str):
    card = get_player_card(player_name)
    if not card:
        return {"error": "not found"}
    return card

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
