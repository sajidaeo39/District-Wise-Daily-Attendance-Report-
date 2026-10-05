# -*- coding: utf-8 -*-
"""Punjab SIS district-wise daily attendance collector.

Accuracy base: the working Okara attendance collector.
The public dashboard remains DISTRICT ONLY; the collector aggregates
official SIS Markaz-level responses underneath so district totals are
complete and are never inferred from unsupported district summary rows.
"""
import argparse, json, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
OUT = Path("data/punjab_district_attendance.json")
TIMEOUT = 45
MAX_WORKERS = 20
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
        total=4, connect=4, read=4, backoff_factor=.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(
        max_retries=retry, pool_connections=32, pool_maxsize=32
    ))
    s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
    return s


def parse_options(text):
    out = []
    for value, name in re.findall(
        r"<option[^>]*value\s*=\s*['\"]([^'\"]*)['\"][^>]*>\s*(.*?)\s*</option>",
        text or "", re.I | re.S
    ):
        value = clean(value)
        name = clean(unescape(re.sub(r"<[^>]+>", " ", name)))
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
            clean(unescape(re.sub(r"<[^>]+>", " ", c)))
            for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.I | re.S)
        ]
        if cells:
            rows.append(cells)
    return rows


def request(s, path, params=None, html=False):
    r = s.get(
        BASE + path, params=params, timeout=TIMEOUT,
        headers={
            "Accept": "text/html,application/json,text/javascript,*/*;q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/dashboard",
        },
    )
    r.raise_for_status()
    if html:
        return r.text
    try:
        return r.json()
    except Exception:
        return r.text


def csrf(s):
    html = request(s, "/dashboard", html=True)
    m = re.search(
        r'name=["\']csrf_test_name["\'][^>]*value=["\']([^"\']+)',
        html, re.I
    )
    return m.group(1) if m else s.cookies.get("csrf_cookie_name", "")


def district_options(s):
    html = request(s, "/dashboard", html=True)
    blocks = re.findall(r"<select\b([^>]*)>(.*?)</select>", html, re.I | re.S)
    candidates = []
    for attrs, body in blocks:
        if "district" in attrs.lower():
            opts = parse_options(body)
            if 20 <= len(opts) <= 60:
                candidates.append(opts)
    if candidates:
        return max(candidates, key=len)

    # Safe fallback: only accept the plausible Punjab district selector.
    for _, body in blocks:
        opts = parse_options(body)
        names = {n.upper() for _, n in opts}
        if "LAHORE" in names and "OKARA" in names and 20 <= len(opts) <= 60:
            return opts
    raise RuntimeError("Could not isolate SIS district selector")


def tehsils(s, district_id, token):
    data = request(s, "/user/get_tehsils", {
        "district": district_id,
        "selectedTehsil": "false",
        "all": "All",
        "csrf_test_name": token,
    })
    html = data.get("html", "") if isinstance(data, dict) else str(data)
    opts = parse_options(html)
    if not opts:
        raise RuntimeError(f"No tehsils returned for district {district_id}")
    return opts


def markazes(s, tehsil_id, token):
    data = request(s, "/user/get_markazes", {
        "tehsil": tehsil_id,
        "selectedMarkaz": "false",
        "all": "All",
        "csrf_test_name": token,
    })
    html = data.get("html", "") if isinstance(data, dict) else str(data)
    opts = parse_options(html)
    if not opts:
        raise RuntimeError(f"No markazes returned for tehsil {tehsil_id}")
    return opts


def student_attendance(s, district_id, tehsil_id, markaz_id, date_from):
    html = request(s, "/dashboard/attendance_table", {
        "district_id": district_id,
        "tehsil_id": tehsil_id,
        "markaz_id": markaz_id,
        "date_from": date_from,
        "only_kpztp_districts": "false",
    }, html=True)

    # Proven Okara school-row structure:
    # # | EMIS - School | Enrolled | Present | % | Absent | % | Not Marked | %
    total = {"enrolled": 0, "present": 0, "absent": 0, "unmarked": 0, "schools": 0}

    for cells in parse_rows(html):
        if len(cells) < 9:
            continue
        m = re.search(r"(?<!\d)(\d{8})(?!\d)", cells[1])
        if not m:
            continue

        enrolled = int(num(cells[2]))
        present = int(num(cells[3]))
        absent = int(num(cells[5]))
        attendance_total = max(enrolled, present + absent)
        unmarked = max(0, attendance_total - present - absent)

        total["enrolled"] += attendance_total
        total["present"] += present
        total["absent"] += absent
        total["unmarked"] += unmarked
        total["schools"] += 1

    if total["schools"] == 0:
        raise RuntimeError(
            f"No school attendance rows: district={district_id}, "
            f"tehsil={tehsil_id}, markaz={markaz_id}"
        )
    return total


def get_filled_staff_from_sanctioned_posts(s, district_id, tehsil_id, markaz_id, school_id, emis_code=""):
    data = request(s, "/dashboard/sanctioned_posts_tab", {
        "district_id": str(district_id),
        "tehsil_id": str(tehsil_id),
        "markaz_id": str(markaz_id),
        "school_id": str(school_id),
        "s_id_emis_code": str(emis_code or ""),
    })
    html = data.get("data", "") if isinstance(data, dict) else str(data)
    for row in parse_rows(html):
        joined = " ".join(row).lower()
        if row and (joined.startswith("total") or "overall" in joined):
            nums = [num(c) for c in row[1:] if re.search(r"\d", c)]
            if len(nums) >= 3:
                return int(max(0, nums[1]))
    raise RuntimeError(
        f"Filled staff total not found: district={district_id}, "
        f"tehsil={tehsil_id}, markaz={markaz_id}, school={school_id}"
    )


def teacher_attendance(s, district_id, tehsil_id, markaz_id, school_id, emis_code, filled_total):
    data = request(s, "/attendance/get_teachers_today_attendance_stats", {
        "district": district_id,
        "tehsil": tehsil_id,
        "markaz": markaz_id,
        "school": school_id,
        "s_id_emis_code": emis_code,
        "ony_kpztp_districts": "false",
    })
    if not isinstance(data, dict):
        raise RuntimeError("Teacher attendance endpoint did not return JSON")

    present = int(num(data.get("present_count")))
    absent = int(num(data.get("absent_count")))
    total = int(max(0, filled_total))

    if present + absent > total:
        raise RuntimeError(
            f"Teacher attendance exceeds filled staff: {present}+{absent}>{total}"
        )

    return {
        "total": total,
        "present": present,
        "absent": absent,
        "unmarked": max(0, total - present - absent),
    }

def collect_markaz(s, district_id, tehsil_id, markaz_id, date_from, token):
    # Each concurrent Markaz gets its own HTTP session. requests.Session is not
    # shared across threads, which avoids intermittent SIS connection/cookie races.
    s = make_session()
    # Student attendance is returned as school rows for a Markaz.
    st = student_attendance(s, district_id, tehsil_id, markaz_id, date_from)

    # Teacher attendance must use the same school-level SIS denominator
    # proven by the working Okara collector. Do not infer teacher totals
    # from a Markaz-level sanctioned-posts summary.
    school_data = request(s, "/user/get_schools", {
        "markaz": markaz_id,
        "selectedSchool": "false",
        "all": "All",
        "csrf_test_name": token,
    })
    html = school_data.get("html", "") if isinstance(school_data, dict) else str(school_data)
    schools = parse_options(html)
    if not schools:
        raise RuntimeError(f"No schools returned for markaz {markaz_id}")

    teacher_total = teacher_present = teacher_absent = 0
    teacher_errors = []

    def one_school(item):
        sid, sname = item
        m = re.search(r"(?<!\d)(\d{8})(?!\d)", sname)
        emis = m.group(1) if m else (sid if re.fullmatch(r"\d{8}", sid) else "")
        if not emis:
            raise RuntimeError(f"EMIS not found for school {sid}: {sname}")

        local = make_session()
        filled = get_filled_staff_from_sanctioned_posts(
            local, district_id, tehsil_id, markaz_id, sid, emis
        )
        ta = teacher_attendance(
            local, district_id, tehsil_id, markaz_id, sid, emis, filled
        )
        return ta

    with ThreadPoolExecutor(max_workers=min(20, max(1, len(schools)))) as ex:
        futures = {ex.submit(one_school, item): item for item in schools}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                ta = fut.result()
                teacher_total += ta["total"]
                teacher_present += ta["present"]
                teacher_absent += ta["absent"]
            except Exception as e:
                teacher_errors.append(f"{item[1]}: {type(e).__name__}: {e}")

    if teacher_errors:
        raise RuntimeError(
            f"{len(teacher_errors)} of {len(schools)} school teacher collections failed. "
            f"First error: {teacher_errors[0]}"
        )

    return {
        "schools": st["schools"],
        "teachers": teacher_total,
        "teachers_present": teacher_present,
        "teachers_absent": teacher_absent,
        "teachers_unmarked": max(0, teacher_total - teacher_present - teacher_absent),
        "students": st["enrolled"],
        "students_present": st["present"],
        "students_absent": st["absent"],
        "students_unmarked": st["unmarked"],
    }

def collect_district(s, district_id, district_name, date_from, token):
    tehsil_list = tehsils(s, district_id, token)
    tasks = []
    for tid, tname in tehsil_list:
        for mid, mname in markazes(s, tid, token):
            tasks.append((tid, tname, mid, mname))

    agg = {
        "total_schools": 0, "total_teachers": 0, "teachers_present": 0,
        "teachers_absent": 0, "teachers_unmarked": 0,
        "total_students": 0, "students_present": 0,
        "students_absent": 0, "students_unmarked": 0,
    }
    errors = []

    # Markaz requests are independent. Parallelize them, but never turn
    # failed requests into zeros.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(
                collect_markaz, s, district_id, tid, mid, date_from, token
            ): (tid, tname, mid, mname)
            for tid, tname, mid, mname in tasks
        }
        for fut in as_completed(futures):
            tid, tname, mid, mname = futures[fut]
            try:
                x = fut.result()
                agg["total_schools"] += x["schools"]
                agg["total_teachers"] += x["teachers"]
                agg["teachers_present"] += x["teachers_present"]
                agg["teachers_absent"] += x["teachers_absent"]
                agg["teachers_unmarked"] += x["teachers_unmarked"]
                agg["total_students"] += x["students"]
                agg["students_present"] += x["students_present"]
                agg["students_absent"] += x["students_absent"]
                agg["students_unmarked"] += x["students_unmarked"]
            except Exception as e:
                errors.append(
                    f"{tname}/{mname}: {type(e).__name__}: {e}"
                )

    # Completeness is strict: a district is OK only when every discovered
    # Markaz was collected. This prevents partial data being shown as live.
    complete = len(errors) == 0 and len(tasks) > 0
    if not complete:
        raise RuntimeError(
            f"{len(errors)} of {len(tasks)} Markaz collections failed. "
            f"First error: {errors[0] if errors else 'unknown'}"
        )

    tt = agg["total_teachers"]
    st = agg["total_students"]
    tp = agg["teachers_present"]
    sp = agg["students_present"]
    people = tt + st

    return {
        "district_id": district_id,
        "district": district_name,
        "status": "OK",
        "total_schools": agg["total_schools"],
        "total_teachers": tt,
        "teachers_present": tp,
        "teachers_absent": agg["teachers_absent"],
        "teachers_unmarked": agg["teachers_unmarked"],
        "teacher_attendance_pct": round(tp / tt * 100, 2) if tt else 0,
        "total_students": st,
        "students_present": sp,
        "students_absent": agg["students_absent"],
        "students_unmarked": agg["students_unmarked"],
        "student_attendance_pct": round(sp / st * 100, 2) if st else 0,
        "overall_attendance_pct": round((tp + sp) / people * 100, 2) if people else 0,
        "markaz_count": len(tasks),
        "errors": [],
    }


def collect():
    s = make_session()
    token = csrf(s)
    districts = district_options(s)
    date_from = datetime.now().astimezone().strftime("%d/%m/%Y")

    rows = []
    for i, (did, name) in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}] {name}...")
        try:
            rows.append(collect_district(s, did, name, date_from, token))
            print(f"  OK")
        except Exception as e:
            rows.append({
                "district_id": did, "district": name, "status": "ERROR",
                "errors": [str(e)]
            })
            print(f"  ERROR: {e}")

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
