#!/usr/bin/env python3
"""Build the launcher's "Trending in India" file: the top 3 titles per service from TMDB's weekly trending
list, keeping only titles streaming (flatrate) on that service in India.

Usage: TMDB_TOKEN=... python3 build_trending.py <out.json>
       (or the token in ~/.tmdb_token). Standard library only, so it runs anywhere, the daily cloud job included.

Output (what the TV reads):
  {"updated": "2026-10-29", "services": [{"name": "Netflix", "pkg": "com.netflix.ninja",
     "items": [{"title": "Lupin", "poster": "/abc.jpg"}, ...]}, ...]}
Posters are fetched by the TV itself from https://image.tmdb.org/t/p/w185<poster>.
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SERVICES = [("Netflix", 8, "com.netflix.ninja"),
            ("Prime Video", 119, "com.amazon.amazonvideo.livingroom"),
            ("JioHotstar", 2336, "in.startv.hotstar")]
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
        for x, ps in checked:                      # trending order kept
            if pid in ps and x.get("poster_path") and len(items) < PER_SERVICE:
                items.append({"title": x.get("name") or x.get("title"), "poster": x["poster_path"]})
        services.append({"name": name, "pkg": pkg, "items": items})
    data = {"updated": datetime.date.today().isoformat(), "services": services}
    if any(len(s["items"]) == 0 for s in services):
        sys.exit("a service came back empty; not writing (the TV keeps its last good list)")
    with open(out, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for s in services:
        print(s["name"], [i["title"] for i in s["items"]])


if __name__ == "__main__":
    main()
