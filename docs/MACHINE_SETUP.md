# Machine setup — running the platform on a new Mac

Everything the host needs before a run can pass. Each item here comes from a real
failure, so the "why" is kept: without it the next person removes the guard.

## 0. From zero on a new Mac

Run the doctor at every stage — it names what is missing and the exact command to
fix it, so you never have to guess how far you got:

```bash
bash scripts/doctor.sh          # exit 0 = ready to run
```

### 0.1 Install the tools

```bash
xcode-select --install
xcodebuild -downloadPlatform iOS              # REQUIRED — see §1
brew install node redis
brew services start redis
npm i -g yarn appium
appium driver install xcuitest
sudo gem install cocoapods
brew tap facebook/fb && brew install idb-companion && pipx install fb-idb
```

### 0.2 The platform itself

```bash
git clone <this repo> && cd AutomationTesting
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill it in — see §2
mkdir -p ~/.vya-platform      # the database lives HERE, not in the repo
(cd automation/dashboard && npm install)
```

### 0.3 Start everything

```bash
nohup bash scripts/supervise_backend.sh > logs/supervisor.out 2>&1 &   # :8000
(cd automation/dashboard && npm run dev &)                             # :5173
appium &                                                               # :4723
```

Seed the first login (only needed once, on an empty database):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/seed      # admin / admin
```

### 0.4 Simulators and app repos

Register each project in the dashboard (Projects → Register Project) and let it
clone. Then per app repo, one bundler each on its own port:

```bash
cd repos/<project-id> && npx react-native start --port 8081
```

Boot the devices the flows are PINNED to in `cross_app_config.json` — the platform
does not use "whatever is booted":

```bash
xcrun simctl boot <udid>
```

### 0.5 Bring your projects and scenarios across

The database lives outside the repo (§2), so a fresh machine starts EMPTY — same
repo, same apps, no scenarios. They ship as a committed seed instead:

```bash
# on the machine that HAS the data, whenever it changes:
.venv/bin/python scripts/platform_seed.py export     # writes seeds/platform.json
git add seeds/platform.json && git commit -m "seed: platform projects + scenarios"

# on the new machine:
.venv/bin/python scripts/platform_seed.py import
.venv/bin/python scripts/platform_seed.py diff       # confirm nothing is missing
```

Import is ADDITIVE and idempotent — it inserts what is absent and never edits or
deletes what is already there, so it is safe to re-run on a half-set-up machine.
Run HISTORY is deliberately not exported: runs belong to the machine that produced
them, and copying them would invent a test history the new machine never had.

Imported projects still need their repo cloned — open **Projects** in the dashboard
and let each one clone.

Then `bash scripts/doctor.sh` again. It should print **READY**.

### 0.5 First real check

```bash
.venv/bin/python -m pytest tests/ -q      # unit suite, no device needed
```

Then run one quick demo flow from the dashboard. If it passes, the machine is good.

## 1. Host prerequisites

| Need | Why |
|---|---|
| macOS + Xcode, matching **iOS platform installed** | `xcodebuild -downloadPlatform iOS`. Deleting a simulator *runtime* to save disk breaks WebDriverAgent — Xcode needs the platform to build for iOS at all, even when targeting an older simulator. |
| Node 20 (`.nvmrc` / nvm) + **Yarn** | The app repos are Yarn workspaces. Running `npm install` in them corrupts the tree. |
| CocoaPods, run with a UTF-8 locale | `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 pod install` — plain `pod install` dies with `Unicode Normalization not appropriate for ASCII-8BIT`. |
| Appium + `appium-xcuitest-driver`, `idb` | |
| Redis | |
| **≥ 40 GB free disk, ≥ 16 GB RAM** | See "Resource pressure". |

## 2. Platform config (`.env`, never committed)

```env
DATABASE_URL=sqlite:////Users/<you>/.vya-platform/platform.db
LLM_PROVIDER_TYPE=groq
OPENAI_API_KEY=<groq key>          # Groq is OpenAI-compatible; this same var feeds it
GITHUB_TOKEN=<token>               # PR list + PR-QA comments
VYA_BUSINESS_WAITER_USER=...
VYA_BUSINESS_WAITER_PASSWORD=...
VYA_BUSINESS_KITCHEN_USER=...
VYA_BUSINESS_KITCHEN_PASSWORD=...
PR_TEST_IOS_DEVICE=<udid>
```

**Keep the database OUTSIDE the repo.** `.db` files exist in this repo's git history,
so any checkout/rebase crossing those commits overwrites an in-repo database with the
committed one — that is how a full set of projects, scenarios and runs was lost.

**Without `LLM_PROVIDER_TYPE` + `OPENAI_API_KEY` the AI silently falls back to a mock
provider** that returns a hardcoded "login button selector" story. RCA, Weekly Trend
and the PR planner then present fiction as analysis. There is no error — just fake data.

## 3. Simulators

Devices are pinned in `cross_app_config.json`; the platform does not "use whatever is
booted". Boot the pinned consumer and waiter devices before a cross-app run.

## 4. App repos (`repos/<project-id>/`)

Plain git clones, **not submodules** — nothing updates them automatically.

For each app, per machine:

```bash
yarn install                                        # NOT npm
cd ios && LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 pod install
```

### Dependency pins that must match the app team's `package.json`

A clone that drifts from the team's manifest fails in ways that look like automation bugs:

- **`react-native-compressor` must be declared** (e.g. `1.10.3`). `App/Utils/videoUploadTracker.js`
  requires it. The require is lazy and guarded, but Metro resolves statically, so an
  undeclared package 500s the whole bundle and the app shows a red screen.
- **`react-native-qrcode-svg` must be pinned to `6.2.0`**, not `^6.0.7`. Later 6.x imports
  `react-native-svg/css`, a subpath that only exists in `react-native-svg` v15+ — and this
  app pins `^12.5.1`. The floating range silently upgrades and breaks the bundle.

Always take `package.json` from the app team rather than letting ranges float.

### Building against a newer Xcode

RN 0.68-era pods do not compile under current clang. The `ios/Podfile` `post_install`
carries two guards — keep them:

- `_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION=1` — C++17 removed
  `std::unary_function`; boost 1.76 still uses it, so every build dies in
  `container_hash/hash.hpp`.
- `-Wno-incompatible-function-pointer-types` — newer clang promotes this to an error,
  which `@react-native-community/datetimepicker` trips.

### Metro

One bundler per app repo, on the port that repo's app was built against. A JS-only
change needs a Metro restart; **anything touching native modules needs a rebuild and
reinstall** — otherwise the app loads the bundle and dies with
`Invariant Violation: Module AppRegistry is not a registered callable module`, which
means the JS expects native modules the installed binary does not have.

## 5. Resource pressure

The stack (3 simulators + 4 Metro bundlers + Appium + WDA + Vite + Redis) exhausts a
16 GB machine. Metro and the backend get killed, and the failures look like flaky tests.

Disk fills the same way. Reclaimable, in order:

```bash
uv cache prune                    # this grew to 21 GB
xcrun simctl delete unavailable
rm -rf ~/Library/Developer/Xcode/DerivedData/*
```

Do **not** delete simulator runtimes to save space — that breaks WebDriverAgent (§1).

## 6. Preflight before trusting a run

```bash
bash scripts/doctor.sh
```

Checks host tools, the iOS SDK and runtimes, disk and memory headroom, `.env`
(including the two settings that fail SILENTLY — see §2), every service, and that
the devices the flows are pinned to are actually booted. Each miss prints the fix.

Run it before believing any failure. A red here makes every downstream failure an
ENVIRONMENT failure, not an app or automation one — on 2026-09-02 four separate
environment faults each surfaced hours later as a confusing test result.

## 7. What the platform fixes for you

`automation/projects/builder.py` repairs known breakages before every build, so a new
machine should not hit them by hand:

- **`RN_KNOWN_FIXES`** — pins declared dependencies that are incompatible with the
  project's React Native line (e.g. `react-native-qrcode-svg` → `6.1.2`, because 6.2+
  imports `react-native-svg/css`, which needs svg v13.7+ while this app pins 12.5.1).
- **`RN_REQUIRED_DEPS`** — installs packages the source imports but `package.json`
  never declares (e.g. `react-native-compressor`). This table is deliberately *not*
  gated on the package already being present: being absent is the whole failure.
- **`_patch_dead_podspec_urls`** — repoints podspecs whose download host is gone.

Both tables are keyed by RN major.minor. When a new undeclared import or version
conflict shows up, add it there rather than fixing one clone by hand — otherwise the
next machine repeats the whole diagnosis.

**These tables rewrite `package.json` during a build.** If a pin here disagrees with
the app team's manifest, the platform wins and the file changes under you. Reconcile
deliberately; do not assume an unexpected `package.json` diff was a person.

### LogBox

`AppBuilder._silence_logbox` adds `LogBox.ignoreAllLogs(true)` to each app's
`index.js` before a build. The debug builds stack LogBox toasts along the bottom and
draw them OVER real controls — measured on Vya Business: `addNewEvent` (y=723), the
lower half of `saveBtn` (685..735) on the iPad, the whole time-slot row on the phone
(slot y=730, toast 726..774). Tapping any of them opens the LogBox viewer, whose own
Dismiss button is also covered, so there is no way out. A booking could not be
created by hand or by automation until this was silenced.

It is idempotent and only ever ADDS a line. Unlike the node_modules patches it
touches **tracked source**, so `index.js` will show as modified in the app repo. That
is deliberate: the real fix belongs in the app, and a silent change would hide the
fact that the shipped build has controls users cannot tap either.
