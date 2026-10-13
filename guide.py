"""Fodbold-tv-guide.

Finder ud af, hvilke tv-kanaler der sender hvilke fodboldkampe de næste dage.
Kilder: kampprogram fra ESPN og offentlige tv-programoversigter (XMLTV) fra epgshare01.
Resultatet skrives som en lille JSON-fil:

  ch      kanaltabel: [nøgler adskilt af "|", land]
  events  kampe: tid, liga, hold, stilling og hvilke kanaler (indeks i ch) der sender dem
  more    andre direkte sportsudsendelser (kampe), som ikke er i kampprogrammet

Kør:  python guide.py out/guide.json
"""
import bisect, gzip, html, json, os, re, sys, time, unicodedata, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

EPG_SOURCES = {"uk": "UK1", "us": "US2", "es": "ES1", "dk": "DK1",
               "mx": "MX1", "co": "CO1", "ar": "AR1", "cl": "CL1", "br": "BR1",
               "fr": "FR1", "de": "DE1", "it": "IT1", "pt": "PT1", "nl": "NL1", "se": "SE1", "no": "NO1", "ae": "AE1", "intl": "BEIN1"}
EPG_URL = "https://epgshare01.online/epgshare01/epg_ripper_{}.xml.gz"
LEAGUES = [
    ("soccer", "eng.1", "Premier League"), ("soccer", "uefa.champions", "Champions League"),
    ("soccer", "uefa.europa", "Europa League"), ("soccer", "uefa.europa.conf", "Conference League"),
    ("soccer", "esp.1", "La Liga"), ("soccer", "den.1", "Superligaen"), ("soccer", "ger.1", "Bundesliga"),
    ("soccer", "ita.1", "Serie A"), ("soccer", "fra.1", "Ligue 1"), ("soccer", "eng.fa", "FA Cup"),
    ("soccer", "eng.league_cup", "Carabao Cup"), ("soccer", "esp.copa_del_rey", "Copa del Rey"),
    ("soccer", "ned.1", "Eredivisie"), ("soccer", "por.1", "Liga Portugal"), ("soccer", "eng.2", "Championship"),
    ("soccer", "sco.1", "Skotsk Premiership"), ("soccer", "bel.1", "Belgisk liga"), ("soccer", "tur.1", "Süper Lig"),
    ("soccer", "swe.1", "Allsvenskan"), ("soccer", "nor.1", "Eliteserien"), ("soccer", "ger.dfb_pokal", "DFB-Pokal"),
    ("soccer", "ita.coppa_italia", "Coppa Italia"), ("soccer", "fifa.world", "VM"), ("soccer", "uefa.euro", "EM"),
    ("soccer", "uefa.nations", "Nations League"), ("soccer", "fifa.worldq.uefa", "VM-kvalifikation"),
]
SKIP = re.compile(r"\b(nfl|nba|wnba|nhl|mlb|ncaa|college|baseball|basketball|hockey|american football|football \(us\))\b", re.I)
UA = {"User-Agent": "Mozilla/5.0 (fodbold-tv-guide)"}

# ---------- tekst og kanalnavne ----------
TRANSLIT = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a", "ß": "ss", "ł": "l", "đ": "d"})


def fold(s):
    s = unicodedata.normalize("NFKD", str(s).translate(TRANSLIT)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s.replace("&", " and ")).strip()
    return re.sub(r"\b([a-z]) (?=[a-z]\b)", r"\1", s)


CC_NORM = {"uk": "uk", "gb": "uk", "eng": "uk", "us": "us", "usa": "us", "dk": "dk", "den": "dk", "es": "es", "esp": "es", "spa": "es",
           "mx": "mx", "co": "co", "ar": "ar", "cl": "cl", "pe": "pe", "ec": "ec", "uy": "uy", "cr": "cr", "bo": "bo", "py": "py", "ve": "ve",
           "br": "br", "latam": "latam", "fr": "fr", "de": "de", "it": "it", "pt": "pt", "nl": "nl", "se": "se", "no": "no",
           "ae": "ae", "qa": "ae", "sa": "ae"}
COUNTRY_WORDS = {"united states": "us", "usa": "us", "united kingdom": "uk", "uk": "uk", "england": "uk", "mexico": "mx",
                 "argentina": "ar", "colombia": "co", "chile": "cl", "peru": "pe", "ecuador": "ec", "uruguay": "uy",
                 "costa rica": "cr", "bolivia": "bo", "paraguay": "py", "venezuela": "ve", "brazil": "br", "brasil": "br",
                 "spain": "es", "espana": "es", "denmark": "dk", "danmark": "dk", "latin america": "latam", "latinoamerica": "latam",
                 "france": "fr", "germany": "de", "deutschland": "de", "italy": "it", "italia": "it", "portugal": "pt",
                 "netherlands": "nl", "holland": "nl", "sweden": "se", "norway": "no", "mena": "ae", "arabia": "ae",
                 "south": "latam", "north": "latam"}
LATAM_RE = re.compile(r"\b(latin\s*america|latinoamerica|latam)\b", re.I)
JUNK = {"hd", "fhd", "uhd", "sd", "4k", "hevc", "h265", "h264", "1080p", "720p", "1080", "720", "50fps", "60fps", "fps",
        "feed", "backup", "raw", "vip", "east", "west", "stream", "live", "ultra", "hq"}
KEY_SYN = [("viaplaysport", "vsport"), ("viaplayfotball", "vsportfotball"), ("bein sports", "beinsports"),
           ("skysports", "skysports"), ("canalplus", "canal"), ("canal+", "canal")]


def chan_key(name, tid=""):
    """Kanalnavn -> (kompakt nøgle, land). Fx 'Sky Sports Football (UK)' -> ('skysportsfootball', 'uk')."""
    cc = None
    m = re.search(r"\.([a-z]{2})\d?(?:@([^.]*))?$", tid or "", re.I)
    if m:
        cc = CC_NORM.get(m.group(1).lower(), m.group(1).lower())
    region = fold(m.group(2)) if m and m.group(2) else ""
    name = str(name)
    if LATAM_RE.search(name) or LATAM_RE.search(tid or ""):
        cc = "latam"
        name = LATAM_RE.sub(" ", name)
    if region in COUNTRY_WORDS and COUNTRY_WORDS[region] != "us":
        cc = COUNTRY_WORDS[region] if cc in (None, "us", "latam") else cc
    for par in re.findall(r"\(([^)]*)\)", name):
        w = fold(par)
        if w in COUNTRY_WORDS:
            cc = COUNTRY_WORDS[w]
        elif w and not re.fullmatch(r"(\d{3,4}[pi]|hd|fhd|uhd|sd|4k|backup|raw|vip|hevc|\d+)( \w+)?", w) and cc is None:
            cc = "zz"
    n = re.sub(r"\(.*?\)|\[.*?\]", " ", name)
    m = re.match(r"^\s*\|?\s*([a-z]{2,3})\s*[|:]\s*", n, re.I)
    if m and m.group(1).lower() in CC_NORM:
        cc = cc or CC_NORM[m.group(1).lower()]
        n = n[m.end():]
    words = fold(n).split()
    while words and (words[-1] in JUNK or words[-1] in CC_NORM):
        if words[-1] in CC_NORM:
            cc = cc or CC_NORM[words[-1]]
        words.pop()
    while words and words[0] in CC_NORM:
        w = words.pop(0)
        cc = cc or CC_NORM[w]
    while len(words) > 1:
        two, one = " ".join(words[-2:]), words[-1]
        if two in COUNTRY_WORDS and len(words) > 2:
            cc = COUNTRY_WORDS[two]; del words[-2:]
        elif one in COUNTRY_WORDS and one not in ("south", "north", "uk", "usa"):
            cc = COUNTRY_WORDS[one]; words.pop()
        else:
            break
    words = [w for w in words if w not in JUNK]
    key = "".join(words)
    for a, b in KEY_SYN:
        if key.startswith(a):
            key = b + key[len(a):]
    return key, cc


# ---------- programoversigter ----------
PAIR = re.compile(r"\s(?:v|vs\.?|at|-|–)\s", re.I)
FOOTY = re.compile(r"champions|europa league|conference league|premier league|la ?liga|serie a|bundesliga|ligue 1|superliga|eredivisie|"
                   r"liga portugal|primeira|calcio|f[uú]tbol|futebol|fu[sß]ball|fodbold|fotboll|fotball|voetbal|football|soccer|copa del rey|fa cup", re.I)


def _ts(s):
    s = s.strip()
    t = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    off = re.search(r"([+-])(\d\d)(\d\d)$", s)
    if off:
        d = timedelta(hours=int(off.group(2)), minutes=int(off.group(3)))
        t = t - d if off.group(1) == "+" else t + d
    return t.replace(tzinfo=timezone.utc).timestamp()


def _download(code):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(EPG_URL.format(code), headers=UA), timeout=120) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            time.sleep(5 * (attempt + 1))
    return None


def _parse(cc, data):
    text = gzip.decompress(data).decode("utf-8", "replace")
    chans, progs = {}, []
    for m in re.finditer(r'<channel id="([^"]+)">(.*?)</channel>', text, re.S):
        cid = html.unescape(m.group(1))
        base = cid.split("..", 1)[1] if ".." in cid and "/" in cid.split("..", 1)[0] else cid
        keys = {chan_key(re.sub(r"\.[a-z]{2}\d?$", "", base).replace(".", " "), cid)[0]}
        for d in re.findall(r"<display-name[^>]*>([^<]+)</display-name>", m.group(2)):
            keys.add(chan_key(html.unescape(d))[0])
        keys |= {k[5:] for k in keys if k.startswith("canal") and len(k) > 7}
        chans[cid] = {"keys": {k for k in keys if k}, "cc": cc}
    lo, hi = time.time() - 6 * 3600, time.time() + 3 * 86400
    for m in re.finditer(r"<programme\b([^>]*)>(.*?)</programme>", text, re.S):
        a, body = m.group(1), m.group(2)
        ch = re.search(r'channel="([^"]+)"', a)
        st, sp = re.search(r'start="([^"]+)"', a), re.search(r'stop="([^"]+)"', a)
        if not (ch and st and sp):
            continue
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
        sub = re.search(r"<sub-title[^>]*>(.*?)</sub-title>", body, re.S)
        title = html.unescape(title.group(1)).strip() if title else ""
        sub = html.unescape(sub.group(1)).strip() if sub else ""
        full = f"{title} | {sub}" if sub else title
        live = bool(re.match(r"live\b", title, re.I))
        if not PAIR.search(full) and not live and not FOOTY.search(full):
            continue
        try:
            start, stop = _ts(st.group(1)), _ts(sp.group(1))
        except ValueError:
            continue
        if stop < lo or start > hi:
            continue
        progs.append((start, stop, html.unescape(ch.group(1)), title, sub, fold(full), live))
    return chans, progs


# ---------- kampprogram ----------
ALIAS = {
    "manchester united": ["man utd", "man united", "manchester utd"], "manchester city": ["man city"],
    "tottenham hotspur": ["spurs", "tottenham"], "wolverhampton wanderers": ["wolves", "wolverhampton"],
    "brighton and hove albion": ["brighton"], "nottingham forest": ["nottm forest", "nott m forest", "nottingham"],
    "west ham united": ["west ham"], "newcastle united": ["newcastle"], "afc bournemouth": ["bournemouth"],
    "leeds united": ["leeds"], "leicester city": ["leicester"], "ipswich town": ["ipswich"], "crystal palace": ["palace"],
    "brondby if": ["brondby"], "fc midtjylland": ["midtjylland", "fcm"], "borussia dortmund": ["dortmund", "bvb"],
    "bayer leverkusen": ["leverkusen"], "athletic club": ["athletic bilbao", "athletic"], "real betis": ["betis"],
    "celta vigo": ["celta"], "rb leipzig": ["leipzig"], "eintracht frankfurt": ["frankfurt"], "as roma": ["roma"], "ss lazio": ["lazio"],
    "sporting cp": ["sporting cp", "sporting lisbon", "sporting lissabon", "sporting club portugal", "sporting portugal", "sporting clube de portugal"],
    "benfica": ["benfica", "benfica lissabon", "sl benfica"], "fc porto": ["porto"], "lask linz": ["linzer ask", "lask"],
    "bayern munich": ["bayern", "bayern munchen", "bayern monaco", "bayern de munich", "bayern munique"],
    "internazionale": ["inter", "inter milan", "inter mailand", "inter milano", "inter de milan"],
    "ac milan": ["milan", "ac mailand", "milan ac"], "juventus": ["juventus", "juventus turin", "juve"],
    "fc barcelona": ["barcelona", "barca", "fc barcelone", "barcelone"], "real madrid": ["real madrid"],
    "atletico madrid": ["atletico", "atletico de madrid", "atletico madri"], "paris saint germain": ["psg", "paris sg", "paris saint germain", "paris"],
    "olympique lyonnais": ["lyon", "olympique lyon", "ol"], "olympique marseille": ["marseille", "olympique de marseille", "om"],
    "napoli": ["napoli", "ssc napoli", "neapel", "naples"], "psv eindhoven": ["psv"], "feyenoord rotterdam": ["feyenoord"],
    "ajax amsterdam": ["ajax"], "club brugge": ["club brugge", "fc brugge", "bruges", "club bruges"],
    "galatasaray": ["galatasaray"], "fenerbahce": ["fenerbahce"], "shakhtar donetsk": ["shakhtar", "schachtar donezk", "chakhtar"],
    "slavia prague": ["slavia praha", "slavia prag", "slavia"],
    "fc copenhagen": ["kobenhavn", "fc kobenhavn", "fck", "copenhagen", "kopenhagen", "copenhague"],
    "bodo glimt": ["bodo glimt", "glimt"], "red star belgrade": ["crvena zvezda", "roter stern belgrad", "etoile rouge"],
}
CLUB = {"fc", "afc", "cf", "sc", "cd", "ud", "rc", "rcd", "sd", "ca", "if", "bk", "fk", "sk", "ssc", "as", "ss", "ac", "us", "vfb", "vfl", "tsg", "sv"}
WEAK = {"united", "city", "real", "club", "town", "sporting", "athletic", "inter", "racing", "county", "rovers", "athletico"}
TV_FIX = {"usanet": "usanetwork", "fs1": "foxsports1", "fs2": "foxsports2", "nbcsn": "nbcsports", "cbssn": "cbssportsnetwork"}
STREAMING = re.compile(r"\+|paramount|peacock|prime video|amazon|\bmax\b|apple|fubo|sling|app\b|\(local\)|stream|tubi|pluto|youtube", re.I)


def _aliases(team):
    out = set()
    for k in ("displayName", "shortDisplayName", "name", "location"):
        v = fold(team.get(k) or "")
        if not v:
            continue
        out.add(v)
        stripped = " ".join(w for w in v.split() if w not in CLUB)
        if stripped:
            out.add(stripped)
    for v in list(out):
        out.update(ALIAS.get(v, []))
    return sorted({a for a in out if len(a) >= 3 and a not in WEAK}, key=len, reverse=True)


def _pattern(aliases):
    return re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b") if aliases else None


def _espn(job):
    sport, lg, label, day = job
    u = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={day}&limit=200"
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in data.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        st = (comp.get("status") or e.get("status") or {}).get("type", {})
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in teams or "away" not in teams:
            continue
        try:
            start = datetime.strptime(e["date"].replace("Z", ""), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        side = lambda c: {"n": c["team"].get("shortDisplayName") or c["team"].get("displayName"), "full": c["team"].get("displayName"),
                          "logo": c["team"].get("logo", ""), "s": c.get("score")}
        out.append({"id": f"{lg}:{e.get('id')}", "eid": str(e.get("id")), "lg": lg, "league": label, "start": start,
                    "state": st.get("state", "pre"), "detail": st.get("shortDetail", ""),
                    "home": side(teams["home"]), "away": side(teams["away"]),
                    "hre": _pattern(_aliases(teams["home"]["team"])), "are": _pattern(_aliases(teams["away"]["team"])),
                    "tv": sorted({n for b in comp.get("broadcasts", []) for n in b.get("names", [])})})
    return out


def fixtures():
    now = datetime.now(timezone.utc)
    days = [(now + timedelta(days=d)).strftime("%Y%m%d") for d in (0, 1, 2, 3)]
    with ThreadPoolExecutor(12) as ex:
        res = ex.map(_espn, [(s, lg, label, day) for s, lg, label in LEAGUES for day in days])
    lo, hi = time.time() - 4 * 3600, time.time() + 72 * 3600
    events = list({e["id"]: e for r in res for e in r if (e["state"] == "in" or (e["state"] == "pre" and lo < e["start"] < hi))}.values())
    events.sort(key=lambda e: e["start"])
    return events


# ---------- sammenstil ----------
def build():
    with ThreadPoolExecutor(6) as ex:
        raw = dict(zip(EPG_SOURCES, ex.map(_download, EPG_SOURCES.values())))
    chans, progs = {}, []
    for cc, data in raw.items():
        if not data:
            continue
        c, p = _parse(cc, data)
        chans.update(c)
        progs += p
    progs.sort(key=lambda x: x[0])
    starts = [p[0] for p in progs]
    events = fixtures()

    table, index = [], {}
    def cidx(cid):
        if cid not in index:
            c = chans[cid]
            index[cid] = len(table)
            table.append(["|".join(sorted(c["keys"])), c["cc"]])
        return index[cid]

    out, used = [], set()
    for e in events:
        cs = []
        i, j = bisect.bisect_left(starts, e["start"] - 75 * 60), bisect.bisect_right(starts, e["start"] + 30 * 60)
        for p in progs[i:j]:
            if e["hre"] and e["are"] and e["hre"].search(p[5]) and e["are"].search(p[5]) and p[2] in chans:
                cs.append(cidx(p[2]))
                used.add(id(p))
        tvk = []
        for n in e["tv"]:
            if STREAMING.search(n):
                continue
            k = chan_key(n)[0]
            tvk.append(TV_FIX.get(k, k))
        out.append({"id": e["id"], "eid": e["eid"], "lg": e["lg"], "league": e["league"], "start": int(e["start"] * 1000),
                    "state": e["state"], "detail": e["detail"], "home": e["home"], "away": e["away"],
                    "tv": e["tv"], "tvk": sorted(set(tvk)), "c": sorted(set(cs))})

    more, now = {}, time.time()
    i, j = bisect.bisect_left(starts, now - 4 * 3600), bisect.bisect_right(starts, now + 24 * 3600)
    for p in progs[i:j]:
        if not p[6] or id(p) in used or p[1] < now or p[2] not in chans:
            continue
        if not PAIR.search(p[3] + "  " + p[4]) or SKIP.search(p[3]):
            continue
        m = more.setdefault((p[5], p[0]), {"title": re.sub(r"^live\s*:?\s*", "", p[3], flags=re.I),
                                           "sub": re.sub(r"\s*\((kick-off|start-time)[^)]*\)", "", p[4], flags=re.I),
                                           "start": int(p[0] * 1000), "stop": int(p[1] * 1000), "c": []})
        m["c"].append(cidx(p[2]))
    more = sorted(more.values(), key=lambda m: m["start"])[:400]
    for m in more:
        m["c"] = sorted(set(m["c"]))
    return {"v": 1, "at": int(time.time() * 1000), "ch": table, "events": out, "more": more}


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "guide.json"
    t = time.time()
    data = build()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(data['events'])} kampe, {len(data['more'])} andre udsendelser, {len(data['ch'])} kanaler, "
          f"{os.path.getsize(dest) // 1024} KB på {time.time() - t:.0f} s")
