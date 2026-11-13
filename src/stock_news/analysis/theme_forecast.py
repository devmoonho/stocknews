"""테마 예측 — 저녁 1차(19:30 match.py --session evening, 종가매매 리포트) · 아침 최종(07:00 match.py) · 기준선 · 헤지 ·
적중 판정(예측 대상일 15:35 theme_daily.py에서, 1차·최종 각각).

예측 점수 = 구성요소(0~1) 가중 평균 — 가중치는 config/theme_forecast.json (처음엔 같게, 사람이 조정):
  stage       전일 진행 단계 (확산 > 태동 > 과열 > 소멸)
  breadth     전일 폭 (상승 비율 · +5% 종목 수)
  trade_value 전일 거래대금 증가율 (20일 평균 대비)
  material    밤사이 새 재료 (정부 정책 ×2 · 제목 기사 · 규모 있는 공시) — 다이제스트 D·E와 후보 공시에서
신뢰도: 0.5 이상인 구성요소 수 3개↑ 상 · 2개 중 · 그 외 하.
기준선: 전일 중앙값 초과 상위가 그대로 이어진다는 예측. 예측·기준선·실제 순위 모두 같은 중복 회피 규칙(theme_groups.pick_top).
"""
from __future__ import annotations

import ast
import datetime as dt
import json
from collections import defaultdict

from . import theme_groups as tg


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        try:
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return []
    return []


def prev_day(conn, date: str) -> str | None:
    r = conn.execute("SELECT MAX(dt) FROM theme_daily WHERE dt < ?", (date,)).fetchone()
    return r[0] if r else None


def _day_rows(conn, day: str) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    rows = {r["group_id"]: dict(r) for r in conn.execute("SELECT * FROM theme_daily WHERE dt=?", (day,))}
    mem: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM theme_members_daily WHERE dt=? ORDER BY group_id, pos", (day,)):
        mem[r["group_id"]].append(dict(r))
    return rows, mem


def material_by_group(candidates: dict, groups: list[dict], names_to_groups: dict[str, set[str]],
                      cfg: dict) -> dict[str, dict]:
    """밤사이 재료 → {group_id: {gov, title, disclosure, notes:[...], leaders:[code]}}."""
    pts = cfg["material_points"]
    out: dict[str, dict] = defaultdict(lambda: {"gov": 0, "title": 0, "disclosure": 0, "notes": [], "leaders": []})
    members = {g["group_id"]: set(g["members"]) for g in groups}
    for sig in candidates.get("theme_signals", []):
        gids = set().union(*(names_to_groups.get(n, set()) for n in sig.get("themes", []))) if sig.get("themes") else set()
        for gid in gids:
            o = out[gid]
            o["gov"] += min(sig.get("gov_articles") or 0, 2)
            o["title"] += min(sig.get("title_mentions") or 0, 3)
            if sig.get("sample_articles"):
                o["notes"].append(sig["sample_articles"][0]["title"])
            o["leaders"] += [m["code"] for m in sig.get("members", []) if m.get("rank") in (1, 2)]
    for gp in candidates.get("gov_policy", []):
        for name in _as_list(gp.get("themes")):
            for gid in names_to_groups.get(name, set()):
                out[gid]["gov"] += 1
                out[gid]["notes"].append(f"[정책] {gp.get('title', '')}")
        for ld in gp.get("leaders") or []:
            for gid in names_to_groups.get(ld.get("theme", ""), set()):
                out[gid]["leaders"] += [m["code"] for m in ld.get("top", []) if m.get("rank") in (1, 2)]
    for s in candidates.get("stocks", []):
        big = [d for d in s.get("disclosures", []) if d.get("signal") == "buy" and (d.get("scale_score") or 0) >= 3
               and not (d.get("figures") or {}).get("is_correction")]
        if not big:
            continue
        for gid, ms in members.items():
            if s["code"] in ms:
                out[gid]["disclosure"] += 1
                out[gid]["notes"].append(f"[공시] {s['name']} {big[0]['report_nm'][:30]}")
    for o in out.values():
        o["points"] = o["gov"] * pts["gov"] + o["title"] * pts["title"] + o["disclosure"] * pts["disclosure"]
        o["notes"] = list(dict.fromkeys(o["notes"]))[:4]
        o["leaders"] = list(dict.fromkeys(o["leaders"]))
    return dict(out)


def names_index(conn, groups: list[dict]) -> dict[str, set[str]]:
    """테마 이름 → 그 테마가 든 group_id들 (다이제스트의 테마 이름을 묶음으로)."""
    names = {(s, c): n for s, c, n in conn.execute("SELECT source, theme_code, name FROM themes")}
    idx: dict[str, set[str]] = defaultdict(set)
    for g in groups:
        for key in g["themes"]:
            if key in names:
                idx[names[key]].add(g["group_id"])
    return idx


def components(row: dict | None, mat: dict | None, cfg: dict) -> dict:
    c = {"stage": 0.0, "breadth": 0.0, "trade_value": 0.0, "material": 0.0}
    if row:
        c["stage"] = cfg["stage_points"].get(row.get("stage") or "", 0.0)
        c["breadth"] = round(0.5 * (row.get("rise_ratio") or 0) + 0.5 * min((row.get("up5") or 0) / 5, 1), 3)
        tv = row.get("tv_ratio20")
        c["trade_value"] = round(min(max((tv or 1) - 1, 0) / 2, 1), 3)
    if mat:
        c["material"] = round(min(mat["points"] / 4, 1), 3)
    return c


def confidence(c: dict) -> str:
    k = sum(v >= 0.5 for v in c.values())
    return "상" if k >= 3 else "중" if k == 2 else "하"


def forecast(conn, date: str, candidates: dict, cfg: dict | None = None, save: bool = True,
             phase: str = "morning") -> dict:
    """date(예측 대상 거래일) 테마 전망. 기준은 date 직전 집계일(theme_daily).
    phase: evening = 전날 저녁 19:30 1차(종가매매 리포트, 15:30~19:30 재료) · morning = 그날 07:00 최종(밤사이 재료 전부)."""
    cfg = cfg or tg.load_config()
    pday = prev_day(conn, date)
    out = {"date": date, "phase": phase, "made_at": f"{dt.datetime.now():%Y-%m-%d %H:%M}", "prev_day": pday,
           "forecast": [], "forecast_raw": [], "baseline": [], "leaders_yday": [],
           "small_yday": [], "hedge": None, "track": track_record(conn, date, phase)}
    if not pday:
        return out
    groups = [g for g in tg.load_groups(conn)]
    gmap = {g["group_id"]: g for g in groups}
    rows, mem = _day_rows(conn, pday)
    mats = material_by_group(candidates, groups, names_index(conn, groups), cfg)
    nxt = _nxt_map(conn)
    rising = tg.rising_sets(mem, cfg["dedupe_rise_pct"])
    w = cfg["weights"]

    ranked_rows = sorted((r for r in rows.values() if not r["small"] and r.get("rank")), key=lambda r: r["rank"])
    pool = {r["group_id"] for r in ranked_rows[:cfg["candidate_pool"]]}
    pool |= {gid for gid, m in mats.items() if m["points"] > 0 and gid in gmap and not gmap[gid]["small"]}
    items = []
    for gid in pool:
        if gid not in gmap:                       # 그 사이 테마 구성이 바뀌어 없어진 묶음
            continue
        row, mat = rows.get(gid), mats.get(gid)
        c = components(row, mat, cfg)
        score = sum(w[k] * v for k, v in c.items()) / sum(w.values())
        ms = mem.get(gid, [])
        leader = (row or {}).get("leader_code") or next((x for x in (mat or {}).get("leaders", [])
                                                        if x in set(gmap[gid]["members"])), None)
        seconds = [m["code"] for m in ms if m.get("label") == "2등주"][:2]
        items.append({"group_id": gid, "name": gmap[gid]["name"] if gid in gmap else (row or {}).get("group_name"),
                      "score": round(score, 3), "components": c, "confidence": confidence(c),
                      "leader_code": leader, "seconds": seconds,
                      "basis": _basis(row, mat), "stage": (row or {}).get("stage"),
                      "nxt": {x: nxt.get(x) for x in ([leader] if leader else []) + seconds}})
    items.sort(key=lambda x: -x["score"])
    n = cfg["top_n"]
    out["forecast"] = tg.pick_top(items, n, rising, cfg, dedupe=True)
    out["forecast_raw"] = tg.pick_top(items, n, rising, cfg, dedupe=False)
    base = [{"group_id": r["group_id"], "name": r["group_name"], "score": r["median_excess"], "components": {},
             "confidence": None, "leader_code": r.get("leader_code"),
             "seconds": [m["code"] for m in mem.get(r["group_id"], []) if m.get("label") == "2등주"][:2]}
            for r in ranked_rows]
    out["baseline"] = tg.pick_top(base, n, rising, cfg, dedupe=True)
    out["baseline_raw"] = tg.pick_top(base, n, rising, cfg, dedupe=False)
    # 어제 주도 테마(중복 회피) + 서열
    lead = []
    for it in tg.pick_top([dict(r, name=r["group_name"]) for r in ranked_rows], n, rising, cfg):
        ms = mem.get(it["group_id"], [])
        lead.append({**{k: it.get(k) for k in ("group_id", "name", "median_excess", "avg_excess", "wavg_excess",
                                               "rise_ratio", "up5", "limit_ups", "stage", "streak", "tv_ratio20",
                                               "leader_changed", "related")},
                     "approx": it.get("approx"),
                     "leader": [m for m in ms if m["label"] == "대장"],
                     "seconds": [m for m in ms if m["label"] == "2등주"],
                     "followers": [m for m in ms if m["label"] == "후발주"][:3]})
    out["leaders_yday"] = lead
    out["small_yday"] = sorted(({"name": r["group_name"], "median_excess": r["median_excess"], "n": r["n"]}
                                for r in rows.values() if r["small"] and (r["median_excess"] or 0) >= 5),
                               key=lambda x: -x["median_excess"])[:5]
    out["hedge"] = hedge(conn, pday, cfg)
    # 강세 단계 묶음 소속 종목 표시(다이제스트용 — 점수에는 stage_bonus가 켜졌을 때만)
    stages: dict[str, list[str]] = defaultdict(list)
    for r in rows.values():
        if r.get("stage") in ("태동", "확산", "과열") and not r["small"]:
            for c in gmap.get(r["group_id"], {}).get("members", []):
                stages[c].append(f"{r['group_name']}({r['stage']} {r['streak']}일차)")
    out["stock_stages"] = {c: v[:2] for c, v in stages.items()}
    names = _names(conn)
    for lst in ("forecast", "forecast_raw", "baseline", "baseline_raw"):
        for it in out[lst]:
            it["leader_name"] = names.get(it.get("leader_code"))
            it["second_names"] = [names.get(c) for c in it.get("seconds", [])]
    for it in out["leaders_yday"]:
        for k in ("leader", "seconds", "followers"):
            for m in it[k]:
                m["name"] = names.get(m["code"])
                m["nxt"] = nxt.get(m["code"])
    if save:
        save_forecast(conn, date, out)
    return out


def _basis(row: dict | None, mat: dict | None) -> list[str]:
    b = []
    if row and row.get("stage"):
        b.append(f"{row['stage']}({row.get('streak') or 0}일)")
    if row and row.get("median_excess") is not None:
        b.append(f"중앙값 초과 {row['median_excess']:+.1f}%p·상승 {round((row.get('rise_ratio') or 0) * 100)}%")
    if row and row.get("tv_ratio20"):
        b.append(f"거래대금 {row['tv_ratio20']:.1f}배")
    if mat and mat["points"]:
        parts = [f"정책 {mat['gov']}" if mat["gov"] else "", f"기사 {mat['title']}" if mat["title"] else "",
                 f"공시 {mat['disclosure']}" if mat["disclosure"] else ""]
        b.append("밤사이 재료(" + "·".join(p for p in parts if p) + ")")
    return b


def _nxt_map(conn) -> dict[str, bool]:
    out = {}
    for code, raw in conn.execute("SELECT code, raw FROM stocks"):
        try:
            out[code] = json.loads(raw or "{}").get("nxtEnable") == "Y"
        except json.JSONDecodeError:
            out[code] = None
    return out


def _names(conn) -> dict[str, str]:
    return {c: n for c, n in conn.execute("SELECT code, name FROM stocks")}


def save_forecast(conn, date: str, out: dict) -> None:
    now = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
    phase = out.get("phase", "morning")
    conn.execute("DELETE FROM theme_forecasts WHERE date=? AND phase=?", (date, phase))
    for kind, lst, dd in (("forecast", out["forecast"], 1), ("forecast", out["forecast_raw"], 0),
                          ("baseline", out["baseline"], 1), ("baseline", out.get("baseline_raw", []), 0)):
        for i, it in enumerate(lst, 1):
            conn.execute("INSERT INTO theme_forecasts (date, phase, kind, rank, group_id, group_name, score, components, "
                         "confidence, leader_pick, second_picks, related, dedupe, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (date, phase, kind, i, it["group_id"], it["name"], it.get("score"),
                          json.dumps(it.get("components"), ensure_ascii=False), it.get("confidence"),
                          it.get("leader_code"), json.dumps(it.get("seconds", [])),
                          json.dumps(it.get("related", []), ensure_ascii=False), dd, now))
    conn.commit()


def hedge(conn, upto: str, cfg: dict) -> dict:
    """최근 N거래일 중 코스피 하락일만 모아, 그날들 중앙값 등락 합이 플러스인 묶음 중 초과수익 누적 상위 3 (스팩 제외)."""
    days = [r[0] for r in conn.execute("SELECT DISTINCT dt FROM theme_daily WHERE dt<=? ORDER BY dt DESC LIMIT ?",
                                       (upto, cfg["hedge_lookback"]))]
    idx = {r[0]: r[1] for r in conn.execute("SELECT dt, close FROM index_daily WHERE code='001' ORDER BY dt")}
    seq = sorted(idx)
    down = []
    for d in days:
        if d in idx and seq.index(d) > 0:
            prev = idx[seq[seq.index(d) - 1]]
            if prev and idx[d] < prev:
                down.append(d)
    if not down:
        return {"down_days": 0, "items": [], "display_only": True}
    q = ",".join("?" * len(down))
    # 하락일에 '실제로 오른'(중앙값 등락 > 0 누적) 테마 — 초과수익만 보면 스팩·저변동 종목이 뽑혀서 절대 등락으로 거른다
    agg = conn.execute(f"SELECT group_id, MAX(group_name) AS name, SUM(median_excess) AS s, SUM(median_pct) AS p, "
                       f"COUNT(*) AS k, MAX(leader_code) AS leader FROM theme_daily WHERE small=0 AND dt IN ({q}) "
                       f"AND group_name NOT LIKE '%스팩%' AND group_name NOT LIKE '%SPAC%' "
                       f"GROUP BY group_id HAVING p > 0 ORDER BY s DESC LIMIT 3", down).fetchall()
    names = _names(conn)
    latest = {r["group_id"]: r["leader_code"] for r in conn.execute("SELECT group_id, leader_code FROM theme_daily WHERE dt=?",
                                                                     (upto,))}
    return {"down_days": len(down), "display_only": len(down) < cfg["hedge_min_down_days"],
            "items": [{"group_id": r["group_id"], "name": r["name"], "cum_excess": round(r["s"], 2), "cum_pct": round(r["p"], 2),
                       "days": r["k"], "leader_name": names.get(latest.get(r["group_id"]) or r["leader"])} for r in agg]}


def verify(conn, day: str, cfg: dict | None = None) -> list[dict]:
    """day(오늘)의 실제 결과로 그날 아침 예측·기준선 채점 → theme_forecast_results."""
    cfg = cfg or tg.load_config()
    rows, mem = _day_rows(conn, day)
    if not rows:
        return []
    rising = tg.rising_sets(mem, cfg["dedupe_rise_pct"])
    ranked = [dict(r, name=r["group_name"]) for r in sorted((r for r in rows.values() if r.get("rank")),
                                                          key=lambda r: r["rank"])]
    labels = {(gid, m["code"]): m["label"] for gid, ms in mem.items() for m in ms}
    out = []
    for dd in (1, 0):
        top = {x["group_id"] for x in tg.pick_top(ranked, cfg["hit_top_n"], rising, cfg, dedupe=bool(dd))}
        for phase, kind in (("evening", "forecast"), ("morning", "forecast"), ("evening", "baseline"),
                            ("morning", "baseline")):
            fc = conn.execute("SELECT * FROM theme_forecasts WHERE date=? AND phase=? AND kind=? AND dedupe=? "
                              "ORDER BY rank", (day, phase, kind, dd)).fetchall()
            if not fc:
                continue
            ex = [rows[f["group_id"]]["median_excess"] for f in fc if f["group_id"] in rows]
            hits = [f["group_id"] in top for f in fc]
            lh = [rows.get(f["group_id"], {}).get("leader_code") == f["leader_pick"] for f in fc if f["leader_pick"]]
            sec = [labels.get((f["group_id"], c)) in ("대장", "2등주") for f in fc for c in json.loads(f["second_picks"] or "[]")]
            res = {"date": day, "phase": phase, "kind": kind, "dedupe": dd, "n": len(fc),
                   "avg_excess": round(sum(ex) / len(ex), 3) if ex else None,
                   "top10_hits": sum(hits), "top10_ratio": round(sum(hits) / len(fc), 3),
                   "leader_hits": sum(lh), "leader_n": len(lh), "second_hits": sum(sec), "second_n": len(sec),
                   "detail": json.dumps([{"group": f["group_name"], "excess": rows.get(f["group_id"], {}).get("median_excess"),
                                          "rank": rows.get(f["group_id"], {}).get("rank"), "top": h}
                                         for f, h in zip(fc, hits)], ensure_ascii=False),
                   "updated_at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"}
            conn.execute(f"INSERT OR REPLACE INTO theme_forecast_results ({', '.join(res)}) VALUES ({', '.join('?' * len(res))})",
                         list(res.values()))
            out.append(res)
    conn.commit()
    return out


def track_record(conn, before: str, phase: str = "morning", days: int = 20) -> dict:
    """리포트에 붙일 누적 성적(중복 회피 적용분, 같은 단계끼리): 예측 vs 기준선."""
    rec = {}
    for kind in ("forecast", "baseline"):
        r = conn.execute("SELECT COUNT(*), AVG(avg_excess), AVG(top10_ratio), SUM(leader_hits), SUM(leader_n) "
                         "FROM (SELECT * FROM theme_forecast_results WHERE phase=? AND kind=? AND dedupe=1 AND date<? "
                         "ORDER BY date DESC LIMIT ?)", (phase, kind, before, days)).fetchone()
        rec[kind] = {"days": r[0], "avg_excess": r[1], "top10_ratio": r[2],
                     "leader_ratio": (r[3] / r[4]) if r[4] else None}
    last = conn.execute("SELECT * FROM theme_forecast_results WHERE phase=? AND dedupe=1 AND date<? "
                        "ORDER BY date DESC LIMIT 2", (phase, before)).fetchall()
    rec["last"] = {r["kind"]: dict(r) for r in last}
    return rec
