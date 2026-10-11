#!/usr/bin/env python3
"""Tap and read a simulator through Xcode's own device-interaction tools.

  python3 xcode_device.py start --device <UDID>     # open a session (Xcode asks once to allow it)
  python3 xcode_device.py look                      # screenshot + UI hierarchy, lists what can be tapped
  python3 xcode_device.py tap "Expand search"       # tap an element by its label, then look again
  python3 xcode_device.py do "t 200 560 f 200 260 0.4"   # any interaction command, then look
  python3 xcode_device.py end                       # close the session (they are expensive)

Xcode exposes MCP tools through `xcrun mcpbridge`, among them
DeviceInteractionStartSession, DeviceInteractionSynthesize and DeviceInteractionEndSession.
This is a small stdio client for those three, so a coding agent can drive them from a
shell. Commands for `do` are the device-interaction skill's: `t x y` (tap), `d x y`
(double tap), `t x1 y1 f x2 y2 secs` (swipe), `drag`, `mt` (multi-touch), `b h|p|u|d`
(buttons), `w secs` (wait), `orientation <name>`, and `sender keyboard kbd <text>` (last).
Export the skill itself with `xcrun agent skills export` from Xcode 27.1.

On the iPhone Duo simulator (tested with Xcode 27.1):
  * Shut, everything works: a real screenshot of the cover, the hierarchy with labels
    and hit points, taps and swipes.
  * Open, Xcode still sees only the cover: the screenshot is black, the hierarchy stops
    at the cover's width, and taps don't land. Fold it shut with duo.py to tap, and use
    duo.py for pictures of the open phone. There is no hinge command here.
  * A hierarchy captured during an animation has stale hit points. If a tap does
    nothing, look again and retry once.

Environment: DEVELOPER_DIR picks the Xcode (use 27.1 for the Duo); XCODE_PID picks the
running Xcode instance when several are open. Session state lives in ~/.cache/duo-kit.

From https://foldhunt.com/guides/everything-your-agent-needs-for-iphone-duo/
"""
import argparse
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

CACHE = pathlib.Path(os.environ.get('DUO_KIT_CACHE', pathlib.Path.home() / '.cache/duo-kit'))
STATE = CACHE / 'xcode-session.json'
ELEMENT = re.compile(r"^(?P<indent>\s*)(?P<kind>[A-Za-z]+), \{\{(?P<x>[-\d.]+), (?P<y>[-\d.]+)\}, "
                     r"\{(?P<w>[\d.]+), (?P<h>[\d.]+)\}\}(?P<rest>.*)$")
HIT = re.compile(r"hitPoint: \{(?P<x>[-\d.]+), (?P<y>[-\d.]+)\}")
LABEL = re.compile(r"label: '(?P<label>(?:[^'\\]|\\.)*)'")
IDENT = re.compile(r"identifier: '(?P<id>(?:[^'\\]|\\.)*)'")


def xcode_pid():
    if os.environ.get('XCODE_PID'):
        return os.environ['XCODE_PID']
    developer = os.environ.get('DEVELOPER_DIR')
    if not developer:
        return None
    app = developer.split('/Contents/Developer')[0]
    found = subprocess.run(['pgrep', '-f', f'{app}/Contents/MacOS/Xcode'],
                           capture_output=True, text=True).stdout.split()
    return found[0] if found else None


class Bridge:
    """One `xcrun mcpbridge` process speaking JSON-RPC over stdio."""

    def __init__(self, timeout):
        env = dict(os.environ)
        pid = xcode_pid()
        if pid:
            env['MCP_XCODE_PID'] = pid
        self.timeout = timeout
        self.proc = subprocess.Popen(['xcrun', 'mcpbridge'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        self.lines = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(l) for l in self.proc.stdout], daemon=True).start()
        self.next_id = 0
        self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                    'clientInfo': {'name': 'duo-kit', 'version': '1'}})
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        self.proc.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.next_id,
                                          'method': method, 'params': params}) + '\n')
        self.proc.stdin.flush()
        deadline = time.time() + self.timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                raise SystemExit(f'Xcode did not answer {method} within {self.timeout:g}s. The first '
                                 'connection waits for you to allow it in Xcode; check Xcode for a prompt, '
                                 'and that Xcode is running (set DEVELOPER_DIR / XCODE_PID).')
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get('id') == self.next_id:
                if 'error' in message:
                    raise SystemExit(f"{method}: {message['error'].get('message', message['error'])}")
                return message['result']

    def call(self, tool, arguments):
        result = self.request('tools/call', {'name': tool, 'arguments': arguments})
        data = result.get('structuredContent')
        if data is None:
            text = ''.join(c.get('text', '') for c in result.get('content', []))
            try:
                data = json.loads(text)
            except ValueError:
                data = {'text': text}
        if isinstance(data, dict) and data.get('type') == 'error':
            raise SystemExit(f"{tool}: {data.get('data')}")
        return data

    def close(self):
        self.proc.terminate()


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        raise SystemExit('No session; run: xcode_device.py start --device <UDID>')


def elements(hierarchy_text):
    """(kind, label, identifier, frame, hit point) for every element of the app."""
    found = []
    for line in hierarchy_text.splitlines():
        match = ELEMENT.match(line)
        if not match:
            continue
        rest = match['rest']
        hit, label, ident = HIT.search(rest), LABEL.search(rest), IDENT.search(rest)
        found.append({
            'kind': match['kind'],
            'label': label['label'] if label else '',
            'id': ident['id'] if ident else '',
            'frame': tuple(float(match[k]) for k in ('x', 'y', 'w', 'h')),
            'hit': (float(hit['x']), float(hit['y'])) if hit else None,
            'other_app': 'activationBundleId' in rest,
        })
    return found


def interact(command, out, quiet=False):
    state = load_state()
    bridge = Bridge(timeout=180)
    try:
        data = bridge.call('DeviceInteractionSynthesize',
                           {'interactSessionKey': state['key'], 'interactionCommand': command})
    finally:
        bridge.close()
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%H%M%S')
    saved = {}
    for kind, key in (('screenshot', 'screenshotPath'), ('hierarchy', 'hierarchyPath'), ('logs', 'logsPath')):
        source = data.get(key)
        if source and pathlib.Path(source).exists():
            suffix = '.png' if kind == 'screenshot' else '.txt'
            target = out / f'{stamp}-{kind}{suffix}'
            shutil.copy2(source, target)
            saved[kind] = target
    if not quiet:
        for kind, path in saved.items():
            print(f'  {kind}: {path}')
    return saved


def summarize(hierarchy_path):
    text = pathlib.Path(hierarchy_path).read_text(errors='replace')
    items = elements(text)
    window = next((e for e in items if e['kind'] == 'Window' and not e['other_app']), None)
    head = text.splitlines()[:4]
    for line in head:
        if 'orientation' in line.lower():
            print(f'  {line.strip()}')
    if window:
        w, h = window['frame'][2], window['frame'][3]
        print(f'  app window: {w:g}x{h:g} pt')
        if {round(w), round(h)} in ({951, 669},):
            print('  warning: the Duo is open, but Xcode reads the device through its cover panel. '
                  'Elements past the cover\'s width are missing and taps may not land; fold it shut '
                  '(duo.py fold close) to interact, and capture the open phone with duo.py shot.')
    controls = ('Button', 'Cell', 'TextField', 'SearchField', 'SecureTextField', 'Switch', 'Slider', 'Link', 'Tab')
    tappable = [e for e in items if e['hit'] and not e['other_app'] and (e['kind'] in controls or e['label'])]
    # Controls first: the rest is mostly text, much of it from views that are hidden.
    tappable.sort(key=lambda e: e['kind'] not in controls)
    for e in tappable[:60]:
        name = e['label'] or e['id'] or ''
        print(f"  {e['kind']:<11} {name[:60]!r:<64} hit {e['hit'][0]:g},{e['hit'][1]:g}")
    if len(tappable) > 60:
        print(f'  … {len(tappable) - 60} more in {hierarchy_path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('start', help='open a device-interaction session')
    p.add_argument('--device', required=True, help='simulator UDID (exact, so nothing else boots)')
    p.add_argument('--name', default='Duo Check', help='session title shown in Xcode')
    for name, helptext in (('look', 'capture screenshot and hierarchy'), ('do', 'run an interaction command'),
                           ('tap', 'tap an element by label or identifier')):
        p = commands.add_parser(name, help=helptext)
        if name == 'do':
            p.add_argument('interaction')
        if name == 'tap':
            p.add_argument('label')
        p.add_argument('--out', default='device-shots')
    commands.add_parser('end', help='close the session')
    args = parser.parse_args()

    if args.command == 'start':
        bridge = Bridge(timeout=600)
        try:
            data = bridge.call('DeviceInteractionStartSession',
                               {'deviceIdentifier': args.device, 'sessionIdentifier': args.name})
        finally:
            bridge.close()
        key = data.get('interactionSessionKey', args.name)
        if data.get('deviceUUID') and data['deviceUUID'] != args.device:
            print(f"  warning: Xcode picked {data['deviceUUID']}, not {args.device}")
        CACHE.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({'key': key, 'device': data.get('deviceUUID', args.device)}))
        print(f"  session {key!r} on {data.get('deviceUUID', args.device)}")
    elif args.command == 'end':
        state = load_state()
        bridge = Bridge(timeout=120)
        try:
            bridge.call('DeviceInteractionEndSession', {'interactionSessionKey': state['key']})
        finally:
            bridge.close()
        STATE.unlink(missing_ok=True)
        print(f"  closed {state['key']!r}")
    elif args.command in ('look', 'do'):
        saved = interact('' if args.command == 'look' else args.interaction, args.out)
        if 'hierarchy' in saved:
            summarize(saved['hierarchy'])
    elif args.command == 'tap':
        saved = interact('', args.out, quiet=True)
        if 'hierarchy' not in saved:
            raise SystemExit('no hierarchy came back')
        items = [e for e in elements(saved['hierarchy'].read_text(errors='replace'))
                 if e['hit'] and not e['other_app']]
        wanted = args.label.lower()
        match = (next((e for e in items if e['label'] == args.label or e['id'] == args.label), None)
                 or next((e for e in items if wanted in e['label'].lower()), None))
        if not match:
            raise SystemExit(f'no element labelled {args.label!r}; run `look` to see what is there')
        x, y = match['hit']
        print(f"  tap {match['kind']} {match['label'] or match['id']!r} at {x:g},{y:g}")
        saved = interact(f't {x:g} {y:g}', args.out)
        if 'hierarchy' in saved:
            summarize(saved['hierarchy'])


if __name__ == '__main__':
    main()
