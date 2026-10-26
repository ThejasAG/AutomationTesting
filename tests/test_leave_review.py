"""The 'leave a review' handler (@leave_review).

No flow touched Screens/Wallet/Review.js before this draft — the only id any
flow touched on that screen was the generic `screenBackBtn` fallback. The
Wallet's Finished tab shows a star-rating prompt per unreviewed order
(accessibilityLabel 'Restuarantrating' (sic), Screens/Wallet/Finished.js);
tapping it opens Review.js, which exposes real per-screen ids: `star1..star5`,
`writeFeedback` (accessibilityLabel only, no testID), `submitReview`.

These tests pin: the right screens are found (Wallet -> Finished tab -> an
UNREVIEWED card -> Review), the rating is set by the screen's own real id
(`star2`) rather than trusting whatever the entry tap happened to preset, and
Submit's own success signal (submitReview leaving the screen) is what counts
— not just "the tap landed". They also pin the documented behaviour the task
asked this scenario to capture: a low (2-star) rating submits with no extra
validation, because Review.js's `submitReview` is disabled only by
`submitting || success` (Review.js:1228) — never by the star count.
"""
import pytest

from automation.scenarios import cross_app_flows as F

WALLET = ["upcomingBlock", "finishedBlock", "walletUpcomingSearchInput"]
REVIEW = ["screenBackBtn", "star1", "star2", "star3", "star4", "star5",
          "writeFeedback", "addUploadPhoto", "submitReview"]


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda *_: None)


class FakeField:
    def __init__(self, sink):
        self._sink = sink

    def click(self):
        pass

    def send_keys(self, text):
        self._sink.append(text)


class FakeDriver:
    """Stands in for ScenarioRunner.d — only `writeFeedback` is reached this
    way (accessibilityLabel only, no testID, so a real step reaches it the
    same way: an ACCESSIBILITY_ID lookup, not idb)."""

    def __init__(self):
        self.typed = []
        self.hidden = 0

    def find_elements(self, by, value):
        if value == "writeFeedback":
            return [FakeField(self.typed)]
        return []

    def hide_keyboard(self):
        self.hidden += 1


class Screen:
    """A scripted device. `frames` is a list of label-lists; each tap OR
    scroll advances it one frame (clamped at the last once exhausted — a
    screen that stops reacting looks exactly like one that never changed)."""

    def __init__(self, frames):
        self.frames = [list(f) for f in frames]
        self.actions = 0
        self.tap_log = []

    def _cur(self):
        return self.frames[min(self.actions, len(self.frames) - 1)]

    def els(self, udid=""):
        return [{"id": "", "label": l, "type": "GenericElement",
                 "x": 0, "y": 0, "w": 100, "h": 40, "cx": 50, "cy": 20}
                for l in self._cur()]

    def tap(self, x, y, udid=""):
        self.tap_log.append((x, y))
        self.actions += 1
        return True

    def scroll(self):
        self.actions += 1


class FakeR:
    """Stands in for the ScenarioRunner passed as `r` — only `.d` and
    `.run_one('scroll down', ...)` are touched by @leave_review."""

    def __init__(self, scr):
        self.d = FakeDriver()
        self.scr = scr
        self.scrolls = 0

    def run_one(self, step, idx):
        assert step == "scroll down"
        self.scrolls += 1
        self.scr.scroll()
        return type("Res", (), {"ok": True})()


def run(frames):
    fr = object.__new__(F.FlowRunner)
    scr = Screen(frames)
    fr._idb_els = scr.els
    fr._idb_tap = scr.tap
    r = FakeR(scr)
    notes = []
    return fr._leave_review(r, notes), notes, scr, r


def joined(notes):
    return " ".join(notes)


# ── dispatch ─────────────────────────────────────────────────────────────────
def test_the_token_reaches_a_dedicated_handler():
    assert hasattr(F.FlowRunner, "_leave_review")
    fr = object.__new__(F.FlowRunner)
    fr._leave_review = lambda r, notes: (notes.append("called"), True)[1]
    notes = []
    assert fr._handle_special(None, "@leave_review", notes) is True
    assert notes == ["called"]


def test_the_flow_block_uses_the_token():
    assert "@leave_review" in F._C_LEAVE_REVIEW
    assert F.FLOWS["cc_leave_review"]["segments"][0]["steps"] == F._C_LEAVE_REVIEW


# ── happy path ───────────────────────────────────────────────────────────────
def test_rates_writes_feedback_and_submits():
    ok, notes, scr, r = run([
        WALLET,                                   # on Wallet (Upcoming active)
        WALLET + ["Restuarantrating"],             # Finished tab: one unreviewed card
        REVIEW,                                    # tapped the prompt -> Review opened
        REVIEW,                                    # star2 tapped (still open)
        [],                                        # submitReview tapped -> screen cleared
    ])
    assert ok is True, notes
    assert "submitted a 2-star review" in joined(notes)
    assert "no minimum rating is enforced" in joined(notes)
    assert r.d.typed == ["Automated regression check."]
    assert r.d.hidden == 1
    assert len(scr.tap_log) == 4            # finishedBlock, the prompt, star2, submitReview
    assert r.scrolls == 0                   # the first card already needed a review


# ── scrolling to find an unreviewed card ─────────────────────────────────────
def test_scrolls_past_already_reviewed_cards_to_find_one_that_needs_it():
    ok, notes, scr, r = run([
        WALLET,                                     # Upcoming
        WALLET,                                     # Finished tab tapped: no prompt yet
        WALLET,                                     # 1st scroll: still none
        WALLET + ["Restuarantrating"],               # 2nd scroll: found one
        REVIEW,
        REVIEW,
        [],
    ])
    assert ok is True, notes
    assert r.scrolls == 2


# ── failures that must not be silent ────────────────────────────────────────
def test_no_unreviewed_order_fails_with_that_reason_not_a_timeout():
    ok, notes, scr, r = run([WALLET])             # Restuarantrating never appears
    assert ok is False
    assert "no finished order on the Wallet still needs a review" in joined(notes)
    assert r.scrolls == 6                          # scrolled the max, never guessed a tap


def test_tapping_the_prompt_but_review_never_opens_fails_loudly():
    ok, notes, scr, r = run([
        WALLET,
        WALLET + ["Restuarantrating"],             # tapped; the screen never changes
    ])
    assert ok is False
    assert "Review screen did not open" in joined(notes)


def test_missing_star2_on_the_review_screen_fails_loudly():
    no_star2 = [l for l in REVIEW if l != "star2"]
    ok, notes, scr, r = run([
        WALLET,
        WALLET + ["Restuarantrating"],
        no_star2,
    ])
    assert ok is False
    assert "'star2' is not on screen" in joined(notes)


def test_submit_that_never_closes_the_screen_fails_loudly():
    """The whole point: a landed tap on submitReview is not a submission --
    the screen must actually leave Review, or the server call may never have
    happened (or the app rejected it and stayed put)."""
    ok, notes, scr, r = run([
        WALLET,
        WALLET + ["Restuarantrating"],
        REVIEW,                                     # stays on REVIEW forever (clamped)
    ])
    assert ok is False
    assert "Review screen never" in joined(notes)


def test_no_wallet_and_no_tab_fails_without_guessing():
    ok, notes, scr, r = run([["Home", "Menu"]])
    assert ok is False
    assert "not on the Wallet" in joined(notes)
    assert scr.tap_log == []


def test_opens_the_wallet_first_if_not_already_there():
    ok, notes, scr, r = run([
        ["Home", "walletTab", "Menu"],              # not on Wallet yet
        WALLET,                                      # walletTab tapped -> Wallet (Upcoming)
        WALLET + ["Restuarantrating"],               # finishedBlock tapped
        REVIEW,
        REVIEW,
        [],
    ])
    assert ok is True, notes
    assert len(scr.tap_log) == 5     # walletTab, finishedBlock, the prompt, star2, submitReview
