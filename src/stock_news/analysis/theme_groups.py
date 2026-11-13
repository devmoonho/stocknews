"""테마 묶기(대표 중심) + 상위 선정 시 중복 회피.

묶기 — 구성이 사실상 같은 테마를 하나로 (네이버 265 + 키움 142):
  1. 테마를 구성 종목 수 내림차순(같으면 네이버 먼저, 코드순)으로 정렬
  2. 아직 배정되지 않은 가장 큰 테마를 대표로
  3. 대표와 **직접** 겹침 계수 |A∩B|/min(|A|,|B|) ≥ overlap 이고 크기 비율 min/max ≥ size_ratio 인 미배정 테마만 묶음에
     (B를 거쳐 C가 들어오는 사슬 연결 없음 · 큰 일반 테마가 세부 테마를 삼키지 않음)
  4. 구성 = 대표 + 묶인 테마 종목 합집합, 대표의 union_cap배를 넘으면 대표 종목만
  묶음 ID = 대표 'source:theme_code'. 다시 묶었을 때 예전 대표가 정확히 하나 들어 있으면 그 ID를 이어받는다.

상위 선정(pick_top) — 묶기는 그대로 두고 고를 때만:
  순위대로 내려가며 이미 뽑힌 묶음과 대장주가 같거나, 당일 상승 종목 겹침 계수 ≥ dedupe_overlap 이면 건너뛰고
  뽑힌 묶음의 related에 이름만 붙인다.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from ..config import ROOT

CONFIG_PATH = ROOT / "config" / "theme_forecast.json"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def overlap(a: set, b: set) -> float:
    m = min(len(a), len(b))
    return len(a & b) / m if m else 0.0


def build_groups(themes: dict[tuple[str, str], set[str]], names: dict[tuple[str, str], str],
                 cfg: dict | None = None, prev_reps: dict[str, str] | None = None) -> list[dict]:
    """themes: {(source, code): {종목코드}} → 묶음 목록.
    prev_reps: {'source:code'(예전 대표 테마): 예전 group_id} — ID 이어받기용."""
    g = (cfg or load_config())["groups"]
    order = sorted((k for k in themes if themes[k]),
                   key=lambda k: (-len(themes[k]), k[0] != "naver", k[1]))
    left = set(order)
    out: list[dict] = []
    for rep in order:
        if rep not in left:
            continue
        left.discard(rep)
        a = themes[rep]
        mates = []
        for k in order:
            if k not in left:
                continue
            b = themes[k]
            if overlap(a, b) >= g["overlap"] and min(len(a), len(b)) / max(len(a), len(b)) >= g["size_ratio"]:
                mates.append(k)
                left.discard(k)
        union = set(a).union(*(themes[k] for k in mates)) if mates else set(a)
        if len(union) > g["union_cap"] * len(a):
            union = set(a)
        out.append({"rep": rep, "themes": [rep] + mates, "members": sorted(union),
                    "name": names.get(rep, rep[1]) + (f" (+유사 {len(mates)}개)" if mates else "")})
    # 묶음 ID: 기본은 대표, 예전 대표가 정확히 하나 들어 있으면 그 ID 유지
    used: set[str] = set()
    prev_reps = prev_reps or {}
    for grp in out:
        olds = [prev_reps[f"{s}:{c}"] for s, c in grp["themes"] if f"{s}:{c}" in prev_reps]
        gid = olds[0] if len(olds) == 1 and olds[0] not in used else f"{grp['rep'][0]}:{grp['rep'][1]}"
        if gid in used:
            gid = f"{grp['rep'][0]}:{grp['rep'][1]}"
        used.add(gid)
        grp["group_id"] = gid
    return out


def load_themes(conn) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], str]]:
    excluded = {r[0] for r in conn.execute("SELECT code FROM stocks WHERE is_excluded=1 OR is_listed=0")}
    themes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for s, t, code in conn.execute("SELECT source, theme_code, stock_code FROM theme_stocks"):
        if code not in excluded:
            themes[(s, t)].add(code)
    names = {(s, t): n for s, t, n in conn.execute("SELECT source, theme_code, name FROM themes")}
    return dict(themes), names


def rebuild(conn, log=print) -> list[dict]:
    """테마 마스터 기준으로 묶음을 다시 계산해 theme_groups에 저장. 구성이 같으면 ID도 그대로 나온다."""
    cfg = load_config()
    themes, names = load_themes(conn)
    prev = {f"{r['source']}:{r['theme_code']}": r["group_id"]
            for r in conn.execute("SELECT source, theme_code, group_id FROM theme_groups WHERE is_rep=1")}
    groups = build_groups(themes, names, cfg, prev)
    now = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
    conn.execute("DELETE FROM theme_groups")
    for grp in groups:
        for s, c in grp["themes"]:
            conn.execute("INSERT INTO theme_groups VALUES (?,?,?,?,?,?,?,?,?)",
                         (grp["group_id"], s, c, int((s, c) == grp["rep"]), grp["name"], len(grp["members"]),
                          len(grp["themes"]), json.dumps(grp["members"]), now))
    conn.commit()
    log(size_report(groups, cfg))
    return groups


def load_groups(conn) -> list[dict]:
    """theme_groups → [{group_id, name, members, themes:[(source, code)], small}]. 비어 있으면 새로 계산."""
    rows = conn.execute("SELECT * FROM theme_groups").fetchall()
    if not rows:
        rebuild(conn, log=lambda m: None)
        rows = conn.execute("SELECT * FROM theme_groups").fetchall()
    cfg = load_config()["groups"]
    by: dict[str, dict] = {}
    for r in rows:
        g = by.setdefault(r["group_id"], {"group_id": r["group_id"], "name": r["group_name"],
                                          "members": json.loads(r["members"]), "themes": []})
        g["themes"].append((r["source"], r["theme_code"]))
        if r["is_rep"]:
            g["name"] = r["group_name"]
    for g in by.values():
        g["small"] = len(g["members"]) < cfg["min_members"]
    return list(by.values())


def size_report(groups: list[dict], cfg: dict | None = None, top: int = 10) -> str:
    cfg = cfg or load_config()
    g = cfg["groups"]
    big = sorted(groups, key=lambda x: -len(x["members"]))[:top]
    lines = [f"테마 묶음 {len(groups)}개 (구성 {g['min_members']}종목 이상 "
             f"{sum(len(x['members']) >= g['min_members'] for x in groups)}개) — 큰 묶음 {top}개:"]
    for x in big:
        warn = " ⚠️ 구성 종목 과다" if len(x["members"]) > g["warn_members"] else ""
        lines.append(f"  {x['name']} — {len(x['members'])}종목 · 테마 {len(x.get('themes', []))}개{warn}")
    return "\n".join(lines)


def pick_top(ranked: list[dict], n: int, rising: dict[str, set[str]] | None = None,
             cfg: dict | None = None, dedupe: bool | None = None) -> list[dict]:
    """ranked(이미 순위순, 각 항목에 group_id·name·leader_code) → 상위 n개.
    rising: {group_id: 당일 상승 종목 집합}. 건너뛴 묶음 이름은 뽑힌 쪽 'related'에."""
    cfg = cfg or load_config()
    dedupe = cfg.get("dedupe_top", True) if dedupe is None else dedupe
    rising = rising or {}
    picked: list[dict] = []
    for it in ranked:
        if len(picked) >= n:
            break
        it = dict(it, related=[])
        hit = None
        if dedupe:
            for p in picked:
                same_leader = it.get("leader_code") and it.get("leader_code") == p.get("leader_code")
                a, b = rising.get(it["group_id"], set()), rising.get(p["group_id"], set())
                if same_leader or (a and b and overlap(a, b) >= cfg["dedupe_overlap"]):
                    hit = p
                    break
        if hit is not None:
            hit["related"].append(it["name"])
            continue
        picked.append(it)
    return picked


def rising_sets(members: dict[str, list[dict]], rise_pct: float) -> dict[str, set[str]]:
    """{group_id: [{code, change_pct}]} → 상승 종목 집합(+rise_pct%↑, 2종목 미만이면 상승 종목 전체)."""
    out = {}
    for gid, ms in members.items():
        strong = {m["code"] for m in ms if (m.get("change_pct") or 0) >= rise_pct}
        out[gid] = strong if len(strong) >= 2 else {m["code"] for m in ms if (m.get("change_pct") or 0) > 0}
    return out
