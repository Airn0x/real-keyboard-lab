# real-keyboard-lab

**Type into a web page with the phone's actual on-screen keyboard, in CI, for free.**

Almost every mobile test tool *injects* text. Espresso calls `injectString`. Maestro and most
agents shell out to `adb shell input text`. Appium can install its own IME. That is fast and
deterministic — and it means **autocorrect, auto-capitalisation and predictive text never run.**

Appium's own documentation says so, about its `unicodeKeyboard` capability:

> "any business logic triggered by keyboard input will therefore not be tested."

So a whole class of bug is invisible to the standard toolchain: the ones your users hit because
their keyboard changed what they typed.

This types by **tapping the keys**.

## How

Gboard exposes no keys to the accessibility tree, so you cannot find them the usual way. This script
takes a screenshot, finds the bright key rectangles in the bottom of the frame, derives a QWERTY grid
from the row geometry, and issues one `adb shell input tap` per character. The characters go through
the keyboard's own engine, so what lands in the field is what a thumb would have produced.

Before trusting the map it **self-calibrates**: types `a`, then Shift+`b`, reads the field back and
deletes them. If that does not come back as `ab`, it falls back to injection and *says so in the
report* rather than quietly pretending.

It also handles the thing that trips people up: keyboards auto-capitalise at the start of a field and
after a space, so pressing Shift there turns the capital *off*. The script guesses, verifies what
actually landed, and corrects itself.

## Use

Needs `adb` on PATH, a booted device or emulator whose IME is a real keyboard, and Pillow.

```bash
pip install pillow
python3 lab.py --url https://example.com/signup --text "ada lovelace" --out out/
```

The emulator image matters: use a **Google Play** system image, because those ship Gboard. A plain
`google_apis` image has the bare AOSP keyboard and you are testing something your users never touch.

`report.json` records, per field:

| Field | Meaning |
|---|---|
| `keyboard_opened_on_tap` | the soft keyboard actually appeared |
| `field_y_before_tap` vs `field_y_with_keyboard` | did the page lift the field above the keyboard |
| `typed_via_keys` vs `injected` | **only key-tapped text went through the real IME** |
| `expected` vs `value` | what the keyboard actually produced |

### It works, and here is it proving the point

First run of the workflow in this repo, against `duckduckgo.com` on Android 13 and 14:

```json
{ "step": "typed",
  "keyboard_opened_on_tap": true,
  "keyboard_mode": "geo",
  "typed_via_keys": "ada lovelace",
  "injected": "",
  "expected": "ada lovelace",
  "value": "Ada lovelace",
  "matches": false }

{ "finding": "THE KEYBOARD CHANGED THE TEXT",
  "expected": "ada lovelace",
  "produced": "Ada lovelace",
  "note": "autocorrect / auto-capitalisation / predictive text —
           an injected-text tool would never have seen this" }
```

Every character went through the keys (`injected` is empty), and Gboard capitalised the first letter.
A tool that injects text returns `ada lovelace` and reports success. On a case-sensitive field, that
difference is a bug your users hit and your test suite does not.

A mismatch between `expected` and `value` is the point of the tool. In our own use it caught a signup
form where the keyboard capitalised the first letter of a case-sensitive code, so valid codes were
being rejected.

## Also here: `hydration_check.mjs`

A different bug, found while building this, general to server-rendered React/Next.js:

A server-rendered `<input>` accepts typing **before React hydrates and attaches `onChange`**. Text
typed in that window lands in the DOM and React never sees it. State stays empty, so a
`disabled={!state.trim()}` button stays dead — with the text sitting there in plain sight. It does
not self-heal, because React has no reason to re-render.

The user's experience: *"I typed it, I can see it, and the button does nothing."*

```bash
npm i playwright && npx playwright install chromium
node hydration_check.mjs --url https://example.com/signup --selector "#email"
```

It forces the race by delaying the JS bundles, so you get a straight pass/fail instead of a
probability. Exit code 0 means the app adopted the text; 1 means the button stayed dead.

The fix is usually a mount effect that adopts whatever is already in the DOM:

```jsx
useEffect(() => {
  const el = document.getElementById("email");
  if (el instanceof HTMLInputElement && el.value) setEmail(el.value);
}, []);
```

## CI

`.github/workflows/example.yml` runs the whole thing on GitHub's free runners — no device cloud, no
account, no paid service. Real emulators with hardware acceleration, which needs the KVM udev rule
the workflow includes.

Roughly 8–10 minutes per Android job on a standard Linux runner.

## Honest limits

- **Android only** for key-tapping. The iOS path here reads the accessibility tree and does not tap
  the software keyboard.
- **Samsung Keyboard is not covered.** It ships only on Samsung hardware, so no emulator has it. If
  you need it, run against a real device — [DeviceFarmer/stf](https://github.com/DeviceFarmer/stf)
  exposes raw touch injection you can point this technique at.
- **Key detection is a brightness heuristic**, tuned for Gboard's light theme. A very different
  keyboard theme may not match; calibration will catch that and fall back rather than type garbage.
- **One field per run.** This is a focused tool, not a flow runner. For multi-step flows, drive the
  navigation yourself and call this for the typing.
- Emulator boots on shared CI runners are occasionally flaky; retry is cheaper than diagnosis.

## Why it exists

Built to chase a real report that "keyboard input is broken on the phone" in a study app. The
standard tools could not reproduce it, because they all bypassed the keyboard that was causing it.

Prior-art check across 37 open-source projects — CI emulator actions, self-hosted device farms,
mobile frameworks, visual-regression tools and vision agents — found nothing that types through a
real on-screen keyboard. If you know of one, please open an issue; that would be genuinely useful to
know.

MIT.
