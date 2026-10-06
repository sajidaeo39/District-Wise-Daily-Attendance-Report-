# -*- coding: utf-8 -*-
"""Punjab district daily student attendance from the official SIS attendance APIs."""
import argparse, json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
OUT = Path("data/punjab_district_attendance.json")
TIMEOUT = 30
WORKERS = 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"

def clean(x): return re.sub(r"\s+", " ", str(x or "")).strip()
def to_int(x):
    try:
        m = re.sub(r"[^\d-]", "", str(x or ""))
        return int(m) if m else 0
    except Exception:
        return 0

def session():
    s = requests.Session()
    retry = Retry(total=4, connect=4, read=4, backoff_factor=.5,
                  status_forcelist=[429,500,502,503,504],
                  allowed_methods=frozenset(["GET"]), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32))
    s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                      "Accept": "application/json, text/javascript, */*;q=0.01"})
    return s

def get(s, path, params=None):
    r = s.get(BASE + path, params=params, timeout=TIMEOUT,
              headers={"Referer": BASE + "/dashboard",
                       "X-Requested-With": "XMLHttpRequest",
                       "Accept": "application/json, text/javascript, */*;q=0.01"})
    if r.status_code == 403:
        try:
            s.get(BASE + "/dashboard", timeout=TIMEOUT, headers={"Referer": BASE + "/"})
            s.get(BASE + "/str/analysis", timeout=TIMEOUT, headers={"Referer": BASE + "/dashboard"})
        except Exception:
            pass
        r = s.get(BASE + path, params=params, timeout=TIMEOUT,
                  headers={"Referer": BASE + "/dashboard",
                           "X-Requested-With": "XMLHttpRequest",
                           "Accept": "application/json, text/javascript, */*;q=0.01"})
    r.raise_for_status()
    try: return r.json()
    except Exception: return r.text

def options(raw):
    if isinstance(raw, dict):
        raw = raw.get("html") or raw.get("data") or raw.get("options") or ""
    out=[]
    for value,name in re.findall(r"<option[^>]*value\s*=\s*['\"]([^'\"]*)['\"][^>]*>\s*(.*?)\s*</option>",
                                 str(raw or ""), re.I|re.S):
        value=clean(value); name=clean(unescape(re.sub(r"<[^>]+>"," ",name)))
        if value and name and name.lower() not in {"all","select district","select tehsil","select markaz","select school","all districts","all tehsils","all markazs","all schools"}:
            out.append((value,name))
    return out

def csrf(s):
    try:
        r=s.get(BASE+"/str/analysis",timeout=TIMEOUT,headers={"User-Agent":UA})
        token=s.cookies.get("csrf_cookie_name","")
        if token: return token
        m=re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)',r.text,re.I)
        return m.group(1) if m else ""
    except Exception:
        return ""

def districts(s):
    raw=get(s,"/user/get_districts")
    opts=options(raw)
    if len(opts)>=20: return opts
    raise RuntimeError("SIS /user/get_districts did not return the Punjab district list")

def children(s, path, params):
    return options(get(s,path,params))

def school_inventory(districts_list, token):
    # Keep one SIS session for the complete hierarchy. The CSRF cookie/token
    # pair is session-bound; mixing sessions causes HTTP 403 on get_schools.
    s = session()
    try:
        s.get(BASE + "/dashboard", timeout=TIMEOUT, headers={"Referer": BASE + "/"})
        s.get(BASE + "/str/analysis", timeout=TIMEOUT, headers={"Referer": BASE + "/dashboard"})
    except Exception:
        pass
    token = csrf(s) or token
    schools=[]
    for di,(did,dname) in enumerate(districts_list,1):
        print(f"[{di}/{len(districts_list)}] Mapping {dname}...",flush=True)
        ts=children(s,"/user/get_tehsils",{"district":did,"selectedTehsil":"false","all":"All","csrf_test_name":token})
        if not ts: raise RuntimeError(f"No tehsils returned for {dname}")
        for tid,tname in ts:
            ms=children(s,"/user/get_markazes",{"tehsil":tid,"selectedMarkaz":"false","all":"All","csrf_test_name":token})
            if not ms: raise RuntimeError(f"No markaz returned for {dname}/{tname}")
            for mid,mname in ms:
                ss=children(s,"/user/get_schools",{"markaz":mid,"selectedSchool":"false","all":"All","csrf_test_name":token})
                if not ss: raise RuntimeError(f"No schools returned for {dname}/{tname}/{mname}")
                for sid,sname in ss:
                    m=re.search(r"(?<!\d)(\d{8})(?!\d)",sname)
                    emis=m.group(1) if m else (sid if re.fullmatch(r"\d{8}",str(sid)) else "")
                    if not emis: continue
                    schools.append({"district_id":did,"district":dname,"tehsil_id":tid,
                                    "markaz_id":mid,"school_id":sid,"emis":emis,
                                    "school":clean(sname)})
    return schools

def school_attendance(s):
    local=session()
    params={"district":s["district_id"],"tehsil":s["tehsil_id"],"markaz":s["markaz_id"],
            "school":s["school_id"],"s_id_emis_code":s["emis"],"ony_kpztp_districts":"false"}
    a=get(local,"/attendance/get_today_attendance_stats",params)
    if not isinstance(a,dict) or not ("present_count" in a or "marked_count" in a):
        raise RuntimeError("SIS attendance API returned unexpected response")
    present=to_int(a.get("present_count")); absent=to_int(a.get("absent_count"))
    marked=to_int(a.get("marked_count"))
    # marked_count is the number of students whose attendance was marked.
    # Get the enrolled denominator from SIS's official school summary.
    e=get(local,"/dashboard_revamp/get_gender_summary_pie",params)
    total=to_int(e.get("total")) if isinstance(e,dict) else 0
    if total <= 0: total=max(marked,present+absent)
    if present+absent > total:
        raise RuntimeError(f"Attendance exceeds enrolled total: {present}+{absent}>{total}")
    return total,present,absent,max(0,total-present-absent)

def collect():
    root=session()
    ds=districts(root)
    token=csrf(root)
    print(f"Found {len(ds)} districts",flush=True)
    schools=school_inventory(ds,token)
    print(f"Discovered {len(schools)} schools",flush=True)
    totals={d[0]:{"district_id":d[0],"district":d[1],"schools":0,"students":0,"present":0,"absent":0,"unmarked":0,"errors":[]} for d in ds}
    failed=[]
    def one(s):
        return s, school_attendance(s)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures={ex.submit(one,s):s for s in schools}
        for i,f in enumerate(as_completed(futures),1):
            s=futures[f]
            try:
                total,p,a,u=f.result()
                x=totals[s["district_id"]]
                x["schools"]+=1; x["students"]+=total; x["present"]+=p; x["absent"]+=a; x["unmarked"]+=u
            except Exception as e:
                failed.append((s,str(e)))
            if i%250==0: print(f"Attendance {i}/{len(schools)}",flush=True)
    if failed:
        print(f"FAILED SCHOOL REQUESTS: {len(failed)}",flush=True)
    rows=[]
    for did,dname in ds:
        x=totals[did]
        if x["schools"]==0:
            raise RuntimeError(f"No attendance collected for district {dname}")
        rows.append({"district_id":did,"district":dname,"status":"OK",
                     "total_schools":x["schools"],"total_students":x["students"],
                     "students_present":x["present"],"students_absent":x["absent"],
                     "students_unmarked":x["unmarked"],
                     "student_attendance_pct":round(x["present"]/x["students"]*100,2) if x["students"] else 0,
                     "errors":[]})
    payload={"generated_at":datetime.now(timezone.utc).isoformat(),"source":BASE,
             "attendance_date":datetime.now().astimezone().strftime("%d/%m/%Y"),
             "level":"district","attendance_type":"student","district_count":len(rows),
             "complete_districts":len(rows),"districts":sorted(rows,key=lambda x:x["district"])}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    if len(rows)!=40: raise RuntimeError(f"Expected 40 Punjab districts, got {len(rows)}")
    if failed: raise RuntimeError(f"Attendance collection incomplete: {len(failed)} school requests failed")

if __name__=="__main__":
    argparse.ArgumentParser().parse_args()
    collect()
