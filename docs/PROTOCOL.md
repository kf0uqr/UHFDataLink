# UHFLink protocol v1

Request/reply over AX.25 UI frames (1200 baud AFSK, simplex). Everything is
plain printable ASCII (plus LF) — no encryption, no obscuring, callsigns in the
AX.25 address field of every frame (Part 97.119 ID, 97.113 plain language).

Mobile ESP32 <-> Direwolf (home) via a KISS TCP port (8001).

## Framing

| Field    | Value                                                         |
|----------|---------------------------------------------------------------|
| AX.25    | UI frame, control `0x03`, PID `0xF0`, no digipeaters (simplex) |
| Dest     | `UHFLNK` (tocall). Server ignores frames to anything else     |
| Source   | The sender's real callsign-SSID (mobile `KF0UQR-9`, home `KF0UQR-1`) |
| Info     | <= 200 bytes, ASCII 0x20-0x7E and LF (0x0A). Never CR, never trailing space |

The mobile sends to dest `UHFLNK`; the server replies with dest = the mobile's
callsign-SSID and source = the server's callsign-SSID.

## Messages (info field)

### Request, mobile -> home

```
Q<rr> <CMD> [args]
```

`<rr>` = 2 hex digits request id, chosen by the mobile (just increment, wraps
at FF). Together with the source callsign it identifies the reply in the
server's cache.

| Request                | Meaning                                      |
|------------------------|----------------------------------------------|
| `Q01 PING`             | Link test; reply `PONG`                      |
| `Q02 WX`               | Current weather                              |
| `Q03 NF`               | List configured feeds (`id name`)            |
| `Q04 NL <feed> [page]` | Headline list page (1-based, default 1)      |
| `Q05 NS <feed> <id>`   | Summary of one item (`id` from the NL list)  |

### Reply, home -> mobile

Reply text is split into chunks. Each chunk is one frame:

```
R<rr><i><t><payload>
```

* `<rr>`  request id echoed
* `<i>`   chunk index, 1 hex digit, 0-based
* `<t>`   total chunk count, 1 hex digit (1..F, so max 15 chunks)
* payload up to 190 bytes (`max_info` 200 minus the 5-byte header)

The mobile concatenates payloads in index order. Chunks are split on LF when
there is one in the back half of the window, otherwise mid-line; the
concatenation is exact. Max reply is 15 x 190 = 2850 bytes (server truncates).

An error is a single chunk whose payload starts with `!`:

```
R<rr>01!<code> <message>
```

| Code | Meaning                         | Mobile action              |
|------|---------------------------------|----------------------------|
| 400  | Bad/unknown request             | Fix UI, don't retry        |
| 404  | Unknown feed or item            | Refresh list               |
| 410  | Retry for an expired request id | Send the original Q again  |
| 429  | Too many requests               | Wait, retry later          |
| 502  | Upstream (weather/RSS) failed   | Show message, retry later  |

### Selective retry, mobile -> home

```
T<rr> <mask>
```

`<mask>` = 4 hex digits, bit i set means "resend chunk i". Missing chunks 0
and 2 -> `0005`. The server resends exactly those chunks from its cache
(default TTL 10 min). If the cache entry is gone it answers `!410`.

### Mobile receive state machine

1. Send `Q`. Start timer: first chunk deadline = ~(request airtime + server
   delay + 1 chunk airtime + margin), about 8 s.
2. On the first chunk you learn `t`. Keep receiving until all `t` chunks are
   present or no frame arrives for ~4 s.
3. If chunks are missing: send `T` with the mask. Max 3 tries, then show
   "partial" with what you have.
4. If *no* chunk ever arrived: resend the same `Q<rr>` (same id). The server
   treats a repeated Q for a cached id as "resend everything", so this is
   idempotent and won't re-hit the upstream API.
5. Don't send a new request until the previous one finishes (half-duplex,
   you'd be keying over the reply).

## Payload formats

Lines separated by LF. Mobile renders text as-is (word-wrap at the display
width, about 40 columns at font size 2 on 480 px).

### WX

```
WX 1405
Temp 72F
Feels 70F
Wind 8 mph
Humidity 45%
```

Lines are `label value` pairs configured on the server, so the weather layout
changes without touching the firmware.

### NF

```
NF
hn Hacker News
ars Ars Technica
```

### NL (headline page)

```
NL hn 1/3
a3f9 Show HN: A tiny AFSK modem for ESP32
07bc Linux 7.1 released
...
```

* First line: `NL <feed> <page>/<pages>`.
* Then up to 10 lines `<id4> <title>`, id = 4 hex digits, stable for the item.
  Titles are cut to 70 characters. ~760 bytes -> about 5 chunks.

### NS (summary)

```
NS hn a3f9
Show HN: A tiny AFSK modem for ESP32
Wed 07 Oct 14:02
<summary text, HTML stripped, ASCII, <= 600 chars>
```

## Airtime budget

1200 baud AFSK ~ 150 bytes/s raw. A 200-byte frame with HDLC overhead
(flags, 16 header+addr bytes, FCS, bit-stuffing) plus TXDELAY (~300 ms) is
about 1.9 s. Headline list (5 chunks) ~ 10 s, weather (1 chunk) ~ 2 s,
summary (4 chunks) ~ 8 s. Keep TXDELAY low on the SA818 side but long enough
for the receiver squelch/AGC; tune with real hardware.

## Notes

* This is point-to-point, on-request data at the request of the control
  operator of the mobile station. It is not a broadcast; don't turn it into
  an unattended feed that anyone can tune in to.
* The server is not an IGate and never digipeats; it only answers frames
  addressed to `UHFLNK`. Keep Direwolf's `IGSERVER`/`DIGIPEAT` disabled.
