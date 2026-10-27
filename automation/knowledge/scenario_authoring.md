# Writing a scenario fast

What a "Create scenarios" session used to rediscover every time. MEASURED 2026-10-27
(session cf6bd414): 23 of its 57 minutes went to helper agents reading
`cross_app_flows.py` (10k lines) for the step forms, the idb API and the unit-test
pattern. They are all here and in `agent_tools.py steps`. Don't spend helper agents on
the platform itself; only the APP's screens need reading.

## The order of work

1. `steps` gives every step form, every `@token` with its handler method, and
   every shared block with the flows that use it. `show-flow` the closest flow.
2. Each feature in your task already lists its screens and ids (from the feature
   map). Read only those files (`app-show` / Read) and confirm each id is in the
   code you are about to tap. Use `app-analyst` only for a feature listed
   without screens.
3. Write **all** drafts first, then `check-flows` once and `check-ids` per draft.
4. Run the drafts **back to back** (`run cc_x --label new`, one after another).
5. When a draft fails, read `run-info` (the `↳ on screen:` line lists the ids that
   WERE there) and the screenshot. Fix it. If it failed in segment 2 or later, re-run
   with `run cc_x --resume <failed run> --label new`: the segments that passed
   are carried over, booking included.
6. Unit tests: run only the files you added (`unit-tests tests/test_x.py`). The
   full suite takes about 7 minutes, and the checks after the session run it anyway.

## Steps

| Step | What it does |
|---|---|
| `click <id>` | Taps the element with that exact id/label, verified by idb and scrolled into view. `click order later` is read as `orderLater`. |
| `?click <id>` | Optional: tapped if it shows within ~6s, else skipped. Use it for "only in some builds". |
| `type <text> in <field>` | Types into the field. |
| `select <text>` | Taps by visible text (`select Any`, `select Indoor`). |
| `scroll down` | One swipe. |
| `@see:<text>` | **The outcome check to use.** Passes as soon as a label containing the text (case and punctuation ignored) is on screen. It polls idb up to 12s, then reads the pixels once (OCR) for Overlay text. It catches toasts. |
| `verify <text>` | Slow fuzzy resolver (MEASURED 49s). It misses toasts. Don't use it in new drafts. |
| `@wait_screen:<name>` | Waits for a named screen (`wallet`, …). |

A scenario must **prove** something. End each segment with the `@see:` (or a
handler's own check) of the state the feature produces: a status, a total, a
"N result(s) shown" line, a toast. "The screen opened" proves nothing.

## Facts that cost a failed run when missed

- The consumer **adult `+` (`counterPlus`) opens My Contacts**. It does not add 1.
  Use `@invite_guests:<N>` to raise the party size.
- A booking card opens only **within 30 min of its time**. The shared booking blocks
  book the first slot, so reuse them and don't build your own booking.
- The consumer account is **shared** on staging, so someone else may have a booking at
  the same slot (known_issues.md). Handlers must pick THIS run's booking.
- Overlay sheets (magnus `Overlay`: filter, void/comp reasons, split) have **no ids**
  in idb or Appium. In a handler, tap by text with `self._ocr_tap(udid, box, text)`,
  as `_filter_orders` does. Ids that are templates in the code
  (`${username}finishedCard`, `itemName-${name}`, `tableChip${name}`) are built at
  runtime, so match their prefix or suffix in a handler.
- Some restaurant data makes a check impossible. MEASURED: the seat-capacity guard
  never tripped because it is skipped when the area has no capacity data. Before you
  build a scenario around a guard, read the condition that skips it.
- iPad = waiter and kitchen (same app, different login); iPhone = consumer.

## A new handler (only when no step or token above does it)

Put it in `FlowRunner` (`automation/scenarios/cross_app_flows.py`), beside a similar
handler:

```python
    def _my_thing(self, notes: List[str]) -> bool:
        """What it does and what it proves (cite the app file:line it relies on)."""
        tag = "@my_thing"
        udid = self._business_udid()   # waiter/kitchen iPad; consumer: self.devices.get("consumer") or DEFAULT_CONSUMER_UDID
        els = _idbd.describe_all(udid)                      # one screen read, ~1s
        ok, how = _idbd.tap(udid, ["someBtn"], els)         # exact ids; scrolls into view
        if not ok:
            notes.append(f"[FAIL] {tag} — someBtn: {how}")
            return False
        els, done = self._wait_els(udid, lambda es: self._has(es, "resultId"), 8.0)
        if not done:
            notes.append(f"[FAIL] {tag} — tapped someBtn but resultId never showed")
            return False
        notes.append(f"[ok] {tag} — {how}; resultId showed")
        return True
```

Then:
- In `_handle_special`: `if step == "@my_thing": return self._my_thing(notes)`, or
  for an argument, `if step.startswith("@my_thing:"): return self._my_thing(step.split(":", 1)[1].strip(), notes)`.
  The token must appear there as a string literal, because `check-flows` reads it from there.
- Add one `STEP_CATALOG` line, since that's what the dashboard editor offers.

The idb API (`_idbd`, `automation/scenarios/idb_driver.py`):
- **Reading the screen:**
  - `describe_all(udid)` → elements.
  - `name(e)` → the id, else the label.
  - `frame(e)` → (x, y, w, h).
  - `app_size(els)` → (w, h).
- **Tapping:**
  - `tap(udid, [names], els, scroll=True)` → (ok, how).
  - `tap_el(udid, e, els)` taps an element you picked yourself (a prefix match, the Nth row).
  - `tap_point(udid, x, y)`.
- **Moving and typing:**
  - `swipe(udid, x1, y1, x2, y2, duration)`.
  - `fill(udid, field, text)`.
- **Helpers on the runner:**
  - `self._wait_els(udid, pick, secs)` → (els, pick result).
  - `self._has(els, id)`.
  - `self._ocr_tap(udid, box, text)`.
  - `self._ocr_sees(udid, text)`.
  - `self._see_text(text, notes)`.

Never call Appium `find_elements` / `page_source` in a handler: this app's tree makes
them hang for minutes.

## Its unit test

Take `tests/test_open_restaurant.py` (a fake screen that changes when tapped) or
`tests/test_filter_orders.py` (OCR) as the model:
- Build the runner with `FlowRunner.__new__(FlowRunner)` and set `_cur_udid` and `devices`.
- Monkeypatch `caf._idbd.describe_all` / `tap` / `tap_el` with the fake screen's methods.
- Replace `caf.time` with a fake clock so waits cost nothing.

Three tests are enough:
1. The token reaches the handler (`FlowRunner._handle_special(fr, None, "@my_thing", [])`).
2. The happy path proves the outcome.
3. The failure path says why in `notes[-1]`.
