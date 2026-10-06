# -*- coding: utf-8 -*-
"""Punjab SIS hierarchical daily attendance collector.
Builds District -> Tehsil -> Markaz -> School data from official SIS APIs.
No synthetic attendance values are generated.
"""
import json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
OUT = Path("data/punjab_attendance.json")
TIMEOUT = 30
WORKERS = 12
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"

def clean(x): return re.sub(r"\s+", " ", str(x or "")).strip()
def to_int(x):
    try:
        m=re.sub(r"[^\d-]","",str(x or ""))
        return int(m) if m else 0
    except Exception: return 0

def session():
    s=requests.Session()
    retry=Retry(total=3,connect=3,read=3,backoff_factor=0.8,
                status_forcelist=[429,500,502,503,504],
                allowed_methods=frozenset(["GET"]),raise_on_status=False)
    s.mount("https://",HTTPAdapter(max_retries=retry,pool_connections=12,pool_maxsize=12))
    s.headers.update({"User-Agent":UA,"X-Requested-With":"XMLHttpRequest",
                      "Accept":"application/json, text/javascript, */*;q=0.01"})
    return s

def prime(s):
    try:
        s.get(BASE+"/dashboard",timeout=TIMEOUT,headers={"Referer":BASE+"/"})
        s.get(BASE+"/str/analysis",timeout=TIMEOUT,headers={"Referer":BASE+"/dashboard"})
    except Exception: pass

def get(s,path,params=None):
    h={"Referer":BASE+"/dashboard","X-Requested-With":"XMLHttpRequest",
       "Accept":"application/json, text/javascript, */*;q=0.01"}
    last=None
    for attempt in range(1,4):
        try:
            r=s.get(BASE+path,params=params,timeout=TIMEOUT,headers=h)
            if r.status_code==403:
                prime(s)
                continue
            r.raise_for_status()
            try: return r.json()
            except Exception: return r.text
        except Exception as e:
            last=e
            time.sleep(min(4,attempt))
    raise last or RuntimeError("SIS request failed")

def options(raw):
    if isinstance(raw,dict): raw=raw.get("html") or raw.get("data") or raw.get("options") or ""
    out=[]
    for value,name in re.findall(r"<option[^>]*value\s*=\s*['\"]([^'\"]*)['\"][^>]*>\s*(.*?)\s*</option>",
                                 str(raw or ""),re.I|re.S):
        value=clean(value); name=clean(unescape(re.sub(r"<[^>]+>"," ",name)))
        bad={"all","select district","select tehsil","select markaz","select school",
             "all districts","all tehsils","all markazs","all schools"}
        if value and name and name.lower() not in bad: out.append((value,name))
    return out

def children(s,path,params): return options(get(s,path,params))

def csrf(s):
    prime(s)
    return s.cookies.get("csrf_cookie_name","")

def inventory(ds):
    # Mapping is the slowest part of SIS collection. District/tehsil discovery is
    # kept bounded, while Markaz -> School calls run in parallel with independent
    # sessions so one stale CSRF/session cannot block the whole inventory.
    def district_map(item):
        did,dname=item
        s=session(); prime(s); token=csrf(s)
        ts=children(s,"/user/get_tehsils",{"district":did,"selectedTehsil":"false","all":"All","csrf_test_name":token})
        if not ts:
            raise RuntimeError(f"No tehsils returned for {dname}")
        markaz_jobs=[]
        for tid,tname in ts:
            ms=children(s,"/user/get_markazes",{"tehsil":tid,"selectedMarkaz":"false","all":"All","csrf_test_name":token})
            if not ms:
                print(f"WARNING: no markaz returned for {dname}/{tname}; skipping",flush=True)
                continue
            for mid,mname in ms:
                markaz_jobs.append((did,dname,tid,tname,mid,mname))
        return markaz_jobs

    all_markazes=[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures={ex.submit(district_map,d):d for d in ds}
        for i,f in enumerate(as_completed(futures),1):
            d=futures[f]
            try:
                jobs=f.result()
                all_markazes.extend(jobs)
                print(f"Mapping districts {i}/{len(ds)}: {d[1]} -> {len(jobs)} Markazs",flush=True)
            except Exception as e:
                raise RuntimeError(f"Mapping failed for district {d[1]}: {e}") from e

    print(f"Discovered {len(all_markazes)} Markazs; fetching school lists in parallel...",flush=True)

    def markaz_schools(job):
        did,dname,tid,tname,mid,mname=job
        s=session(); prime(s); token=csrf(s)
        p={"markaz":mid,"selectedSchool":"false","all":"All","csrf_test_name":token}
        ss=[]
        last=None
        for attempt in range(3):
            try:
                ss=children(s,"/user/get_schools",p)
                if ss: break
            except Exception as e:
                last=e
            time.sleep(0.8*(attempt+1))
            prime(s); token=csrf(s); p["csrf_test_name"]=token
        if not ss:
            print(f"WARNING: no schools returned for {dname}/{tname}/{mname} (markaz {mid})",flush=True)
            return []
        out=[]
        for sid,sname in ss:
            m=re.search(r"(?<!\\d)(\\d{8})(?!\\d)",sname)
            emis=m.group(1) if m else (sid if re.fullmatch(r"\\d{8}",str(sid)) else "")
            if emis:
                out.append({"district_id":str(did),"district":clean(dname),
                            "tehsil_id":str(tid),"tehsil":clean(tname),
                            "markaz_id":str(mid),"markaz":clean(mname),
                            "school_id":str(sid),"emis":emis,"school":clean(sname)})
        return out

    schools=[]
    completed=0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures={ex.submit(markaz_schools,j):j for j in all_markazes}
        for f in as_completed(futures):
            completed += 1
            try:
                schools.extend(f.result())
            except Exception as e:
                j=futures[f]
                print(f"WARNING: Markaz {j[4]} school mapping failed: {e}",flush=True)
            if completed % 100 == 0 or completed == len(all_markazes):
                print(f"School mapping {completed}/{len(all_markazes)} Markazs",flush=True)

    uniq={}
    for x in schools:
        uniq[x["emis"]]=x
    return list(uniq.values())

def attendance_worker():
    s=session(); prime(s)
    return s

def school_attendance(s, sess):
    p={"district":s["district_id"],"tehsil":s["tehsil_id"],"markaz":s["markaz_id"],
       "school":s["school_id"],"s_id_emis_code":s["emis"],"ony_kpztp_districts":"false"}
    a=get(sess,"/attendance/get_today_attendance_stats",p)
    if not isinstance(a,dict) or not ("present_count" in a or "marked_count" in a):
        raise RuntimeError("unexpected student attendance response")
    present=to_int(a.get("present_count")); absent=to_int(a.get("absent_count")); marked=to_int(a.get("marked_count"))
    e=get(sess,"/dashboard_revamp/get_gender_summary_pie",p)
    total=to_int(e.get("total")) if isinstance(e,dict) else 0
    if total<=0: total=max(marked,present+absent)
    if present+absent>total: raise RuntimeError("attendance exceeds enrolled total")
    teacher=None
    try:
        t=get(sess,"/attendance/get_teachers_today_attendance_stats",p)
        if isinstance(t,dict) and ("present_count" in t or "marked_count" in t):
            tp=to_int(t.get("present_count")); ta=to_int(t.get("absent_count")); tm=to_int(t.get("marked_count"))
            teacher={"present":tp,"absent":ta,"marked":tm}
    except Exception:
        teacher=None
    return {"total_students":total,"students_present":present,"students_absent":absent,
            "students_unmarked":max(0,total-present-absent),
            "teachers":teacher,"attendance_status":"OK"}

def collect():
    root=session(); ds=options(get(root,"/user/get_districts"))
    if len(ds)<20: raise RuntimeError("Punjab district list incomplete")
    print(f"Found {len(ds)} districts",flush=True)
    schools=inventory(ds)
    print(f"Discovered {len(schools)} schools",flush=True)
    if len(schools)<1000: raise RuntimeError(f"SIS inventory incomplete: {len(schools)} schools")
    records=[]; failed=[]
    def one(s):
        # Each task owns one persistent SIS session; low concurrency prevents API throttling.
        sess=attendance_worker()
        last=None
        for attempt in range(3):
            try: return s,school_attendance(s,sess)
            except Exception as e:
                last=e; time.sleep(2*(attempt+1)); prime(sess)
        raise last or RuntimeError("attendance failed")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures={ex.submit(one,s):s for s in schools}
        for i,f in enumerate(as_completed(futures),1):
            s=futures[f]
            try:
                info=f.result(); records.append(dict(s,**info[1]))
            except Exception as e:
                failed.append({"emis":s["emis"],"district":s["district"],"tehsil":s["tehsil"],
                               "markaz":s["markaz"],"school":s["school"],"error":str(e)})
            if i%250==0: print(f"Attendance {i}/{len(schools)}",flush=True)
    print(f"Successful attendance: {len(records)}/{len(schools)}",flush=True)
    print(f"Failed attendance: {len(failed)}",flush=True)
    if len(records)==0: raise RuntimeError("No SIS attendance was collected")
    # Build hierarchy from successful actual SIS records.
    D={}
    for r in records:
        d=D.setdefault(r["district"],{"district_id":r["district_id"],"district":r["district"],"tehsils":{}})
        t=d["tehsils"].setdefault(r["tehsil"],{"tehsil_id":r["tehsil_id"],"tehsil":r["tehsil"],"markazs":{}})
        m=t["markazs"].setdefault(r["markaz"],{"markaz_id":r["markaz_id"],"markaz":r["markaz"],"schools":[]})
        m["schools"].append(r)
    def aggregate(rows):
        ss=sum(r["total_students"] for r in rows); sp=sum(r["students_present"] for r in rows); sa=sum(r["students_absent"] for r in rows)
        tp=sum((r["teachers"] or {}).get("present",0) for r in rows); ta=sum((r["teachers"] or {}).get("absent",0) for r in rows); tm=sum((r["teachers"] or {}).get("marked",0) for r in rows)
        # Teacher total is marked + unmarked only when marked data exists; otherwise remains unavailable.
        return {"total_schools":len(rows),"total_students":ss,"students_present":sp,"students_absent":sa,
                "students_unmarked":max(0,ss-sp-sa),
                "student_attendance_pct":round(sp/ss*100,2) if ss else 0,
                "teachers_present":tp,"teachers_absent":ta,"teachers_marked":tm,
                "teacher_attendance_pct":round(tp/(tp+ta)*100,2) if tp+ta else None}
    districts_out=[]
    for d in D.values():
        trs=[]
        for t in d["tehsils"].values():
            mrs=[]
            for m in t["markazs"].values():
                m["summary"]=aggregate(m["schools"]); mrs.append(m)
            t["markazs"]=sorted(mrs,key=lambda x:x["markaz"]); trs.extend(t["markazs"])
            t["summary"]=aggregate([r for m in mrs for r in m["schools"]])
        d["tehsils"]=sorted(d["tehsils"].values(),key=lambda x:x["tehsil"])
        d["summary"]=aggregate([r for t in d["tehsils"] for m in t["markazs"] for r in m["schools"]])
        districts_out.append(d)
    payload={"generated_at":datetime.now(timezone.utc).isoformat(),
             "source":BASE,"attendance_date":datetime.now().astimezone().strftime("%d/%m/%Y"),
             "level":"district_tehsil_markaz_school","district_count":len(districts_out),
             "inventory_schools":len(schools),"successful_schools":len(records),
             "failed_schools":len(failed),"complete":len(failed)==0,
             "districts":sorted(districts_out,key=lambda x:x["district"]),
             "failed_school_requests":failed[:5000]}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    if len(districts_out)<40: raise RuntimeError(f"Only {len(districts_out)} districts have SIS attendance")
    # Do not publish fake/zero records. A run may publish real partial data, but is flagged incomplete.
    if len(records) < len(schools)*0.95:
        raise RuntimeError(f"Attendance collection incomplete: {len(records)}/{len(schools)} successful; {len(failed)} failed")

if __name__=="__main__":
    collect()
