---
name: iphone-duo-simulator
description: Use when adapting, testing or capturing an iOS app on the iPhone Duo simulator. Folds and turns the simulator from a script, captures and films whichever panel is drawing, taps through Xcode's device-interaction tools, renders mock-ups in Apple's bezels, and lists the numbers each pose reports and the layout rules that matter on the foldable.
---

# iPhone Duo simulator kit

Scripts are in `scripts/` next to this file. The full guide, with the reasoning and pictures, is
https://foldhunt.com/guides/everything-your-agent-needs-for-iphone-duo/ (Markdown: append `index.md`).
The same files are on GitHub: https://github.com/WhitenWhiten/iphone-duo-simulator-skill

## Setup, once

1. Xcode 27.1 with the iOS 27.1 simulator runtime. Build with its **SDK**, not only its runtime:
   `DEVELOPER_DIR=/Applications/<Xcode 27.1>.app/Contents/Developer xcodebuild … -destination 'platform=iOS Simulator,id=<DUO-UDID>'`.
   Check before trusting a build: `grep -rl --include='*.swiftinterface' reservedRegions "$(xcrun --show-sdk-path -sdk iphonesimulator)/System/Library/Frameworks/SwiftUICore.framework"` must print a file. Against the 27.0 SDK it prints nothing and the fold APIs don't exist; the app still runs, but stops short of the status bar's column on the inner display with its bars horizontal.
2. Apple's own guidance ships in Xcode 27.1: `xcrun agent skills export --output-dir ./xcode-skills` writes `app-resizability`, which treats "prepare for iPhone Duo" as running all of its tasks (`UIScreen.main`, orientation checks, scene lifecycle, asymmetric safe areas, idiom checks, Info.plist prerequisites). Run it on the codebase first.
3. `python3 scripts/duo.py devices` finds the Duo simulator. Use `--udid` or `DUO_UDID` when there are several. Never create, clone or erase simulators to get one.

## Hard rules

- **Never run `xcrun simctl io <duo> screenConfig`.** On a Duo it leaves SpringBoard crash-looping on the next hinge event. Recovery: `simctl shutdown` and `simctl boot` the same UDID.
- **Check every capture.** The panel that isn't drawing captures pure black. `duo.py shot` retries until a panel lights up and says which one.
- **No angle readback exists.** `devicectl device motion hinge-angle`, which `hinge get` calls, is rejected as unknown by the `devicectl` in Xcode 27.0 and 27.1 RC (651.13.5). Remember what you set (`duo.py status` shows the last value). The pose does read back: `devicectl device info displays --device <udid>` marks the drawing panel `"active"` (`LCD` is the cover, `LCD-1` the inner display), so shut or open.
- **Bend from flat.** The simulator's hinge has hysteresis: opened from shut, the cover stays lit and the hinge reports `closed` up to about 150°; closed from flat, the inner display stays lit down to about 20°. `duo.py fold` to a part-folded angle opens the phone flat first when the cover is drawing. Start a `path` or `sweep` at 180, not 0.
- Post an orientation after the hinge has settled; `duo.py orient` waits and posts it twice. `devicectl device orientation set` reports success on the Duo but turns nothing, and `devicectl device orientation get` reads `portrait` whatever the pose.
- Relaunch the app after changing the pose for screenshots, and give it time: `scene --settle` (default 3 s) may be too short for an app that loads data on launch.

## Driving it

```sh
python3 scripts/duo.py fold close|open|book|laptop|0..180
python3 scripts/duo.py orient sideways|upright      # open phone: sideways is its native pose
python3 scripts/duo.py path 1.0 180 140 180         # a timed bend and back, for hinge gestures
python3 scripts/duo.py launch <bundle-id> -- <launch arguments>
python3 scripts/duo.py shot --out shots/            # the panel that is drawing
python3 scripts/duo.py scene --fold laptop --orient upright --bundle <bundle-id> --out shots/
python3 scripts/duo.py record --out films/ path:1.2:0:180 rec:inner wait:4   # film a fold
```

`record` films one panel at a time (a second `recordVideo` on the same device never finishes its file): it starts on the panel that is drawing, and `rec:cover|inner` switches to the other once a fold has lit it. Extract frames with `ffmpeg -i film.mp4 -fps_mode passthrough -enc_time_base 1/1000 -frame_pts 1 frames/%06d.png` (named by their time in ms); frames arrive only when something changes. To find the jumps, `compare -metric RMSE` each frame with the one before and read the largest first: a hard cut is one large step, a crossfade a run of small ones. The system covers the hand-over between panels with a blurred snapshot, so whatever moves after it is the app's. In the simulator the crease turns active about a second after the hinge stops.

Taps and the UI hierarchy come from Xcode, through `scripts/xcode_device.py` (Xcode running, `DEVELOPER_DIR` set to 27.1; Xcode asks once to allow the connection):

```sh
python3 scripts/xcode_device.py start --device <DUO-UDID>
python3 scripts/xcode_device.py look                 # screenshot, hierarchy, tappable elements
python3 scripts/xcode_device.py tap "Settings"       # by accessibility label
python3 scripts/xcode_device.py do "t 200 560 f 200 260 0.4"
python3 scripts/xcode_device.py end                  # always close the session
```

On the Duo, Xcode's tools read the device through its **cover**: they work fully while the phone is shut, but with the phone open the screenshot is black, the hierarchy stops at the cover's width and taps don't land, and there is no hinge command. So: `duo.py` folds, turns and captures every pose; `xcode_device.py` taps and reads the screen while shut. To test a flow on the open display, check it shut first, then open the phone with `duo.py` and capture. Or tap the open phone with [duoctl](https://github.com/skarol/duoctl) (`duoctl tap <x> <y>`, interface points), which sends touches through the lit display's own touchscreen; its hinge-angle readback comes back null on `devicectl` 651.13.5. A hierarchy read during an animation has stale hit points: look again before a retry.

## Every change, every pose

Build with 27.1, install, then walk: shut (`fold close`), open sideways (`fold open`, `orient sideways`), book (`fold book`), open upright (`orient upright`, `fold open`) and laptop (`orient upright`, `fold laptop`). Set the orientation for every pose, once the hinge has stopped: the orientation survives a fold, so the cover comes up sideways after an upright pose. Turn every pose into every other, both ways, with a detail open; the app must keep its place and keep responding. Look at each picture. Seed real data first, in the language of the screenshots you want; an empty screen hides layout bugs. Split View (half the open phone, 475×669, compact) and a video pinned in Picture in Picture (951×420; 669×455 over the laptop's raised half) can't be driven headless: resize a hosting window through those sizes in a unit test, and every page must settle and keep its state. (`devicectl device appResize start --preferred-size WxH` resizes the real app while the phone is shut and unlocked, but on a plain display without the Duo's column or crease.) StandBy: `devicectl device settings standby --show --duration 10` presents it from the lock screen with the interface landscape (open sideways, or shut and turned `landscape-right`); folding and turning alone never do, and it opens on a welcome screen.

| Pose | Window (pt) | Safe-area insets | System chrome | Crease (active part folded, inactive lying flat) | `toolbarVerticalEdge` |
|---|---|---|---|---|---|
| Shut | 466×678 | trailing 84, bottom 34 | clock column 84×170 at the top of the trailing edge; camera | none | trailing |
| Open sideways | 951×669 | trailing 84, bottom 34 | clock column 84×120 | x 455.5–495.5, down the middle of the glass | trailing |
| Open upright | 669×951 | top 82, bottom 34 | status cluster 134×82, top right | y 455.5–495.5, across the middle | nil |

Measured on iOS 27.1 (24A94232). The page inside the insets is 382 pt wide shut and 867 pt open sideways. The book pose is about 128°, the laptop pose an upright phone at about 110°. Shut while turned, the cover lies 678×466 with the system's column down the leading edge.

If a turn hangs the app: the simulator's crash report is in the Mac's `~/Library/Logs/DiagnosticReports/` (`0x8BADF00D`, the watchdog), `sample <pid> 3` shows where the main thread is stuck, and `log show --info --debug --predicate 'subsystem BEGINSWITH "com.apple.SwiftUI"'` (through `simctl spawn`) prints faults like "tried to update multiple times per frame" and the output of `Self._logChanges()` in suspect bodies.

## APIs (Xcode 27.1 SDK)

- `@Environment(\.toolbarVerticalEdge)`: `.trailing` or `.leading`, the system's preferred edge for a vertical bar even while none is showing (from the locale and the device); nil only where it never uses one, such as the open phone upright. Pair it with the safe-area inset on that edge. UIKit: `traitCollection.verticalBarEdge` (`.unspecified` for nil).
- `GeometryProxy.reservedRegions(kind: .division)` for the crease, `.occlusion` for the clock and camera; each has `frame` (in the queried view's coordinates, mirrored for right-to-left unless `layoutDirectionBehavior: .fixed`), `margins`, `isActive`. Only active regions come back by default in the simulator, though Apple's method documentation says otherwise: filter on `isActive` yourself. Nor are they limited to the regions that intersect the queried view, as the documentation says: a small view gets every region on the panel, so test frames against your bounds. Read them in a `GeometryReader` body or an `onGeometryChange` transform: they arrive after the first layout pass. UIKit: `UIView.reservedRegions(kind:options:)`. `options: .includeInactive` adds the fold while the phone lies flat (inactive, where it will bend; 40 pt wide in the simulator, zero wide per Apple) and the inner display's front camera while unused. For one pass after a turn or a close the regions are the previous pose's: check them against the page.
- `onHingeChange { old, new in }` passes two `DeviceHingeContext`s; `hinge` is nil without one, else `status` (`.closed`, `.partiallyOpen`, `.fullyOpen`; a struct, so `switch` needs `default`) and `angle`. For interactions and effects only; layout comes from the regions.
- Bars: `axisBehavior(.verticalPreferred | .horizontalOnly)` on toolbar items, `toolbarVerticalBehavior(.disabled)` and `toolbarVerticalCompressionBehavior` are 27.1 SDK; `visibilityPriority` and `ToolbarOverflowMenu` are in 27.0. Custom views stay horizontal unless they opt in.
- Keep stock Xcode 27.0 building: a compilation condition on by default and cleared for `[sdk=iphonesimulator27.0*]` / `[sdk=iphoneos27.0*]`, around `if #available(iOS 27.1, *)`. With a Mac Catalyst destination clear it for `[sdk=macosx*]` too: Xcode 27.1 RC builds Catalyst against macOS 27.0, which has none of the fold APIs. `#if compiler` can't tell the two Xcodes apart (both Swift 6.4). Inside a `.toolbar { }` builder `if #available` needs iOS 17.5; with an older deployment target, branch around the whole `.toolbar` call.

## Layout rules that held up

- Keep the system's insets. Don't reserve a band for the clock; don't mirror the side column on the other edge. Photographs may run under the side column, never under a split view's list: the detail column's leading inset is the list's whole width (455 pt open sideways), so treat an inset wider than any bar as a column.
- A custom tab bar belongs in the system's column when `toolbarVerticalEdge` isn't nil (below the clock, at the foot), not floating beside an empty column and not stood on its side over the page. The column is shared: fit the bar below the active occlusions over it (the Dynamic Island grows down it with Live Activities), shrinking to 44pt targets, and give way to the horizontal bar when it no longer fits.
- Custom toolbar items join the column only with `.axisBehavior(.verticalPreferred)` and a vertical layout; keep them `.horizontalOnly` where your own rail stands. A sheet whose bar holds one close or done button: `.toolbarVerticalBehavior(.disabled)`.
- A column narrower than its page centres on the glass, not on the inset page, moving only into its own margin.
- Redesign wide screens instead of stretching the phone layout: spreads and panes when the page is wider than tall; one column when it stands upright. Keep content at its own shape: narrow a column rather than crop posters to squares.
- A portrait photograph on a page lying wide gets a leaf of its own (the open phone's left leaf is about 455×669, a poster's shape), not a full-width banner that shows its top quarter.
- Side-by-side sections share one heading height; a control in one heading otherwise drops its title and card below its neighbour's.
- Let text cross the crease, keep controls off it, put two-pane gutters and grid gaps on it (the same number of items on each leaf). Lying flat, the `.includeInactive` division region says where the crease will run: lay the gutter there too, only while the page lies wider than tall and the region spans the page's full height, so bending the phone reflows nothing. Don't work it out from the hinge angle: a threshold lapses mid-bend.
- Move what the eye and the thumb go to (a centred carousel's front item, a lone control) off the crease, and its caption with it; scrolling content may cross.
- Never decide a layout from the size the decision produces. A two-pane row that chose its gutter from its own frame flipped for ever inside one layout pass and hung the app on the turn to upright. Measure the width you're offered: `.frame(minWidth: 0, maxWidth: .infinity)` takes it whatever the content asks. A page that picks its layout from its own frame needs the same, heights too: one pass at the window's width chose a fixed-width leaf, the page outgrew its split column, and it re-centred and re-measured itself at full CPU.
- Laptop pose (upright, about 110°), on every page that can be open in it: what you look at on the raised half, what you touch on the flat half, each scrolling on its own. The raised half is wider than tall: show the content's landscape form there, not a shrunken portrait. Keep every action the other poses have; a pose layout that swaps a page can drop one.
- Below about 800pt (the open phone upright, an iPad mini in portrait; observed on iOS 27.1, not documented) a `NavigationSplitView` floats its list over a dimmed detail and can drop it on a turn. Give it `.environment(\.horizontalSizeClass, .compact)` there so it collapses into a stack.
- Wider, line a split view up with the crease: the crease's near edge as the sidebar's ideal width (where the system puts the divider itself when part folded), a detail minimum of 320pt (else the detail keeps ~448pt and the list stops short lying flat), and a leading inset of the crease on the detail. Never `min == ideal == max`: it spun the layout at full CPU.
- A split view expanded from a stack (opened, or turned sideways) gives its list a fresh, visible navigation bar despite `.toolbar(.hidden, …)`. `.toolbar(removing: .sidebarToggle)`, then flip the bar `.visible` → `.hidden` once when the size class turns regular.
- Collapsed with a detail open (turned upright), it comes up on the list although `preferredCompactColumn` says `.detail`: set it to the list and back on the next main-actor turn. Claims on shared chrome (hide the tab bar) belong to each view, not to the item: the collapse builds the new view before the old one leaves.
- Don't leave a detail pane empty beside its list on the open phone; show the newest items instead of a prompt.
- A hinge gesture must not fire on the way to shut or while resting half open, and needs a second way in.
- Animate only a bend: the crease turning active or resting while the window (content plus insets) keeps its size. Decide it where the fold readings are published (`withAnimation` for a bend, none under Reduce Motion), never with `.animation(_:value:)` on the pose, which also fires during a turn. Opening, shutting and turning are the system's. A layout that comes in draws images already decoded in its first frame (the exact request, else a decode of the same picture at least half the size), or it shows placeholders. A page whose crossfade blinks (Flicit's two scroll-view layouts) cuts: `.transaction(value:) { $0.animation = nil }`.
- Don't switch view identity when chrome appears (`if height > 0 { content.modifier() } else { content }` rebuilds every scroll view under it).

## Mock-ups

`scripts/duo_mockup.py <capture> <out> --pose closed|open|book|upright|laptop [--deg N]` sets a capture in Apple's product bezels, folding the art for book and laptop. Needs numpy and Pillow, and the bezels from Apple Design Resources in `~/Library/Application Support/apple-duo-bezels/` (or `--bezels`). This kit ships no Apple artwork; don't republish the bezel PNGs.
