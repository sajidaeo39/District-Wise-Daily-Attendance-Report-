# -*- coding: utf-8 -*-
"""Punjab SIS hierarchical daily attendance collector.
Builds District -> Tehsil -> Markaz -> School data from official SIS APIs.
No synthetic attendance values are generated.
"""
import json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import local
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
OUT = Path("data/punjab_attendance.json")
TIMEOUT = 30
WORKERS = 35  # Parallel SIS requests; keep below aggressive levels to avoid throttling
THREAD_SESSIONS = local()
DISTRICT_SIS = {}
MAPPING_FAILURES = []
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
    # SIS endpoints may wrap their HTML in several JSON response layers.
    for _ in range(4):
        if not isinstance(raw, dict):
            break
        nxt = raw.get("html") or raw.get("data") or raw.get("options") or raw.get("response")
        if nxt is None or nxt is raw:
            raw = ""
            break
        raw = nxt
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
    # Match the working Okara live-attendance collector: SIS validates the
    # form token embedded in /dashboard against the session CSRF cookie.
    # Returning only the cookie value can make every dependent dropdown empty.
    try:
        r = s.get(
            BASE + "/dashboard",
            timeout=max(TIMEOUT, 30),
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": BASE + "/",
                "User-Agent": UA,
            },
        )
        r.raise_for_status()
        m = re.search(
            r"""name=["']csrf_test_name["'][^>]*value=["']([^"']+)""",
            r.text, re.I
        )
        if m:
            return m.group(1)
    except Exception as e:
        print(f"WARNING: could not read SIS dashboard CSRF token: {e}", flush=True)
    return s.cookies.get("csrf_cookie_name", "")

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
        # A failed SIS request must never be mislabelled as a zero-school Markaz.
        s=getattr(THREAD_SESSIONS, 'inventory_session', None)
        if s is None:
            s=session(); prime(s); THREAD_SESSIONS.inventory_session=s
        token=csrf(s)
        p={"markaz":mid,"selectedSchool":"false","all":"All","csrf_test_name":token}
        ss=[]; last=None; confirmed_empty=False
        for attempt in range(3):
            try:
                raw=get(s,"/user/get_schools",p)
                ss=options(raw)
                raw_text=""
                if isinstance(raw,dict):
                    raw_text=" ".join(str(raw.get(k,"")) for k in ("html","data","options","response"))
                elif isinstance(raw,(str,list)):
                    raw_text=str(raw)
                if ss:
                    break
                if isinstance(raw,list) and not raw:
                    confirmed_empty=True
                    break
                if re.search(r"<option\\b",raw_text,re.I):
                    # SIS returned a valid dropdown containing only its placeholder.
                    confirmed_empty=True
                    break
                last=RuntimeError("SIS returned no usable school options; cannot confirm zero-school Markaz")
            except Exception as e:
                last=e
            if attempt<2:
                time.sleep(0.8*(attempt+1))
                prime(s); token=csrf(s); p["csrf_test_name"]=token
        if not ss:
            if confirmed_empty:
                print(f"SKIP CONFIRMED ZERO-SCHOOL MARKAZ: {dname}/{tname}/{mname} (markaz {mid})",flush=True)
                return []
            raise RuntimeError(f"School list unavailable for {dname}/{tname}/{mname} (Markaz {mid}): {last}")
        out=[]
        for sid,sname in ss:
            m=re.search(r"(?<!\d)(\d{8})(?!\d)",sname)
            emis=m.group(1) if m else (sid if re.fullmatch(r"\d{8}",str(sid)) else "")
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
                MAPPING_FAILURES.append({"district":j[1],"tehsil":j[3],"markaz":j[5],"markaz_id":str(j[4]),"error":str(e)})
                print(f"WARNING: Markaz {j[4]} school mapping failed (not treated as zero-school): {e}",flush=True)
            if completed % 100 == 0 or completed == len(all_markazes):
                print(f"School mapping {completed}/{len(all_markazes)} Markazs",flush=True)

    uniq={}
    for x in schools:
        uniq[x["emis"]]=x
    return list(uniq.values())

def attendance_worker():
    # Keep a warm authenticated SIS session per worker thread. Previously every
    # school created and primed a new session, adding two extra requests per school.
    s=getattr(THREAD_SESSIONS, 'attendance_session', None)
    if s is None:
        s=session(); prime(s); THREAD_SESSIONS.attendance_session=s
    return s

def filled_staff_total(s, sess):
    """Read SIS sanctioned-posts table; use Filled staff as total working teachers."""
    data = get(sess, "/dashboard/sanctioned_posts_tab", {
        "district_id": str(s["district_id"]), "tehsil_id": str(s["tehsil_id"]),
        "markaz_id": str(s["markaz_id"]), "school_id": str(s["school_id"]),
        "s_id_emis_code": str(s["emis"]),
    })
    html = data.get("data") if isinstance(data, dict) else data
    html = html or ""
    if not isinstance(html, str):
        html = str(html)
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        cells = [clean(unescape(re.sub(r"<[^>]+>", " ", cell)))
                 for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)]
        joined = " ".join(cells).strip().lower()
        if cells and (joined.startswith("total") or "overall" in joined):
            nums = [to_int(cell) for cell in cells[1:] if re.search(r"\d", cell)]
            if len(nums) >= 3:
                return max(0, nums[1])  # Total, Filled, Vacant
    raise RuntimeError("Filled staff total not found in sanctioned-posts response")


def normalize_stats(raw):
    """Preserve SIS present/absent/unmarked counts instead of inferring unmarked."""
    if not isinstance(raw, dict) or not any(k in raw for k in ("present_count","marked_count","unmarked_count")):
        raise RuntimeError("unexpected SIS attendance stats response")
    present=to_int(raw.get("present_count"))
    absent=to_int(raw.get("absent_count"))
    marked=to_int(raw.get("marked_count", present+absent))
    unmarked=to_int(raw.get("unmarked_count")) if "unmarked_count" in raw else 0
    total=present+absent+unmarked if "unmarked_count" in raw else max(marked,present+absent)
    if "unmarked_count" not in raw:
        unmarked=max(0,total-present-absent)
    return {"present":present,"absent":absent,"marked":marked,"unmarked":unmarked,
            "total":total,"unmarked_reported":"unmarked_count" in raw,
            "progress_pct":round(100*present/total,2) if total else None,
            "raw_present_pct":raw.get("present_percentage",raw.get("present_percent")),
            "raw_absent_pct":raw.get("absent_percentage",raw.get("absent_percent"))}

def school_attendance(s, sess):
    p={"district":s["district_id"],"tehsil":s["tehsil_id"],"markaz":s["markaz_id"],
       "school":s["school_id"],"s_id_emis_code":s["emis"],"ony_kpztp_districts":"false"}
    a=get(sess,"/attendance/get_today_attendance_stats",p)
    student=normalize_stats(a)
    # Keep SIS's unmarked count. The gender/enrolment endpoint is retained only
    # as a cross-check, never as a replacement for attendance endpoint counts.
    try:
        e=get(sess,"/dashboard_revamp/get_gender_summary_pie",p)
        enrolment=to_int(e.get("total")) if isinstance(e,dict) else 0
    except Exception:
        enrolment=0
    teacher=None
    try:
        t=get(sess,"/attendance/get_teachers_today_attendance_stats",p)
        teacher=normalize_stats(t)
        # If SIS omits an explicit unmarked_count, use the SIS Filled staff
        # count to calculate the missing staff count rather than silently showing 0.
        if not teacher["unmarked_reported"]:
            try:
                staff_total=filled_staff_total(s, sess)
                if staff_total >= teacher["present"]+teacher["absent"]:
                    teacher["total"]=staff_total
                    teacher["unmarked"]=max(0,staff_total-teacher["present"]-teacher["absent"])
                    teacher["progress_pct"]=round(100*teacher["present"]/staff_total,2) if staff_total else None
                    teacher["total_source"]="SIS sanctioned posts Filled"
            except Exception as staff_error:
                print(f"WARNING: cannot verify unmarked staff total for {s.get('emis')}: {staff_error}",flush=True)
    except Exception as e:
        print(f"Teacher stats incomplete for {s.get('emis')}: {e}", flush=True)
    return {"total_students":student["total"],"enrolment_students":enrolment,
            "students_present":student["present"],"students_absent":student["absent"],
            "students_marked":student["marked"],"students_unmarked":student["unmarked"],
            "student_progress_pct":student["progress_pct"],
            "teachers":teacher,"attendance_status":"OK"}

def save_snapshot(ds, schools, records, failed, complete=False):
    """Publish SIS hierarchy with directly fetched district-level attendance as authority."""
    D={clean(name):{"district_id":str(did),"district":clean(name),"tehsils":{}} for did,name in ds}
    # District aggregate endpoint is what SIS district summary displays. It is kept
    # separate from the sum of school rows so incomplete school collection cannot
    # silently alter the district's official totals.
    global DISTRICT_SIS
    if not DISTRICT_SIS:
        for did,dname in ds:
            try:
                s=session(); prime(s); token=csrf(s)
                params={"district":str(did),"tehsil":"","markaz":"","school":"","s_id_emis_code":"",
                        "ony_kpztp_districts":"false","csrf_test_name":token}
                st=normalize_stats(get(s,"/attendance/get_today_attendance_stats",params))
                tt=normalize_stats(get(s,"/attendance/get_teachers_today_attendance_stats",params))
                DISTRICT_SIS[clean(dname)]={"students":st,"teachers":tt,"district_id":str(did)}
            except Exception as e:
                print(f"WARNING: direct SIS district summary unavailable for {dname}: {e}",flush=True)
    district_rows=DISTRICT_SIS
    for r in records:
        d=D.setdefault(r["district"],{"district_id":r["district_id"],"district":r["district"],"tehsils":{}})
        t=d["tehsils"].setdefault(r["tehsil"],{"tehsil_id":r["tehsil_id"],"tehsil":clean(r["tehsil"]),"markazs":{}})
        m=t["markazs"].setdefault(r["markaz"],{"markaz_id":r["markaz_id"],"markaz":clean(r["markaz"]),"schools":[]})
        m["schools"].append(r)
    def aggregate(rows):
        ss=sum(r["total_students"] for r in rows); sp=sum(r["students_present"] for r in rows); sa=sum(r["students_absent"] for r in rows)
        tp=sum((r["teachers"] or {}).get("present",0) for r in rows); ta=sum((r["teachers"] or {}).get("absent",0) for r in rows)
        tm=sum((r["teachers"] or {}).get("marked",0) for r in rows)
        tt=sum((r["teachers"] or {}).get("total",0) for r in rows)
        tu=sum((r["teachers"] or {}).get("unmarked",max(0,(r["teachers"] or {}).get("total",0)-(r["teachers"] or {}).get("present",0)-(r["teachers"] or {}).get("absent",0))) for r in rows)
        return {"total_schools":len(rows),"total_students":ss,"students_present":sp,"students_absent":sa,
                "students_unmarked":max(0,ss-sp-sa),
                "student_attendance_pct":round(sp/ss*100,2) if ss else 0,
                "teachers_total":tt,"teachers_present":tp,"teachers_absent":ta,"teachers_unmarked":tu,"teachers_marked":tm,
                "teacher_attendance_pct":round(tp/tt*100,2) if tt else None}
    districts_out=[]
    for d in D.values():
        for t in d["tehsils"].values():
            mrs=[]
            for m in t["markazs"].values():
                m["summary"]=aggregate(m["schools"]); mrs.append(m)
            t["markazs"]=sorted(mrs,key=lambda x:x["markaz"])
            t["summary"]=aggregate([r for m in mrs for r in m["schools"]])
        d["tehsils"]=sorted(d["tehsils"].values(),key=lambda x:x["tehsil"])
        d["summary"]=aggregate([r for t in d["tehsils"] for m in t["markazs"] for r in m["schools"]])
        districts_out.append(d)
    for d in districts_out:
        official=district_rows.get(d["district"])
        if official:
            teachers=dict(official["teachers"])
            if not teachers.get("unmarked_reported"):
                # If the district-level teacher endpoint omits unmarked count,
                # use the SIS Filled staff counts already collected school-wise.
                district_staff=[r for r in records if r["district"]==d["district"] and r.get("teachers")]
                staff_total=sum((r.get("teachers") or {}).get("total",0) for r in district_staff)
                if staff_total >= teachers["present"]+teachers["absent"]:
                    teachers["total"]=staff_total
                    teachers["unmarked"]=max(0,staff_total-teachers["present"]-teachers["absent"])
                    teachers["progress_pct"]=round(100*teachers["present"]/staff_total,2) if staff_total else None
                    teachers["total_source"]="SIS sanctioned posts Filled"
            d["sis_summary"]={"students":official["students"],"teachers":teachers,
                              "verified_directly_from_sis":True}
        else:
            d["sis_summary"]={"students":None,"teachers":None,"verified_directly_from_sis":False}
    payload={"generated_at":datetime.now(timezone.utc).isoformat(),"source":BASE,
             "attendance_date":datetime.now().astimezone().strftime("%d/%m/%Y"),
             "level":"district_tehsil_markaz_school","district_count":len(districts_out),
             "inventory_schools":len(schools),"successful_schools":len(records),
             "failed_schools":len(failed),"mapping_failures":MAPPING_FAILURES[:5000],
             "complete":bool(complete and len(failed)==0 and not MAPPING_FAILURES),
             "collection_in_progress":not complete,
             "districts":sorted(districts_out,key=lambda x:x["district"]),
             "failed_school_requests":failed[:5000]}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    temp=OUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    temp.replace(OUT)
    print(f"Saved {'final' if complete else 'checkpoint'} JSON: {len(records)}/{len(schools)} schools; {len(districts_out)} districts",flush=True)

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
        for attempt in range(4):
            try: return s,school_attendance(s,sess)
            except Exception as e:
                last=e
                time.sleep(min(8, 1.5*(attempt+1)))
                prime(sess)
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
            if i%250==0:
                print(f"Attendance {i}/{len(schools)}",flush=True)
                save_snapshot(ds, schools, records, failed, complete=False)
    print(f"Successful attendance: {len(records)}/{len(schools)}",flush=True)
    print(f"Failed attendance: {len(failed)}",flush=True)
    if len(records)==0: raise RuntimeError("No SIS attendance was collected")
    save_snapshot(ds, schools, records, failed, complete=True)
    # Do not publish fake/zero records. A run may publish real partial data, but is flagged incomplete.
    # Publish real partial data even when SIS temporarily fails for some schools.
    # Failed schools are explicitly listed in failed_school_requests and are never
    # fabricated as zero-attendance records; failing this step would block Pages deployment.

if __name__=="__main__":
    collect()
