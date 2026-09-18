# Install & verify

For someone setting this platform up on their own Mac and checking it actually works.
Every step ends in something you can look at, so you always know whether it worked.

If anything is unclear or fails, run `bash scripts/doctor.sh` — it checks 25 things
and prints the exact command to fix each one.

---

## 1. Install the tools (~15 min, mostly downloads)

```bash
xcode-select --install
xcodebuild -downloadPlatform iOS          # ~10 GB. REQUIRED — see the warning below
brew install node redis
brew services start redis
npm i -g yarn appium
appium driver install xcuitest
sudo gem install cocoapods
brew tap facebook/fb && brew install idb-companion && pipx install fb-idb
```

> **Do not delete iOS simulator runtimes to save disk.** Xcode needs the iOS platform
> to build WebDriverAgent at all. Removing it makes every run fail with
> `xcodebuild failed with code 70`, and nothing points at the cause.

### Which iOS version do I need?

**Whichever one your Xcode already installed.** Any iOS **16 through 26** runtime
works — see `ios-support.json`. You do not need to match anyone else's version, and
you should not download an extra runtime to do so.

Nothing pins a version: the platform finds a simulator your Mac actually has
(preferring one already booted, else an available iPhone) and boots it for you. Test
capabilities deliberately omit `platformVersion`, because Appium treats it as an
*exact* match — a pinned `18.3` is why a Mac shipping only iOS 26 used to fail to
start a session at all, even though the tests run fine on 26.

Leave `PR_TEST_IOS_DEVICE` empty in `.env` (step 3) so this auto-selection applies. A
simulator UDID only exists on the Mac it came from.

**Check:** `xcodebuild -showsdks | grep iphonesimulator` prints an SDK, and
`bash scripts/doctor.sh` reports an iOS runtime in the supported range.

---

## 2. Clone into your workspace

```bash
cd ~/your-workspace
git clone <platform-repo-url> AutomationTesting
cd AutomationTesting

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
(cd automation/dashboard && npm install)
```

**Check:** `.venv/bin/python -m pytest tests/ -q` — the unit suite passes with no
device and no simulator. If this fails, stop here; nothing else will work.

---

## 3. Configure

```bash
cp .env.example .env
mkdir -p ~/.vya-platform
```

Edit `.env`:

```env
DATABASE_URL=sqlite:////Users/<you>/.vya-platform/platform.db
LLM_PROVIDER_TYPE=groq
OPENAI_API_KEY=<groq key from console.groq.com>
GITHUB_TOKEN=<token with repo scope>
```

Two of these fail **silently** if you skip them:

- **`DATABASE_URL` must be an absolute path OUTSIDE the repo.** `.db` files exist in
  this repo's git history, so a checkout or rebase crossing those commits overwrites
  a live database. That is how a full set of projects and scenarios was lost once.
- **Without the LLM keys the AI returns a canned fake analysis** — a hardcoded story
  about a "login button selector" — and never errors. You will believe it.

**Check:** `bash scripts/doctor.sh` — the *Platform config* section is all green.

---

## 4. Start it

```bash
nohup bash scripts/supervise_backend.sh > logs/supervisor.out 2>&1 &   # :8000
(cd automation/dashboard && npm run dev &)                             # :5173
appium &                                                               # :4723

curl -X POST http://127.0.0.1:8000/api/v1/auth/seed                    # first login only
```

**Check:** open http://localhost:5173 and sign in as `admin` / `admin`.

---

## 5. Get the test scenarios

The database lives outside the repo, so a fresh install starts with none. They ship
as a committed seed:

```bash
# keep the projects you register yourself; just add the scenarios:
.venv/bin/python scripts/platform_seed.py import --scenarios-only
.venv/bin/python scripts/platform_seed.py diff        # confirm nothing is missing
```

Import is additive and idempotent — it never edits or deletes what you already have,
and re-running it is a no-op. Scenarios attach to your own projects by name, so your
clone paths and branches are left exactly as they are.

**Check:** **Scenarios** in the dashboard lists them with their step counts.

---

## 6. Register your apps

Dashboard → **Projects** → **Register Project**, give it the git URL and branch, and
let it clone into `repos/<project-id>/`.

Then per app repo, one Metro bundler each on its own port:

```bash
cd repos/<project-id>
yarn install                                   # NOT npm — these are Yarn workspaces
(cd ios && LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 pod install)
npx react-native start --port 8081 &
```

> Plain `pod install` dies with `Unicode Normalization not appropriate for ASCII-8BIT`.
> The locale prefix is not optional.

**Check:**
```bash
curl "http://127.0.0.1:8081/index.bundle?platform=ios&dev=true" -o /tmp/b.js -w "%{http_code}\n"
grep -c registerComponent /tmp/b.js
```
`200`, several MB, and a non-zero count. **A 200 with zero `registerComponent` means
the bundle built but contains no app** — the device will show a blank screen and say
`Module AppRegistry is not a registered callable module`.

---

## 7. Boot the devices and run something

Flows use the UDIDs pinned in `cross_app_config.json`, not whatever happens to be
booted:

```bash
xcrun simctl boot <udid>
bash scripts/doctor.sh          # must print READY
```

Then run one quick demo flow from the dashboard. If it passes, the install is good.

---

## When something fails

1. `bash scripts/doctor.sh` first — most failures are environment, not code.
2. A failing run now carries the app's own words on the failing step, tagged
   `[evidence]`: JS console errors, device log, and any crash report.
3. **Inspector** in the dashboard shows the live UI tree and flags anything covered
   or off screen — the cause of most "it says it tapped it but nothing happened".

See `docs/MACHINE_SETUP.md` for the detail behind each of these and the known
app-repo dependency pins.
