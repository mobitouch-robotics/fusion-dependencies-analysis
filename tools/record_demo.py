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

Needs once:
  * System Settings > Privacy & Security > Accessibility: turn on Terminal (the app you run it from).
  * Safari > Settings > Advanced: "Show features for web developers", then
    Develop menu > "Allow JavaScript from Apple Events" (used to find buttons on the page).
  * Fusion open with the design, on the Solid tab, window maximised; nothing else on top.
Start the screen recording during the countdown. Press Ctrl+C in Terminal to stop at any time.
"""
import ctypes, ctypes.util, json, subprocess, sys, time

DRY = '--dry' in sys.argv
SKIP_GEN = '--skip-generation' in sys.argv or '--from' in sys.argv
SKIP_INTRO = '--skip-intro' in sys.argv

# ---- scenario: edit the texts/timings here ---------------------------------------------------
FUSION_MANAGE_TAB = (647, 104)      # screen points, for a maximised Fusion window on this Mac
FUSION_GRAPH_BTN = (273, 137)
DIALOG_FULL_TEXT = (1640, 536)
DIALOG_QUICK_TEXT = (1650, 610)
DIALOG_FULL_BTN = (1770, 768)
HOVER_BOXES = ['Main J2 profile', 'Stepper motor screws']
ITEM = 'Stepper_Motor_Screw_Holes'      # selected, played back
ITEM_PARENT = 'Stepper motor screws'    # clicked in its side panel
ROUTE_TO = 'Main objects placement'     # route button used
SECOND_ITEM = 'Gearbox screws'          # Cmd+clicked, from another branch (Only the selected branch is off then)
SEARCH = 'endstop'
ANALYSIS_TIMEOUT = 900              # seconds to wait for the page to open in Safari
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
KEYS = {'esc': 53, 'space': 49, 'right': 124, 'left': 123, 'enter': 36, 'backspace': 51, 'a': 0}

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

# A box in the graph, found by its label (long labels are cut with "…", so a prefix match is used
# when there is no exact one).
FIND_G = ("(n=>{const gs=[...document.querySelectorAll('#graph g.nd')];const lab=g=>(g.querySelector(':scope > text')||{}).textContent||'';"
          "return gs.find(g=>lab(g)===n)||gs.find(g=>lab(g).endsWith('\u2026')&&n.startsWith(lab(g).slice(0,-1)));})('%s')")

def box(name): return "%s?.querySelector('rect')" % (FIND_G % name)

def route_btn(name): return "%s?.querySelector('.rbtn circle')" % (FIND_G % name)

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

def zoom_on(finder, width=200, dur=1.2):
    """Glides to an element, then zooms around it with the wheel until it is about `width` px wide."""
    xy = el_xy(finder)
    if xy is None:                     # outside the view: Fit first, then look again
        press(sel('#fit'), 1.0, 1.5); xy = el_xy(finder)
    if go(xy, dur, False) is False: return
    for _ in range(80):
        w = el_width(finder)
        if w is None: return
        if w < width * 0.9: scroll(1, 1, 0.05)
        elif w > width * 1.25: scroll(-1, 1, 0.05)
        else: break
    xy = el_xy(finder)                 # re-centre the cursor on it after zooming
    if xy: move(xy[0], xy[1], 0.4)

def graph_tabs():
    """URLs of all open Safari tabs showing a generated page."""
    if osa('application "Safari" is running', True) != 'true': return []
    out = osa('tell application "Safari" to get URL of every tab of every window', True)
    return [u.strip() for u in out.split(',') if 'dependencies_graph' in u]

def open_newest_page():
    import glob, os, tempfile
    d = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph')
    files = glob.glob(os.path.join(d, '*.html'))
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
        "and how any two features are connected. And you can select them back in Fusion with one click.", True)
    wait(1.2)

def fusion_part():
    activate('Autodesk Fusion')
    move(1300, 760, 0.8)
    say("Let's build the graph for this design. The add-in lives on the Manage tab.")
    activate('Autodesk Fusion')
    print('Fusion part: Manage tab, Dependencies Graph, Full analysis')
    go(FUSION_MANAGE_TAB, 1.5); wait(0.8)
    go(FUSION_GRAPH_BTN, 1.2); wait(1.0); hush()
    say("There are two ways to build the graph. Full analysis suppresses every item in turn, so every link is a real dependency.")
    go(DIALOG_FULL_TEXT, 1.5, False); hush()
    say("Quick estimate takes seconds, but only reads what each feature references, so it can miss some links.")
    go(DIALOG_QUICK_TEXT, 0.8, False); hush()
    say("Let's run the full analysis.")
    go(DIALOG_FULL_BTN, 1.1); wait(1.0)
    move(1560, 900, 1.2); hush()
    say("It can take a few minutes. When it's done, the graph opens in the browser, and the design is left exactly as it was.")
    if DRY: return
    print('Full analysis running, waiting for the page…')
    before = set(graph_tabs())
    t0 = time.time()
    while time.time() - t0 < ANALYSIS_TIMEOUT:
        wait(2)
        new = [u for u in graph_tabs() if u not in before]
        if new:
            print('Page opened:', new[-1]); break
    else:
        raise SystemExit('The page did not open in Safari in time.')
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
  if(q('#expAll'))q('#expAll').click();
  const svg=q('#graph');if(svg)svg.dispatchEvent(new MouseEvent('click',{bubbles:true}));
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  return '1';})()"""

def prepare(state):
    """Puts the page into the state a step expects, without visible cursor work."""
    if state is None: return
    js(RESET_JS); wait(0.6)
    if state == 'item':
        js("(()=>{const g=%s;if(g)g.dispatchEvent(new MouseEvent('click',{bubbles:true}));})()" % FIND_G % ITEM); wait(1.2)
    js("document.getElementById('fit').click()"); wait(1.2)
    move(*graph_xy(.92, .12), 0.6)

def step_0():
    """Close the info bar"""
    if js("document.body.classList.contains('infoopen')?'1':''") == '1':
        say("First, let's close the info bar at the bottom.")
        press(sel('#infoClose'), 1.2, 1.0); hush()

def step_1():
    """Getting around: drag, zoom, Fit"""
    topic("Drag the background to move around, and scroll to zoom.")
    move(*graph_xy(.75, .7), 1.0); wait(0.5)
    drag(-380, -160, 1.6); wait(0.8)
    scroll(2, 8, 1.0); wait(0.8); scroll(-2, 8, 1.0); hush()
    say("Fit brings the whole design back into view.")
    press(sel('#fit'), 1.2, 1.5); hush()

def step_2():
    """Hover"""
    topic("Hovering a box shows its direct links: blue arrows lead in from the items it uses, and green arrows lead out to the items that use it.")
    zoom_on(box(HOVER_BOXES[0]), 200, 0.9); hush(); wait(0.3)
    for i, name in enumerate(HOVER_BOXES):
        xy = el_xy(box(name))
        if xy is None: continue
        print('  hover:', name)
        move(xy[0] - 30, xy[1], 0.8); move(xy[0], xy[1], 0.2)   # a last small step, so the page sees the pointer arrive
        wait(2.4)
        if i < len(HOVER_BOXES) - 1:
            move(xy[0], xy[1] - 90, 0.4); wait(0.7)             # off the box: the highlight clears for a moment
    press(sel('#fit'), 0.9, 1.2)

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

def step_4():
    """Display menu: What uses it"""
    topic("The Display menu decides what a selection shows around it. Let's also turn on what uses it.")
    press(sel('#dispBtn'), 1.1, 1.0)
    hush(); press(by_text('#dispBox label', 'What uses it', False), 1.0, 1.5)
    say("Now the items built on top of the selection are highlighted too, joined to it by green arrows.", True)
    wait(1.5)
    press(by_text('#dispBox label', 'What uses it', False), 0.6, 1.0)
    press(sel('#dispBtn'), 0.9, 1.0)

def step_5():
    """Routes"""
    rb = ANY_ROUTE_BTN % route_btn(ROUTE_TO)
    xy = el_xy(rb)
    if xy is None:
        press(sel('#fit'), 1.0, 1.5); xy = el_xy(rb)
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
          "Turning off Only the selected branch, in the Display menu, shows it inside the whole design instead.")
    hush(); press(sel('#dispBtn'), 1.1, 0.8)
    press(by_text('#dispBox label', 'Only the selected branch', False), 1.0, 1.0)
    press(sel('#dispBtn'), 0.9, 0.5); press(sel('#fit'), 1.0, 1.5)
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
    press(sel('#dispBtn'), 0.9, 0.5); press(sel('#fit'), 1.0, 2.0); hush(); wait(0.6)
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
          "All links, in the Display menu, shows every link at once, in grey.")
    hush(); press(sel('#dispBtn'), 1.1, 0.8)
    press(by_text('#dispBox label', 'All links', False), 1.0, 1.0)
    press(sel('#dispBtn'), 0.9, 0.5); press(sel('#fit'), 1.0, 2.5)
    say("That's the whole web of dependencies.", True); wait(0.8)
    topic("Collapse all folds every timeline group into one box. "
          "Now the links show how the groups depend on each other, and the number on a line says how many links it stands for.")
    press(sel('#colAll'), 1.3, 1.5); press(sel('#fit'), 1.0, 1.0); hush(); wait(2.0)
    say("Expand all opens them again.")
    hush(); press(sel('#expAll'), 1.0, 1.0); press(sel('#fit'), 1.0, 1.5)
    say("Let's turn All links off again. Hover and selection usually tell more.")
    press(sel('#dispBtn'), 1.1, 0.8)
    press(by_text('#dispBox label', 'All links', False), 1.0, 1.0)
    press(sel('#dispBtn'), 0.9, 0.5); hush()

def step_8():
    """Search"""
    topic("Search finds items by name. Let's look for everything about the endstop.")
    hush(); press(sel('#search'), 1.2, 0.3); type_text(SEARCH); wait(2.0)
    say("The arrows jump from one match to the next.")
    hush(); press(sel('#sNext'), 1.0, 2.0); press(sel('#sNext'), 0.5, 2.0)
    press(sel('#search'), 1.0, 0.3); key('a', CMD, False); key('backspace', 0, False); wait(1.0)
    press(sel('#fit'), 1.0, 1.0)

def step_9():
    """Filters"""
    topic("The Filter menu hides whole kinds of items. Let's hide the parameters and the construction geometry.")
    hush(); press(sel('#filterBtn'), 1.2, 0.8)
    press(by_text('#cats label', 'Parameter', False), 1.0, 1.0)
    press(by_text('#cats label', 'Construction', False), 0.8, 1.0)
    press(sel('#filterBtn'), 0.9, 0.5); press(sel('#fit'), 1.0, 1.5)
    say("The graph is smaller now. Hidden items are skipped, not cut out: their links are joined through them.", True)
    wait(0.8)
    say("All shows everything again.")
    press(sel('#filterBtn'), 1.0, 0.8); press(sel('#catAll'), 0.8, 1.0); press(sel('#filterBtn'), 0.8, 0.5)
    press(sel('#fit'), 1.0, 1.0); hush()

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
    clear_selection(); press(sel('#fit'), 1.1, 1.0)
    move(*graph_xy(.5, .9), 1.0)
    hush(); wait(1.0)
    say("That's the dependencies graph: see what every feature depends on, before you change it.", True)
    wait(1.5)

STEPS = [
    ('Close the info bar', None, step_0),
    ('Getting around: drag, zoom, Fit', 'clear', step_1),
    ('Hover', 'clear', step_2),
    ('Select an item, side panel, Back', 'clear', step_3),
    ('Display menu: What uses it', 'item', step_4),
    ('Routes', 'item', step_5),
    ('Only the selected branch off + Cmd+click multi-select', 'item', step_6),
    ('All links, then Collapse all / Expand all', 'clear', step_7),
    ('Search', 'clear', step_8),
    ('Filters', 'clear', step_9),
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
        say("This is the graph of the J2 arm, a robot arm joint with about two hundred items. "
        "Every box is a timeline feature or a parameter. To keep a big design readable, "
        "the links between them appear when you hover or select something.", True)
    wait(0.6)
    for k, (name, prep, fn) in enumerate(STEPS):
        if k < start: continue
        print('Step %d: %s' % (k, name))
        release_mods()
        if k == start and k > 0: prepare(prep)
        fn()

if __name__ == '__main__':
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
