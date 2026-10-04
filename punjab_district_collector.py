# -*- coding: utf-8 -*-
"""Punjab SIS district-wise daily attendance collector.

Base: working Okara attendance dashboard pattern.
District mode intentionally avoids Tehsil/Markaz/School traversal.

SIS endpoints:
  Teacher: /attendance/get_teachers_today_attendance_stats
  Student: /dashboard/attendance_table
"""
import argparse, json, re
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
OUT = Path("data/punjab_district_attendance.json")
TIMEOUT = 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154 Safari/537.36")


def clean(x):
    return re.sub(r"\s+", " ", str(x or "")).strip()


def num(x):
    s = re.sub(r"[^\d.-]", "", str(x or ""))
    try:
        return float(s) if s else 0.0
    except Exception:
        return 0.0


def make_session():
    s = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(
        max_retries=retry, pool_connections=16, pool_maxsize=16
    ))
    s.headers.update({
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def parse_options(text):
    out = []
    for value, name in re.findall(
        r"<option[^>]*value\s*=\s*['\"]([^'\"]*)['\"][^>]*>\s*(.*?)\s*</option>",
        text or "", re.I | re.S
    ):
        name = clean(re.sub(r"<[^>]+>", " ", name))
        value = clean(value)
        if value and name and name.lower() not in {
            "all", "select district", "all districts", "all tehsils", "all schools"
        }:
            out.append((value, name))
    return out


def parse_rows(html):
    if isinstance(html, dict):
        html = html.get("data") or html.get("html") or ""
    rows = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", str(html), re.I | re.S):
        cells = [
            clean(re.sub(r"<[^>]+>", " ", c))
            for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.I | re.S)
        ]
        if cells:
            rows.append(cells)
    return rows


def get(s, path, params=None, html=False):
    r = s.get(
        BASE + path, params=params, timeout=TIMEOUT,
        headers={
            "Accept": "text/html,application/json,text/javascript,*/*;q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/",
        },
    )
    r.raise_for_status()
    if html:
        return r.text
    try:
        return r.json()
    except Exception:
        return r.text


def district_options(s):
    html = get(s, "/dashboard", html=True)
    blocks = re.findall(r"<select\b([^>]*)>(.*?)</select>", html, re.I | re.S)
    candidates = []
    for attrs, body in blocks:
        if "district" not in attrs.lower():
            continue
        opts = parse_options(body)
        if 20 <= len(opts) <= 60:
            candidates.append(opts)
    if candidates:
        return max(candidates, key=len)

    for _, body in blocks:
        opts = parse_options(body)
        names = {n.upper() for _, n in opts}
        if "LAHORE" in names and "OKARA" in names and len(opts) <= 60:
            return opts
    raise RuntimeError("Could not isolate SIS district selector")


def teacher_stats(s, district_id):
    data = get(s, "/attendance/get_teachers_today_attendance_stats", {
        "district": district_id,
        "tehsil": "",
        "markaz": "",
        "school": "",
        "s_id_emis_code": "",
        "ony_kpztp_districts": "false",
    })
    if not isinstance(data, dict):
        raise RuntimeError("Teacher endpoint did not return JSON")

    present = int(num(data.get("present_count")))
    absent = int(num(data.get("absent_count")))
    unmarked = int(num(data.get("unmarked_count")))
    marked = int(num(data.get("marked_count")))

    # SIS explicitly returns marked_count and unmarked_count.
    # Their sum is the attendance population represented by this endpoint.
    total = marked + unmarked

    if total < present + absent:
        raise RuntimeError(
            f"Invalid teacher totals: total={total}, "
            f"present={present}, absent={absent}"
        )

    return {
        "total_teachers": total,
        "teachers_present": present,
        "teachers_absent": absent,
        "teachers_unmarked": unmarked,
        "teachers_marked": marked,
        "teacher_attendance_pct": round(present / total * 100, 2) if total else 0.0,
    }


def student_stats(s, district_id, date_from):
    """Use the same student attendance table used by the working Okara dashboard.

    At district scope, SIS returns summary rows rather than school EMIS rows.
    We aggregate only rows that have the standard numeric attendance columns.
    """
    html = get(s, "/dashboard/attendance_table", {
        "district_id": district_id,
        "tehsil_id": "",
        "markaz_id": "",
        "date_from": date_from,
        "only_kpztp_districts": "false",
    }, html=True)

    totals = {"enrolled": 0, "present": 0, "absent": 0, "unmarked": 0}
    usable = 0

    for cells in parse_rows(html):
        # Okara dashboard's standard attendance row:
        # name | enrolled | present | % | absent | % | not marked | %
        if len(cells) >= 8:
            candidates = [
                (cells[1], cells[2], cells[3], cells[5], cells[7]),
                (cells[2], cells[3], cells[4], cells[6], cells[8]) if len(cells) >= 9 else None,
            ]
            chosen = None
            for c in candidates:
                if not c:
                    continue
                name, enrolled, present, absent, unmarked = c
                vals = [num(enrolled), num(present), num(absent), num(unmarked)]
                if vals[0] >= vals[1] + vals[2] and vals[0] > 0:
                    chosen = vals
                    break
            if chosen:
                totals["enrolled"] += int(chosen[0])
                totals["present"] += int(chosen[1])
                totals["absent"] += int(chosen[2])
                totals["unmarked"] += int(chosen[3])
                usable += 1

    if usable == 0:
        raise RuntimeError(
            f"No usable district student rows from /dashboard/attendance_table "
            f"for district {district_id}"
        )

    total = totals["enrolled"]
    totals.update({
        "total_students": total,
        "students_present": totals["present"],
        "students_absent": totals["absent"],
        "students_unmarked": totals["unmarked"],
        "student_attendance_pct": round(totals["present"] / total * 100, 2) if total else 0.0,
        "student_rows": usable,
    })
    return totals


def collect_district(s, district_id, district_name, date_from):
    out = {
        "district_id": district_id,
        "district": district_name,
        "status": "ERROR",
        "errors": [],
    }
    try:
        t = teacher_stats(s, district_id)
        st = student_stats(s, district_id, date_from)

        out.update(t)
        out.update(st)
        total_people = t["total_teachers"] + st["total_students"]
        present_people = t["teachers_present"] + st["students_present"]
        out["overall_attendance_pct"] = (
            round(present_people / total_people * 100, 2)
            if total_people else 0.0
        )
        out["total_schools"] = None
        out["status"] = "OK"
    except Exception as e:
        out["errors"] = [str(e)]
    return out


def collect():
    s = make_session()
    districts = district_options(s)
    date_from = datetime.now().astimezone().strftime("%d/%m/%Y")

    rows = []
    for did, name in districts:
        rec = collect_district(s, did, name, date_from)
        rows.append(rec)
        print(f"{name}: {rec['status']}")
        if rec.get("errors"):
            print("  ", rec["errors"][0])

    complete = sum(x.get("status") == "OK" for x in rows)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "attendance_date": date_from,
        "level": "district",
        "district_count": len(rows),
        "complete_districts": complete,
        "districts": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if len(rows) < 20 or complete != len(rows):
        raise RuntimeError(
            f"Punjab district collection incomplete: {complete}/{len(rows)} complete"
        )


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    collect()
