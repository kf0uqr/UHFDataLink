#!/usr/bin/env python3
"""UHFLink home server: answers AX.25 UI requests (via Direwolf KISS/TCP)
with weather and RSS data. Stdlib only, Python 3.11+. See docs/PROTOCOL.md."""

import argparse
import hashlib
import json
import logging
import re
import socket
import threading
import time
import tomllib
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

log = logging.getLogger("uhflink")

MAX_CHUNKS = 15
HDR_LEN = 5  # R + rr + i + t


# ---------------------------------------------------------------- AX.25/KISS

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


def _addr(call: str, last: bool) -> bytes:
    base, _, ssid = call.upper().partition("-")
    if not base or len(base) > 6:
        raise ValueError(f"bad callsign {call!r}")
    b = bytearray((ord(c) << 1) for c in base.ljust(6))
    b.append(0x60 | ((int(ssid or 0) & 0x0F) << 1) | (1 if last else 0))
    return bytes(b)


def _unaddr(b: bytes) -> str:
    base = "".join(chr(c >> 1) for c in b[:6]).strip()
    ssid = (b[6] >> 1) & 0x0F
    return f"{base}-{ssid}" if ssid else base


def ax25_ui(dest: str, src: str, info: bytes) -> bytes:
    return _addr(dest, False) + _addr(src, True) + b"\x03\xf0" + info


def ax25_parse(frame: bytes):
    """Return (dest, src, info) for a UI frame, else None."""
    if len(frame) < 16:
        return None
    addrs, i = [], 0
    while i + 7 <= len(frame) and len(addrs) < 10:
        addrs.append(frame[i:i + 7])
        i += 7
        if frame[i - 1] & 1:
            break
    else:
        return None
    if len(addrs) < 2 or frame[i:i + 2] != b"\x03\xf0":
        return None
    return _unaddr(addrs[0]), _unaddr(addrs[1]), frame[i + 2:]


def kiss_encode(frame: bytes, port: int = 0) -> bytes:
    out = bytearray([FEND, (port << 4) & 0xF0])
    for c in frame:
        if c == FEND:
            out += bytes([FESC, TFEND])
        elif c == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(c)
    out.append(FEND)
    return bytes(out)


class KissDecoder:
    def __init__(self):
        self.buf, self.esc, self.active = bytearray(), False, False

    def feed(self, data: bytes):
        for c in data:
            if c == FEND:
                if self.active and len(self.buf) > 1 and (self.buf[0] & 0x0F) == 0:
                    yield bytes(self.buf[1:])
                self.buf.clear()
                self.esc, self.active = False, True
            elif not self.active:
                continue
            elif self.esc:
                self.buf.append({TFEND: FEND, TFESC: FESC}.get(c, c))
                self.esc = False
            elif c == FESC:
                self.esc = True
            else:
                self.buf.append(c)
            if len(self.buf) > 600:  # runaway frame
                self.buf.clear()
                self.active = False


# ------------------------------------------------------------- text helpers

_SUBS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
         "—": "-", "…": "...", " ": " ", "•": "*", "°": "deg"}


def to_ascii(s: str) -> str:
    s = "".join(_SUBS.get(c, c) for c in s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[ \t\r\n\f\v]+", " ", s).strip()


def shorten(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n - 3]
    sp = cut.rfind(" ")
    if sp > n // 2:
        cut = cut[:sp]
    return cut.rstrip() + "..."


class _Strip(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "ul", "ol", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def strip_html(s: str) -> str:
    p = _Strip()
    try:
        p.feed(s)
        p.close()
    except Exception:  # malformed markup: fall back to a crude strip
        return to_ascii(re.sub(r"<[^>]*>", " ", s))
    return to_ascii("".join(p.parts))


def chunk_text(text: str, size: int) -> list[str]:
    """Split so that ''.join(chunks) == text, preferring LF boundaries."""
    out = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        cut = cut + 1 if cut >= size // 2 else size
        out.append(text[:cut])
        text = text[cut:]
    return out


# ------------------------------------------------------------------ weather

def _dig(obj, path: str):
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


class Weather:
    def __init__(self, cfg: dict):
        self.cfg, self.cached, self.at = cfg, "", 0.0
        self.lock = threading.Lock()

    def get(self) -> str:
        with self.lock:
            if self.cached and time.time() - self.at < self.cfg.get("cache_ttl", 300):
                return self.cached
            req = urllib.request.Request(self.cfg["url"], headers={"User-Agent": "uhflink"})
            with urllib.request.urlopen(req, timeout=self.cfg.get("timeout", 8)) as r:
                data = json.load(r)
            lines = ["WX " + time.strftime("%H%M")]
            for f in self.cfg.get("field", []):
                try:
                    v = _dig(data, f["path"])
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                if isinstance(v, float):
                    v = f"{v:.1f}".rstrip("0").rstrip(".")
                lines.append(to_ascii(f"{f['label']} {v}{f.get('suffix', '')}"))
            self.cached, self.at = "\n".join(lines), time.time()
            return self.cached


# ---------------------------------------------------------------------- RSS

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def parse_feed(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for it in list(root.iter("item")) + list(root.iter(ATOM + "entry")):
        def t(*names):
            for n in names:
                e = it.find(n)
                if e is not None and (e.text or "").strip():
                    return e.text
            return ""
        title = strip_html(t("title", ATOM + "title"))
        link = t("link")
        if not link:
            le = it.find(ATOM + "link")
            link = le.get("href", "") if le is not None else ""
        uid = t("guid", ATOM + "id") or link or title
        body = t("description", CONTENT, ATOM + "summary", ATOM + "content")
        date = t("pubDate", ATOM + "updated", ATOM + "published")
        try:
            when = parsedate_to_datetime(date).strftime("%a %d %b %H:%M")
        except (TypeError, ValueError):
            when = to_ascii(date)[:16]
        items.append({"id": hashlib.sha1(uid.encode()).hexdigest()[:4], "title": title,
                      "summary": strip_html(body), "when": when})
    return items


class Feeds:
    def __init__(self, feeds: list[dict], cfg: dict):
        self.feeds = {f["id"]: f for f in feeds}
        self.cfg = cfg
        self.items: dict[str, list[dict]] = {}
        self.lock = threading.Lock()

    def refresh(self, fid: str):
        req = urllib.request.Request(self.feeds[fid]["url"], headers={"User-Agent": "uhflink"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(2_000_000)
        items = parse_feed(data)
        with self.lock:
            self.items[fid] = items
        log.info("feed %s: %d items", fid, len(items))

    def refresh_all(self):
        for fid in self.feeds:
            try:
                self.refresh(fid)
            except Exception as e:
                log.warning("feed %s refresh failed: %s", fid, e)

    def loop(self):
        while True:
            self.refresh_all()
            time.sleep(self.cfg.get("refresh", 600))

    def listing(self) -> str:
        return "NF\n" + "\n".join(f"{f['id']} {to_ascii(f['name'])}" for f in self.feeds.values())

    def page(self, fid: str, page: int) -> str:
        per = self.cfg.get("per_page", 10)
        with self.lock:
            items = self.items.get(fid)
        if not items:
            raise LookupError("empty")
        pages = (len(items) + per - 1) // per
        if not 1 <= page <= pages:
            raise KeyError("page")
        lines = [f"NL {fid} {page}/{pages}"]
        for it in items[(page - 1) * per: page * per]:
            lines.append(f"{it['id']} {shorten(it['title'], self.cfg.get('title_len', 70))}")
        return "\n".join(lines)

    def summary(self, fid: str, iid: str) -> str:
        with self.lock:
            items = self.items.get(fid, [])
        for it in items:
            if it["id"] == iid:
                body = shorten(it["summary"], self.cfg.get("summary_len", 600)) or "(no summary)"
                return f"NS {fid} {iid}\n{shorten(it['title'], 120)}\n{it['when']}\n{body}"
        raise KeyError("item")


# ------------------------------------------------------------ protocol core

class Server:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.call = cfg["station"]["callsign"].upper()
        self.tocall = cfg["station"].get("tocall", "UHFLNK").upper()
        self.allow = {c.upper() for c in cfg["station"].get("allow", [])}
        link = cfg.get("link", {})
        self.max_payload = link.get("max_info", 200) - HDR_LEN
        self.ttl = link.get("cache_ttl", 600)
        self.min_interval = link.get("min_interval", 3.0)
        self.weather = Weather(cfg.get("weather", {}))
        self.feeds = Feeds(cfg.get("feeds", []), cfg.get("rss", {}))
        self.cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self.last_q: dict[str, float] = {}

    def _frames(self, rid: str, text: str) -> list[str]:
        text = text[:MAX_CHUNKS * self.max_payload]
        chunks = chunk_text(text, self.max_payload)
        return [f"R{rid}{i:X}{len(chunks):X}{c}" for i, c in enumerate(chunks)]

    def _err(self, rid: str, code: int, msg: str) -> list[str]:
        return [f"R{rid}01!{code} {msg}"]

    def _dispatch(self, cmd: str, args: list[str]) -> str:
        if cmd == "PING":
            return "PONG"
        if cmd == "WX":
            return self.weather.get()
        if cmd == "NF":
            return self.feeds.listing()
        if cmd == "NL" and 1 <= len(args) <= 2:
            return self.feeds.page(args[0], int(args[1]) if len(args) > 1 else 1)
        if cmd == "NS" and len(args) == 2:
            return self.feeds.summary(args[0], args[1])
        raise ValueError(cmd)

    def handle(self, src: str, info: str, now: float | None = None) -> list[str]:
        """Process one request info string; return reply info strings."""
        now = time.time() if now is None else now
        self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
        m = re.fullmatch(r"([QT])([0-9A-Fa-f]{2}) ?(.*)", info.strip())
        if not m:
            return []  # not ours; stay silent rather than spam the channel
        kind, rid, rest = m.group(1), m.group(2).upper(), m.group(3)
        key = (src, rid)

        if kind == "T":
            hit = self.cache.get(key)
            if not hit:
                return self._err(rid, 410, "expired")
            try:
                mask = int(rest.strip(), 16)
            except ValueError:
                return self._err(rid, 400, "bad mask")
            return [f for i, f in enumerate(hit[1]) if mask >> i & 1]

        if key in self.cache:  # repeated Q: resend everything, no upstream hit
            return self.cache[key][1]
        if now - self.last_q.get(src, -1e9) < self.min_interval:
            return self._err(rid, 429, "slow down")
        self.last_q[src] = now

        parts = rest.split()
        if not parts:
            return self._err(rid, 400, "empty")
        try:
            text = self._dispatch(parts[0].upper(), parts[1:])
        except ValueError:
            return self._err(rid, 400, "bad request")
        except (KeyError, LookupError):
            return self._err(rid, 404, "not found")
        except Exception as e:
            log.warning("upstream error for %s %s: %s", src, rest, e)
            return self._err(rid, 502, "upstream")
        frames = self._frames(rid, text)
        self.cache[key] = (now + self.ttl, frames)
        return frames

    # ----------------------------------------------------------- KISS I/O
    def run(self):
        threading.Thread(target=self.feeds.loop, daemon=True).start()
        k, link = self.cfg["kiss"], self.cfg.get("link", {})
        while True:
            try:
                with socket.create_connection((k["host"], k["port"])) as s:
                    log.info("connected to KISS %s:%s as %s", k["host"], k["port"], self.call)
                    dec = KissDecoder()
                    while data := s.recv(4096):
                        for fr in dec.feed(data):
                            self._on_frame(s, fr, link)
            except OSError as e:
                log.warning("KISS link: %s; retrying in 5 s", e)
            time.sleep(5)

    def _on_frame(self, s, frame: bytes, link: dict):
        p = ax25_parse(frame)
        if not p or p[0] != self.tocall:
            return
        _, src, info = p
        if self.allow and src not in self.allow:
            log.info("ignored %s (not in allow list)", src)
            return
        text = info.decode("ascii", "replace")
        log.info("%s: %s", src, text)
        replies = self.handle(src, text)
        if replies:
            time.sleep(link.get("reply_delay", 0.6))
        for r in replies:
            s.sendall(kiss_encode(ax25_ui(src, self.call, r.encode("ascii", "replace"))))
            time.sleep(link.get("chunk_gap", 0.0))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("--simulate", metavar="INFO", help='run one request offline, e.g. "Q01 WX"')
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(message)s")
    with open(a.config, "rb") as f:
        srv = Server(tomllib.load(f))
    if a.simulate:
        srv.feeds.refresh_all()
        srv.min_interval = 0
        for fr in srv.handle("SIM-0", a.simulate):
            print(f"[{len(fr):3d}B] {fr!r}")
        return
    srv.run()


if __name__ == "__main__":
    main()
