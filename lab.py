#!/usr/bin/env python3
"""Type into a web page using the phone's REAL on-screen keyboard.

Almost every mobile test tool injects text: Espresso's injectString, Maestro and
most agents shelling out to `adb shell input text`, Appium's own IME replacement.
That is fast and deterministic, and it means autocorrect, auto-capitalisation and
predictive text never run. Appium's docs say so plainly about its unicodeKeyboard:

    "any business logic triggered by keyboard input will therefore not be tested."

This types by tapping the keys. Gboard exposes no keys to the accessibility tree,
so the script finds the key faces in a screenshot, derives a qwerty grid from the
row geometry, and issues `adb shell input tap` per character. Characters then pass
through the keyboard's own engine, so what lands in the field is what a thumb would
have produced.

    python3 lab.py --url https://example.com/signup --selector "#email" --text "ada lovelace"

It self-calibrates before trusting the map (types "a", Shift+"b", reads the field
back, deletes them) and falls back to injection if the layout does not match, which
it reports rather than hiding.

Requires: adb on PATH, a booted device/emulator whose IME is a real keyboard, and
Pillow. Android only for the key-tapping; iOS support here is limited to the
accessibility tree.
"""
import argparse, json, os, re, subprocess, time
import xml.etree.ElementTree as ET

T0 = time.time()


def sh(cmd, timeout=90, binary=False):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, timeout=timeout)
    return r.stdout if binary else r.stdout.decode(errors="replace")


class Node:
    def __init__(self, label, text, rid, cls, pkg, b):
        self.label, self.text = (label or "").strip(), (text or "").strip()
        self.rid, self.cls, self.pkg = rid or "", cls or "", pkg or ""
        self.x1, self.y1, self.x2, self.y2 = b

    cx = property(lambda s: (s.x1 + s.x2) // 2)
    cy = property(lambda s: (s.y1 + s.y2) // 2)

    def names(self):
        return [self.label.lower(), self.text.lower()]

    def is_key(self):
        return self.pkg == "key"

    def __repr__(self):
        return f"<{self.cls.rsplit('.', 1)[-1]} {self.label!r}/{self.text!r} {self.rid} @{self.cx},{self.cy}>"


class Android:
    kind = "android"
    symbol_rows = ["1234567890", "@#$_&-+()/", "*\"':;!?"]

    def __init__(self):
        sh("adb wait-for-device", timeout=120)
        sh("adb shell settings put secure show_ime_with_hard_keyboard 1")  # emulator has a "hardware" keyboard

    def raw_dump(self):
        return sh("adb exec-out uiautomator dump /dev/tty", timeout=40)

    events = []

    def nodes(self, _retry=False):
        for _ in range(5):
            out = self.raw_dump()
            i, j = out.find("<?xml"), out.rfind("</hierarchy>")
            if i >= 0 and j > i:
                try:
                    res = []
                    for n in ET.fromstring(out[i:j + len("</hierarchy>")]).iter("node"):
                        b = re.findall(r"\d+", n.get("bounds", ""))
                        if len(b) != 4:
                            continue
                        pkg = n.get("package", "")
                        res.append(Node(n.get("content-desc"), n.get("text"), n.get("resource-id"), n.get("class"),
                                        "key" if ("inputmethod" in pkg or "keyboard" in pkg) else pkg, tuple(map(int, b))))
                except ET.ParseError:
                    time.sleep(1)
                    continue
                anr = [n for n in res if "isn't responding" in n.text.lower() or "keeps stopping" in n.text.lower()]
                if anr and not _retry:  # emulator hiccup, not the app: press Wait and look again
                    btn = next((n for n in res if n.text.lower() in ("wait", "close app", "ok")), None)
                    self.events.append({"anr": anr[0].text, "pressed": btn.text if btn else None})
                    if btn:
                        self.tap(btn.cx, btn.cy)
                        time.sleep(1.5)
                        return self.nodes(_retry=True)
                return res
            time.sleep(1)
        return []

    def clear_field(self, n):
        sh("adb shell input keyevent 123")  # MOVE_END
        for _ in range(n):
            sh("adb shell input keyevent 67")

    def tap(self, x, y):
        sh(f"adb shell input tap {x} {y}")

    def screenshot(self, path):
        with open(path, "wb") as f:
            f.write(sh("adb exec-out screencap -p", timeout=40, binary=True))

    def ime_shown(self):
        return "mInputShown=true" in sh("adb shell dumpsys input_method")

    def hide_keyboard(self):  # BACK closes the IME first; only called when it is shown
        sh("adb shell input keyevent 4")

    def inject(self, text):
        sh(["adb", "shell", "input", "text", text.replace(" ", "%s")])

    def open_url(self, url):
        sh(["adb", "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url])

    def swipe(self, x, y1, y2):
        sh(f"adb shell input swipe {x} {y1} {x} {y2} 400")

    def back(self):
        sh("adb shell input keyevent 4")

    def info(self):
        return {"android": sh("adb shell getprop ro.build.version.release").strip(),
                "chrome": sh("adb shell dumpsys package com.android.chrome | grep versionName | head -1").strip(),
                "ime": sh("adb shell settings get secure default_input_method").strip(),
                "screen": sh("adb shell wm size").strip()}


class IOS:
    kind = "ios"
    symbol_rows = ["1234567890", "-/:;()$&@\"", ".,?!'"]
    scale = 3.0  # screenshot px per point, refined from the first screenshot

    def __init__(self, udid):
        self.u = udid

    def _idb(self, *a, timeout=90):
        return sh(["idb", *a, "--udid", self.u], timeout=timeout)

    def raw_dump(self):
        return self._idb("ui", "describe-all", "--json")

    events = []

    def nodes(self, _retry=False):
        out = self.raw_dump()
        try:
            items = json.loads(out)
        except ValueError:
            items = []
            for line in out.splitlines():
                try:
                    items.append(json.loads(line))
                except ValueError:
                    pass
        if isinstance(items, dict):
            items = [items]
        labels = [(e.get("AXLabel") or "") for e in items]
        sheet = any(l == "dismiss popup" for l in labels) or any("personalized suggestions" in l for l in labels)
        if not _retry and sheet:
            for e in items:
                f = e.get("frame") or {}
                if (e.get("AXLabel") or "") in ("Close", "Continue") and (e.get("type") == "Button") and f:
                    self.events.append({"popup": f"safari sheet dismissed via {e.get('AXLabel')}"})
                    self.tap(int(f["x"] + f["width"] / 2), int(f["y"] + f["height"] / 2))
                    time.sleep(1.2)
                    return self.nodes(_retry=True)
        res = []
        for e in items:
            f = e.get("frame") or {}
            try:
                x, y, w, h = (int(float(f[k])) for k in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            t = e.get("type") or ""
            val = e.get("AXValue")
            res.append(Node(e.get("AXLabel"), val if isinstance(val, str) else "", e.get("AXUniqueId") or "", t,
                            "key" if t == "Key" else "page", (x, y, x + w, y + h)))
        self._last_nodes = res
        return res

    def tap(self, x, y):  # accessibility coords are points; anything larger than the screen is pixels
        if x > 1000 or y > 1000:
            x, y = x / self.scale, y / self.scale
        self._idb("ui", "tap", str(int(x)), str(int(y)))

    def screenshot(self, path):
        sh(["xcrun", "simctl", "io", self.u, "screenshot", path], timeout=60)
        try:
            from PIL import Image
            w = Image.open(path).size[0]
            app = next((n for n in self._last_nodes if n.cls == "Application"), None)
            if app and app.x2 > 0:
                self.scale = w / app.x2
        except Exception:  # noqa: BLE001
            pass

    _last_nodes = []

    def ime_shown(self):  # describe-all lists no Key elements on iOS 26; look at the pixels instead
        p = "/tmp/_ios_kb.png"
        self.screenshot(p)
        keys, _ = keys_from_screenshot(p, "letters")
        return bool(keys)

    def hide_keyboard(self):  # tap the page background just under the status bar
        self._idb("ui", "tap", "20", "70")

    def inject(self, text):
        self._idb("ui", "text", text)

    def clear_field(self, n):
        for _ in range(n):
            self._idb("ui", "key", "42")  # HID backspace

    def open_url(self, url):
        sh(["xcrun", "simctl", "openurl", self.u, url])

    def swipe(self, x, y1, y2):
        self._idb("ui", "swipe", str(x), str(y1), str(x), str(y2))

    def back(self):
        pass

    def info(self):
        return {"ios": sh(["xcrun", "simctl", "list", "devices", "booted"]).strip()[-200:]}


# ---------------------------------------------------------------- screenshot key finder
LETTER_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
SYMBOL_ROWS = ["1234567890", "@#$_&-+()/", "*\"':;!?"]


def keys_from_screenshot(path, layer, symbol_rows=None):
    """Locate Gboard's white key faces in the bottom part of a screenshot and map them to
    characters by row. Returns {char: (x, y)} plus 'shift', 'delete', 'toggle', ' ', '.'."""
    from PIL import Image
    im = Image.open(path).convert("L")
    W, H = im.size
    px = im.load()
    bands, cur = [], None
    for y in range(int(H * 0.4), H):
        frac = sum(1 for x in range(0, W, 4) if px[x, y] >= 250) / (W / 4)
        if frac > 0.2:
            cur = [y, y] if cur is None else [cur[0], y]
        else:
            if cur and cur[1] - cur[0] > 30:
                bands.append(tuple(cur))
            cur = None
    if cur and cur[1] - cur[0] > 30:
        bands.append(tuple(cur))
    rows = []
    for a, b in bands:  # union of several scanlines so key glyphs don't split a key face
        ys = [a + int((b - a) * f) for f in (0.12, 0.3, 0.5, 0.7, 0.88)]
        white = [any(px[x, y] >= 250 for y in ys) for x in range(W)]
        blobs, start = [], None
        for x in range(W + 1):
            w = x < W and white[x]
            if w and start is None:
                start = x
            elif not w and start is not None:
                if x - start > 12:
                    blobs.append(((start + x) // 2, start, x))
                start = None
        if blobs and max(bl[2] - bl[1] for bl in blobs) < 0.6 * W:
            rows.append(((a + b) // 2, blobs))
    letter_rows = [r for r in rows if len(r[1]) >= 6][-3:]
    if len(letter_rows) < 3:
        return {}, {"bands": len(bands), "rows": [(y, len(bl)) for y, bl in rows]}
    (y1, r1), (y2, r2), (y3, r3) = letter_rows
    xs1 = [b[0] for b in r1]
    pitch = (xs1[-1] - xs1[0]) / max(len(xs1) - 1, 1)
    if len(xs1) != 10:  # derive a 10-key row from the outer edges
        pitch = (r1[-1][2] - r1[0][1]) / 10
        xs1 = [int(r1[0][1] + pitch * (i + 0.5)) for i in range(10)]
    xs2 = [b[0] for b in r2] if len(r2) == 9 else [int(xs1[i] + pitch / 2) for i in range(9)]
    xs3 = [b[0] for b in r3] if len(r3) == 7 else [int(xs1[i + 1] + pitch / 2) for i in range(7)]
    names = LETTER_ROWS if layer == "letters" else (symbol_rows or SYMBOL_ROWS)
    keys = {}
    for chars, xs, y in ((names[0], xs1, y1), (names[1], xs2, y2), (names[2], xs3, y3)):
        for ch, x in zip(chars, xs):
            keys[ch] = (x, y)
    keys["shift"] = (int(xs3[0] - pitch), y3)
    keys["delete"] = (int(xs3[-1] + pitch), y3)
    below = [r for r in rows if r[0] > y3 and any(0.2 * W < bl[2] - bl[1] < 0.6 * W for bl in r[1])]
    if below:
        y4, r4 = below[0]
        space = max(r4, key=lambda bl: bl[2] - bl[1])
        keys[" "] = (space[0], y4)
        keys["."] = (int(space[2] + pitch * 0.55), y4)
        keys["toggle"] = (keys["shift"][0], y4)
    else:
        y4 = int(y3 + (y3 - y2))
        keys[" "] = (W // 2, y4)
        keys["."] = (int(xs1[7] + pitch / 2), y4)
        keys["toggle"] = (keys["shift"][0], y4)
    return keys, {"pitch": round(pitch, 1), "rows": [len(r1), len(r2), len(r3)], "ime_top": letter_rows[0][0] - 60}



ALIASES = {" ": ["space"], "@": ["@", "at"], ".": [".", "period", "full stop", "dot"],
           "-": ["-", "dash", "hyphen", "minus"], "+": ["+", "plus"], "_": ["_", "underscore"]}
SHIFT = ["shift"]
SYMBOLS = ["?123", "symbols", "symbol keyboard", "more", "numbers", "123", "numbers and symbols"]
LETTERS = ["abc", "letters", "letter keyboard"]
DISMISS = ["accept & continue", "use without an account", "no thanks", "no, thanks", "not now",
           "got it", "skip", "wait", "close app"]
FIELD_CLASSES = ("EditText", "TextField", "SecureTextField")


def match(n, cands):
    for x in n.names():
        for c in cands:
            if x == c or x.startswith(c + " "):
                return True
    return False


class Lab:
    """Open a URL, put text into one field using the real keyboard, report what landed."""

    def __init__(self, dev, url, out):
        self.dev, self.url, self.out = dev, url, out
        os.makedirs(out, exist_ok=True)
        self.n, self.steps, self._keys = 0, [], None
        self.kb_mode, self.layer, self.geo, self.geo_meta = None, "letters", {}, {}
        self.H, self.W, self._field_sel = 2000, 1080, None

    def log(self, **kw):
        kw["t"] = round(time.time() - T0, 1)
        self.steps.append(kw)
        print(json.dumps(kw, ensure_ascii=False), flush=True)

    def shot(self, name):
        self.n += 1
        f = f"{self.n:02d}-{name}.png"
        try:
            self.dev.screenshot(os.path.join(self.out, f))
        except Exception as e:  # noqa: BLE001
            f = f"{f} (failed: {e!r})"
        return f

    def nodes(self):
        ns = self.dev.nodes()
        if ns:
            self.H = max(self.H, max(n.y2 for n in ns))
            self.W = max(n.x2 for n in ns)
        return ns

    def find(self, pred, nodes=None):
        for n in (self.nodes() if nodes is None else nodes):
            if pred(n):
                return n

    def find_text(self, texts, nodes=None):
        t = [x.lower() for x in texts]
        return self.find(lambda n: not n.is_key() and match(n, t), nodes)

    def first_field(self, nodes=None):
        """The page's text box. Browsers expose it as an EditText; the URL bar is excluded."""
        ns = self.nodes() if nodes is None else nodes
        for n in ns:
            if not n.is_key() and n.cls.endswith(FIELD_CLASSES) and "url_bar" not in n.rid:
                return n

    def value(self):
        n = self.first_field()
        return n.text if n else None

    def tap(self, n, wait=0.8):
        self.dev.tap(n.cx, n.cy)
        time.sleep(wait)

    def wait_for(self, pred, secs=60, dismiss=False):
        end = time.time() + secs
        while time.time() < end:
            ns = self.nodes()
            n = self.find(pred, ns)
            if n:
                return n
            if dismiss:
                d = self.find_text(DISMISS, ns)
                if d:
                    self.log(action="dismiss", node=repr(d))
                    self.tap(d, 1.5)
                    continue
            time.sleep(2)

    # ---- the real keyboard
    def keys(self, fresh=False):
        if fresh or self._keys is None:
            self._keys = [n for n in self.nodes() if n.is_key()]
        return self._keys

    def find_key(self, cands):
        for fresh in (False, True):
            k = self.find(lambda n: match(n, cands), self.keys(fresh))
            if k:
                return k

    def geo_keys(self, fresh=False):
        if fresh or not self.geo:
            p = os.path.join(self.out, "_kb.png")
            for _ in range(4):  # the keyboard may still be animating in
                self.dev.screenshot(p)
                self.geo, self.geo_meta = keys_from_screenshot(p, self.layer, self.dev.symbol_rows)
                if self.geo:
                    break
                time.sleep(1.0)
        return self.geo

    def decide_mode(self):
        if self.kb_mode:
            return
        self.kb_mode = "a11y" if self.keys(True) else "geo"
        self.geo_keys(True)
        self.log(action="keyboard-mode", mode=self.kb_mode,
                 keys_in_tree=len(self.keys()), keys_by_geometry=len(self.geo))
        if self.kb_mode == "geo":
            self.calibrate()

    def calibrate(self):
        """Prove the key map on this layout before trusting it: type 'a', then Shift+'b',
        read the field back, delete both. Anything unexpected falls back to injection."""
        if not all(k in self.geo for k in ("a", "b", "shift", "delete")):
            self.kb_mode, why = "inject", "key map incomplete"
        else:
            before = self.value() or ""
            self.dev.tap(*self.geo["a"]); time.sleep(0.3)
            self.dev.tap(*self.geo["shift"]); time.sleep(0.3)
            self.dev.tap(*self.geo["b"]); time.sleep(0.5)
            got = (self.value() or "")[len(before):]
            for _ in range(len(got) or 2):
                self.dev.tap(*self.geo["delete"]); time.sleep(0.25)
            left = (self.value() or "")[len(before):]
            why = f"typed {got!r}, left {left!r}"
            if got.lower() != "ab" or left:
                self.kb_mode = "inject"
                self.dev.clear_field(len(self.value() or "") + 2)
        self.log(action="keyboard-calibration", mode=self.kb_mode, detail=why,
                 shot=self.shot("calibration"))

    def tap_key(self, ch):
        self.decide_mode()
        if self.kb_mode == "a11y":
            if ch.isalpha() and ch.isupper():
                s = self.find_key(SHIFT)
                if s:
                    self.tap(s, 0.3)
                    self._keys = None
            cands = ALIASES.get(ch, [ch.lower()])
            k = self.find_key(cands)
            if not k:
                layer = self.find_key(LETTERS if ch.isalpha() else SYMBOLS)
                if layer:
                    self.tap(layer, 0.4)
                    self._keys = None
                    k = self.find_key(cands)
            if not k:
                return False
            self.tap(k, 0.25)
            return True
        if self.kb_mode == "inject":
            return False
        want = "letters" if (ch.isalpha() or ch in " .") else "symbols"
        if want != self.layer and "toggle" in self.geo_keys():
            self.dev.tap(*self.geo["toggle"]); time.sleep(0.5)
            self.layer = want
            self.geo_keys(True)
        keys = self.geo_keys()
        pos = keys.get(ch.lower() if ch.isalpha() else ch)
        if not pos:
            return False
        # Keyboards auto-capitalise at the start of a field and after a space, so
        # pressing Shift there would turn the capital OFF. Guess, then verify.
        upper = ch.isalpha() and ch.isupper() and "shift" in keys
        if upper:
            cur = self.value() or ""
            if not (cur == "" or cur.endswith(" ")):
                self.dev.tap(*keys["shift"]); time.sleep(0.3)
        self.dev.tap(*pos); time.sleep(0.2)
        if upper:
            got = (self.value() or "")[-1:]
            if got and got != ch:
                self.shift_retries = getattr(self, "shift_retries", 0) + 1
                self.dev.tap(*keys["delete"]); time.sleep(0.3)
                self.dev.tap(*keys["shift"]); time.sleep(0.3)
                self.dev.tap(*pos); time.sleep(0.3)
        return True

    def type_text(self, s):
        typed = injected = ""
        for ch in s:
            if self.tap_key(ch):
                typed += ch
            else:
                self.dev.inject(ch)
                injected += ch
        return typed, injected

    def run(self, text):
        self.log(info=self.dev.info(), url=self.url, text=text)
        self.dev.open_url(self.url)
        time.sleep(6)
        field = self.wait_for(lambda x: self.first_field([x]), 90, dismiss=True)
        self.log(step="landing", found_field=bool(field), shot=self.shot("landing"))
        if not field:
            with open(os.path.join(self.out, "page-dump.txt"), "w") as f:
                f.write(self.dev.raw_dump())
            return
        before = self.first_field()
        self.tap(before, 1.2)
        ime = False
        for attempt in range(6):
            ime = self.dev.ime_shown()
            if ime and self.dev.kind == "android":
                self.layer, self.geo = "letters", {}
                ime = bool(self.geo_keys(True)) or self.kb_mode == "a11y"
            if ime:
                break
            time.sleep(0.7)
            if attempt in (1, 3):
                self.tap(before, 0.8)
        after = self.first_field()
        focused = self.shot("focused")
        typed, injected = self.type_text(text)
        time.sleep(1.0)
        got = self.value()
        self.log(
            step="typed",
            keyboard_opened_on_tap=ime,
            field_y_before_tap=before.cy,
            field_y_with_keyboard=(after.cy if after else None),
            keyboard_top=self.geo_meta.get("ime_top"),
            keyboard_mode=self.kb_mode,
            typed_via_keys=typed,
            injected=injected,
            expected=text,
            value=got,
            matches=(got == text) if got is not None else None,
            shot=[focused, self.shot("typed")],
        )
        if got is not None and got != text:
            self.log(
                finding="THE KEYBOARD CHANGED THE TEXT",
                expected=text, produced=got,
                note="autocorrect / auto-capitalisation / predictive text — an injected-text tool would never have seen this",
            )

    def write(self):
        with open(os.path.join(self.out, "report.json"), "w") as f:
            json.dump(self.steps, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Type into a web page with the device's real keyboard.")
    ap.add_argument("--url", required=True, help="page to open (its first text field is used)")
    ap.add_argument("--text", default="ada lovelace", help="text to type")
    ap.add_argument("--out", default="out", help="directory for screenshots and report.json")
    ap.add_argument("--platform", choices=["android", "ios"], default="android")
    ap.add_argument("--udid", help="iOS simulator udid")
    a = ap.parse_args()
    lab = Lab(Android() if a.platform == "android" else IOS(a.udid), a.url, a.out)
    try:
        lab.run(a.text)
    except Exception as e:  # noqa: BLE001 — always leave a report and screenshots behind
        lab.log(error=repr(e)[:400], shot=lab.shot("crash"))
    finally:
        lab.write()
