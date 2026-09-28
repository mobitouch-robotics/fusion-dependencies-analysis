#!/usr/bin/env python3
"""Plays the Fusion part of the demo video with the real cursor, so you can screen-record it.

Run it from Terminal:   python3 tools/record_demo.py          (full run)
                        python3 tools/record_demo.py --dry     (cursor only, no clicks)
                        python3 tools/record_demo.py --mute    (no spoken narration)
                        python3 tools/record_demo.py --skip-intro   (no introduction talk; combine freely)
                        python3 tools/record_demo.py --list         (the steps of the Safari tour, numbered)
                        python3 tools/record_demo.py --step 6       (start the tour at step 6, in the page open
                                                                     in Safari, set up as that step expects)
                        python3 tools/record_demo.py --skip-generation   (open the newest existing page in Safari
                                                                          and do only the Safari part)
                        python3 tools/record_demo.py --calibrate    (on another Mac or window layout: point at each
                                                                     Fusion element once; saved in tools/demo_positions.json)

Needs once:
  * System Settings > Privacy & Security > Accessibility: turn on Terminal (the app you run it from).
  * Safari > Settings > Advanced: "Show features for web developers", then
    Develop menu > "Allow JavaScript from Apple Events" (used to find buttons on the page).
  * Fusion open with the design, on the Solid tab, window maximised; nothing else on top.
  * Fusion maximised, the Dependencies Graph dialog docked on the right, as on the Mac mini the points were measured
    on (FUSION_POINTS); anything else: --calibrate. Screen Recording for Terminal lets the script see whether
    "Include linked designs" is ticked (it ticks it when not).
  * Run a Full analysis with linked designs once before recording: the linked designs are then taken from the
    earlier run (Reuse earlier results), so the recorded run takes minutes, not hours.
Start the screen recording during the countdown. Press Ctrl+C in Terminal to stop at any time.

What the tour shows, in Fusion: the Manage tab button, the dialog (Full analysis / Quick estimate, Thumbnails,
Advanced options: Include linked designs, Group test for linked designs, Reuse earlier results) and the progress
panel (a row per design with a bar per step, sorted in progress / waiting / finished; Cancel). In the page (--list for the numbered steps): the
side panel and its tabs, getting around, the design frames of linked designs, the four layouts (Components as
component frames), hover, selection and the Selection tab, selecting a whole block, the Display tab, routes,
multi-selection, all links and folding, search, the Filter tab (kinds and linked designs), the suppression preview,
Select in Fusion, the Legend tab, history playback.
"""
import ctypes, ctypes.util, json, os, subprocess, sys, time

DRY = '--dry' in sys.argv
SKIP_GEN = '--skip-generation' in sys.argv or '--from' in sys.argv
SKIP_INTRO = '--skip-intro' in sys.argv

# ---- scenario: edit the texts/timings here ---------------------------------------------------
# Fusion: screen points (the mouse's coordinates) for the maximised Fusion window on the Mac mini, measured from a
# screenshot with the dialog docked on the right (screen = 1.0158 x picture - (55.3, 5.7), fitted on labels whose
# real positions were known). Thumbnails, Save to and Choose file are taken with Advanced options closed (as the
# tour reaches them), the rest with it open (opening it widens the label column). --calibrate re-measures them on
# another Mac (saved in tools/demo_positions.json, which overrides these).
FUSION_POINTS = [
    # name,               point,          what to point at when calibrating
    ('manage_tab',        (283, 106),     'the MANAGE tab in the toolbar'),
    ('graph_btn',         (275, 139),     'on the Manage tab: the Dependencies Graph button (icon)'),
    ('thumbs',            (1492, 488),    'in the dialog: the "Thumbnails" label'),
    ('save_to',           (1712, 523),    'in the dialog: the "Save to" file path'),
    ('choose_file',       (1584, 556),    'in the dialog: the "Choose file..." button'),
    ('advanced',          (1460, 597),    'in the dialog: the arrow left of "Advanced options"'),
    ('linked',            (1521, 628),    'Advanced options open: the "Include linked designs" label'),
    ('linked_box',        (1648, 628),    'Advanced options open: the "Include linked designs" checkbox'),
    ('linked_groups',     (1540, 660),    'Advanced options open: the "Group test for linked designs" label'),
    ('reuse',             (1516, 690),    'Advanced options open: the "Reuse earlier results" label'),
    ('full_text',         (1503, 734),    'Advanced options open: the "Full analysis" description text'),
    ('quick_text',        (1509, 819),    'Advanced options open: the "Quick estimate" description text'),
    ('full_btn',          (1767, 896),    'the "Full analysis" button (do not click it)'),
    ('progress_panel',    (1671, 502),    'during a run: the middle of the progress panel'),
]
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'demo_positions.json')
DESIGN_INTRO = ("This is the graph of the master assembly of a robot arm: the design itself, and every design it "
                "links, each in a frame of its own. Every box is a timeline feature, a component or a parameter. To keep "
                "a big design readable, the links between boxes appear when you hover or select something.")
LINKED_DESIGN = 'J2 arm'            # a linked design unfolded in the tour (its name starts with this)
# The tour works inside one linked design (LINKED_DESIGN, unfolded early on): the Master assembly itself has only a
# few items. Box names are looked up in that design first (the same names can be in another design, e.g. J3 arm).
HOVER_BOXES = ['Main J2 profile', 'Stepper motor screws']
ITEM = 'Stepper_Motor_Screw_Holes'      # selected, played back
ITEM_PARENT = 'Stepper motor screws'    # clicked in its side panel
ROUTE_TO = 'Main objects placement'     # route button used
SECOND_ITEM = 'Gearbox screws'          # Cmd+clicked, from another branch (Only the selected branch is off then)
SEARCH = 'endstop'
SUPPRESS_ITEM = 'Stepper motor screws'   # switched off in the suppression preview (a sketch with a long cascade)
BREAK_ITEM = 'Derived from Parameters v47'   # switching it off makes features fail (red) and warn (amber)
FUSION_ITEM = 'Component Insert J2 assembly'   # an item of the Master assembly itself: Select in Fusion (name start: the version changes)
BLOCK_GROUP = 'Holes_And_Nut_Pockets'   # the timeline group holding ITEM: its block is selected as a whole
ANALYSIS_TIMEOUT = 1800             # seconds to wait for the page to open in Safari
# ------------------------------------------------------------------------------------------------

cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreGraphics'))
class P(ctypes.Structure): _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]
cg.CGEventCreate.restype = ctypes.c_void_p
cg.CGEventGetLocation.restype = P; cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, P, ctypes.c_uint32]
cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
cg.CFRelease.argtypes = [ctypes.c_void_p]

def pos():
    e = cg.CGEventCreate(None); p = cg.CGEventGetLocation(e); cg.CFRelease(e); return p

def _ev(kind, x, y):
    e = cg.CGEventCreateMouseEvent(None, kind, P(x, y), 0)
    cg.CGEventSetFlags(e, 0)
    if kind in (1, 2): cg.CGEventSetIntegerValueField(e, 1, 1)
    cg.CGEventPost(0, e); cg.CFRelease(e)

def move(x, y, dur=1.0):
    """Eased glide from the current position."""
    p = pos(); n = max(2, int(dur * 60))
    for i in range(1, n + 1):
        t = i / n; k = t * t * (3 - 2 * t)
        _ev(5, p.x + (x - p.x) * k, p.y + (y - p.y) * k); time.sleep(dur / n)

def click(pause=0.35):
    time.sleep(pause)
    if DRY: return
    p = pos(); _ev(1, p.x, p.y); time.sleep(0.12); _ev(2, p.x, p.y)

cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
cg.CGEventCreateScrollWheelEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32]
cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p]
cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
CMD = 1 << 20
SHIFT = 1 << 17
KEYS = {'esc': 53, 'space': 49, 'right': 124, 'left': 123, 'enter': 36, 'backspace': 51, 'a': 0, 'g': 5}

def release_mods():
    """Makes sure no modifier (Command) is left held: a Command key-up and a flags-cleared event."""
    e = cg.CGEventCreateKeyboardEvent(None, 55, False); cg.CGEventSetFlags(e, 0); cg.CGEventPost(0, e); cg.CFRelease(e)
    p = pos(); e = cg.CGEventCreateMouseEvent(None, 5, P(p.x, p.y), 0); cg.CGEventSetFlags(e, 0); cg.CGEventPost(0, e); cg.CFRelease(e)

def cmd_click(pause=0.35):
    time.sleep(pause)
    if DRY: return
    p = pos()
    for kind in (1, 2):
        e = cg.CGEventCreateMouseEvent(None, kind, P(p.x, p.y), 0)
        cg.CGEventSetIntegerValueField(e, 1, 1); cg.CGEventSetFlags(e, CMD); cg.CGEventPost(0, e); cg.CFRelease(e)
        time.sleep(0.12)
    release_mods()

def drag(dx, dy, dur=1.4):
    """Press, glide by (dx, dy), release: pans the graph when started on empty background."""
    p = pos(); n = max(2, int(dur * 60))
    if not DRY: _ev(1, p.x, p.y)
    for i in range(1, n + 1):
        t = i / n; k = t * t * (3 - 2 * t)
        _ev(6 if not DRY else 5, p.x + dx * k, p.y + dy * k); time.sleep(dur / n)
    if not DRY: _ev(2, p.x + dx, p.y + dy)

def scroll(lines, steps=12, dur=1.2):
    """Wheel at the cursor: positive zooms the graph in, negative zooms out."""
    for _ in range(steps):
        e = cg.CGEventCreateScrollWheelEvent(None, 1, 1, int(lines)); cg.CGEventPost(0, e); cg.CFRelease(e)
        time.sleep(dur / steps)

def key(name, flags=0, blur=True):
    """A key press. blur: first take the focus off any checkbox/input, as the page ignores shortcuts typed into them."""
    if blur:
        try: js("(()=>{const a=document.activeElement;if(a&&a!==document.body)a.blur();})()")
        except Exception: pass
        time.sleep(0.1)
    for down in (True, False):
        e = cg.CGEventCreateKeyboardEvent(None, KEYS[name], down)
        if flags: cg.CGEventSetFlags(e, flags)
        cg.CGEventPost(0, e); cg.CFRelease(e); time.sleep(0.05)
    if flags: release_mods()

def type_text(text, delay=0.16):
    for ch in text:
        for down in (True, False):
            e = cg.CGEventCreateKeyboardEvent(None, 0, down); cg.CGEventKeyboardSetUnicodeString(e, 1, ch)
            cg.CGEventPost(0, e); cg.CFRelease(e)
        time.sleep(delay)

def go(xy, dur=1.0, then_click=True):
    if xy is None: return False
    move(xy[0], xy[1], dur)
    if then_click: click()

def wait(s): time.sleep(s)

def load_points():
    pts = {k: v for k, v, _ in FUSION_POINTS}
    try:
        with open(POSITIONS_FILE, encoding='utf-8') as f:
            pts.update({k: tuple(v) if v else None for k, v in json.load(f).items()})
    except FileNotFoundError:
        pass
    return pts

FP = load_points()

def fp(name):
    """A Fusion screen point."""
    return FP.get(name)

def pixel_brightness(xy, r=4):
    """The average brightness (0..255) of the screen around a point, from a small screenshot (converted to BMP with
    sips, both built in); None when it cannot be read (Screen Recording not allowed for Terminal)."""
    import tempfile, struct
    png = os.path.join(tempfile.gettempdir(), '_demo_px.png'); bmp = png[:-4] + '.bmp'
    try:
        subprocess.run(['screencapture', '-x', '-R%d,%d,%d,%d' % (xy[0] - r, xy[1] - r, 2 * r, 2 * r), png],
                       capture_output=True, timeout=10)
        subprocess.run(['sips', '-s', 'format', 'bmp', png, '--out', bmp], capture_output=True, timeout=10)
        d = open(bmp, 'rb').read()
        off, w, h, bpp = struct.unpack_from('<I', d, 10)[0], *struct.unpack_from('<iiHH', d, 18)[:2], struct.unpack_from('<H', d, 28)[0]
        step, row = bpp // 8, ((w * bpp // 8) + 3) & ~3
        vals = [sum(d[off + y * row + x * step: off + y * row + x * step + 3]) / 3 for y in range(abs(h)) for x in range(w)]
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None

def checkbox_ticked(xy):
    """Fusion's checkbox: grey filled with a white tick when ticked, white when not. None: cannot tell."""
    b = pixel_brightness(xy)
    if b is None: return None
    print('  (checkbox brightness %.0f: %s)' % (b, 'ticked' if b < 200 else 'not ticked'))
    return b < 200

def calibrate():
    """Point at each Fusion element in turn and press Enter in Terminal; the positions are saved. Leave the mouse
    anywhere and type s + Enter to skip an element (it keeps its current value)."""
    pts = load_points()
    print('Calibration: bring Fusion to the front, maximised. For each element: point at it with the mouse, then press')
    print('Enter here (type s and Enter to keep the current value). Open the dialog / start a run when an element asks.')
    for name, _, what in FUSION_POINTS:
        cur = pts.get(name)
        ans = input('  %-16s %s%s: ' % (name, what, '  [now %s]' % (cur,) if cur else '')).strip().lower()
        if ans == 's':
            continue
        p = pos(); pts[name] = (round(p.x), round(p.y)); print('      ->', pts[name])
    with open(POSITIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(pts, f, indent=1)
    print('Saved', POSITIONS_FILE)

# ---- narration: macOS text to speech (the built-in `say` command) ------------------------------
VOICE = None          # e.g. 'Samantha', 'Daniel'; None = the system voice. List them: say -v '?'
RATE = 175            # words per minute
MUTE = '--mute' in sys.argv
_speech = None

def say(text, block=False):
    """Speaks while the actions go on; the next say() first waits for this one to finish."""
    global _speech
    print('  🗣', text)
    if MUTE: return
    hush()
    cmd = ['say', '-r', str(RATE)] + (['-v', VOICE] if VOICE else []) + [text]
    _speech = subprocess.Popen(cmd)
    if block: hush()

def topic(text):
    """Starts talking about a new feature: finishes the last sentence, a short pause, then speaks."""
    hush(); wait(1.2); say(text)

def hush():
    """Waits until the current sentence has been spoken."""
    if _speech is not None: _speech.wait()

def osa(script, quiet=False):
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if r.returncode and not quiet: print('  osascript:', r.stderr.strip())
    return r.stdout.strip()

APP_IDS = {'Autodesk Fusion': 'com.autodesk.fusion360', 'Safari': 'com.apple.Safari'}

def front_app():
    """Bundle id of the frontmost app (lsappinfo needs no extra permission)."""
    try:
        asn = subprocess.run(['lsappinfo', 'front'], capture_output=True, text=True).stdout.strip()
        out = subprocess.run(['lsappinfo', 'info', '-only', 'bundleid', asn], capture_output=True, text=True).stdout
        return out.split('=')[-1].strip().strip('"')
    except Exception:
        return ''

def activate(app):
    """Brings an app to the front by its bundle id. If the front app can be read and is clearly another
    one, tries again; if it cannot be read, carries on (you start the script with the right app in front)."""
    bid = APP_IDS.get(app)
    for _ in range(3):
        osa('tell application id "%s" to activate' % bid if bid else 'tell application "%s" to activate' % app, True)
        wait(1.0)
        front = front_app()
        if not bid or '.' not in front or front.lower() == bid.lower(): return
    print('  (warning: %s may not be in front; front app: %s)' % (app, front))

def js(code):
    code = code.replace('\\', '\\\\').replace('"', '\\"')
    return osa('tell application "Safari" to do JavaScript "%s" in current tab of front window' % code)

# Screen position (points) of a page element's centre; finder is JS returning an element.
def el_xy(finder):
    r = js("(()=>{const e=(%s);if(!e)return '';e.scrollIntoView({block:'nearest'});const b=e.getBoundingClientRect();"
           "if(b.width<2||b.height<2||b.right<0||b.bottom<0||b.left>innerWidth||b.top>innerHeight)return '';"
           "return JSON.stringify({x:b.x+b.width/2,y:b.y+b.height/2,sx:window.screenX,sy:window.screenY,"
           "top:window.outerHeight-window.innerHeight,side:(window.outerWidth-window.innerWidth)/2});})()" % finder)
    if not r:
        print('  (skipped, not visible on the page:', finder[:90] + ')'); return None
    d = json.loads(r)
    return (d['sx'] + d['side'] + d['x'], d['sy'] + d['top'] + d['y'])

# The frame id ('X8') of a linked design, from its frame title (the frame rect before it carries data-d).
DSG_ID = ("(n=>{const t=[...document.querySelectorAll('#graph g.dframes text')].find(t=>t.textContent.includes(n));"
          "return t&&t.previousElementSibling?t.previousElementSibling.dataset.d||'':'';})('%s')")

# A box in the graph, found by its label (long labels are cut with "…", so a prefix match is used when there is no
# exact one). Boxes of the design `where` come first: LINKED_DESIGN by default, 'main' for the design itself
# (item ids 'n12'; a linked design's are 'x8:n12').
FIND_G = ("((n,w)=>{const d=w==='main'?'':%s;const mine=g=>{const i=g.dataset.id||'';return w==='main'?/^n\\d+$/.test(i):"
          "(d?i.startsWith(d.toLowerCase()+':'):true);};const gs=[...document.querySelectorAll('#graph g.nd')].sort((a,b)=>mine(b)-mine(a));"
          "const lab=g=>(g.querySelector(':scope > text')||{}).textContent||'';"
          "return gs.find(g=>g.dataset.name===n)||gs.find(g=>lab(g)===n)||gs.find(g=>lab(g).endsWith('\u2026')&&n.startsWith(lab(g).slice(0,-1)))"
          "||gs.find(g=>(g.dataset.name||'').startsWith(n));})('%s','%s')")

def find_g(name, where=None):
    return FIND_G % (DSG_ID % LINKED_DESIGN, name, where or '')

def box(name, where=None): return "%s?.querySelector('rect')" % find_g(name, where)

def route_btn(name): return "%s?.querySelector('.rbtn circle')" % find_g(name)

def power_btn(name): return "%s?.querySelector('g.act')" % find_g(name)

# The + button of the linked design's folded box, and the − on its frame when it is open.
UNFOLD_BTN = "(()=>{const d=%s;return d?document.querySelector('#graph g.ctog[data-for=\"g'+d+'\"]'):null;})()"
FOLD_BTN = "(()=>{const d=%s;return d?document.querySelector('#graph g.ctog[data-d=\"'+d+'\"]'):null;})()"
def unfold_btn(): return UNFOLD_BTN % (DSG_ID % LINKED_DESIGN)
def fold_btn(): return FOLD_BTN % (DSG_ID % LINKED_DESIGN)

# Any route button that is on screen: the preferred box's if visible, else the one farthest from the
# selection (a longer route is more interesting to show).
ANY_ROUTE_BTN = ("(()=>{const vis=c=>{const b=c.getBoundingClientRect();const g=document.getElementById('graph').getBoundingClientRect();"
                 "return b.width>2&&b.left>g.left+20&&b.right<g.right-20&&b.top>g.top+20&&b.bottom<g.bottom-20;};"
                 "const pref=%s;if(pref&&vis(pref))return pref;"
                 "const s=document.querySelector('#graph .selglow,#graph g.nd.sel')||null;const sb=s&&s.getBoundingClientRect();"
                 "const cs=[...document.querySelectorAll('#graph .rbtn:not(.on) circle')].filter(vis);"
                 "if(!sb)return cs[0];const d=c=>{const b=c.getBoundingClientRect();return Math.hypot(b.x-sb.x,b.y-sb.y);};"
                 "return cs.sort((a,b)=>d(b)-d(a))[0];})()")

def sel(css): return "document.querySelector('%s')" % css

# The title of a block (a timeline group's, or in the Components layout a component's frame) by its name.
def block_title(name):
    return ("[...document.querySelectorAll('#graph text')].find(t=>t.parentNode&&t.parentNode.style.cursor==='pointer'"
            "&&t.firstChild&&t.firstChild.nodeValue&&t.firstChild.nodeValue.startsWith('%s'))" % name)

def by_text(css, text, exact=True):
    return "[...document.querySelectorAll('%s')].find(e=>e.textContent.trim()%s'%s')" % (
        css, "==='" [:3] if exact else '.includes(', text) if exact else         "[...document.querySelectorAll('%s')].find(e=>e.textContent.includes('%s'))" % (css, text)

def empty_xy():
    """A point of the bare graph background: the svg itself is under it, not a group block, box or line
    (clicking a group block selects the group). Scans a fine grid, preferring the middle."""
    r = js("(()=>{const svg=document.getElementById('graph');const g=svg.getBoundingClientRect();let best=null,bd=1e9;"
           "for(let i=2;i<60;i++)for(let j=2;j<36;j++){const x=g.left+g.width*i/61,y=g.top+g.height*j/37;"
           "if(document.elementFromPoint(x,y)!==svg)continue;"
           "let ok=true;for(const[dx,dy]of[[-8,0],[8,0],[0,-8],[0,8]])if(document.elementFromPoint(x+dx,y+dy)!==svg)ok=false;if(!ok)continue;"
           "const d=Math.hypot(x-g.left-g.width/2,y-g.top-g.height/2);if(d<bd){bd=d;best={x,y};}}"
           "if(!best)return '';return JSON.stringify({x:best.x,y:best.y,sx:window.screenX,sy:window.screenY,"
           "top:window.outerHeight-window.innerHeight,side:(window.outerWidth-window.innerWidth)/2});})()")
    if not r: return None
    d = json.loads(r)
    return (d['sx'] + d['side'] + d['x'], d['sy'] + d['top'] + d['y'])

def has_selection():
    return js("document.querySelector('#graph .selglow')?'1':''") == '1'

def clear_selection():
    """Esc; if the page did not take it, a click on empty background (which also clears it)."""
    key('esc'); wait(0.8)
    if has_selection():
        xy = empty_xy()
        if xy: go(xy, 1.0); wait(0.5)

def graph_xy(fx, fy):
    """A point of the graph area, as fractions of its width/height (for empty-background moves)."""
    d = json.loads(js("(()=>{const b=document.getElementById('graph').getBoundingClientRect();return JSON.stringify("
                      "{x:b.x+b.width*%f,y:b.y+b.height*%f,sx:window.screenX,sy:window.screenY,"
                      "top:window.outerHeight-window.innerHeight,side:(window.outerWidth-window.innerWidth)/2});})()" % (fx, fy)))
    return (d['sx'] + d['side'] + d['x'], d['sy'] + d['top'] + d['y'])

def hover(finder, dur=1.0, stay=2.5):
    if go(el_xy(finder), dur, False) is not False: wait(stay)
def press(finder, dur=1.0, stay=1.5):
    if go(el_xy(finder), dur) is not False: wait(stay)
def near(finder, dur=1.2): go(el_xy(finder), dur, False)

def el_width(finder):
    r = js("(()=>{const e=(%s);return e?String(e.getBoundingClientRect().width):'';})()" % finder)
    return float(r) if r else None

CLOSE_ZOOM = 0.7          # the zoom most of the tour is shown at (1 = boxes at full size; Fit on this assembly is ~0.1)
SEL_ZOOM = 0.6            # with a selection (its tree needs a little more room)

def box_id(name):
    """The data-id of the box of a tour item (in the tour's linked design first), or ''."""
    return js("(()=>{const g=%s;return g?g.dataset.id:'';})()" % find_g(name)) or ''

def view_design(dur=1.0, k=CLOSE_ZOOM, near=None):
    """Brings the tour back into view at a readable zoom, never the whole assembly (far too much for the video).
    With a selection: Fit (it fits the selection), then closer (SEL_ZOOM) if that left it far away, halfway between
    the selection and the middle of its tree.
    Without: around the box `near` (the tour's item by default) at zoom k. k=None: the whole tour design's frame
    (its width). Pages from an add-in without these hooks: Fit."""
    if has_selection():
        press(sel('#fit'), dur, 1.2)
        if k and js("(()=>window.dgZoomAround&&window.dgZoomAround('',%s,800)?'1':'')()" % min(k, SEL_ZOOM)) == '1': wait(1.0)
        return
    if k:
        bid = box_id(near or ITEM)
        if bid and js("(()=>window.dgZoomAround&&window.dgZoomAround('%s',%s,800,true)?'1':'')()" % (bid, k)) == '1':
            wait(1.2); return
    if js("(()=>window.dgZoomToDesign&&window.dgZoomToDesign(%s,700)?'1':'')()" % (DSG_ID % LINKED_DESIGN)) == '1':
        wait(1.3)
    else:
        press(sel('#fit'), dur, 1.5)

def bring_into_view(finder):
    """Centres the page on the box an element belongs to (too small or outside the view)."""
    r = js("(()=>{const e=(%s);const g=e&&e.closest('g.nd');return g&&window.dgFocus&&window.dgFocus(g.dataset.id,700)?'1':'';})()" % finder)
    if r == '1': wait(1.2)
    else: view_design()

def zoom_on(finder, width=200, dur=1.2):
    """Glides to an element, then zooms around it with the wheel until it is about `width` px wide."""
    xy = el_xy(finder)
    if xy is None:                     # too small or outside the view: bring it in first, then look again
        bring_into_view(finder); xy = el_xy(finder)
    if go(xy, dur, False) is False: return
    for _ in range(80):
        w = el_width(finder)
        if w is None: return
        if w < width * 0.9: scroll(1, 1, 0.05)
        elif w > width * 1.25: scroll(-1, 1, 0.05)
        else: break
    xy = el_xy(finder)                 # re-centre the cursor on it after zooming
    if xy: move(xy[0], xy[1], 0.4)

# Where the add-in records the finished page (any file name chosen in its Save dialog): path and time written.
LAST_PAGE = os.path.expanduser('~/Library/Application Support/FusionDependenciesGraph/last_page.json')

def last_page(since=0):
    """The page the add-in last finished, if it was written after `since` and still exists; else None."""
    try:
        with open(LAST_PAGE, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return None
    p = d.get('path')
    return p if p and d.get('time', 0) >= since - 5 and os.path.exists(p) else None

def safari_windows():
    """Number of open Safari windows; 0 when Safari is not running (never starts it)."""
    if osa('application "Safari" is running', True) != 'true': return 0
    try: return int(osa('tell application "Safari" to count windows', True) or 0)
    except ValueError: return 0

def finished_page(since):
    """The generated page once the add-in has finished it: the first Safari window that opens during the run
    (close Safari's windows before the run - the pages saved while it goes on are not opened). Returns the URL of
    that window's tab once it has loaded, with Safari brought to the front."""
    if not safari_windows(): return None
    url = ''
    for _ in range(15):          # the window opens before its page has loaded
        url = osa('tell application "Safari" to return URL of current tab of front window', True).strip()
        if url and url != 'missing value' and page_ready(): break
        wait(1)
    activate('Safari')
    return url or 'a new Safari window'

def graph_tabs():
    """URLs of all open Safari tabs showing a generated page."""
    if osa('application "Safari" is running', True) != 'true': return []
    out = osa('tell application "Safari" to get URL of every tab of every window', True)
    return [u.strip() for u in out.split(',') if u.strip().startswith('file://') and u.strip().endswith('.html')]

def open_newest_page():
    import glob, tempfile
    d = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph')
    files = glob.glob(os.path.join(d, '*.html'))
    for f in glob.glob(os.path.expanduser('~/Downloads/*.html')):      # a page saved with "Save to"
        try:
            with open(f, encoding='utf-8', errors='ignore') as h:
                if '<title>Dependencies graph</title>' in h.read(4000): files.append(f)
        except OSError:
            pass
    try:        # a file chosen with "Save to" in the dialog
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'DependenciesGraph',
                               'settings.json'), encoding='utf-8') as f:
            sp = json.load(f).get('savePath')
        if sp and os.path.exists(sp): files.append(sp)
    except Exception:
        pass
    lp = last_page()         # the page the add-in last finished, wherever it was saved
    if lp: files.append(lp)
    if not files: raise SystemExit('No generated page found in ' + d)
    f = max(files, key=os.path.getmtime)
    print('Opening', os.path.basename(f))
    subprocess.run(['open', '-a', 'Safari', f]); wait(4)

def page_ready():
    try: return js("document.querySelector('#gpList .gn')?'1':''") == '1'
    except Exception: return False

# ---- the scenario ---------------------------------------------------------------------------------
def intro():
    """What the add-in is and why it helps; spoken at the start of every run."""
    say("Meet Dependencies Graph, an add-in for Autodesk Fusion.", True); wait(0.5)
    say("In a parametric design, every feature is built on something earlier: sketches, planes, bodies and parameters. "
        "After a few hundred steps, nobody remembers what depends on what.", True); wait(0.4)
    say("So a small change, like editing a sketch or suppressing a feature, can break things far away in the timeline.", True); wait(0.6)
    say("And in Fusion itself, these dependencies are hard to figure out. The timeline shows the order of the features, "
        "but not what each of them is built on.", True); wait(0.6)
    say("That's why we created this add-in: to make them easy to see and understand, visually.", True); wait(0.6)
    say("Dependencies Graph maps all of it. It reads the whole timeline, "
        "and it can test each feature by suppressing it, so every link it shows is a real dependency.", True); wait(0.4)
    say("The result is an interactive page in your browser. You can see what a feature depends on, what would break if you changed it, "
        "and how any two features are connected. And you can select them back in Fusion with one click.", True); wait(0.4)
    say("It also follows the designs yours links to, through Derive features and inserted components, at any depth, "
        "and it can preview what suppressing a feature would do, without touching the design.", True)
    wait(1.2)

# The position of a "Replace" button in the frontmost app's windows or sheets (the Save panel asks before it
# overwrites a file), as "x,y"; "" when there is none. Needs the Accessibility permission the mouse events need too.
REPLACE_BTN = """
on findIn(c)
  tell application "System Events"
    try
      set b to button "Replace" of c
      set {x, y} to position of b
      set {bw, bh} to size of b
      return ((x + bw div 2) as text) & "," & ((y + bh div 2) as text)
    end try
    try
      repeat with s in (sheets of c)
        set r to my findIn(s)
        if r is not "" then return r
      end repeat
    end try
  end tell
  return ""
end findIn
tell application "System Events"
  repeat with w in (windows of (first process whose frontmost is true))
    set r to my findIn(w)
    if r is not "" then return r
  end repeat
end tell
return ""
"""

def save_in_downloads():
    """In the system Save panel (open, the file name already filled in): Go to Folder ~/Downloads, Save, and
    Replace when a page of that name is already there."""
    wait(1.5)
    key('g', CMD | SHIFT, False); wait(1.2)          # Go to Folder
    type_text('~/Downloads', 0.07); wait(0.8)
    key('enter', 0, False); wait(1.5)
    key('enter', 0, False); wait(1.5)                # Save
    r = osa(REPLACE_BTN, True)
    if r and ',' in r:
        x, y = (int(float(v)) for v in r.split(','))
        go((x, y), 0.8); wait(1.0)

def fusion_part():
    activate('Autodesk Fusion')
    move(1300, 760, 0.8)
    say("Let's build the graph for this design. The add-in lives on the Manage tab.")
    activate('Autodesk Fusion')
    print('Fusion part: Manage tab, Dependencies Graph, options, Full analysis, progress panel')
    go(fp('manage_tab'), 1.5); wait(1.0)
    go(fp('graph_btn'), 1.2); wait(2.5); hush()
    say("The dialog has a few options. Thumbnails adds a picture of every step, framed on the feature itself.")
    go(fp('thumbs'), 1.2, False); hush()
    say("The page is saved to a file named after the design and its version, in the folder used last time. "
        "Choose file picks another place. Let's save it in Downloads.")
    go(fp('save_to'), 1.0, False); wait(0.6)
    go(fp('choose_file'), 0.9)
    if not DRY: save_in_downloads()
    activate('Autodesk Fusion'); hush()
    say("The rest is under Advanced options.")
    go(fp('advanced'), 1.1); wait(1.2); hush()
    say("Include linked designs also reads every design this one links, through Derive features or inserted "
        "components, and the designs those link. Each is tested too, in a hidden copy that is closed without saving.")
    go(fp('linked'), 1.1, False)
    ticked = checkbox_ticked(fp('linked_box'))
    if ticked is False or ticked is None and '--tick-linked' in sys.argv:
        go(fp('linked_box'), 0.8); wait(0.5)       # tick it (it is remembered for next time)
    hush()
    say("The group test for linked designs is optional. It takes about as long again, "
        "and without it their groups are estimated from their items.")
    go(fp('linked_groups'), 0.9, False); hush()
    say("And Reuse earlier results: a saved version never changes, so a design tested once is taken from the "
        "earlier run, and only what changed is tested again.")
    go(fp('reuse'), 0.9, False); hush()
    say("There are two ways to build the graph. Full analysis suppresses every item in turn, so every link is a real dependency.")
    go(fp('full_text'), 1.2, False); hush()
    say("Quick estimate takes seconds, but only reads what each feature references, so it can miss some links.")
    go(fp('quick_text'), 0.8, False); hush()
    if not DRY and safari_windows():
        print('  (warning: Safari has %d window(s) open; the finished page is recognised by the first Safari window '
              'that opens, so close them now)' % safari_windows())
        t_w = time.time()
        while safari_windows() and time.time() - t_w < 30: wait(1)
    say("Let's run the full analysis.")
    t_start = time.time()
    go(fp('full_btn'), 1.1); wait(3.0); hush()
    say("The progress panel has a row for every design: what is being done right now, and a bar for each of its steps, "
        "reading, the item test and the group test. The design being worked on is at the top, then the ones waiting, "
        "then the finished ones. Cancel stops the whole run.")
    go(fp('progress_panel'), 1.2, False); hush()
    say("On a big assembly this can take a while. The page is saved as each design is done, so you can look at it early. "
        "When it's finished, it opens in the browser, and the design is left exactly as it was.")
    if DRY: return
    print('Full analysis running, waiting for the page…')
    t0 = time.time()
    while time.time() - t0 < ANALYSIS_TIMEOUT:
        wait(2)
        u = finished_page(t_start)
        if u:
            print('Page opened:', u); break
    else:
        raise SystemExit('No Safari window opened in time.')
    wait(4)
    wait(3)

# ---- preparing the page for a step (instant, through JavaScript, so it can start from any step) ----
RESET_JS = """(()=>{
  const q=s=>document.querySelector(s);
  if(q('#pbStop')&&q('#pbStop').offsetParent)q('#pbStop').click();
  if(document.body.classList.contains('infoopen')&&q('#infoClose'))q('#infoClose').click();
  document.querySelectorAll('.pop.open,.pop .open').forEach(e=>e.classList.remove('open'));
  const s=q('#search');if(s&&s.value){s.value='';s.dispatchEvent(new Event('input',{bubbles:true}));}
  const set=(id,v)=>{const e=document.getElementById(id);if(e&&e.checked!==v){e.checked=v;e.dispatchEvent(new Event('change',{bubbles:true}));}};
  set('focus',true);set('relUp',true);set('relUpAll',true);set('relDn',false);set('allLinks',false);
  if([...document.querySelectorAll('#cats input')].some(i=>!i.checked)&&q('#catAll'))q('#catAll').click();
  if(document.body.classList.contains('sidemin')&&q('#sideClose'))q('#sideClose').click();   // the side panel open,
  if(q('#tabSel'))q('#tabSel').click();                                                          // on its Selection tab
  if(q('#laneBtn')&&!q('#laneBtn').classList.contains('on'))q('#laneBtn').click();
  const rs=[...document.querySelectorAll('#simBar button')].find(b=>b.textContent==='Reset');if(rs)rs.click();
  const svg=q('#graph');if(svg)svg.dispatchEvent(new MouseEvent('click',{bubbles:true}));
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  return '1';})()"""

# Every timeline group open, every linked design folded except `keep` (a frame id, or '' for none): the page as it
# opens ('folded'), or with the tour's design open ('clear', 'item'). Instant, through the page's own buttons.
DESIGNS_JS = """((keep)=>{if(window.dgDesignsOnly)return window.dgDesignsOnly(keep)?'1':'';
  const q=s=>document.querySelector(s);if(q('#expAll'))q('#expAll').click();
  for(let k=0;k<80;k++){const b=[...document.querySelectorAll('#graph g.ctog[data-d]')].find(b=>b.dataset.d!==keep);
    if(!b)break;b.dispatchEvent(new MouseEvent('click',{bubbles:true}));}
  return '1';})(%s)"""

def designs_state(keep_linked):
    js(DESIGNS_JS % ((DSG_ID % LINKED_DESIGN) if keep_linked else "''")); wait(1.0)

def prepare(state):
    """Puts the page into the state a step expects, without visible cursor work."""
    if state is None: return
    js(RESET_JS); wait(0.6)
    designs_state(state != 'folded')
    if state == 'item':
        js("(()=>{const g=%s;if(g)g.dispatchEvent(new MouseEvent('click',{bubbles:true}));})()" % find_g(ITEM)); wait(1.2)
    if state == 'folded':
        js("document.getElementById('fit').click()")
    elif not js("(()=>window.dgZoomAround&&window.dgZoomAround(%s,%s,1,true)?'1':'')()" % (
            "''" if state == 'item' else "'%s'" % box_id(ITEM), CLOSE_ZOOM)):
        if not js("(()=>window.dgZoomToDesign&&window.dgZoomToDesign(%s,1)?'1':'')()" % (DSG_ID % LINKED_DESIGN)):
            js("document.getElementById('fit').click()")
    wait(1.2)
    move(*graph_xy(.92, .12), 0.6)

def step_0():
    """Close the info bar"""
    if js("document.body.classList.contains('infoopen')?'1':''") == '1':
        say("First, let's close the info bar at the bottom.")
        press(sel('#infoClose'), 1.2, 1.0); hush()

def step_panel():
    """The side panel and its tabs"""
    topic("On the right is the side panel. Its tabs hold everything around the graph: the Selection, the timeline "
          "Groups, the Filter, the Display options and the Legend.")
    for t in ('#tabSel', '#tabGroups', '#filterBtn', '#dispBtn', '#legendBtn'):
        hover(sel(t), 0.5, 0.5)
    hush()
    say("The Groups tab lists the timeline groups of every design. Each can be selected, folded, or switched off "
        "in the preview from here.")
    press(sel('#tabGroups'), 0.8, 1.2)
    hover("document.querySelector('#gpList .gprow:nth-child(3)')", 0.9, 1.5); hush()
    say("The arrows button hides the panel to a narrow strip of the tabs, for more room.")
    hover(sel('#sideClose'), 0.9, 1.5); hush()
    press(sel('#tabSel'), 0.9, 0.8)

def step_1():
    """Getting around: drag, zoom, Fit"""
    topic("Drag the background to move around, and scroll to zoom.")
    move(*graph_xy(.75, .7), 1.0); wait(0.5)
    drag(-380, -160, 1.6); wait(0.8)
    scroll(2, 8, 1.0); wait(0.8); scroll(-2, 8, 1.0); hush()
    say("Fit brings the whole design back into view.")
    press(sel('#fit'), 1.2, 1.5); hush()

def step_linked():
    """Linked designs: frames, pictures, unfolding one"""
    topic("Every design this one links has a frame of its own, with a picture of the finished part. "
          "They start folded into one box each, so the whole assembly fits on the screen.")
    press(sel('#fit'), 1.0, 1.0)
    hover("document.querySelector('#graph g.dframes image')", 1.2, 2.5); hush()
    say("The line into a folded design comes from its connector, on the bottom edge of the frame. It leads to the item "
        "that brings the design in: a Derive feature, or an inserted component.")
    wait(1.0); hush()
    say("Let's open the J2 arm, the design this tour looks at.")
    zoom_on(unfold_btn(), 18, 1.2); press(unfold_btn(), 0.6, 2.0)
    view_design(k=None); hush()
    say("It opens with its timeline groups, each a block inside its frame, showing its features, each with its own "
        "picture. The minus button on the frame folds it again.")
    hover(fold_btn(), 1.2, 2.0); hush()
    move(*graph_xy(.92, .12), 0.8)

def step_layouts():
    """Layouts: Depth, Components, Timeline, Groups"""
    topic("The graph has four layouts. Groups, the default, gives every timeline group a block of its own.")
    hush()
    say("Depth arranges the boxes in rows, by how deep each one is in the dependencies.")
    press(sel('#layDeps'), 1.2, 1.0); view_design(0.9, 0.4); hush()
    say("Components works like Groups, but with components: a component is a frame, and its timeline groups are "
        "blocks inside it, or single entries when folded.")
    press(sel('#compBtn'), 1.0, 1.0); view_design(0.9, 0.4); hush()
    say("And Timeline puts every item in one row, in timeline order, with the longer links arcing above it.")
    press(sel('#layTime'), 1.0, 1.0); view_design(0.9, 0.4); hush()
    say("Back to Groups.")
    press(sel('#laneBtn'), 1.0, 1.0); view_design(0.9, 0.4); hush()

def step_2():
    """Hover"""
    topic("Hovering a box shows its direct links: blue arrows lead in from the items it uses, and green arrows lead "
          "out to the items that use it. The rest dims for a moment.")
    zoom_on(box(HOVER_BOXES[0]), 200, 0.9); hush(); wait(0.3)
    for i, name in enumerate(HOVER_BOXES):
        xy = el_xy(box(name))
        if xy is None:
            bring_into_view(box(name)); xy = el_xy(box(name))
        if xy is None:
            print('  (hover skipped: %s not found on the page)' % name); continue
        print('  hover:', name)
        move(xy[0] - 30, xy[1], 0.8); move(xy[0], xy[1], 0.2)   # a last small step, so the page sees the pointer arrive
        wait(2.4)
        if i < len(HOVER_BOXES) - 1:
            move(xy[0], xy[1] - 90, 0.4); wait(0.7)             # off the box: the highlight clears for a moment
    view_design(0.9)

def step_3():
    """Select an item, side panel, Back"""
    topic("Now let's select a feature: the stepper motor screw holes.")
    zoom_on(box(ITEM), 170); wait(0.4)
    hush(); press(box(ITEM), 0.8, 2.0)
    say("It gets a gold frame, and everything it depends on is highlighted, all the way back to the user parameters. "
        "The rest of the design is hidden, so only this branch is left.")
    move(*graph_xy(.92, .12), 0.9); hush(); wait(0.8)
    say("The side panel lists what it depends on, what uses it, and what suppressing it would take with it. Hovering an entry shows its link in the graph.")
    hover(by_text('#details li', 'Sketch', False), 1.2, 2.0)
    hover(by_text('#details li', 'Extrude', False), 0.8, 2.0); hush()
    say("Clicking an entry jumps to it.")
    press(by_text('#details li .name', ITEM_PARENT), 0.8, 2.5); hush()
    say("And the back button returns to the previous selection, like in a browser.")
    press(sel('#hBack'), 1.3, 2.0); hush()

def step_blocks():
    """Selecting a whole block"""
    topic("A click on a block's title selects the whole timeline group: everything in it, and everything it depends on.")
    zoom_on(block_title(BLOCK_GROUP), 160, 1.2); hush()
    press(block_title(BLOCK_GROUP), 0.6, 2.0)
    say("The side panel shows what suppressing the whole group would do. In the Components layout, a component's "
        "title selects the whole component the same way.")
    move(*graph_xy(.92, .12), 0.9); hover(by_text('#details h3', 'Suppressing', False), 1.0, 2.0); hush()
    clear_selection()

def step_4():
    """Display tab: What uses it"""
    topic("The side panel's Display tab decides what a selection shows around it. Let's also turn on what uses it.")
    press(sel('#dispBtn'), 1.1, 1.0)
    hush(); press(by_text('#dispBox label', 'What uses it', False), 1.0, 1.5)
    say("Now the items built on top of the selection are highlighted too, joined to it by green arrows.", True)
    wait(1.5)
    press(by_text('#dispBox label', 'What uses it', False), 0.6, 1.0)
    press(sel('#tabSel'), 0.9, 1.0)

def step_5():
    """Routes"""
    rb = ANY_ROUTE_BTN % route_btn(ROUTE_TO)
    xy = el_xy(rb)
    if xy is None:
        view_design(1.0); xy = el_xy(rb)
    if xy is not None:
        topic("How exactly are two items connected? Every other box of the branch has a small orange route button in its corner.")
        hush(); go(xy, 1.4, False); wait(0.4)
        say("Hovering it previews every route between that box and the selection.", True); wait(1.5)
        say("Clicking it keeps them. Both ends now look selected, and the side panel lists each route, step by step.")
        click(); wait(2.0)
        move(*graph_xy(.92, .12), 0.9)
        hover(sel('#details .rsec'), 1.2, 1.0); hush(); wait(1.0)
        say("Back returns to the selection.")
        press(sel('#hBack'), 1.2, 1.5); hush()
    else:
        print('  (routes skipped: no route button visible)')

def step_6():
    """Only the selected branch off + Cmd+click multi-select"""
    topic("By default, only the selected branch is shown. "
          "Turning off Only the selected branch, in the Display tab, shows it inside the whole design instead.")
    hush(); press(sel('#dispBtn'), 1.1, 0.8)
    press(by_text('#dispBox label', 'Only the selected branch', False), 1.0, 1.0)
    press(sel('#tabSel'), 0.9, 0.5); view_design(1.0)
    say("The related boxes move next to the selection, and the rest of the design stays around them, dimmed.", True)
    wait(0.8)
    topic("Now items from other branches can be reached too. Holding command while clicking adds one to the selection. "
          "Let's add the gearbox screws sketch.")
    hush(); zoom_on(box(SECOND_ITEM), 150, 1.3); cmd_click(); wait(2.0)
    say("Both are framed in gold, each with its own branch, and the side panel lists them together.")
    move(*graph_xy(.92, .12), 0.9)
    hover(by_text('#details li', 'Hole', False), 1.2, 1.5); hush(); wait(0.6)
    say("Let's turn Only the selected branch back on. The view is cleaner with it.")
    press(sel('#dispBtn'), 1.1, 0.8)
    press(by_text('#dispBox label', 'Only the selected branch', False), 1.0, 1.0)
    press(sel('#tabSel'), 0.9, 0.5); view_design(1.0); hush(); wait(0.6)
    say("A click on empty space, or Escape, clears the selection.")
    xy = empty_xy()
    if xy: go(xy, 1.2)
    else: key('esc')
    wait(0.8)
    if has_selection(): key('esc')
    hush(); wait(1.2)

def step_7():
    """All links, then Collapse all / Expand all"""
    topic("With nothing selected, links only appear on hover. "
          "All links, in the Display tab, shows every link at once, in grey.")
    hush(); press(sel('#dispBtn'), 1.1, 0.8)
    press(by_text('#dispBox label', 'All links', False), 1.0, 1.0)
    press(sel('#tabSel'), 0.9, 0.5); view_design(1.0, 0.5)
    say("That's the whole web of dependencies.", True); wait(0.8)
    topic("Collapse all folds every timeline group into one box. "
          "Now the links show how the groups depend on each other, and the number on a line says how many links it stands for.")
    press(sel('#colAll'), 1.3, 1.5); press(sel('#fit'), 1.0, 1.0); hush(); wait(2.0)
    say("Expand all opens everything again, every linked design too. Here, let's just open the J2 arm again.")
    hush(); designs_state(True); view_design(1.0, 0.5)
    say("Let's turn All links off again. Hover and selection usually tell more.")
    press(sel('#dispBtn'), 1.1, 0.8)
    press(by_text('#dispBox label', 'All links', False), 1.0, 1.0)
    press(sel('#tabSel'), 0.9, 0.5); hush()

def step_8():
    """Search"""
    topic("Search finds items by name. Let's look for everything about the endstop.")
    hush(); press(sel('#search'), 1.2, 0.3); type_text(SEARCH); wait(2.0)
    say("The arrows jump from one match to the next.")
    hush(); press(sel('#sNext'), 1.0, 2.0); press(sel('#sNext'), 0.5, 2.0)
    press(sel('#search'), 1.0, 0.3); key('a', CMD, False); key('backspace', 0, False); wait(1.0)
    view_design(1.0)

def step_9():
    """Filters"""
    topic("The Filter tab hides whole kinds of items, or whole linked designs. Let's hide the parameters and the construction geometry.")
    hush(); press(sel('#filterBtn'), 1.2, 0.8)
    press(by_text('#cats label', 'Parameter', False), 1.0, 1.0)
    press(by_text('#cats label', 'Construction', False), 0.8, 1.0)
    press(sel('#tabSel'), 0.9, 0.5); view_design(1.0, 0.5)
    say("The graph is smaller now. Hidden items are skipped, not cut out: their links are joined through them.", True)
    wait(0.8)
    say("All shows everything again. Below, whole linked designs can be hidden the same way: links, and routes, still "
        "pass through them.")
    press(sel('#filterBtn'), 1.0, 0.8); press(sel('#catAll'), 0.8, 1.0); hover(sel('#dsgs'), 0.8, 2.0)
    press(sel('#tabSel'), 0.8, 0.5)
    view_design(1.0); hush()

def step_preview():
    """Suppression preview"""
    topic("What would suppressing a feature do? Every box has an on and off switch. It only changes the page, "
          "never the design.")
    zoom_on(power_btn(SUPPRESS_ITEM), 18, 1.3); hush()
    say("Let's switch off the stepper motor screws sketch.")
    press(power_btn(SUPPRESS_ITEM), 0.6, 1.5); view_design(1.0, 0.55, SUPPRESS_ITEM); hush()
    say("Everything Fusion suppressed along with it in the test is crossed out and dashed, and features that would fail "
        "to compute are marked in red. The bar at the top counts them.")
    hover(sel('#simBar'), 1.2, 2.0); hush()
    press("[...document.querySelectorAll('#simBar button')].find(b=>b.textContent==='Reset')", 1.0, 1.5); hush()
    # errors and warnings
    topic("Switching something off does not always just switch other features off. Some of them fail to compute, "
          "or compute with a warning, and the test records that too. Let's switch off the Derive feature that brings "
          "in the parameters.")
    zoom_on(power_btn(BREAK_ITEM), 18, 1.3); hush()
    press(power_btn(BREAK_ITEM), 0.6, 1.5); view_design(1.0, 0.55, BREAK_ITEM)
    say("Nothing is suppressed this time, but ten features would fail to compute, and six more would warn. "
        "The buttons at the top count them.")
    hover(sel('#health .hb'), 1.2, 1.5); hush()
    say("Clicking the red one goes to each feature that would fail, in turn. It is outlined in red, "
        "and the side panel says why.")
    press(sel('#health .hb'), 0.8, 2.5); move(*graph_xy(.92, .12), 0.8); wait(1.0)
    press(sel('#health .hb'), 1.0, 2.5); hush()
    say("The amber one does the same for the warnings.")
    press(sel('#health .hw'), 1.0, 2.5); hush()
    say("Without a preview, the same buttons show what fails or warns in the design as it is now.")
    clear_selection()
    press("[...document.querySelectorAll('#simBar button')].find(b=>b.textContent==='Reset')", 1.0, 1.5); hush()
    say("Reset switches everything back on.")

def step_select_in_fusion():
    """Select in Fusion"""
    topic("Selections can go back to Fusion. Let's select the J2 assembly insert, in the master assembly itself.")
    zoom_on(box(FUSION_ITEM, 'main'), 170, 1.3); press(box(FUSION_ITEM, 'main'), 0.6, 1.5); hush()
    say("Select in Fusion, in the side panel, selects it in the timeline and the browser. For an item of a linked "
        "design, Fusion opens that design and selects it there.")
    press(by_text('#details button', 'Select in Fusion'), 1.2, 1.5); hush()
    if not DRY:
        activate('Autodesk Fusion'); wait(3.0)
        activate('Safari'); wait(1.0)
    clear_selection()

def step_legend():
    """Legend"""
    topic("And the Legend tab of the side panel explains every colour, outline, marker and line.")
    press(sel('#legendBtn'), 1.2, 3.0); hush()
    press(sel('#tabSel'), 1.0, 1.0)

def playback_done():
    return js("(()=>{const n=document.getElementById('pbNext');const on=document.body.classList.contains('pbon');"
              "return (!on||(n&&n.disabled))?'1':'';})()") == '1'

def wait_playback_end(timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout and not playback_done(): wait(0.5)

def step_10():
    """History playback"""
    topic("Finally, history playback. With an item selected, Play animates how it was built, step by step, in timeline order.")
    zoom_on(box(ITEM), 150); press(box(ITEM), 0.6, 1.5); hush()
    press(sel('#pbStart'), 1.2, 0.5); move(*graph_xy(.92, .12), 0.9); wait(7)
    hush()
    say("Pause stops it for a moment, or press space.")
    press(sel('#pbPlay'), 1.1, 1.0); hush()
    say("While paused, the arrows step one item forward, and back.")
    press(sel('#pbNext'), 0.9, 2.5); press(sel('#pbPrev'), 0.7, 2.5); hush()
    say("The speed button makes it faster. Let's continue at double speed.")
    press(sel('#pbSpeed'), 1.1, 0.5); press(sel('#pbPlay'), 0.7, 0.3); move(*graph_xy(.92, .12), 0.9); hush()
    wait_playback_end()
    wait(1.0); say("And that's how it was built.", True); wait(0.8)
    if js("document.body.classList.contains('pbon')?'1':''") == '1':   # still open at the end: close it quietly
        press(sel('#pbStop'), 1.1, 1.0)

def step_11():
    """Closing words"""
    clear_selection(); view_design(1.1)
    move(*graph_xy(.5, .9), 1.0)
    hush(); wait(1.0)
    say("That's the dependencies graph: see what every feature depends on, before you change it.", True)
    wait(1.5)

STEPS = [
    ('Close the info bar', None, step_0),
    ('The side panel and its tabs', None, step_panel),
    ('Getting around: drag, zoom, Fit', 'folded', step_1),
    ('Linked designs: frames, pictures, unfolding one', 'folded', step_linked),
    ('Layouts: Depth, Components, Timeline, Groups', 'clear', step_layouts),
    ('Hover', 'clear', step_2),
    ('Select an item, side panel, Back', 'clear', step_3),
    ('Selecting a whole block', 'clear', step_blocks),
    ('Display tab: What uses it', 'item', step_4),
    ('Routes', 'item', step_5),
    ('Only the selected branch off + Cmd+click multi-select', 'item', step_6),
    ('All links, then Collapse all / Expand all', 'clear', step_7),
    ('Search', 'clear', step_8),
    ('Filters', 'clear', step_9),
    ('Suppression preview', 'clear', step_preview),
    ('Select in Fusion', 'clear', step_select_in_fusion),
    ('Legend', 'clear', step_legend),
    ('History playback', 'clear', step_10),
    ('Closing words', 'clear', step_11),
]

def list_steps():
    for k, (name, _, _) in enumerate(STEPS): print('  %2d  %s' % (k, name))

def safari_part(start=0):
    activate('Safari')
    for _ in range(10):
        if page_ready(): break
        wait(1)
    else:
        raise SystemExit('Cannot read the page. Is Develop > "Allow JavaScript from Apple Events" on in Safari?')
    if start == 0:
        move(*graph_xy(.5, .55), 1.0)
        say(DESIGN_INTRO, True)
    wait(0.6)
    for k, (name, prep, fn) in enumerate(STEPS):
        if k < start: continue
        print('Step %d: %s' % (k, name))
        release_mods()
        if k == start and k > 0: prepare(prep)
        fn()

def version():
    """The script's commit and date, printed at the start (to tell which copy is running)."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(['git', '-C', here, 'log', '-1', '--format=%h %ci', '--', os.path.basename(__file__)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        out = ''
    return out or time.strftime('file saved %Y-%m-%d %H:%M', time.localtime(os.path.getmtime(__file__)))

if __name__ == '__main__':
    print('record_demo.py,', version())
    if '--calibrate' in sys.argv:
        calibrate(); sys.exit()

    if '--list' in sys.argv:
        print('Steps (start from one with --step N):'); list_steps(); sys.exit()
    START = int(sys.argv[sys.argv.index('--step') + 1]) if '--step' in sys.argv else None
    if START is not None and not 0 <= START < len(STEPS):
        print('No step %d. The steps are:' % START); list_steps(); sys.exit(1)
    if START is not None:
        print('Starting at step %d: %s' % (START, STEPS[START][0]))
    for i in (5, 4, 3, 2, 1):
        print('Starting in %d… (start the screen recording now)' % i); time.sleep(1)
    if START is not None:          # a single part of the tour: use the page already open in Safari
        if not any(graph_tabs()): open_newest_page()
        safari_part(START)
    elif SKIP_GEN:
        open_newest_page(); activate('Safari')
        if not SKIP_INTRO: intro()
        safari_part()
    else:
        activate('Autodesk Fusion')
        if not SKIP_INTRO: intro()
        fusion_part()
        safari_part()
    hush()
    print('Done - stop the recording.')
