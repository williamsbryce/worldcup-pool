#!/usr/bin/env python3
"""
update.py — Fetch 2026 FIFA World Cup results from ESPN and recompute standings.
Run manually or via GitHub Actions cron.
"""
import json
import re
import sys
import os
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")

TOURNAMENT_START = date(2026, 6, 11)
TOURNAMENT_END   = date(2026, 7, 19)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"

# Teams whose names differ between entries.json and what ESPN returns
TEAM_MAP = {
    "USA":                    "United States",
    "Bosnia & Herzegovina":   "Bosnia and Herzegovina",
    "Ivory Coast":            "Côte d'Ivoire",
    "DR Congo":               "DR Congo",
    "Curacao":                "Curaçao",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load(filename):
    path = os.path.join(DATA_DIR, filename)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save(filename, data):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  WARN fetch failed: {url}\n       {e}", file=sys.stderr)
        return None

def norm(s):
    """Lowercase ASCII, punctuation stripped."""
    nfkd = unicodedata.normalize("NFKD", str(s))
    a = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", a.lower()).strip()

def team_eq(our_name, espn_name):
    mapped = TEAM_MAP.get(our_name, our_name)
    return norm(mapped) == norm(espn_name) or norm(our_name) == norm(espn_name)

def scorer_eq(entry_name, espn_name, aliases):
    entry_name = entry_name.strip()
    canonical  = aliases.get(entry_name, entry_name)
    if norm(canonical) == norm(espn_name):
        return True
    en = norm(entry_name)
    an = norm(espn_name)
    if en == an:
        return True
    # token matching: every significant token in the entry name must appear in espn name
    e_tokens = [t for t in en.split() if len(t) > 1]
    a_tokens  = set(an.split())
    if not e_tokens:
        return False
    return all(any(at.startswith(et) or et.startswith(at) for at in a_tokens) for et in e_tokens)


# ---------------------------------------------------------------------------
# ESPN data fetching
# ---------------------------------------------------------------------------

def parse_stage(event_name, note_text):
    txt = (event_name + " " + note_text).lower()
    if "third" in txt or "3rd place" in txt:
        return "THIRD_PLACE"
    if "semifinal" in txt or "semi-final" in txt or "semi final" in txt:
        return "SEMI_FINAL"
    if "quarterfinal" in txt or "quarter-final" in txt or "quarter final" in txt:
        return "QUARTER_FINAL"
    if "round of 16" in txt or "round of sixteen" in txt:
        return "ROUND_OF_16"
    if "final" in txt:
        return "FINAL"
    return "GROUP"

def parse_group(event_name, note_text):
    m = re.search(r"Group ([A-L])\b", event_name + " " + note_text)
    return m.group(1) if m else None

def fetch_scoreboard_day(d):
    return fetch(f"{ESPN_BASE}/scoreboard?dates={d.strftime('%Y%m%d')}&limit=20")

def fetch_goals(event_id):
    """Parse goal scorer data from ESPN match summary rosters."""
    data = fetch(f"{ESPN_BASE}/summary?event={event_id}")
    if not data:
        return []
    goals = []
    for team_block in data.get("rosters", []):
        team_name = team_block.get("team", {}).get("displayName", "")
        for player in team_block.get("roster", []):
            athlete = player.get("athlete", {}).get("fullName", "")
            for play in player.get("plays", []):
                if not play.get("scoringPlay"):
                    continue
                if play.get("didAssist"):  # exclude assists, only count actual goals
                    continue
                goals.append({
                    "scorer":    athlete,
                    "team":      team_name,
                    "minute":    play.get("clock", {}).get("displayValue", "?"),
                    "type":      "penalty" if play.get("penaltyKick") else "regular",
                    "own_goal":  bool(play.get("ownGoal")),
                    "shootout":  bool(play.get("shootoutPlay", False)),
                })
    return goals

def fetch_all_matches(existing_by_id):
    today = min(date.today(), TOURNAMENT_END)
    matches = dict(existing_by_id)  # copy

    d = TOURNAMENT_START
    while d <= today:
        data = fetch_scoreboard_day(d)
        if not data:
            d += timedelta(days=1)
            continue

        for event in data.get("events", []):
            eid  = event["id"]
            comp = event["competitions"][0]
            status = comp["status"]["type"]["name"]

            if "FINAL" not in status and "FULL" not in status:
                continue  # not finished

            home_c = next((c for c in comp["competitors"] if c.get("homeAway") == "home"), comp["competitors"][0])
            away_c = next((c for c in comp["competitors"] if c.get("homeAway") == "away"), comp["competitors"][1])

            home       = home_c["team"]["displayName"]
            away       = away_c["team"]["displayName"]
            home_score = int(home_c.get("score") or 0)
            away_score = int(away_c.get("score") or 0)

            notes     = comp.get("notes", [])
            note_text = " ".join(n.get("headline", "") for n in notes)
            stage = parse_stage(event.get("name", ""), note_text)
            group = parse_group(event.get("name", ""), note_text)

            situation   = comp.get("situation") or {}
            h_pen = int(situation.get("homeShootoutScore") or 0)
            a_pen = int(situation.get("awayShootoutScore") or 0)
            shootout = h_pen > 0 or a_pen > 0

            existing = matches.get(eid, {})
            matches[eid] = {
                "espn_id":            eid,
                "date":               d.isoformat(),
                "home":               home,
                "away":               away,
                "home_score":         home_score,
                "away_score":         away_score,
                "status":             "FINISHED",
                "stage":              stage,
                "group":              group,
                "shootout":           shootout,
                "home_shootout_score": h_pen,
                "away_shootout_score": a_pen,
                "home_group_rank":    existing.get("home_group_rank"),
                "away_group_rank":    existing.get("away_group_rank"),
                "goals":              existing.get("goals", []),
            }

        d += timedelta(days=1)

    return matches


# ---------------------------------------------------------------------------
# Group standings
# ---------------------------------------------------------------------------

def infer_groups(matches_list):
    """
    ESPN scoreboard doesn't expose the group letter.
    Infer groups by finding which teams played each other in the group stage.
    Returns a list of frozensets, each containing the 4 teams in a group.
    Only returns complete groups (where all 6 matches have been played).
    """
    from collections import defaultdict
    opponents = defaultdict(set)
    match_count = defaultdict(int)
    for m in matches_list:
        if m.get("stage") != "GROUP":
            continue
        opponents[m["home"]].add(m["away"])
        opponents[m["away"]].add(m["home"])
        match_count[m["home"]] += 1
        match_count[m["away"]] += 1

    groups = []
    assigned = set()
    for team in list(opponents):
        if team in assigned:
            continue
        if match_count[team] < 3:
            continue  # group not complete yet
        group_teams = {team} | opponents[team]
        if len(group_teams) == 4 and all(match_count.get(t, 0) >= 3 for t in group_teams):
            groups.append(frozenset(group_teams))
            assigned.update(group_teams)
    return groups

def compute_group_standings(matches_list):
    """Compute standings for each complete group."""
    complete_groups = infer_groups(matches_list)
    result = {}
    for gset in complete_groups:
        label = ",".join(sorted(gset))  # stable key for the group
        stats = {}
        for m in matches_list:
            if m.get("stage") != "GROUP":
                continue
            if m["home"] not in gset or m["away"] not in gset:
                continue
            for team, scored, conceded in [
                (m["home"], m["home_score"], m["away_score"]),
                (m["away"], m["away_score"], m["home_score"]),
            ]:
                if team not in stats:
                    stats[team] = {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0}
                s = stats[team]
                s["p"]  += 1
                s["gf"] += scored
                s["ga"] += conceded
                if m.get("shootout"):
                    s["d"]   += 1
                    s["pts"] += 1
                elif scored > conceded:
                    s["w"]   += 1
                    s["pts"] += 3
                elif scored == conceded:
                    s["d"]   += 1
                    s["pts"] += 1
                else:
                    s["l"] += 1
        ranked = sorted(
            stats.items(),
            key=lambda x: (x[1]["pts"], x[1]["gf"] - x[1]["ga"], x[1]["gf"]),
            reverse=True,
        )
        result[label] = {team: rank + 1 for rank, (team, _) in enumerate(ranked)}
    return result

def apply_group_ranks(matches_list, group_standings):
    team_rank = {}
    for rankings in group_standings.values():
        for team, rank in rankings.items():
            team_rank[team] = rank

    for m in matches_list:
        if m.get("stage") == "GROUP":
            if m["home"] in team_rank and m.get("home_group_rank") is None:
                m["home_group_rank"] = team_rank[m["home"]]
            if m["away"] in team_rank and m.get("away_group_rank") is None:
                m["away_group_rank"] = team_rank[m["away"]]


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------

def apply_overrides(matches_by_id, overrides):
    for ov in overrides.get("overrides", []):
        eid = ov["espn_id"]
        if eid not in matches_by_id:
            continue
        t = ov["type"]
        if t == "remove_goal":
            matches_by_id[eid]["goals"] = [
                g for g in matches_by_id[eid]["goals"]
                if not (norm(g["scorer"]) == norm(ov["scorer"])
                        and norm(g["team"]) == norm(ov["team"]))
            ]
        elif t == "add_goal":
            matches_by_id[eid]["goals"].append({
                "scorer":   ov["scorer"],
                "team":     ov["team"],
                "minute":   ov.get("minute", "?"),
                "type":     ov.get("goal_type", "regular"),
                "own_goal": ov.get("own_goal", False),
                "shootout": ov.get("shootout", False),
            })
        elif t == "set_group_rank":
            side = "home" if norm(ov["team"]) == norm(matches_by_id[eid]["home"]) else "away"
            matches_by_id[eid][f"{side}_group_rank"] = ov["rank"]


# ---------------------------------------------------------------------------
# Elimination tracking
# ---------------------------------------------------------------------------

def compute_eliminated_teams(matches_list):
    """
    Returns a set of norm'd team names that are out of the tournament.
    Group stage: teams finishing 3rd or 4th (once group is complete).
    Knockout rounds: loser of each finished match.
    """
    eliminated = set()

    # Group stage — rank 3 or 4 means out
    team_rank = {}
    for m in matches_list:
        if m.get("stage") != "GROUP":
            continue
        if m.get("home_group_rank") is not None:
            team_rank[norm(m["home"])] = m["home_group_rank"]
        if m.get("away_group_rank") is not None:
            team_rank[norm(m["away"])] = m["away_group_rank"]
    for team_key, rank in team_rank.items():
        if rank >= 3:
            eliminated.add(team_key)

    # Knockout rounds — loser is out
    knockout = {"ROUND_OF_16", "QUARTER_FINAL", "SEMI_FINAL", "THIRD_PLACE", "FINAL"}
    for m in matches_list:
        if m.get("stage") not in knockout:
            continue
        h = m["home_score"]
        a = m["away_score"]
        is_so = m.get("shootout", False)
        h_pen = m.get("home_shootout_score", 0)
        a_pen = m.get("away_shootout_score", 0)
        if is_so:
            eliminated.add(norm(m["away"] if h_pen > a_pen else m["home"]))
        elif h > a:
            eliminated.add(norm(m["away"]))
        elif a > h:
            eliminated.add(norm(m["home"]))

    return eliminated


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def espn_team_key(espn_name):
    return norm(espn_name)

def our_team_key(our_name):
    mapped = TEAM_MAP.get(our_name, our_name)
    return norm(mapped)

def compute_standings(entries_data, rules, matches_list, aliases):
    pm  = rules["per_match"]
    pp  = rules["pool_placement"]
    tf  = rules["tournament_finish"]
    sp  = rules["scorer_points"]

    player_aliases = aliases.get("players", aliases)  # support both flat and nested
    eliminated = compute_eliminated_teams(matches_list)

    standings = []

    for entry in entries_data["entries"]:
        # Per-team stats tracking
        pick_stats = {}
        for p in entry["picks"]:
            pick_stats[p["team"]] = {
                "box": p["box"], "team": p["team"], "bonus": p["bonus"],
                "match": 0, "scorer": 0, "pool": 0, "tourney": 0, "games": 0,
                "scorers": p["scorers"],
            }

        picks_by_key = {}
        for p in entry["picks"]:
            key = our_team_key(p["team"])
            picks_by_key[key] = p

        for m in matches_list:
            for side, opp in [("home", "away"), ("away", "home")]:
                team_name = m[side]
                team_key  = espn_team_key(team_name)

                pick = picks_by_key.get(team_key)
                if pick is None:
                    for our, espn in TEAM_MAP.items():
                        if norm(espn) == team_key:
                            pick = picks_by_key.get(norm(our))
                            if pick:
                                break
                if pick is None:
                    continue

                ps = pick_stats[pick["team"]]  # per-team accumulator
                ps["games"] += 1

                ts = m[f"{side}_score"]
                os = m[f"{opp}_score"]
                is_so = m.get("shootout", False)
                h_pen = m.get("home_shootout_score", 0)
                a_pen = m.get("away_shootout_score", 0)
                team_pen = h_pen if side == "home" else a_pen

                # --- game played ---
                ps["match"] += pm["game_played"]

                # --- win / tie / loss + GD ---
                if is_so:
                    ps["match"] += pm["tie"]
                    won = (side == "home" and h_pen > a_pen) or (side == "away" and a_pen > h_pen)
                    if won:
                        ps["match"] += pm["shootout_win"]
                    ps["match"] += team_pen * pm["shootout_goal"]
                    gd = 0
                elif ts > os:
                    ps["match"] += pm["win"]
                    gd = ts - os
                elif ts == os:
                    ps["match"] += pm["tie"]
                    gd = 0
                else:
                    gd = 0

                ps["match"] += gd * pm["gd_per_goal"]

                # --- shutout ---
                if os == 0:
                    ps["match"] += pm["shutout"]

                # --- pool placement ---
                rank = m.get(f"{side}_group_rank")
                if rank is not None:
                    ps["pool"] += pp.get(str(rank), 0)

                # --- tournament finish ---
                stage = m.get("stage", "GROUP")
                if stage == "FINAL":
                    won = (not is_so and ts > os) or (is_so and ((side == "home" and h_pen > a_pen) or (side == "away" and a_pen > h_pen)))
                    ps["tourney"] += tf["winner"] if won else tf["runner_up"]
                elif stage == "THIRD_PLACE":
                    won = (not is_so and ts > os) or (is_so and ((side == "home" and h_pen > a_pen) or (side == "away" and a_pen > h_pen)))
                    if won:
                        ps["tourney"] += tf["third"]

                # --- goal scorer points ---
                pts_per_goal = sp["boxes_7_8"] if pick["box"] in (7, 8) else sp["boxes_1_6"]

                for goal in m.get("goals", []):
                    if goal.get("own_goal") or goal.get("shootout"):
                        continue
                    if not team_eq(pick["team"], goal["team"]):
                        continue
                    for sname in pick["scorers"]:
                        if scorer_eq(sname, goal["scorer"], player_aliases):
                            ps["scorer"] += pts_per_goal
                            break

        # Aggregate totals from per-team stats
        for ps in pick_stats.values():
            ps["total"] = ps["bonus"] + ps["match"] + ps["scorer"] + ps["pool"] + ps["tourney"]

        match_pts   = sum(ps["match"]   for ps in pick_stats.values())
        scorer_pts  = sum(ps["scorer"]  for ps in pick_stats.values())
        pool_pts    = sum(ps["pool"]    for ps in pick_stats.values())
        tourney_pts = sum(ps["tourney"] for ps in pick_stats.values())
        bonuses     = sum(ps["bonus"]   for ps in pick_stats.values())
        total_games = sum(ps["games"]   for ps in pick_stats.values())

        picks_list = sorted(pick_stats.values(), key=lambda x: (x["box"], x["team"]))

        teams_remaining = sum(
            1 for p in entry["picks"]
            if our_team_key(p["team"]) not in eliminated
        )

        total = bonuses + match_pts + scorer_pts + pool_pts + tourney_pts
        standings.append({
            "id":   entry["id"],
            "name": entry["name"],
            "total": total,
            "total_games": total_games,
            "teams_remaining": teams_remaining,
            "picks": picks_list,
            "breakdown": {
                "bonuses":          bonuses,
                "match_results":    match_pts,
                "scorer_goals":     scorer_pts,
                "pool_placement":   pool_pts,
                "tournament_finish": tourney_pts,
            },
        })

    standings.sort(key=lambda x: (x["id"] == "bryce-williams", -x["total"], x["name"]))
    for i, s in enumerate(standings):
        s["rank"] = i + 1

    return standings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"=== update.py started {ts} ===")

    entries   = load("entries.json")
    rules     = load("rules.json")
    overrides = load("overrides.json") or {"overrides": []}
    aliases   = load("aliases.json")   or {}

    if not entries or not rules:
        print("ERROR: entries.json or rules.json missing — aborting", file=sys.stderr)
        sys.exit(1)

    existing_data    = load("matches.json") or {"matches": []}
    existing_by_id   = {m["espn_id"]: m for m in existing_data.get("matches", [])}

    print("Fetching match schedule from ESPN...")
    try:
        matches_by_id = fetch_all_matches(existing_by_id)
    except Exception as e:
        print(f"ERROR fetching matches: {e} — keeping existing data unchanged", file=sys.stderr)
        sys.exit(0)

    # Fetch goal data for finished matches that don't have it yet
    for eid, m in matches_by_id.items():
        if not m.get("goals"):
            print(f"  Fetching goals: {m['home']} vs {m['away']} ({m['date']})")
            goals = fetch_goals(eid)
            m["goals"] = goals
            if goals:
                print(f"    {len(goals)} goal(s) found")

    apply_overrides(matches_by_id, overrides)

    matches_list = list(matches_by_id.values())

    group_standings = compute_group_standings(matches_list)
    apply_group_ranks(matches_list, group_standings)

    save("matches.json", {
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches":    matches_list,
    })
    print(f"Saved {len(matches_list)} matches.")

    print("Computing standings...")
    standings = compute_standings(entries, rules, matches_list, aliases)

    save("standings.json", {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "standings":    standings,
    })

    print(f"\nStandings ({len(standings)} entries):")
    for s in standings:
        bd = s["breakdown"]
        print(
            f"  {s['rank']:2}. {s['name']:<22} {s['total']:4} pts"
            f"  (bonus {bd['bonuses']} | match {bd['match_results']}"
            f" | goals {bd['scorer_goals']} | pool {bd['pool_placement']}"
            f" | tourney {bd['tournament_finish']})"
        )

    print(f"\n=== done {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} ===")


if __name__ == "__main__":
    main()
