"""Free external-event ingestion for the paper decision engine.

The worker turns official RSS/HTML plus an explicitly low-trust public Telegram
lead source into structured events. It does not trade. Social/news headlines are
never sufficient by themselves: event score is combined later with live market
confirmation by decision_worker.py.
"""
from __future__ import annotations
import hashlib, html, json, math, os, re, sqlite3, time, urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET
from decision_engine import ExternalEvent,event_reliability,external_event_score

VERSION="external-events-0.2.0"
UA="crypto-paper-lab-events/0.1 contact=research-paper-only"

@dataclass(frozen=True)
class Source:
    name:str; url:str; tier:str; kind:str; link_contains:str|None=None

SOURCES=(
 Source("FED_ALL","https://www.federalreserve.gov/feeds/press_all.xml","A","RSS"),
 Source("FED_MONETARY","https://www.federalreserve.gov/feeds/press_monetary.xml","A","RSS"),
 Source("FED_SPEECHES","https://www.federalreserve.gov/feeds/speeches.xml","A","RSS"),
 Source("SEC_PRESS","https://www.sec.gov/news/pressreleases.rss","A","RSS"),
 Source("SEC_SPEECHES","https://www.sec.gov/news/speeches-statements.rss","A","RSS"),
 Source("CFTC_PRESS","https://www.cftc.gov/RSS/RSSGP/rssgp.xml","A","RSS"),
 Source("CFTC_SPEECHES","https://www.cftc.gov/RSS/RSSST/rssst.xml","A","RSS"),
 Source("BLS_JOBS","https://www.bls.gov/feed/empsit.rss","A","RSS"),
 Source("BLS_CPI","https://www.bls.gov/feed/cpi.rss","A","RSS"),
 Source("BEA_RELEASES","https://www.bea.gov/news/current-releases","A","HTML","/news/2026/"),
 Source("NHC_ATLANTIC","https://www.nhc.noaa.gov/index-at.xml","A","RSS"),
 Source("WHITE_HOUSE","https://www.whitehouse.gov/briefings-statements/","A","HTML","/briefings-statements/"),
 Source("WHITE_HOUSE_ACTIONS","https://www.whitehouse.gov/presidential-actions/","A","HTML","/presidential-actions/"),
 Source("TREASURY","https://home.treasury.gov/news/press-releases","A","HTML","/news/press-releases/"),
 Source("OFAC","https://ofac.treasury.gov/recent-actions","A","HTML","/recent-actions/"),
 Source("HOUSE_FIN_SERVICES","https://financialservices.house.gov/news/default.aspx","A","HTML","/news/documentsingle.aspx?DocumentID="),
 Source("SENATE_BANKING","https://www.banking.senate.gov/newsroom/","A","HTML","/newsroom/"),
 Source("CENTCOM","https://www.centcom.mil/MEDIA/PUBLIC-RELEASES/","A","HTML","/MEDIA/"),
 Source("GLOBALFINANCE_ZH","https://t.me/s/GlobalFinance_ZH","D","TELEGRAM"),
 Source("USGS_SIGNIFICANT","https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_hour.geojson","A","USGS"),
)

NEGATIVE=("war","attack","strike","missile","drone attack","invasion","blockade","sanction","terror","explosion","bank failure","default","emergency","shutdown","ban","hack","hacked","exploit","breach","outage","earthquake","hurricane","major hurricane","tropical storm","tariff","export restriction","export control")
POSITIVE=("rate cut","easing","dovish","approval","approved","reopens","ceasefire","peace agreement","breakthrough","record investment","stimulus")
FED_NEG=("rate hike","higher for longer","hawkish","tightening","inflation remains elevated")
CRYPTO_WORDS=("crypto","digital asset","bitcoin","ethereum","stablecoin","token","blockchain","exchange")


def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)


def get_bytes(url,limit=3_000_000):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/rss+xml, application/xml, text/html, application/json;q=0.9, */*;q=0.5"})
    with urllib.request.urlopen(req,timeout=12) as r:
        raw=r.read(limit+1)
        if len(raw)>limit:raise ValueError("oversize source")
    return raw


def _strip(s):
    return re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]+>"," ",str(s or "")))).strip()


def _published(text,default=None):
    if not text:return default
    try:
        dt=parsedate_to_datetime(text)
        if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:return default


def parse_rss(raw:bytes):
    root=ET.fromstring(raw);out=[]
    # Support RSS item and Atom entry without external dependencies.
    nodes=list(root.findall(".//item")) or list(root.findall(".//{*}entry"))
    for node in nodes[:30]:
        def text(names):
            for n in names:
                el=node.find(n)
                if el is not None and el.text:return el.text.strip()
            return None
        title=text(("title","{*}title")) or ""
        link=text(("link",))
        if not link:
            el=node.find("{*}link");link=el.get("href") if el is not None else None
        guid=text(("guid","id","{*}id")) or link or title
        desc=text(("description","summary","{*}summary","content","{*}content")) or ""
        pub=text(("pubDate","published","updated","{*}published","{*}updated"))
        out.append({"id":str(guid),"title":_strip(title),"summary":_strip(desc),"url":str(link or ""),"published_at":_published(pub)})
    return out


class LinkParser(HTMLParser):
    def __init__(self,needle):super().__init__();self.needle=needle;self.href=None;self.text=[];self.items=[]
    def handle_starttag(self,tag,attrs):
        if tag=="a":
            h=dict(attrs).get("href") or ""
            if self.needle in h:self.href=h;self.text=[]
    def handle_data(self,data):
        if self.href:self.text.append(data)
    def handle_endtag(self,tag):
        if tag=="a" and self.href:
            t=_strip(" ".join(self.text))
            if t:self.items.append((self.href,t))
            self.href=None;self.text=[]


def parse_html_links(raw:bytes,needle:str,base_url:str):
    p=LinkParser(needle);p.feed(raw.decode("utf-8","ignore"));out=[];seen=set()
    for href,title in p.items:
        if href in seen or len(title)<8:continue
        seen.add(href)
        if href.startswith("/"):
            m=re.match(r"^(https?://[^/]+)",base_url);href=(m.group(1) if m else "")+href
        out.append({"id":href,"title":title,"summary":"","url":href,"published_at":None})
        if len(out)>=20:break
    return out


class TgParser(HTMLParser):
    def __init__(self):super().__init__();self.current=None;self.in_text=0;self.in_time=0;self.out=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="div" and a.get("data-post"):
            self.current={"id":a["data-post"],"title_parts":[],"published_at":None}
        if self.current and tag=="div" and "tgme_widget_message_text" in a.get("class",""):self.in_text+=1
        if self.current and tag=="time":
            self.in_time+=1
            dt=a.get("datetime")
            if dt:
                try:self.current["published_at"]=__import__("datetime").datetime.fromisoformat(dt.replace("Z","+00:00")).timestamp()
                except Exception:pass
    def handle_data(self,data):
        if self.current and self.in_text:self.current["title_parts"].append(data)
    def handle_endtag(self,tag):
        if tag=="div" and self.in_text:self.in_text=max(0,self.in_text-1)
        if tag=="time" and self.in_time:self.in_time-=1
        # Telegram wraps messages in multiple divs; finalize when a new data-post arrives or at parser close.
    def close(self):
        super().close()
        # fallback parser below is used for IDs/text because HTML nesting varies.


def parse_telegram(raw:bytes):
    s=raw.decode("utf-8","ignore");out=[]
    blocks=re.split(r'(?=data-post="GlobalFinance_ZH/\d+")',s)
    for b in blocks:
        m=re.search(r'data-post="(GlobalFinance_ZH/(\d+))"',b)
        if not m:continue
        msgid=m.group(1);tm=re.search(r'<time[^>]+datetime="([^"]+)"',b)
        pub=None
        if tm:
            try:pub=__import__("datetime").datetime.fromisoformat(tm.group(1).replace("Z","+00:00")).timestamp()
            except Exception:pass
        tx=re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>',b,re.S)
        title=_strip(tx.group(1) if tx else "")
        if not title:continue
        out.append({"id":msgid,"title":title[:1000],"summary":"","url":"https://t.me/"+msgid,"published_at":pub})
    return out[-20:]


def parse_usgs(raw:bytes):
    body=json.loads(raw);features=body.get("features")
    if not isinstance(features,list):raise ValueError("malformed USGS feed")
    out=[]
    for f in features[:20]:
        if not isinstance(f,dict):continue
        p=f.get("properties") or {};eid=f.get("id") or p.get("url")
        mag=p.get("mag");place=p.get("place") or ""
        if eid and mag is not None:
            out.append({"id":str(eid),"title":f"M{mag} earthquake {place}","summary":"","url":str(p.get("url") or ""),"published_at":float(p.get("time") or 0)/1000 or None})
    return out


def classify_text(title,summary=""):
    text=(str(title)+" "+str(summary)).lower();category="OTHER";severity=.25;direction=0
    if any(k in text for k in ("fomc","federal reserve","fed ","powell","interest rate","monetary policy")):
        category="FED";severity=.65
        if any(k in text for k in POSITIVE):direction=1
        if any(k in text for k in FED_NEG):direction=-1
    if any(k in text for k in ("consumer price index","cpi","inflation","personal income and outlays","pce price")):
        category="INFLATION";severity=max(severity,.72);direction=0
    if any(k in text for k in ("gross domestic product","gdp ","gdp (")):
        category="GROWTH";severity=max(severity,.66);direction=0
    if any(k in text for k in ("employment situation","nonfarm","payroll","unemployment","jobs report")):
        category="JOBS";severity=max(severity,.70);direction=0
    if any(k in text for k in ("hormuz","red sea","missile","airstrike","air strike","military","war ","attack","blockade","terror")):
        category="WAR";severity=max(severity,.90);direction=-1
    if any(k in text for k in ("sec ","cftc","regulation","enforcement","court","congress","clarity act","crypto bill","stablecoin","digital asset")) and any(k in text for k in CRYPTO_WORDS):
        category="REGULATION";severity=max(severity,.62)
        if any(k in text for k in ("approve","approval","clarity","legal","signed into law")):direction=1
        elif any(k in text for k in ("ban","charge","sue","enforcement","illegal","prohibit")):direction=-1
    if any(k in text for k in ("tariff","trade war","export control","export restriction","sanction")):
        category="POLITICS_TRADE";severity=max(severity,.72);direction=-1
    if any(k in text for k in ("ai ","artificial intelligence","semiconductor","chip","nvidia","data center")):
        category="AI_TECH";severity=max(severity,.50)
        if any(k in text for k in ("breakthrough","investment","approved","launch","record")):direction=1
        if any(k in text for k in ("ban","restriction","shortage","export control")):direction=-1
    if any(k in text for k in ("hurricane","earthquake","tropical storm","storm surge","major storm")):
        category="NATURAL_DISASTER";severity=max(severity,.65);direction=-1
        m=re.search(r'\bm\s*([0-9]+(?:\.[0-9]+)?)\s+earthquake',text)
        if m:severity=max(severity,min(1.0,.45+(float(m.group(1))-5)*.12))
    if any(k in text for k in ("exchange hack","stablecoin depeg","bridge hack","custodian hack","exchange outage","exploit")):
        category="EXCHANGE_SECURITY";severity=max(severity,.90);direction=-1
    # Generic negative/positive is only a low-confidence tie-breaker.
    if direction==0:
        if any(k in text for k in NEGATIVE):direction=-1
        elif any(k in text for k in POSITIVE):direction=1
    return category,min(1.0,severity),direction


def dbopen(path:Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS external_event_sources(source TEXT PRIMARY KEY,bootstrapped INTEGER NOT NULL DEFAULT 0,last_ok REAL,last_error TEXT);
    CREATE TABLE IF NOT EXISTS external_events(
      id INTEGER PRIMARY KEY,source TEXT NOT NULL,event_key TEXT NOT NULL,title TEXT NOT NULL,url TEXT,
      published_at REAL,observed_at REAL NOT NULL,source_tier TEXT NOT NULL,category TEXT NOT NULL,
      severity REAL NOT NULL,direction INTEGER NOT NULL,active INTEGER NOT NULL,raw_json TEXT NOT NULL,
      UNIQUE(source,event_key));
    CREATE INDEX IF NOT EXISTS idx_external_events_time ON external_events(observed_at,published_at);
    CREATE TABLE IF NOT EXISTS external_event_states(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,signed_score REAL NOT NULL,shock_confidence REAL NOT NULL,
      shock_direction TEXT,event_count INTEGER NOT NULL,reasons_json TEXT NOT NULL,source TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_external_state_ts ON external_event_states(ts);
    """);db.commit();return db


def _is_bootstrapped(db,name):
    row=db.execute("SELECT bootstrapped FROM external_event_sources WHERE source=?",(name,)).fetchone();return bool(row and row[0])


def _mark_source(db,name,ok,error=None):
    with db:db.execute("""INSERT INTO external_event_sources(source,bootstrapped,last_ok,last_error) VALUES(?,1,?,?)
      ON CONFLICT(source) DO UPDATE SET bootstrapped=1,last_ok=excluded.last_ok,last_error=excluded.last_error""",(name,time.time() if ok else None,error))


def poll_source(db,src:Source,now:float,fetch=get_bytes):
    raw=fetch(src.url);boot=_is_bootstrapped(db,src.name)
    if src.kind=="RSS":items=parse_rss(raw)
    elif src.kind=="HTML":items=parse_html_links(raw,src.link_contains or "",src.url)
    elif src.kind=="TELEGRAM":items=parse_telegram(raw)
    elif src.kind=="USGS":items=parse_usgs(raw)
    else:raise ValueError("unsupported source type")
    added=0;activated=0
    for item in items:
        key=str(item.get("id") or hashlib.sha256((item.get("title") or "").encode()).hexdigest())
        exists=db.execute("SELECT 1 FROM external_events WHERE source=? AND event_key=?",(src.name,key)).fetchone()
        if exists:continue
        pub=item.get("published_at");age=(now-float(pub)) if pub else None
        # RSS/USGS have publication time, so a genuinely fresh item may be active on first boot.
        active=int(bool((pub and -60<=age<=3600) or boot))
        cat,sev,direction=classify_text(item.get("title"),item.get("summary"))
        with db:db.execute("""INSERT INTO external_events(source,event_key,title,url,published_at,observed_at,source_tier,category,severity,direction,active,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(src.name,key,item.get("title") or "",item.get("url") or "",pub,now,src.tier,cat,sev,direction,active,json.dumps(item,separators=(",",":"),allow_nan=False)))
        added+=1;activated+=active
    _mark_source(db,src.name,True,None)
    return {"source":src.name,"items":len(items),"added":added,"active":activated}


def _market_confirmation(db,direction):
    # Price is the confirmation layer; no headline can manufacture confirmation.
    row=db.execute("SELECT regime,shock_direction,btc_5m,confidence,ts FROM market_regime_states ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    if not row or time.time()-float(row["ts"])>180:return False
    btc=float(row["btc_5m"] or 0)
    if direction>0:return btc>=.002 or (row["regime"] in ("RISK_ON","SHOCK") and row["shock_direction"]=="UP")
    if direction<0:return btc<=-.002 or (row["regime"] in ("RISK_OFF","SHOCK") and row["shock_direction"]=="DOWN")
    return row["regime"]=="SHOCK"


def build_state(db,now=None):
    now=time.time() if now is None else float(now)
    rows=db.execute("""SELECT * FROM external_events WHERE active=1 AND COALESCE(published_at,observed_at)>=? ORDER BY COALESCE(published_at,observed_at) DESC LIMIT 80""",(now-6*3600,)).fetchall()
    ev=[];cats=[]
    for r in rows:
        age=max(0.0,now-float(r["published_at"] or r["observed_at"]));half=900 if r["source_tier"]=="D" else 1800
        freshness=math.exp(-age/half)
        confirmed=_market_confirmation(db,int(r["direction"]))
        # Count independent source names with similar category/direction in a 30m window.
        corr=db.execute("""SELECT COUNT(DISTINCT source) FROM external_events WHERE active=1 AND category=? AND direction=?
          AND COALESCE(published_at,observed_at)>=?""",(r["category"],r["direction"],now-1800)).fetchone()[0]-1
        e=ExternalEvent(r["source_tier"],float(r["severity"]),freshness,max(0,int(corr)),confirmed,int(r["direction"]),r["category"])
        ev.append(e);cats.append(r["category"])
    signed,shock,reasons=external_event_score(ev)
    # Ambiguous scheduled macro releases do not become a hard shock without market confirmation.
    hard_categories={"WAR","EXCHANGE_SECURITY","NATURAL_DISASTER"}
    if shock>0 and not any(e.market_confirmed or e.category in hard_categories for e in ev):shock=0.0
    direction="UP" if signed>=35 else "DOWN" if signed<=-35 else None
    with db:db.execute("INSERT INTO external_event_states(ts,signed_score,shock_confidence,shock_direction,event_count,reasons_json,source) VALUES(?,?,?,?,?,?,?)",
      (now,signed,shock,direction,len(ev),json.dumps(reasons,separators=(",",":")),"FREE_OFFICIAL+TELEGRAM_LEAD"))
    return {"signed_score":signed,"shock_confidence":shock,"shock_direction":direction,"event_count":len(ev),"reasons":reasons,"categories":cats}


def latest_state(db,now=None,max_age=180.0):
    row=db.execute("SELECT * FROM external_event_states ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    if not row:return None
    now=time.time() if now is None else float(now)
    if now-float(row["ts"])>max_age:return None
    return {"signed_score":float(row["signed_score"]),"shock_confidence":float(row["shock_confidence"]),"shock_direction":row["shock_direction"],"event_count":int(row["event_count"]),"reasons":json.loads(row["reasons_json"])}


def cycle(db,now=None,fetch=get_bytes):
    now=time.time() if now is None else float(now);results=[];errors={};raws={}
    # Fetch independent public sources concurrently so a slow government page does
    # not delay every other headline. SQLite writes remain serial/auditable.
    with ThreadPoolExecutor(max_workers=min(8,len(SOURCES))) as pool:
        futures={pool.submit(fetch,src.url):src for src in SOURCES}
        for fut in as_completed(futures):
            src=futures[fut]
            try:raws[src.name]=fut.result()
            except Exception as exc:errors[src.name]=f"{type(exc).__name__}:{str(exc)[:120]}"
    for src in SOURCES:
        if src.name in errors:
            with db:db.execute("INSERT INTO external_event_sources(source,bootstrapped,last_error) VALUES(?,0,?) ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error",(src.name,errors[src.name]))
            continue
        try:results.append(poll_source(db,src,now,lambda _url,raw=raws[src.name]:raw))
        except Exception as exc:
            errors[src.name]=f"{type(exc).__name__}:{str(exc)[:120]}"
            with db:db.execute("INSERT INTO external_event_sources(source,bootstrapped,last_error) VALUES(?,0,?) ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error",(src.name,errors[src.name]))
    state=build_state(db,now)
    emit("EXTERNAL_EVENTS_OK" if len(errors)<len(SOURCES)//2 else "EXTERNAL_EVENTS_DEGRADED",sources_ok=len(SOURCES)-len(errors),errors={k:v.split(":",1)[0] for k,v in errors.items()},state=state,no_trade=True)
    return {"results":results,"errors":errors,"state":state}


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("EXTERNAL_EVENTS_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(15,30-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
