#!/usr/bin/env python3
"""Build the launcher's "Trending in India" file: the top 3 titles per service from TMDB's weekly trending
list, keeping only titles streaming (flatrate) on that service in India.

Usage: TMDB_TOKEN=... python3 build_trending.py <out.json>
       (or the token in ~/.tmdb_token). Standard library only, so it runs anywhere, the daily cloud job included.

Output (what the TV reads):
  {"updated": "2026-10-29", "services": [{"name": "Netflix", "pkg": "com.netflix.ninja",
     "items": [{"title": "Lupin", "poster": "/abc.jpg", "tmdb": "tv/96677",
                "link": "https://www.netflix.com/title/80994082"}, ...]}, ...]}
Posters are fetched by the TV itself from https://image.tmdb.org/t/p/w185<poster>.
"link" opens the title's page in the service's TV app. It comes from JustWatch, matched by TMDB ID and service,
and only in the URL shape that app accepts; when JustWatch has none, the previous list's link for the same TMDB ID
is kept. links.json ({"tv/213375": "https://..."}, added by hand) wins over both. No link: the TV opens the app.
"""
import datetime
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SERVICES = [("Netflix", 8, "com.netflix.ninja"),
            ("Prime Video", 119, "com.amazon.amazonvideo.livingroom"),
            ("JioHotstar", 2336, "in.startv.hotstar")]

# Per service: JustWatch package names that count, and the link shapes the TV app opens (checked on the TV),
# rebuilt from the matched ID so nothing else from the response is passed through.
LINKS = {
    "Netflix": (lambda t: t.startswith("netflix"),
                re.compile(r"^https://www\.netflix\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?title/(\d{5,12})\b"),
                "https://www.netflix.com/title/{}"),
    "Prime Video": (lambda t: t.startswith("amazonprime"),
                    re.compile(r"^https://app\.primevideo\.com/detail\?gti=(amzn1\.dv\.gti\.[0-9a-f-]{36})\b"),
                    "https://app.primevideo.com/detail?gti={}"),
    # Show and movie pages only: a bare https://www.hotstar.com/in/<id> is often an episode, and the app plays it.
    "JioHotstar": (lambda t: "hotstar" in t,
                   re.compile(r"^https://www\.hotstar\.com/in/((?:shows|movies)/[a-z0-9-]{1,80}/\d{6,15})\b"),
                   "https://www.hotstar.com/in/{}"),
}
JW_QUERY = """query S($q:String!){ popularTitles(country:IN, first:5, filter:{searchQuery:$q}){ edges{ node{
 content(country:IN, language:"en"){ externalIds{ tmdbId } }
 offers(country:IN, platform:WEB){ package{ technicalName } standardWebURL } } } } }"""
PER_SERVICE = 3


def token():
    """TMDB read token from TMDB_TOKEN or ~/.tmdb_token. Empty in a cloud job whose network secret makes the
    agent proxy add the Authorization header itself."""
    t = os.environ.get("TMDB_TOKEN", "").strip()
    if not t and os.path.exists(os.path.expanduser("~/.tmdb_token")):
        with open(os.path.expanduser("~/.tmdb_token")) as f:
            t = f.read().strip()
    return t


def api(tok, path, **q):
    url = "https://api.themoviedb.org/3" + path + ("?" + urllib.parse.urlencode(q) if q else "")
    headers = {"accept": "application/json"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def justwatch_link(service, title, tmdb_id):
    """The service's title-page link from JustWatch, for the result whose TMDB ID matches; None if none."""
    pkg_ok, shape, url = LINKS[service]
    body = json.dumps({"query": JW_QUERY, "variables": {"q": title}}).encode()
    req = urllib.request.Request("https://apis.justwatch.com/graphql", body,
                                 {"Content-Type": "application/json", "User-Agent": "cleanyourtv-trending/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        edges = json.load(r)["data"]["popularTitles"]["edges"]
    for e in edges:
        n = e["node"]
        if str(n["content"]["externalIds"].get("tmdbId")) != str(tmdb_id):
            continue
        for o in n["offers"]:
            m = shape.match(o.get("standardWebURL") or "")
            if pkg_ok(o["package"]["technicalName"]) and m:
                return url.format(m.group(1))
    return None


def previous_links(path):
    """TMDB key -> link from the list being replaced, so a JustWatch miss keeps yesterday's link."""
    try:
        with open(path) as f:
            old = json.load(f)
        return {i["tmdb"]: i["link"] for s in old["services"] for i in s["items"] if i.get("tmdb") and i.get("link")}
    except Exception:
        return {}


def manual_links():
    """links.json next to this script: title links added by hand, keyed like the list's "tmdb"."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "links.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def main():
    out = sys.argv[1]
    tok = token()
    trend = []
    for page in range(1, 6):
        trend += [x for x in api(tok, "/trending/all/week", page=page)["results"] if x.get("media_type") in ("tv", "movie")]

    def providers(x):
        try:
            r = api(tok, f"/{x['media_type']}/{x['id']}/watch/providers")["results"].get("IN", {})
            return x, {p["provider_id"] for p in r.get("flatrate", [])}
        except Exception:
            return x, set()

    with ThreadPoolExecutor(8) as ex:
        checked = list(ex.map(providers, trend))
    services = []
    for name, pid, pkg in SERVICES:
        items = []
        seen = set()                               # TMDB's trending pages can repeat a title
        for x, ps in checked:                      # trending order kept
            key = (x["media_type"], x["id"])
            if pid in ps and x.get("poster_path") and len(items) < PER_SERVICE and key not in seen:
                seen.add(key)
                items.append({"title": x.get("name") or x.get("title"), "poster": x["poster_path"],
                              "tmdb": f"{x['media_type']}/{x['id']}", "_id": x["id"]})
        services.append({"name": name, "pkg": pkg, "items": items})
    before = previous_links(out)
    manual = manual_links()

    def link(job):
        name, it = job
        try:
            return justwatch_link(name, it["title"], it["_id"])
        except Exception:
            return None
    jobs = [(s["name"], it) for s in services for it in s["items"]]
    with ThreadPoolExecutor(4) as ex:
        for (name, it), l in zip(jobs, ex.map(link, jobs)):
            for l in (manual.get(it["tmdb"]), l, before.get(it["tmdb"])):
                if l and LINKS[name][1].match(l):
                    it["link"] = l
                    break
            del it["_id"]
    data = {"updated": datetime.date.today().isoformat(), "services": services}
    if any(len(s["items"]) == 0 for s in services):
        sys.exit("a service came back empty; not writing (the TV keeps its last good list)")
    with open(out, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for s in services:
        print(s["name"], [i["title"] for i in s["items"]])
    found = sum(1 for s in services for i in s["items"] if i.get("link"))
    print(f"links: {found} of {sum(len(s['items']) for s in services)}")


if __name__ == "__main__":
    main()
