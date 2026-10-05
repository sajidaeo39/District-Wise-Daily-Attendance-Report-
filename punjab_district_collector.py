# -*- coding: utf-8 -*-
"""Fast Punjab district-level SIS attendance collector.

Only district-level attendance is requested from SIS. No Tehsil, Markaz or
school-by-school API loop is used. A district is accepted only when SIS
returns real attendance rows/totals; failed districts are never converted
to zeroes.
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
MAX_WORKERS = 10
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
    retry = Retry(total=3, connect=3, read=3, backoff_factor=.4,
                  status_forcelist=[429,500,502,503,504],
                  allowed_methods=frozenset(["GET"]), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry,
                                    pool_connections=16, pool_maxsize=16))
    s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
    return s

def request(s, path, params=None, html=False):
    r = s.get(BASE + path, params=params, timeout=TIMEOUT, headers={
        "Accept": "text/html,application/json,text/javascript,*/*;q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE + "/dashboard",
    })
    r.raise_for_status()
    if html:
        return r.text
    try:
        return r.json()
    except Exception:
        return r.text

def parse_options(text):
    out = []
    for value, name in re.findall(
        r"<option[^>]*value\s*=\s*['\"]([^'\"]*)['\"][^>]*>\s*(.*?)\s*</option>",
        text or "", re.I | re.S):
        value = clean(value)
        name = clean(unescape(re.sub(r"<[^>]+>", " ", name)))
        if value and name and name.lower() not in {"all","select district","all districts"}:
            out.append((value, name))
    return out

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
    for _, body in blocks:
        opts = parse_options(body)
        names = {n.upper() for _, n in opts}
        if "LAHORE" in names and "OKARA" in names and 20 <= len(opts) <= 60:
            return opts
    raise RuntimeError("Could not isolate SIS district selector")

def parse_rows(raw):
    if isinstance(raw, dict):
        for k in ("data","html","rows","aaData","results","records"):
            if isinstance(raw.get(k), (str,list,dict)):
                raw = raw[k]
                break
    if isinstance(raw, list):
        rows = []
        for x in raw:
            if isinstance(x, dict):
                rows.append(x)
        return rows
    rows = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", str(raw), re.I | re.S):
        cells = [clean(unescape(re.sub(r"<[^>]+>", " ", c)))
                 for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        if cells:
            rows.append(cells)
    return rows

def pick(d, *keys):
    low = {str(k).lower(): v for k,v in d.items()}
    for k in keys:
        if k in d and d[k] not in (None,""):
            return d[k]
        if k.lower() in low and low[k.lower()] not in (None,""):
            return low[k.lower()]
    return None

def aggregate_json_rows(rows):
    total = {"students":0,"present":0,"absent":0,"unmarked":0,"schools":0}
    for row in rows:
        emis = pick(row,"emis","emis_code","emisCode","school_emis_code","s_id_emis_code")
        enrolled = pick(row,"enrolled","enrollment","total_students","students","student_total")
        present = pick(row,"present","students_present","present_count")
        absent = pick(row,"absent","students_absent","absent_count")
        if not re.search(r"\d{8}", str(emis or "")) or all(v is None for v in (enrolled,present,absent)):
            continue
        e,p,a = int(num(enrolled)),int(num(present)),int(num(absent))
        t=max(e,p+a)
        total["students"] += t
        total["present"] += p
        total["absent"] += a
        total["unmarked"] += max(0,t-p-a)
        total["schools"] += 1
    return total

def aggregate_html_rows(rows):
    total = {"students":0,"present":0,"absent":0,"unmarked":0,"schools":0}
    for cells in rows:
        if len(cells) < 6:
            continue
        # Standard SIS school attendance table: EMIS/name, enrolled, present,
        # %, absent, %, not marked, %.
        if not re.search(r"(?<!\d)\d{8}(?!\d)", " ".join(cells)):
            continue
        nums = [num(x) for x in cells[1:]]
        if len(nums) < 4:
            continue
        e,p,a = int(nums[0]),int(nums[1]),int(nums[3] if len(nums)>3 else 0)
        t=max(e,p+a)
        total["students"] += t
        total["present"] += p
        total["absent"] += a
        total["unmarked"] += max(0,t-p-a)
        total["schools"] += 1
    return total

def district_attendance(district_id, date_from):
    s=make_session()
    raw=request(s,"/dashboard/attendance_table",{
        "district_id": district_id,
        "tehsil_id": "",
        "markaz_id": "",
        "date_from": date_from,
        "only_kpztp_districts": "false",
    })
    rows=parse_rows(raw)
    if rows and isinstance(rows[0],dict):
        total=aggregate_json_rows(rows)
    else:
        total=aggregate_html_rows(rows)
    if total["schools"] == 0:
        preview=clean(str(raw))[:250]
        raise RuntimeError(
            f"SIS returned no district attendance records; response={preview}"
        )
    return total

def collect():
    s=make_session()
    districts=district_options(s)
    date_from=datetime.now().astimezone().strftime("%d/%m/%Y")
    rows=[]
    def one(item):
        did,name=item
        x=district_attendance(did,date_from)
        return {
            "district_id":did,"district":name,"status":"OK",
            "total_schools":x["schools"],
            "total_students":x["students"],
            "students_present":x["present"],
            "students_absent":x["absent"],
            "students_unmarked":x["unmarked"],
            "student_attendance_pct":round(x["present"]/x["students"]*100,2) if x["students"] else 0,
            "errors":[]
        }
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures={ex.submit(one,d):d for d in districts}
        for fut in as_completed(futures):
            did,name=futures[fut]
            try:
                rows.append(fut.result())
                print(f"OK: {name}")
            except Exception as e:
                rows.append({"district_id":did,"district":name,"status":"ERROR","errors":[str(e)]})
                print(f"ERROR: {name}: {e}")
    rows.sort(key=lambda x:x["district"])
    complete=sum(x.get("status")=="OK" for x in rows)
    payload={
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "source":BASE,"attendance_date":date_from,"level":"district",
        "district_count":len(rows),"complete_districts":complete,"districts":rows
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    if complete != len(rows) or len(rows) != 40:
        raise RuntimeError(f"Punjab district collection incomplete: {complete}/{len(rows)}")

if __name__=="__main__":
    argparse.ArgumentParser().parse_args()
    collect()
