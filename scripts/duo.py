#!/usr/bin/env python3
"""Drive the iPhone Duo simulator from a script: fold it, turn it, capture it.

  python3 duo.py devices                      # Duo simulators on this Mac
  python3 duo.py status                       # booted? which panel draws? last pose set
  python3 duo.py fold open                    # or close | book | half | 0..180
  python3 duo.py orient upright               # or sideways (the open phone's native pose)
  python3 duo.py sweep 180 128 0.8            # animate the hinge
  python3 duo.py path 1.0 180 140 180         # one motion through several angles
  python3 duo.py shot --out shots/            # capture the panel that is drawing
  python3 duo.py record --out films/ path:1.2:0:180 rec:inner wait:4   # film a fold
  python3 duo.py launch com.example.app -- -someLaunchArgument value
  python3 duo.py scene --fold 110 --orient upright --bundle com.example.app --out shots/

The simulator has no public way to fold or turn a Duo: Xcode's Device Hub posts a
vendor-defined HID event for its hinge slider and its orientation picker, and
duo_fold.c (next to this file) posts the same event from inside the simulator.
It is compiled once with the iOS Simulator SDK and cached in ~/.cache/duo-kit.

Rules this tool keeps, and you should too:
  * Never run `xcrun simctl io <duo> screenConfig`. On a Duo it leaves SpringBoard
    crash-looping on the next hinge event; recovery is shutdown + boot.
  * There is no angle readback. This tool remembers what it last set, and confirms
    the pose by which panel is drawing (the off panel captures pure black).
  * The simulator's hinge has hysteresis: opened from shut, the cover stays lit and
    the hinge reports closed up to about 150°. `fold` to a part-folded angle from
    shut therefore opens the phone flat first, then bends it.
  * Panels are framebuffer ports whose UUIDs change on every boot. They are resolved
    by pixel size each time: cover 1398x2034, inner 2007x2853.

Pick the device with --udid or DUO_UDID; otherwise the only available simulator whose
name contains "Duo" is used. Nothing here creates, clones or shuts down a simulator.

From https://foldhunt.com/guides/everything-your-agent-needs-for-iphone-duo/
"""
import argparse
import hashlib
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = pathlib.Path(__file__).resolve().parent
CACHE = pathlib.Path(os.environ.get('DUO_KIT_CACHE', pathlib.Path.home() / '.cache/duo-kit'))
PANEL_SIZES = {'cover': (1398, 2034), 'inner': (2007, 2853)}
CONTENT_LUMA = 24          # a lit pixel; an off panel screenshots pure black

FOLDS = {'open': 180.0, 'flat': 180.0, 'close': 0.0, 'closed': 0.0, 'shut': 0.0,
         'book': 128.0, 'laptop': 110.0, 'half': 90.0}
# Device Hub's own names, plus two for the open phone. Open, the Duo's native pose
# is sideways (crease running down), which the device calls portrait; landscape-left
# stands it upright with the crease running across.
ORIENTATIONS = {'portrait': 'portrait', 'pud': 'pud', 'upside-down': 'pud',
                'landscape-left': 'landscape-left', 'landscape-right': 'landscape-right',
                'sideways': 'portrait', 'upright': 'landscape-left'}


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def say(text):
    print(text, flush=True)


# --------------------------------------------------------------------------- device

def duo_devices():
    data = json.loads(run('xcrun', 'simctl', 'list', 'devices', 'available', '-j').stdout)
    found = []
    for runtime, devices in data['devices'].items():
        for device in devices:
            if 'duo' in device['name'].lower():
                found.append({'udid': device['udid'], 'name': device['name'],
                              'state': device['state'], 'runtime': runtime.rsplit('.', 1)[-1]})
    return found


def duo_udid(explicit=None):
    udid = explicit or os.environ.get('DUO_UDID')
    devices = duo_devices()
    if udid:
        if not any(d['udid'] == udid for d in devices):
            raise SystemExit(f'{udid} is not an available Duo simulator (see: duo.py devices).')
        return udid
    if len(devices) == 1:
        return devices[0]['udid']
    if not devices:
        raise SystemExit('No iPhone Duo simulator found. Install Xcode 27.1 and its iOS 27.1 '
                         'simulator runtime; this tool does not create devices.')
    raise SystemExit('Several Duo simulators exist; pick one with --udid or DUO_UDID:\n' +
                     '\n'.join(f"  {d['udid']}  {d['name']}  ({d['state']})" for d in devices))


def ensure_booted(udid):
    state = next((d['state'] for d in duo_devices() if d['udid'] == udid), None)
    if state != 'Booted':
        say(f'  booting {udid}')
        run('xcrun', 'simctl', 'boot', udid, check=False)
        run('xcrun', 'simctl', 'bootstatus', udid, '-b')


def state_path(udid):
    return CACHE / f'state-{udid}.json'


def load_state(udid):
    try:
        return json.loads(state_path(udid).read_text())
    except (OSError, ValueError):
        return {}


def save_state(udid, **values):
    state = load_state(udid)
    state.update(values, updated=time.strftime('%Y-%m-%dT%H:%M:%S'))
    CACHE.mkdir(parents=True, exist_ok=True)
    state_path(udid).write_text(json.dumps(state))


# --------------------------------------------------------------------------- hinge and orientation

def helper():
    """duo_fold.c compiled for the simulator, cached by its source hash."""
    source = HERE / 'duo_fold.c'
    if not source.exists():
        raise SystemExit(f'{source} is missing; fetch it next to duo.py.')
    binary = CACHE / f"duo_fold-{hashlib.sha1(source.read_bytes()).hexdigest()[:12]}"
    if not binary.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = binary.with_suffix('.tmp')
        build = run('xcrun', '-sdk', 'iphonesimulator', 'clang', '-arch', 'arm64',
                    '-mios-simulator-version-min=17.0', '-O2', '-o', str(tmp), str(source),
                    '-framework', 'IOKit', '-framework', 'CoreFoundation', check=False)
        if build.returncode != 0:
            raise SystemExit(f'could not build duo_fold:\n{build.stderr}')
        run('codesign', '-f', '-s', '-', str(tmp), check=False)
        tmp.rename(binary)
    return binary


def spawn(udid, *args):
    result = run('xcrun', 'simctl', 'spawn', udid, str(helper()), *args, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f'duo_fold {" ".join(args)} failed')


def angle_of(text):
    if text in FOLDS:
        return FOLDS[text]
    try:
        value = float(text)
    except ValueError:
        raise SystemExit(f'expected {"|".join(FOLDS)} or 0..180, got {text!r}')
    if not 0 <= value <= 180:
        raise SystemExit('the angle must be between 0 and 180')
    return value


def posture(angle):
    if angle is None:
        return 'unknown'
    return 'shut' if angle <= 20 else 'open' if angle >= 160 else 'part folded'


def lit_panel(udid):
    """'cover' or 'inner', whichever devicectl reports active; None if it can't tell."""
    result = run('xcrun', 'devicectl', 'device', 'info', 'displays', '--device', udid,
                 '-j', '-', check=False)
    try:
        displays = json.loads(result.stdout)['result']['displays']
    except (ValueError, KeyError, TypeError):
        return None
    for display in displays:
        if display.get('active'):
            for which, size in PANEL_SIZES.items():
                if sorted(display.get('nativeSize', [])) == sorted(size):
                    return which
    return None


def fold(udid, target):
    angle = angle_of(target)
    if 0 < angle < 180:
        # Opened from shut, the simulator keeps the cover lit and the hinge closed up
        # to about 150°, and lights the inner display only past 160°. Bend from flat,
        # as a hand would, unless the inner display is already drawing.
        panel = lit_panel(udid)
        if panel == 'cover' or (panel is None and (load_state(udid).get('angle') or 0) <= 20):
            spawn(udid, 'set', '180')
            time.sleep(1.5)
    spawn(udid, 'set', f'{angle:g}')
    save_state(udid, angle=angle)
    say(f'  hinge {angle:g}° ({posture(angle)})')


def orient(udid, name, settle=1.0):
    if name not in ORIENTATIONS:
        raise SystemExit(f"orient expects one of {', '.join(ORIENTATIONS)}, got {name!r}")
    value = ORIENTATIONS[name]
    # An orientation posted while the hinge is still moving is sometimes dropped:
    # post it, let the device settle, post it again. Posting twice is harmless.
    spawn(udid, 'orient', value)
    time.sleep(settle)
    spawn(udid, 'orient', value)
    save_state(udid, orientation=value)
    say(f'  orientation {value}')


# --------------------------------------------------------------------------- panels

def ports(udid):
    """{'cover'|'inner': uuid} from `simctl io enumerate`, matched by pixel size."""
    text = run('xcrun', 'simctl', 'io', udid, 'enumerate').stdout
    found, block = {}, None
    for line in text.splitlines():
        key, _, value = line.strip().partition(':')
        value = value.strip()
        if line.strip() == 'Port:':
            block = {}
        elif block is not None and key == 'UUID':
            block['uuid'] = value
        elif block is not None and key in ('Default width', 'Default height'):
            block[key] = int(value)
        if block and 'uuid' in block and 'Default width' in block and 'Default height' in block:
            for name, size in PANEL_SIZES.items():
                if {block['Default width'], block['Default height']} == set(size):
                    found[name] = block['uuid']
    return found


def png_peak(path, rows=40):
    """Brightest value in the top rows of an 8-bit PNG, decoded with the standard library."""
    raw = pathlib.Path(path).read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    pos, idat, width, depth, color = 8, bytearray(), 0, 8, 6
    while pos + 8 <= len(raw):
        length = struct.unpack('>I', raw[pos:pos + 4])[0]
        kind, chunk = raw[pos + 4:pos + 8], raw[pos + 8:pos + 8 + length]
        if kind == b'IHDR':
            width, _, depth, color = struct.unpack('>IIBB', chunk[:10])
        elif kind == b'IDAT':
            idat += chunk
        elif kind == b'IEND':
            break
        pos += 12 + length
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if channels is None or depth != 8:
        return 255                       # unusual format: assume it drew something
    data, stride = zlib.decompress(bytes(idat)), width * channels
    previous, peak, offset = bytearray(stride), 0, 0
    for _ in range(rows):
        if offset + 1 + stride > len(data):
            break
        kind, line = data[offset], bytearray(data[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up, upleft = previous[i], previous[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 255
            elif kind == 4:
                pa, pb, pc = abs(up - upleft), abs(left - upleft), abs(left + up - 2 * upleft)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else upleft)) & 255
        colour = [line[i] for i in range(stride) if channels < 3 or i % channels < 3]
        peak = max(peak, max(colour or [0]))
        previous = line
    return peak


def grab(udid, uuid, path):
    result = run('xcrun', 'simctl', 'io', udid, 'screenshot', f'--display={uuid}', str(path), check=False)
    return result.returncode == 0 and pathlib.Path(path).exists()


def lit(udid, uuid, path, wait):
    """Capture a panel, retrying until it draws something (an off panel is black)."""
    deadline = time.time() + wait
    while True:
        if grab(udid, uuid, path) and (png_peak(path) or 0) > CONTENT_LUMA:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.6)


def shot(udid, out, name, panel='active', wait=10.0):
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    found = ports(udid)
    if not found:
        raise SystemExit('no display ports found; is the Duo booted?')
    if panel == 'both':
        for which in ('cover', 'inner'):
            if which in found and grab(udid, found[which], out / f'{name}-{which}.png'):
                say(f'  {out / f"{name}-{which}.png"}')
        return
    angle = load_state(udid).get('angle')
    order = [panel] if panel != 'active' else (
        ['cover', 'inner'] if angle is not None and angle <= 20 else ['inner', 'cover'])
    for which in order:
        target = out / f'{name}-{which}.png'
        if which in found and lit(udid, found[which], target, wait):
            say(f'  {target}  ({which} panel)')
            return
        say(f'  ({which} panel stayed dark)')
    raise SystemExit(f'no panel drew within {wait:g}s; is the app running?')


def record(udid, out, name, steps, panels=None, lead=1.5, tail=2.5):
    """Film a fold or a turn, so a transition can be judged frame by frame
    (extract with ffmpeg -fps_mode passthrough: frames land only when something
    changes, at irregular times).

    Steps: fold:<target>, orient:<name>, path:<seconds>:<deg>:<deg>...,
    wait:<seconds> and rec:<cover|inner>. A device films one panel at a time
    (a second recordVideo never finishes its file), so filming starts on the
    panel that is drawing and rec: switches to the other once a fold has lit
    it, leaving one file per panel."""
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    found = ports(udid)
    recorders = []

    def finish():
        # One device films one panel at a time: a second recordVideo never
        # finishes its file, so stop the running one before switching.
        while recorders:
            panel, target, process = recorders.pop()
            process.send_signal(2)       # SIGINT: recordVideo finishes the file
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            written = target.exists() and target.stat().st_size > 0
            say(f'  {target}  ({panel})' if written else f'  ({panel}: no video written)')

    def start(panel, wait_until_lit=False):
        if panel not in found:
            raise SystemExit(f'no {panel} port found')
        if any(p == panel for p, _, _ in recorders):
            return
        if wait_until_lit:
            with tempfile.TemporaryDirectory() as tmp:
                if not lit(udid, found[panel], pathlib.Path(tmp) / 'probe.png', 10):
                    say(f'  ({panel} panel did not light up; not filmed)')
                    return
        target = out / f'{name}-{panel}.mp4'
        finish()
        process = subprocess.Popen(
            ['xcrun', 'simctl', 'io', udid, 'recordVideo', '--codec=h264', '--force',
             f'--display={found[panel]}', str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        recorders.append((panel, target, process))

    angle = load_state(udid).get('angle')
    try:
        start(panels or ('cover' if angle is not None and angle <= 20 else 'inner'))
        time.sleep(lead)                 # recordVideo needs a moment before its first frame
        for step in steps:
            kind, _, rest = step.partition(':')
            values = [v for v in rest.split(':') if v]
            if kind == 'fold' and len(values) == 1:
                fold(udid, values[0])
            elif kind == 'orient' and len(values) == 1:
                orient(udid, values[0])
            elif kind == 'path' and len(values) >= 3:
                spawn(udid, 'path', *values)
                save_state(udid, angle=float(values[-1]))
                say(f"  path {' -> '.join(values[1:])} in {values[0]}s")
            elif kind == 'wait' and len(values) == 1:
                time.sleep(float(values[0]))
            elif kind == 'rec' and len(values) == 1:
                start(values[0], wait_until_lit=True)
            else:
                raise SystemExit(f'unknown step {step!r} (fold:X, orient:X, path:S:A:B..., wait:S, rec:PANEL)')
        time.sleep(tail)
    finally:
        finish()


# --------------------------------------------------------------------------- app

def launch(udid, bundle, extra):
    run('xcrun', 'simctl', 'terminate', udid, bundle, check=False)
    result = run('xcrun', 'simctl', 'launch', udid, bundle, *extra, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or 'launch failed')
    say(f"  launched {bundle} {' '.join(extra)}".rstrip())


def status(udid):
    device = next(d for d in duo_devices() if d['udid'] == udid)
    say(f"  device: {device['name']} {udid} ({device['runtime']}) {device['state'].lower()}")
    if device['state'] != 'Booted':
        return
    state = load_state(udid)
    angle = state.get('angle')
    say(f"  hinge:  {f'{angle:g}°' if angle is not None else 'unset'} (last set by this tool; no readback exists)")
    say(f"  turned: {state.get('orientation', 'unset')} (last set by this tool)")
    found = ports(udid)
    with tempfile.TemporaryDirectory() as tmp:
        for which in ('cover', 'inner'):
            if which in found:
                path = pathlib.Path(tmp) / f'{which}.png'
                draws = grab(udid, found[which], path) and (png_peak(path) or 0) > CONTENT_LUMA
                say(f"  {which} panel: {'drawing' if draws else 'dark'}  {found[which]}")


# --------------------------------------------------------------------------- cli

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--udid', help='the Duo simulator to use (default: $DUO_UDID or the only one)')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('devices', help='list Duo simulators')
    commands.add_parser('status', help='device, last pose set, which panel draws')
    commands.add_parser('panels', help='the framebuffer port of each panel')
    p = commands.add_parser('fold', help='set the hinge: open | close | book | laptop | half | 0..180')
    p.add_argument('target')
    p = commands.add_parser('orient', help='turn the device: upright | sideways | portrait | landscape-left | ...')
    p.add_argument('name')
    p = commands.add_parser('sweep', help='animate the hinge from one angle to another')
    p.add_argument('start', type=float)
    p.add_argument('end', type=float)
    p.add_argument('seconds', type=float, nargs='?', default=1.0)
    p = commands.add_parser('path', help='one timed motion through several angles')
    p.add_argument('seconds', type=float)
    p.add_argument('angles', type=float, nargs='+')
    p = commands.add_parser('shot', help='capture a panel (default: the one that is drawing)')
    p.add_argument('--out', default='.')
    p.add_argument('--name', default='duo')
    p.add_argument('--panel', choices=['active', 'cover', 'inner', 'both'], default='active')
    p.add_argument('--wait', type=float, default=10.0)
    p = commands.add_parser('record', help='film the panels through folds and turns, e.g. path:1.2:0:180 rec:inner wait:4')
    p.add_argument('steps', nargs='+', help='fold:X | orient:X | path:S:A:B... | wait:S | rec:cover|inner')
    p.add_argument('--out', default='.')
    p.add_argument('--name', default='duo')
    p.add_argument('--panel', dest='panels', choices=['cover', 'inner'],
                   help='panel filmed from the start (default: the one drawing)')
    p.add_argument('--lead', type=float, default=1.5, help='seconds filmed before the first step')
    p.add_argument('--tail', type=float, default=2.5, help='seconds filmed after the last step')
    p = commands.add_parser('launch', help='(re)launch an app; launch arguments after --')
    p.add_argument('bundle')
    p.add_argument('extra', nargs=argparse.REMAINDER)
    p = commands.add_parser('scene', help='fold, turn, launch, wait and capture in one go')
    p.add_argument('--fold', default='open')
    p.add_argument('--orient')
    p.add_argument('--bundle', help='app to relaunch after posing (recommended: apps lay out on launch too)')
    p.add_argument('--settle', type=float, default=3.0, help='seconds to wait after each step')
    p.add_argument('--out', default='.')
    p.add_argument('--name', default='scene')
    p.add_argument('--panel', choices=['active', 'cover', 'inner', 'both'], default='active')
    p.add_argument('--wait', type=float, default=15.0)
    p.add_argument('extra', nargs=argparse.REMAINDER, help='launch arguments after --')
    args = parser.parse_args()

    if args.command == 'devices':
        for d in duo_devices():
            say(f"{d['udid']}  {d['name']}  {d['runtime']}  {d['state']}")
        return
    udid = duo_udid(args.udid)
    if args.command == 'status':
        status(udid)
        return
    ensure_booted(udid)
    extra = [a for a in getattr(args, 'extra', []) or [] if a != '--']
    if args.command == 'panels':
        for which, uuid in ports(udid).items():
            say(f'{which}\t{uuid}')
    elif args.command == 'fold':
        fold(udid, args.target)
    elif args.command == 'orient':
        orient(udid, args.name)
    elif args.command == 'sweep':
        spawn(udid, 'sweep', f'{args.start:g}', f'{args.end:g}', f'{args.seconds:g}')
        save_state(udid, angle=args.end)
        say(f'  swept {args.start:g}° -> {args.end:g}° in {args.seconds:g}s')
    elif args.command == 'path':
        if len(args.angles) < 2:
            raise SystemExit('path needs at least two angles')
        spawn(udid, 'path', f'{args.seconds:g}', *[f'{a:g}' for a in args.angles])
        save_state(udid, angle=args.angles[-1])
        say(f"  path {' -> '.join(f'{a:g}°' for a in args.angles)} in {args.seconds:g}s")
    elif args.command == 'shot':
        shot(udid, args.out, args.name, args.panel, args.wait)
    elif args.command == 'record':
        record(udid, args.out, args.name, args.steps, args.panels, args.lead, args.tail)
    elif args.command == 'launch':
        launch(udid, args.bundle, extra)
    elif args.command == 'scene':
        fold(udid, args.fold)
        time.sleep(args.settle)
        if args.orient:
            orient(udid, args.orient)
            time.sleep(args.settle)
        if args.bundle:
            launch(udid, args.bundle, extra)
            time.sleep(args.settle)
        shot(udid, args.out, args.name, args.panel, args.wait)


if __name__ == '__main__':
    main()
