# -*- coding: utf-8 -*-
"""Punjab district attendance collector using proven SIS Markaz aggregation."""
import argparse, json, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE="https://sis.pesrp.edu.pk"
OUT=Path("data/punjab_district_attendance.json")
TIMEOUT=45
MAX_WORKERS=20
UA=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154 Safari/537.36")

def clean(x): return re.sub(r"\\s+"," ",str(x or "")).strip()
def num(x):
    s=re.sub(r"[^\\d.-]","",str(x or ""))
    try:return float(s) if s else 0.0
    except:return 0.0

def make_session():
    s=requests.Session()
    retry=Retry(total=4,connect=4,read=4,backoff_factor=.5,
        status_forcelist=[429,500,502,503,504],
        allowed_methods=frozenset(["GET"]),raise_on_status=False)
    s.mount("https://",HTTPAdapter(max_retries=retry,pool_connections=32,pool_maxsize=32))
    s.headers.update({"User-Agent":UA,"X-Requested-With":"XMLHttpRequest"})
    return s

def request(s,path,params=None,html=False):
    r=s.get(BASE+path,params=params,timeout=TIMEOUT,headers={
        "Accept":"text/html,application/json,text/javascript,*/*;q=0.01",
        "X-Requested-With":"XMLHttpRequest","Referer":BASE+"/dashboard"})
    r.raise_for_status()
    if html:return r.text
    try:return r.json()
    except:return r.text

def parse_options(text):
    out=[]
    for value,name in re.findall(r"<option[^>]*value\\s*=\\s*['\\\"]([^'\\\"]*)['\\\"][^>]*>\\s*(.*?)\\s*</option>",text or "",re.I|re.S):
        value=clean(value); name=clean(unescape(re.sub(r"<[^>]+>"," ",name)))
        if value and name and name.lower() not in {"all","select district","all districts","all tehsils","all schools"}:
            out.append((value,name))
    return out

def district_options(s):
    html=request(s,"/dashboard",html=True)
    blocks=re.findall(r"<select\\b([^>]*)>(.*?)</select>",html,re.I|re.S)
    candidates=[]
    for attrs,body in blocks:
        if "district" in attrs.lower():
            opts=parse_options(body)
            if 20<=len(opts)<=60:candidates.append(opts)
    if candidates:return max(candidates,key=len)
    for _,body in blocks:
        opts=parse_options(body); names={n.upper() for _,n in opts}
        if "LAHORE" in names and "OKARA" in names and 20<=len(opts)<=60:return opts
    raise RuntimeError("Could not isolate SIS district selector")

def tehsils(s,district_id):
    data=request(s,"/user/get_tehsils",{"district":district_id,"selectedTehsil":"false","all":"All"})
    opts=parse_options(data.get("html","") if isinstance(data,dict) else str(data))
    if not opts:raise RuntimeError(f"No tehsils returned for district {district_id}")
    return opts

def markazes(s,tehsil_id):
    data=request(s,"/user/get_markazes",{"tehsil":tehsil_id,"selectedMarkaz":"false","all":"All"})
    opts=parse_options(data.get("html","") if isinstance(data,dict) else str(data))
    if not opts:raise RuntimeError(f"No markazes returned for tehsil {tehsil_id}")
    return opts

def parse_rows(raw):
    if isinstance(raw,dict):
        for k in ("data","html","rows","aaData","results","records"):
            if isinstance(raw.get(k),(str,list,dict)): raw=raw[k]; break
    if isinstance(raw,list): return [x for x in raw if isinstance(x,dict)]
    rows=[]
    for tr in re.findall(r"<tr\\b[^>]*>(.*?)</tr>",str(raw),re.I|re.S):
        cells=[clean(unescape(re.sub(r"<[^>]+>"," ",c))) for c in re.findall(r"<t[dh]\\b[^>]*>(.*?)</t[dh]>",tr,re.I|re.S)]
        if cells:rows.append(cells)
    return rows

def pick(d,*keys):
    low={str(k).lower():v for k,v in d.items()}
    for k in keys:
        if k in d and d[k] not in (None,""):return d[k]
        if k.lower() in low and low[k.lower()] not in (None,""):return low[k.lower()]
    return None

def student_attendance(s,district_id,tehsil_id,markaz_id,date_from):
    raw=request(s,"/dashboard/attendance_table",{
        "district_id":district_id,"tehsil_id":tehsil_id,"markaz_id":markaz_id,
        "date_from":date_from,"only_kpztp_districts":"false"})
    total={"schools":0,"students":0,"present":0,"absent":0,"unmarked":0}
    rows=parse_rows(raw)
    for cells in rows:
        if not isinstance(cells,list) or len(cells)<6:continue
        if not re.search(r"(?<!\\d)\\d{8}(?!\\d)"," ".join(cells)):continue
        nums=[num(x) for x in cells[2:]]
        if len(nums)<4:continue
        enrolled,present,absent=int(nums[0]),int(nums[1]),int(nums[3])
        students=max(enrolled,present+absent)
        total["students"]+=students; total["present"]+=present; total["absent"]+=absent
        total["unmarked"]+=max(0,students-present-absent); total["schools"]+=1
    if total["schools"]==0:
        for row in rows:
            emis=pick(row,"emis","emis_code","emisCode","school_emis_code","s_id_emis_code")
            if not re.search(r"\\d{8}",str(emis or "")):continue
            enrolled=pick(row,"enrolled","enrollment","total_students","students","student_total")
            present=pick(row,"present","students_present","present_count")
            absent=pick(row,"absent","students_absent","absent_count")
            if enrolled is None and present is None and absent is None:continue
            enrolled,present,absent=int(num(enrolled)),int(num(present)),int(num(absent))
            students=max(enrolled,present+absent)
            total["students"]+=students; total["present"]+=present; total["absent"]+=absent
            total["unmarked"]+=max(0,students-present-absent); total["schools"]+=1
    if total["schools"]==0:
        raise RuntimeError(f"No attendance rows for district={district_id}, tehsil={tehsil_id}, markaz={markaz_id}; response={clean(str(raw))[:220]}")
    return total

def collect_district(district_id,district_name,date_from):
    discovery=make_session(); tasks=[]
    for tid,tname in tehsils(discovery,district_id):
        for mid,mname in markazes(discovery,tid):tasks.append((tid,tname,mid,mname))
    if not tasks:raise RuntimeError("No Markaz found for district")
    agg={"schools":0,"students":0,"present":0,"absent":0,"unmarked":0}; errors=[]
    def one(task):
        tid,tname,mid,mname=task
        return student_attendance(make_session(),district_id,tid,mid,date_from)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures={ex.submit(one,t):t for t in tasks}
        for fut in as_completed(futures):
            task=futures[fut]
            try:
                x=fut.result()
                for k in agg:agg[k]+=x[k]
            except Exception as e:errors.append(f"{task[1]}/{task[3]}: {type(e).__name__}: {e}")
    if errors:raise RuntimeError(f"{len(errors)} of {len(tasks)} Markaz attendance requests failed. First error: {errors[0]}")
    st,pr=agg["students"],agg["present"]
    return {"district_id":district_id,"district":district_name,"status":"OK",
            "total_schools":agg["schools"],"total_students":st,"students_present":pr,
            "students_absent":agg["absent"],"students_unmarked":agg["unmarked"],
            "student_attendance_pct":round(pr/st*100,2) if st else 0,
            "markaz_count":len(tasks),"errors":[]}

def collect():
    s=make_session(); districts=district_options(s)
    date_from=datetime.now().astimezone().strftime("%d/%m/%Y"); rows=[]
    for i,(did,name) in enumerate(districts,1):
        print(f"[{i}/{len(districts)}] {name}...")
        try:
            row=collect_district(did,name,date_from); rows.append(row)
            print(f"  OK: {row['students_present']}/{row['total_students']} ({row['student_attendance_pct']}%)")
        except Exception as e:
            rows.append({"district_id":did,"district":name,"status":"ERROR","errors":[str(e)]}); print(f"  ERROR: {e}")
    rows.sort(key=lambda x:x["district"]); complete=sum(x.get("status")=="OK" for x in rows)
    payload={"generated_at":datetime.now(timezone.utc).isoformat(),"source":BASE,
             "attendance_date":date_from,"level":"district","attendance_type":"student",
             "district_count":len(rows),"complete_districts":complete,"districts":rows}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    if len(rows)!=40 or complete!=40:raise RuntimeError(f"Punjab district collection incomplete: {complete}/{len(rows)}")

if __name__=="__main__":
    argparse.ArgumentParser().parse_args(); collect()
