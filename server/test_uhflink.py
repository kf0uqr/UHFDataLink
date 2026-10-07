import os
import tempfile
import unittest

import uhflink_server as u

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Café &amp; “News”</title><guid>g1</guid>
<pubDate>Wed, 07 Oct 2026 14:02:00 +0000</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;script&gt;x()&lt;/script&gt;&lt;/p&gt;&lt;p&gt;Second&lt;/p&gt;</description></item>
%s
</channel></rss>"""


def make_server(tmp, n_items=25):
    extra = "".join(f"<item><title>Item number {i} " + "word " * 30 + f"</title><guid>x{i}</guid>"
                    f"<description>{'long text ' * 200}</description></item>" for i in range(n_items))
    path = os.path.join(tmp, "f.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(RSS % extra)
    cfg = {"station": {"callsign": "N0CALL-1"}, "kiss": {},
           "link": {"min_interval": 0},
           "feeds": [{"id": "t", "name": "Test", "url": "file://" + path}]}
    s = u.Server(cfg)
    s.feeds.refresh("t")
    return s


def reassemble(frames):
    t = int(frames[0][4], 16)
    got = {int(f[3], 16): f[5:] for f in frames}
    assert len(got) == t
    return "".join(got[i] for i in range(t))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = make_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ax25_kiss_roundtrip(self):
        info = b"Q01 WX\xc0\xdb"
        fr = u.ax25_ui("UHFLNK", "KF0UQR-9", info)
        out = list(u.KissDecoder().feed(u.kiss_encode(fr)))
        self.assertEqual(u.ax25_parse(out[0]), ("UHFLNK", "KF0UQR-9", info))

    def test_text_helpers(self):
        self.assertEqual(u.to_ascii("Café “q”—x"), 'Cafe "q"-x')
        self.assertEqual(u.strip_html("<p>a</p><script>z</script><b>b</b>&amp;"), "a b&")
        text = "line one\n" * 40
        ch = u.chunk_text(text, 190)
        self.assertEqual("".join(ch), text)
        self.assertTrue(all(len(c) <= 190 for c in ch))

    def test_ping_and_garbage(self):
        self.assertEqual(self.s.handle("A", "Q01 PING"), ["R01" + "0" + "1" + "PONG"])
        self.assertEqual(self.s.handle("A", "hello"), [])

    def test_headline_page_fits_budget(self):
        fr = self.s.handle("A", "Q02 NL t 1")
        text = reassemble(fr)
        lines = text.split("\n")
        self.assertEqual(lines[0], "NL t 1/3")
        self.assertEqual(len(lines) - 1, 10)
        self.assertLessEqual(len(text), 800)
        self.assertTrue(all(len(f) <= 200 for f in fr))
        self.assertTrue(text.isascii())

    def test_summary_and_ids(self):
        first = self.s.handle("A", "Q03 NL t 1")
        iid = reassemble(first).split("\n")[1][:4]
        text = reassemble(self.s.handle("A", f"Q04 NS t {iid}"))
        self.assertTrue(text.startswith(f"NS t {iid}"))
        self.assertLessEqual(len(text), 120 + 600 + 80)

    def test_html_item(self):
        self.assertEqual(self.s.feeds.items["t"][0]["summary"], "Hello world Second")
        self.assertEqual(self.s.feeds.items["t"][0]["title"], 'Cafe & "News"')

    def test_selective_retry_and_repeat(self):
        fr = self.s.handle("A", "Q05 NL t 1")
        self.assertGreater(len(fr), 2)
        self.assertEqual(self.s.handle("A", "T05 0005"), [fr[0], fr[2]])
        self.assertEqual(self.s.handle("A", "Q05 NL t 1"), fr)  # repeat = resend all
        self.assertEqual(self.s.handle("B", "T05 0001"), ["R05" + "01!410 expired"])

    def test_cache_expiry(self):
        self.s.handle("A", "Q06 PING", now=0)
        self.assertEqual(self.s.handle("A", "T06 0001", now=self.s.ttl + 1),
                         ["R0601!410 expired"])

    def test_errors(self):
        self.assertTrue(self.s.handle("A", "Q07 NL nope")[0].startswith("R0701!404"))
        self.assertTrue(self.s.handle("A", "Q08 NL t 99")[0].startswith("R0801!404"))
        self.assertTrue(self.s.handle("A", "Q09 BOGUS")[0].startswith("R0901!400"))
        self.assertTrue(self.s.handle("A", "Q0A NL t x")[0].startswith("R0A01!400"))

    def test_rate_limit(self):
        self.s.min_interval = 3
        self.assertTrue(self.s.handle("A", "Q10 PING", now=100)[0].endswith("PONG"))
        self.assertTrue(self.s.handle("A", "Q11 PING", now=101)[0].startswith("R1101!429"))
        self.assertTrue(self.s.handle("A", "Q12 PING", now=104)[0].endswith("PONG"))


if __name__ == "__main__":
    unittest.main()
