# -*- coding: utf-8 -*-
"""Punjab SIS district attendance collector.

Uses the SIS endpoints already confirmed by the working Okara collector:
  /user/get_markazes
  /user/get_schools
  /attendance/get_teachers_today_attendance_stats
  /dashboard/attendance_table
  /dashboard/sanctioned_posts_tab

Output is aggregated to district rows only.
"""
import argparse, json, re, time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
OUT = Path("data/punjab_district_attendance.json")
TIMEOUT = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151 Safari/537.36")


def clean(x): return re.sub(r"\s+", " ", str(x or "")).strip()

def num(x):
    s = re.sub(r"[^\d.-]", "", str(x or ""))
    try: return float(s) if s else 0.0
    except Exception: return 0.0

def session():
    s = requests.Session()
    retry = Retry(total=3, connect=3, read=3, backoff_factor=.5,
                  status_forcelist=[429,500,502,503,504],
                  allowed_methods=frozenset(["GET","POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
    s.headers.update({"User-Agent": UA, "X-Requested-With":"XMLHttpRequest"})
    return s

def parse_options(text):
    found=[]
    for value,name in re.findall(r"<option[^>]*value\s*=\s*['\"]([^'\"]*)['\"][^>]*>\s*(.*?)\s*</option>", text or "", re.I|re.S):
        name=clean(re.sub(r"<[^>]+>"," ",unescape(name)))
        value=clean(value)
        if value and name and name.lower() not in {"all","select district","all districts","all tehsils","all schools"}:
            found.append((value,name))
    return found

def parse_rows(html):
    if isinstance(html,dict): html=html.get("data") or html.get("html") or ""
    rows=[]
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>",str(html),re.I|re.S):
        cells=[clean(unescape(re.sub(r"<[^>]+>"," ",c))) for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>",tr,re.I|re.S)]
        if cells: rows.append(cells)
    return rows

def request(s,path,params=None,html=False):
    r=s.get(BASE+path,params=params,timeout=TIMEOUT,headers={
        "Accept":"text/html,application/json,text/javascript,*/*;q=0.01",
        "X-Requested-With":"XMLHttpRequest","Referer":BASE+"/dashboard"})
    r.raise_for_status()
    if html: return r.text
    try: return r.json()
    except Exception: return r.text

def csrf(s):
    r=s.get(BASE+"/dashboard",timeout=TIMEOUT,headers={"Accept":"text/html,*/*","User-Agent":UA})
    r.raise_for_status()
    m=re.search(r'name=["\']csrf_test_name["\'][^>]*value=["\']([^"\']+)',r.text,re.I)
    return m.group(1) if m else s.cookies.get("csrf_cookie_name","")

def district_options(s):
    html=request(s,"/dashboard",html=True)
    opts=parse_options(html)
    # Prefer options whose names look like Punjab districts; the public page
    # contains multiple selectors, so de-duplicate by id/name.
    uniq=[]; seen=set()
    for x in opts:
        if x not in seen: uniq.append(x); seen.add(x)
    if len(uniq)>=20: return uniq
    raise RuntimeError(f"Could not discover district selector from SIS dashboard; found {len(uniq)} options")

def markazes(s,tehsil_id,token):
    data=request(s,"/user/get_markazes",{
        "tehsil":tehsil_id,"selectedMarkaz":"false","all":"All","csrf_test_name":token})
    html=data.get("html","") if isinstance(data,dict) else str(data)
    opts=parse_options(html)
    if opts: return opts
    raise RuntimeError(f"No Markaz records for tehsil {tehsil_id}")

def schools(s,markaz_id,token):
    data=request(s,"/user/get_schools",{
        "markaz":markaz_id,"selectedSchool":"false","all":"All","csrf_test_name":token})
    html=data.get("html","") if isinstance(data,dict) else str(data)
    return parse_options(html)

def tehsils(s,district_id,token):
    # This endpoint is used by SIS's district->tehsil selector. Try common
    # parameter names because deployments have differed historically.
    candidates=[
        ("/user/get_tehsils",{"district":district_id,"selectedTehsil":"false","all":"All","csrf_test_name":token}),
        ("/user/get_tehsils",{"district_id":district_id,"selectedTehsil":"false","all":"All","csrf_test_name":token}),
        ("/user/get_tehsils",{"district":district_id,"all":"All","csrf_test_name":token}),
    ]
    for path,p in candidates:
        try:
            data=request(s,path,p)
            html=data.get("html","") if isinstance(data,dict) else str(data)
            opts=parse_options(html)
            if opts: return opts
        except Exception:
            pass
    raise RuntimeError(f"No tehsil records for district {district_id}")

def attendance_students(s,district_id,tehsil_id,markaz_id,date_from):
    html=request(s,"/dashboard/attendance_table",{
        "district_id":district_id,"tehsil_id":tehsil_id,"markaz_id":markaz_id,
        "date_from":date_from,"only_kpztp_districts":"false"},html=True)
    # Expected markaz row format follows the same columns as school rows:
    # name | enrolled | present | % | absent | % | not marked | %
    total=[0,0,0,0]
    for c in parse_rows(html):
        if len(c)<8: continue
        joined=" ".join(c).lower()
        if "emis" in joined or re.search(r"\b\d{8}\b",joined):
            continue
        vals=[num(c[2]),num(c[3]),num(c[5]),num(c[7])]
        # c[2] is enrolled for the standard SIS row; tolerate a leading # column.
        if len(c)>=9:
            vals=[num(c[2]),num(c[3]),num(c[5]),num(c[7])]
        enrolled,present,absent,unmarked=vals
        if enrolled >= present+absent:
            total=[total[i]+vals[i] for i in range(4)]
    return total

def teacher_stats(s,district_id,tehsil_id,markaz_id):
    data=request(s,"/attendance/get_teachers_today_attendance_stats",{
        "district":district_id,"tehsil":tehsil_id,"markaz":markaz_id,"school":"",
        "s_id_emis_code":"","ony_kpztp_districts":"false"})
    if not isinstance(data,dict): raise RuntimeError("Teacher endpoint did not return JSON")
    return int(num(data.get("present_count"))), int(num(data.get("absent_count")))

def filled_teachers(s,district_id,tehsil_id,markaz_id):
    data=request(s,"/dashboard/sanctioned_posts_tab",{
        "district_id":district_id,"tehsil_id":tehsil_id,"markaz_id":markaz_id,
        "school_id":"","s_id_emis_code":""})
    html=data.get("data") if isinstance(data,dict) else data
    for row in parse_rows(html or ""):
        joined=" ".join(row).lower()
        if row and (joined.startswith("total") or "overall" in joined):
            nums=[num(c) for c in row[1:] if re.search(r"\d",c)]
            if len(nums)>=3: return int(max(0,nums[1]))
    raise RuntimeError("Filled staff total not found")

def collect_markaz(s,district_id,tehsil_id,markaz_id,date_from,token):
    school_rows=schools(s,markaz_id,token)
    sp,sa=teacher_stats(s,district_id,tehsil_id,markaz_id)
    tt=filled_teachers(s,district_id,tehsil_id,markaz_id)
    se,stp,sta,su=attendance_students(s,district_id,tehsil_id,markaz_id,date_from)
    # A markaz record itself is a group of schools. We count school rows from
    # the same attendance table only when present; otherwise leave None rather
    # than invent a count.
    return {"school_count":len(school_rows),"teacher_present":sp,"teacher_absent":sa,"total_teachers":tt,
            "student_enrolled":int(se),"student_present":int(stp),
            "student_absent":int(sta),"student_unmarked":int(su)}

def district_attendance_direct(s,district_id,date_from):
    """Fetch SIS attendance once at district scope; never traverse tehsil/markaz."""
    html=request(s,"/dashboard/attendance_table",{
        "district_id":district_id,"tehsil_id":"","markaz_id":"",
        "date_from":date_from,"only_kpztp_districts":"false"},html=True)
    rows=parse_rows(html)
    vals=[]
    for c in rows:
        if len(c)<8: continue
        joined=" ".join(c).lower()
        if "emis" in joined or re.search(r"\b\d{8}\b",joined): continue
        try:
            enrolled,present,absent,unmarked=map(num,(c[2],c[3],c[5],c[7]))
        except Exception:
            continue
        if enrolled>=present+absent and enrolled>0:
            vals.append((enrolled,present,absent,unmarked))
    if not vals:
        raise RuntimeError(f"SIS returned no district-level attendance row for district {district_id}")
    return {
        "total_students":int(sum(x[0] for x in vals)),
        "students_present":int(sum(x[1] for x in vals)),
        "students_absent":int(sum(x[2] for x in vals)),
        "students_unmarked":int(sum(x[3] for x in vals)),
        "school_rows":len(vals)
    }

def district_teacher_direct(s,district_id):
    """Fetch teacher attendance directly for the district."""
    data=request(s,"/attendance/get_teachers_today_attendance_stats",{
        "district":district_id,"tehsil":"","markaz":"","school":"",
        "s_id_emis_code":"","ony_kpztp_districts":"false"})
    if not isinstance(data,dict):
        raise RuntimeError(f"SIS teacher district response was not JSON for {district_id}")
    present=int(num(data.get("present_count")))
    absent=int(num(data.get("absent_count")))
    total=int(num(data.get("total_count") or data.get("total_teachers") or present+absent))
    if total<=0: total=present+absent
    if total<=0 or present+absent>total:
        raise RuntimeError(f"SIS teacher district response invalid for {district_id}")
    return total,present,absent

def collect_district(s,did,dname,date_from):
    out={"district_id":did,"district":dname,"status":"ERROR","errors":[]}
    try:
        a=district_attendance_direct(s,did,date_from)
        tt,tp,ta=district_teacher_direct(s,did)
        out.update({
            "total_schools":a["school_rows"] if a["school_rows"]>1 else None,
            "total_teachers":tt,"teachers_present":tp,"teachers_absent":ta,
            "teacher_attendance_pct":round(tp/tt*100,2),
            "total_students":a["total_students"],
            "students_present":a["students_present"],
            "students_absent":a["students_absent"],
            "students_unmarked":a["students_unmarked"],
            "student_attendance_pct":round(a["students_present"]/a["total_students"]*100,2) if a["total_students"] else None,
            "overall_attendance_pct":round((tp+a["students_present"])/(tt+a["total_students"])*100,2) if (tt+a["total_students"]) else None,
            "status":"OK"
        })
    except Exception as e:
        out["errors"]=[str(e)]
    return out

def collect():
    s=session()
    districts=district_options(s)
    date_from=datetime.now().strftime("%d/%m/%Y")
    out=[]
    for did,dname in districts:
        rec=collect_district(s,did,dname,date_from)
        out.append(rec)
        print(dname,rec["status"])
    complete=sum(x.get("status")=="OK" for x in out)
    payload={"generated_at":datetime.now(timezone.utc).isoformat(),
             "source":BASE+"/dashboard","attendance_date":date_from,
             "level":"district","district_count":len(out),
             "complete_districts":complete,"errors":[],
             "districts":out}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    if len(out)<20 or complete<len(out):
        raise RuntimeError(f"Punjab district collection incomplete: {complete}/{len(out)} complete")

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--date",default=None); args=ap.parse_args(); collect()
