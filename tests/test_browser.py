"""The browser's single-thread runner and quiet frame updates, without starting a real browser."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest

os.environ.setdefault("OMNI_STATE", tempfile.mkdtemp(prefix="omni-state-"))

from omni.browser import Browser                     # noqa: E402


class FakePage:
    def __init__(self):
        self.url, self._title, self.viewport_size = "https://a.example/", "A", {"width": 1280, "height": 860}

    def is_closed(self):
        return False

    def screenshot(self, **k):
        return b"\xff\xd8"

    def title(self):
        return self._title


class TestBrowserThread(unittest.TestCase):
    def test_all_calls_run_on_one_thread_and_nested_calls_dont_deadlock(self):
        b = Browser(headless=True)
        seen = []

        def inner():
            seen.append(threading.get_ident())
            return "inner"

        def outer():
            seen.append(threading.get_ident())
            return b.run(inner)               # a tool body calling back into run() must not deadlock
        results = [b.run(outer, timeout=5) for _ in range(3)]
        out = []
        t = threading.Thread(target=lambda: out.append(b.run(lambda: threading.get_ident(), timeout=5)))
        t.start()
        t.join()
        self.assertEqual(results, ["inner"] * 3)
        self.assertEqual(len(set(seen + out)), 1)
        self.assertNotEqual(seen[0], threading.get_ident())

    def test_quiet_capture_only_notifies_on_page_change(self):
        b = Browser(headless=True)
        b.page = FakePage()
        b.ctx = object()
        events = []
        b.on_frame.append(lambda seq, info: events.append((seq, info["url"])))
        b.capture()                       # an agent action: always tells the panel
        b.capture(quiet=True)             # background refresh, same page: silent
        self.assertEqual(len(events), 1)
        b.page.url = "https://b.example/"
        b.capture(quiet=True)             # the page changed by itself: tell the panel
        self.assertEqual([u for _, u in events], ["https://a.example/", "https://b.example/"])
        self.assertEqual(b.frame_seq, 3)


if __name__ == "__main__":
    unittest.main()


class TestRealPageWidgets(unittest.TestCase):
    """Styled dropdowns and CAPTCHA fields in a real headless browser (skipped if Playwright/Edge is missing)."""

    HTML = """<html><body>
      <select id=buyer_category style="display:none"><option>--Select--</option><option value=7>Drill Collars</option>
        <option value=9>Office Chairs</option></select>
      <span class="select2-container"><span class="selection"><span role=combobox tabindex=0
        aria-labelledby="select2-buyer_category-container"><span id="select2-buyer_category-container">--Select--</span>
      </span></span></span>
      <div role=combobox id=plain tabindex=0 onclick="document.getElementById('lb').style.display='block'">Pick</div>
      <ul id=lb role=listbox style="display:none"><li role=option onclick="document.getElementById('plain').textContent=this.textContent;
        document.getElementById('lb').style.display='none'">Red</li><li role=option>Blue</li></ul>
      <input id=captcha_entered1 placeholder="Enter captcha">
    </body></html>"""

    def setUp(self):
        try:
            from omni import browser, builtin
            from omni.core import Agent, Config
            self.a = Agent(Config(workspace=tempfile.mkdtemp(), rules="", mode="yolo", browser_headless=True),
                           ui_input=lambda p: "n", ui_print=lambda *x: None,
                           chat_fn=lambda p: {"message": {"content": "ok"}}, modules=[builtin, browser])
            b = self.a.browser
            b.run(lambda: (b._ensure().goto("about:blank"), b.page.set_content(self.HTML)))
            self.snap = b.snapshot()
        except Exception as e:                       # no Playwright / no Edge on this machine
            self.skipTest(f"no browser: {e}")

    def tearDown(self):
        self.a.close()

    def ref(self, text):
        line = next(l for l in self.snap.splitlines() if text in l)
        return int(line[1:line.index("]")])

    def test_select_on_select2_sets_the_hidden_select(self):
        out, err = self.a.execute("browser_select", {"ref": self.ref('combobox "--Select--"'), "option": "office"})
        self.assertFalse(err, out)
        b = self.a.browser
        self.assertEqual(b.run(lambda: b.page.eval_on_selector("#buyer_category", "e => e.value")), "9")

    def test_select_lists_options_when_not_found(self):
        out, err = self.a.execute("browser_select", {"ref": self.ref('combobox "--Select--"'), "option": "sofa"})
        self.assertTrue(err)
        self.assertIn("Drill Collars", out)

    def test_select_on_aria_combobox_clicks_the_option(self):
        out, err = self.a.execute("browser_select", {"ref": self.ref('"Pick"'), "option": "red"})
        self.assertFalse(err, out)
        b = self.a.browser
        self.assertEqual(b.run(lambda: b.page.inner_text("#plain")), "Red")

    def test_captcha_fields_are_flagged_and_refused(self):
        self.assertIn("CAPTCHA", self.snap)
        r = self.ref("captcha")
        for tool, args in (("browser_type", {"text": "x"}), ("browser_set_value", {"value": "x"})):
            out, err = self.a.execute(tool, {"ref": r, **args})
            self.assertTrue(err)
            self.assertIn("does not solve or fill CAPTCHAs", out)
