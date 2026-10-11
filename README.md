# iPhone Duo simulator skill for Claude Code, Codex and other agents

An agent skill that drives Apple's **iPhone Duo simulator** (Xcode 27.1) from the command line. It folds and turns the simulator, screenshots and films whichever display is drawing, taps through Xcode's device-interaction tools, and sets captures in Apple's bezels, folded for the book and laptop poses. `SKILL.md` also carries what each pose reports, measured in the simulator: window sizes, safe-area insets, the crease and the other reserved regions, and the vertical bar's edge. It ends with the layout rules that held up when a real app was adapted.

It comes from adapting [Flicit](https://flicit.top), a SwiftUI app for collecting movie tickets, for iPhone Duo with Claude Code. The full guide, with the reasoning, the measurements and the pictures:

- **English:** [Adapt your app for iPhone Duo with a coding agent](https://foldhunt.com/guides/everything-your-agent-needs-for-iphone-duo/)
- **简体中文：** [借助编程智能体将 App 适配到 iPhone Duo](https://foldhunt.com/zh/guides/everything-your-agent-needs-for-iphone-duo/)

These are the same files the guide links to at [foldhunt.com/kits/iphone-duo](https://foldhunt.com/kits/iphone-duo/SKILL.md), mirrored here.

## Install

For Claude Code, in one project:

```sh
git clone --depth 1 https://github.com/WhitenWhiten/iphone-duo-simulator-skill .claude/skills/iphone-duo-simulator
```

For all your projects, clone it into `~/.claude/skills/iphone-duo-simulator` instead. Agents that read Agent Skills, such as Codex, load the same folder from their own skills directory.

A good first instruction: *Read the iphone-duo-simulator skill and Apple's app-resizability skill. Run app-resizability's prerequisite checks, build with Xcode 27.1, then walk every pose and report what looks wrong before changing any code.* Xcode 27.1 writes Apple's skill with `xcrun agent skills export --output-dir ./xcode-skills`.

## What it does

```sh
python3 scripts/duo.py devices                      # find the Duo simulator
python3 scripts/duo.py fold close|open|book|laptop|0..180
python3 scripts/duo.py orient sideways|upright      # open phone: sideways is its native pose
python3 scripts/duo.py path 1.0 180 140 180         # a timed bend and back, for hinge gestures
python3 scripts/duo.py shot --out shots/            # screenshot the display that is drawing
python3 scripts/duo.py record --out films/ path:1.2:0:180 rec:inner wait:4   # film a fold
python3 scripts/duo.py scene --fold laptop --orient upright --bundle com.example.app --out shots/
python3 scripts/xcode_device.py start --device <DUO-UDID>   # taps and the UI hierarchy, phone shut
python3 scripts/duo_mockup.py capture.png book.png --pose book --deg 128
```

`duo.py` posts the same vendor-defined HID events as Device Hub's hinge slider and orientation picker (usage page `0xFF61`, usage `0x5B`) from inside the simulator, through `duo_fold.c`, which it compiles on first use.

## What the simulator does and doesn't do (Xcode 27.1 RC)

- **No hinge-angle readback.** `devicectl device motion hinge-angle` doesn't exist in `devicectl` 651.13.5. `xcrun devicectl device info displays` does say which display is active, so whether the phone is shut or open.
- **The hinge has hysteresis.** Opened from shut, the cover stays lit and the hinge reports closed up to about 150°. `duo.py fold` therefore opens the phone flat before bending it.
- **Never run `xcrun simctl io <duo> screenConfig`.** It leaves SpringBoard crash-looping on the next hinge event. Recovery is `simctl shutdown` and `simctl boot`.
- **Xcode's device-interaction tools see only the cover.** They tap and read the UI while the phone is shut. To tap the inner display, [duoctl](https://github.com/skarol/duoctl) works.
- **StandBy** comes up with `xcrun devicectl device settings standby --show --duration 10` from the lock screen, with the interface in landscape. Folding and turning alone never trigger it.

## Requirements

- macOS on Apple silicon, Xcode 27.1 with the iOS 27.1 simulator runtime, and Python 3.
- The mock-up script needs numpy and Pillow, and Apple's iPhone Duo bezels from [Apple Design Resources](https://developer.apple.com/design/resources/), which you download yourself. Their licence covers mock-ups of your app, not republishing the art, so none is included here.
- The scripts use private, undocumented simulator interfaces. They were verified against Xcode 27.1 RC (27A9275) and the iOS 27.1 runtime (24A94232); a later Xcode may change them.

## Related

- [hinge](https://github.com/artemnovichkov/hinge), Artem Novichkov's CLI for the hinge angle
- [duoctl](https://github.com/skarol/duoctl), which folds, rotates and taps the simulator, inner display included
- [iPhone Duo by Examples](https://github.com/artemnovichkov/iPhone-Duo-by-Examples), SwiftUI samples of the iOS 27.1 fold APIs
- [iPhone Duo developer tools](https://foldhunt.com/iphone-duo/tools/), a directory of MCP servers, agent skills and libraries

## License

MIT. Not affiliated with Apple. iPhone and Xcode are trademarks of Apple Inc.
