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
                        python3 tools/record_demo.py --probe        (with Fusion in front: print which Fusion elements
                                                                     are found on screen, and where)
                        python3 tools/record_demo.py --calibrate    (only if --probe misses some: point at them once;
                                                                     saved in tools/demo_positions.json)

Needs once:
  * System Settings > Privacy & Security > Accessibility: turn on Terminal (the app you run it from).
  * Safari > Settings > Advanced: "Show features for web developers", then
    Develop menu > "Allow JavaScript from Apple Events" (used to find buttons on the page).
  * Fusion open with the design, on the Solid tab, window maximised; nothing else on top.
  * System Settings > Privacy & Security > Screen Recording: also Terminal. The Fusion elements (the MANAGE tab,
    the Dependencies Graph panel, the dialog's options, the progress panel) are found on screen by their text,
    with macOS's own text recognition, wherever Fusion puts them; then through Accessibility; then at positions
    from --calibrate. Check with --probe.
  * "Include linked designs" and "Reuse earlier results" are switched on by the script (in the add-in's
    settings.json, before the dialog opens); the tour only points at them.
  * Run a Full analysis with linked designs once before recording: the linked designs are then taken from the
    earlier run (Reuse earlier results), so the recorded run takes minutes, not hours.
Start the screen recording during the countdown. Press Ctrl+C in Terminal to stop at any time.

What the tour shows, in Fusion: the Manage tab button, the dialog (Full analysis / Quick estimate, Thumbnails,
Advanced options: Include linked designs, Group test for linked designs, Reuse earlier results) and the progress
panel (a row per design with its steps, time left, Cancel). In the page (--list for the numbered steps): getting
around, the design frames of linked designs, the four layouts, hover, selection and side panel, Display options,
routes, multi-selection, all links and folding, search, filters, the suppression preview, Select in Fusion, the
legend, history playback.
"""
import ctypes, ctypes.util, json, os, subprocess, sys, time

DRY = '--dry' in sys.argv
SKIP_GEN = '--skip-generation' in sys.argv or '--from' in sys.argv
SKIP_INTRO = '--skip-intro' in sys.argv

# ---- scenario: edit the texts/timings here ---------------------------------------------------
# Fusion: its elements are found on screen by their text (see TARGETS / locate). These screen points are only the
# last resort: --calibrate records them in tools/demo_positions.json, which overrides the defaults here.
FUSION_POINTS = [
    # name,               default,       what to point at when calibrating
    ('manage_tab',        (647, 104),    'the MANAGE tab in the toolbar'),
    ('graph_panel',       None,          'the DEPENDENCIES GRAPH panel name on the Manage tab (opens its menu)'),
    ('graph_btn',         (273, 137),    'with that menu open: the Dependencies Graph command in it'),
    ('full_text',         (1640, 536),   'in the open dialog: the "Full analysis" description text'),
    ('quick_text',        (1650, 610),   'in the dialog: the "Quick estimate" description text'),
    ('thumbs',            None,          'in the dialog: the Thumbnails checkbox'),
    ('advanced',          None,          'in the dialog: the "Advanced options" group header (to open it)'),
    ('linked',            None,          'in the dialog, Advanced options open: the "Include linked designs" label'),
    ('linked_groups',     None,          'in the dialog, Advanced options open: the "Group test for linked designs" label'),
    ('reuse',             None,          'in the dialog, Advanced options open: the "Reuse earlier results" label'),
    ('full_btn',          (1770, 768),   'in the dialog: the "Full analysis" button (do not click it)'),
    ('progress_panel',    None,          'during a run: the progress panel (the right-hand palette), its middle'),
    ('progress_cancel',   None,          'during a run: the Cancel button of the progress panel (do not click it)'),
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
FUSION_ITEM = 'Component Insert J2 assembly v49:1'   # an item of the Master assembly itself: Select in Fusion
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

def load_points():
    pts = {k: v for k, v, _ in FUSION_POINTS}
    try:
        with open(POSITIONS_FILE, encoding='utf-8') as f:
            pts.update({k: tuple(v) if v else None for k, v in json.load(f).items()})
    except FileNotFoundError:
        pass
    return pts

FP = load_points()

# ---- finding Fusion's elements on screen --------------------------------------------------------------------
# Each element by the text it shows (any upper/lower case: Fusion versions differ). pick: which match when there are
# several (top / bottom / right: the right-most). below: only matches under that element (the panel's menu opens under
# the panel name). left: that many points left of the label's first letter (a group's fold arrow). Text in the
# macOS menu bar (the top MENU_BAR points) is never used.
TARGETS = {
    'manage_tab':      dict(text='Manage', pick='top'),
    'graph_panel':     dict(text='Dependencies Graph', pick='top'),
    'graph_btn':       dict(text='Dependencies Graph', pick='top', below='graph_panel'),
    'full_text':       dict(text='Full analysis', pick='top'),
    'quick_text':      dict(text='Quick estimate', pick='top'),
    'thumbs':          dict(text='Thumbnails', pick='top'),
    'advanced':        dict(text='Advanced options', pick='top', left=12),   # its fold arrow, left of the label
    'linked':          dict(text='Include linked designs', pick='top'),
    'linked_groups':   dict(text='Group test for linked designs', pick='top'),
    'reuse':           dict(text='Reuse earlier results', pick='top'),
    'full_btn':        dict(text='Full analysis', pick='bottom'),
    'progress_panel':  dict(text='(this design)', pick='right'),
    'progress_cancel': dict(text='Cancel', pick='right'),
}

# macOS text recognition (Vision) on a screenshot of the main display, through JavaScript for Automation: nothing
# to install. Returns every occurrence of the wanted strings with its box in screen points (top-left origin).
OCR_JXA = r"""ObjC.import('Vision');ObjC.import('AppKit');ObjC.import('Foundation');
function run(argv){const path=argv[0],want=JSON.parse(argv[1]);
 const h=$.VNImageRequestHandler.alloc.initWithURLOptions($.NSURL.fileURLWithPath(path),$.NSDictionary.dictionary);
 const r=$.VNRecognizeTextRequest.alloc.init;r.recognitionLevel=0;r.usesLanguageCorrection=false;
 h.performRequestsError($.NSArray.arrayWithObject(r),null);
 const fr=$.NSScreen.mainScreen.frame,W=fr.size.width,H=fr.size.height,res=r.results,out=[];
 for(let i=0;i<res.count;i++){const c=res.objectAtIndex(i).topCandidates(1).objectAtIndex(0);const t=c.string.js;
  if(!want.length){const b=res.objectAtIndex(i).boundingBox;out.push({text:'',line:t,x:(b.origin.x+b.size.width/2)*W,y:(1-b.origin.y-b.size.height/2)*H});continue;}
  for(const w of want){const hay=w.case?t:t.toLowerCase(),ned=w.case?w.text:w.text.toLowerCase();let k=hay.indexOf(ned);
   while(k>=0){const o=c.boundingBoxForRangeError($.NSMakeRange(k,ned.length),null);
    if(o&&!o.isNil()){const b=o.boundingBox;out.push({text:w.text,line:t,x:(b.origin.x+b.size.width/2)*W,
     y:(1-b.origin.y-b.size.height/2)*H,w:b.size.width*W,h:b.size.height*H});}
    k=hay.indexOf(ned,k+1);}}}
 return JSON.stringify(out);}"""

# Accessibility (System Events): Fusion's windows walked for elements whose name, description, title or value holds
# a wanted string. Slower than the text recognition; used for what it did not find.
AX_JXA = r"""function run(argv){const want=JSON.parse(argv[0]);const se=Application('System Events');
 const ps=se.applicationProcesses.whose({bundleIdentifier:'com.autodesk.fusion360'})();if(!ps.length)return '[]';
 const out=[],seen={};let n=0;const txt=e=>{const a=[];for(const f of ['name','description','title','value']){try{const v=e[f]();if(typeof v==='string'&&v)a.push(v);}catch(x){}}return a;};
 const walk=(e,d)=>{if(n++>3000||d>20)return;let ts=[];try{ts=txt(e);}catch(x){}
  for(const w of want){if(seen[w.text])continue;if(ts.some(t=>w.case?t.includes(w.text):t.toLowerCase().includes(w.text.toLowerCase()))){
    try{const p=e.position(),s=e.size();out.push({text:w.text,line:ts.join(' | '),role:(()=>{try{return e.role()}catch(x){return ''}})(),
      x:p[0]+s[0]/2,y:p[1]+s[1]/2,w:s[0],h:s[1]});seen[w.text]=1;}catch(x){}}}
  if(want.every(w=>seen[w.text]))return;let ks=[];try{ks=e.uiElements();}catch(x){}for(const k of ks)walk(k,d+1);};
 for(const w of ps[0].windows())walk(w,0);return JSON.stringify(out);}"""

_ocr_cache = {'t': 0, 'hits': []}
MENU_BAR = 30

def _run(cmd, timeout):
    """A helper process with a time limit: (stdout, error text or '')."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), (r.stderr.strip() if r.returncode else '')
    except subprocess.TimeoutExpired:
        return '', 'no answer in %d s' % timeout

def ocr_scan(fresh=True, everything=False):
    """Every occurrence of every target's text on the main display now (one screenshot, a second or two).
    everything: every recognised line instead (for --probe)."""
    if not fresh and not everything and time.time() - _ocr_cache['t'] < 2: return _ocr_cache['hits']
    import tempfile
    t0 = time.time()
    img = os.path.join(tempfile.gettempdir(), '_demo_screen.png')
    _, err = _run(['screencapture', '-x', '-m', img], 15)
    if err or not os.path.exists(img):
        print('  (screenshot failed: %s)' % (err or 'no file')); return []
    want = [] if everything else [{'text': t, 'case': False} for t in sorted({v['text'] for v in TARGETS.values()})]
    out, err = _run(['osascript', '-l', 'JavaScript', '-e', OCR_JXA, img, json.dumps(want)], 30)
    try: hits = json.loads(out or '[]')
    except Exception: hits = []
    hits = [h for h in hits if h['y'] > MENU_BAR]
    if err: print('  (text recognition failed: %s)' % err[:300])
    elif not everything:
        print('  (text recognition: %d matches in %.1f s)' % (len(hits), time.time() - t0))
        if not hits:
            print('  (nothing recognised: is Terminal allowed under Privacy & Security > Screen Recording?)')
    if not everything: _ocr_cache.update(t=time.time(), hits=hits)
    return hits

AX_TIMEOUT = 25          # seconds the Accessibility search may take (Fusion has thousands of controls)

def ax_scan(names):
    want = json.dumps([{'text': TARGETS[n]['text'], 'case': False} for n in names])
    t0 = time.time()
    print('  (looking for %s through Accessibility, up to %d s...)' % (', '.join(names), AX_TIMEOUT))
    out, err = _run(['osascript', '-l', 'JavaScript', '-e', AX_JXA, want], AX_TIMEOUT)
    if err: print('  (Accessibility: %s)' % err[:200])
    try: hits = json.loads(out or '[]')
    except Exception: hits = []
    print('  (Accessibility: %d found in %.1f s)' % (len(hits), time.time() - t0))
    return [h for h in hits if h['y'] > MENU_BAR]

def _pick(name, hits, found):
    t = TARGETS[name]
    c = [h for h in hits if h['text'] == t['text']]
    ref = found.get(t.get('below')) if t.get('below') else None
    if ref: c = [h for h in c if h['y'] > ref[1] + 5]
    if not c: return None
    h = {'top': min, 'bottom': max}.get(t['pick'], max)(c, key=lambda h: h['x'] if t['pick'] == 'right' else h['y'])
    if t.get('left') is not None:
        w = h.get('w') or len(t['text']) * 6.5            # the label's width; estimated when not reported
        x = h['x'] - w / 2 - t['left']
    else:
        x = h['x']
    return (round(x), round(h['y']))

_found = {}

def locate(name, fresh=True):
    """Where a Fusion element is now: found by its text on screen, else through Accessibility, else a calibrated or
    default position. None when nothing knows (the move is then skipped)."""
    print('  looking for %s ("%s")...' % (name, TARGETS[name]['text']))
    p = _pick(name, ocr_scan(fresh), _found)
    how = 'on screen'
    if p is None:
        p = _pick(name, ax_scan([name]), _found); how = 'accessibility'
    if p is None:
        p = FP.get(name); how = 'calibrated/default'
    if p is None:
        print('  (skipped: %s not found; try --probe)' % name)
    else:
        print('  %s: %s (%s)' % (name, p, how)); _found[name] = p
    return p

def probe():
    """What the script finds of Fusion's elements now (bring Fusion to the front, open the dialog, start a run...)."""
    activate('Autodesk Fusion'); wait(1.0)
    hits = ocr_scan()
    print('Text recognition on the main display:')
    for n in TARGETS:
        p = _pick(n, hits, _found)
        if p: _found[n] = p
        print('  %-16s %-32r %s' % (n, TARGETS[n]['text'], p or '-'))
    missing = [n for n in TARGETS if n not in _found]
    if missing:
        print('Accessibility, for the rest (may take a while):')
        ax = ax_scan(missing)
        for n in missing:
            p = _pick(n, ax, _found)
            line = next((h for h in ax if h['text'] == TARGETS[n]['text']), None)
            print('  %-16s %-32r %s%s' % (n, TARGETS[n]['text'], p or '-', ('  [%s: %s]' % (line['role'], line['line'][:60])) if line else ''))
    lines = sorted(ocr_scan(everything=True), key=lambda h: (round(h['y'] / 12), h['x']))
    print('Text recognised in the top 250 points of the screen (the toolbar):')
    for h in [h for h in lines if h['y'] < 250][:60]:
        print('  (%4d,%4d)  %s' % (h['x'], h['y'], h['line'][:90]))
    print('(Elements of the dialog or the progress panel are only found while they are open.)')

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
          "return gs.find(g=>g.dataset.name===n)||gs.find(g=>lab(g)===n)||gs.find(g=>lab(g).endsWith('\u2026')&&n.startsWith(lab(g).slice(0,-1)));})('%s','%s')")

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
    import glob, tempfile
    d = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph')
    files = glob.glob(os.path.join(d, '*.html'))
    try:        # a file chosen with "Save to" in the dialog
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'DependenciesGraph',
                               'settings.json'), encoding='utf-8') as f:
            sp = json.load(f).get('savePath')
        if sp and os.path.exists(sp): files.append(sp)
    except Exception:
        pass
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

def open_advanced(p):
    """Opens the dialog's Advanced options with the arrow left of its label; checks it opened (its options are then
    on screen) and otherwise tries a little further left, then the label itself."""
    for dx in (0, -8, 6):
        xy = (p[0] + dx, p[1])
        print('  clicking the Advanced options arrow at %s' % (xy,))
        go(xy, 1.1); wait(1.2)
        if _pick('linked', ocr_scan(), _found):
            print('  Advanced options is open'); return True
        print('  (Advanced options did not open with a click at %s)' % (xy,))
    print('  (could not open Advanced options: its options are skipped)')
    return False

# ---- the add-in's options for the recording: written into its settings.json before the dialog opens (the dialog
# reads them each time it opens), so "Include linked designs" is ticked and earlier results are reused.
DEMO_SETTINGS = {'derived': True, 'reuse': True}

def addin_settings_files():
    """settings.json of every Dependencies Graph add-in Fusion can load: its add-ins folder, and this repository."""
    import glob
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs = glob.glob(os.path.expanduser('~/Library/Application Support/Autodesk/*/API/AddIns/*'))
    dirs.append(os.path.join(here, 'DependenciesGraph'))
    return [os.path.join(d, 'settings.json') for d in dirs if os.path.exists(os.path.join(d, 'DependenciesGraph.py'))]

def set_demo_options():
    for f in addin_settings_files():
        try:
            with open(f, encoding='utf-8') as h: st = json.load(h)
        except Exception:
            st = {}
        st.update(DEMO_SETTINGS)
        try:
            with open(f, 'w', encoding='utf-8') as h: json.dump(st, h)
            print('  options set in', f)
        except Exception as ex:
            print('  (could not write %s: %s)' % (f, ex))

def fusion_part():
    activate('Autodesk Fusion')
    move(1300, 760, 0.8)
    say("Let's build the graph for this design. The add-in lives on the Manage tab.")
    activate('Autodesk Fusion')
    print('Fusion part: Manage tab, Dependencies Graph, options, Full analysis, progress panel')
    set_demo_options()          # Include linked designs ticked, earlier results reused
    go(locate('manage_tab'), 1.5); wait(1.0)
    # the panel's name opens its menu, with the command in it
    go(locate('graph_panel'), 1.2); wait(1.0)
    go(locate('graph_btn'), 0.8); wait(2.0); hush()
    ocr_scan()                                  # the dialog is open: one look finds all its elements
    say("There are two ways to build the graph. Full analysis suppresses every item in turn, so every link is a real dependency.")
    go(locate('full_text', False), 1.5, False); hush()
    say("Quick estimate takes seconds, but only reads what each feature references, so it can miss some links.")
    go(locate('quick_text', False), 0.8, False); hush()
    p = locate('thumbs', False)
    if p:
        say("Thumbnails adds a picture of every step, framed on the feature itself.")
        go(p, 1.0, False); hush()
    p = locate('advanced', False)
    if p:
        say("Advanced options holds the rest.")
        open_advanced(p); hush()
        p = locate('linked', False)
        if p:
            say("Include linked designs also reads every design this one links, through Derive features or inserted "
                "components, and the designs those link. Each is tested too, in a hidden copy that is closed without saving.")
            go(p, 1.1, False); hush()
        p = locate('linked_groups', False)
        if p:
            say("The group test for linked designs is optional. It takes about as long again, "
                "and without it their groups are estimated from their items.")
            go(p, 0.9, False); hush()
        p = locate('reuse', False)
        if p:
            say("And Reuse earlier results: a saved version never changes, so a design tested once is taken from the "
                "earlier run, and only what changed is tested again.")
            go(p, 0.9, False); hush()
    say("Let's run the full analysis.")
    go(locate('full_btn'), 1.1); wait(3.0)
    hush()
    ocr_scan()                                  # the progress panel is open
    p = locate('progress_panel', False)
    if p:
        say("The progress panel on the right has a row for every design: what is being done, and a bar for each step, "
            "reading, the item test and the group test. The overall bar shows the time left.")
        go(p, 1.2, False); hush()
        p = locate('progress_cancel', False)
        if p:
            say("Cancel stops the whole run. The design is put back either way.")
            go(p, 0.9, False); hush()
    else:
        move(1560, 900, 1.2)
    say("On a big assembly this can take a while. The page is saved as each design is done, so you can look at it early. "
        "When it's finished, it opens in the browser, and the design is left exactly as it was.")
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
  if(q('#legend')&&q('#legend').classList.contains('open')&&q('#legendClose'))q('#legendClose').click();
  if(q('#laneBtn')&&!q('#laneBtn').classList.contains('on'))q('#laneBtn').click();
  const rs=[...document.querySelectorAll('#simBar button')].find(b=>b.textContent==='Reset');if(rs)rs.click();
  const svg=q('#graph');if(svg)svg.dispatchEvent(new MouseEvent('click',{bubbles:true}));
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  return '1';})()"""

# Every timeline group open, every linked design folded except `keep` (a frame id, or '' for none): the page as it
# opens ('folded'), or with the tour's design open ('clear', 'item'). Instant, through the page's own buttons.
DESIGNS_JS = """((keep)=>{const q=s=>document.querySelector(s);if(q('#expAll'))q('#expAll').click();
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
    press(sel('#fit'), 1.0, 1.5); hush()
    say("Its own timeline groups are blocks inside its frame. The minus button on the frame folds it again.")
    hover(fold_btn(), 1.2, 2.0); hush()
    say("Let's open its timeline groups too, to see its features.")
    designs_state(True); press(sel('#fit'), 1.0, 2.0); hush()
    move(*graph_xy(.92, .12), 0.8)

def step_layouts():
    """Layouts: Depth, Components, Timeline, Groups"""
    topic("The graph has four layouts. Groups, the default, gives every timeline group a block of its own.")
    hush()
    say("Depth arranges the boxes in rows, by how deep each one is in the dependencies.")
    press(sel('#layDeps'), 1.2, 1.0); press(sel('#fit'), 0.9, 2.0); hush()
    say("Components gives every component a block.")
    press(sel('#compBtn'), 1.0, 1.0); press(sel('#fit'), 0.9, 2.0); hush()
    say("And Timeline puts every item in one row, in timeline order, with the longer links arcing above it.")
    press(sel('#layTime'), 1.0, 1.0); press(sel('#fit'), 0.9, 2.5); hush()
    say("Back to Groups.")
    press(sel('#laneBtn'), 1.0, 1.0); press(sel('#fit'), 0.9, 1.5); hush()

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
    say("Expand all opens everything again, every linked design too. Here, let's just open the J2 arm again.")
    hush(); designs_state(True); press(sel('#fit'), 1.0, 1.5)
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

def step_preview():
    """Suppression preview"""
    topic("What would suppressing a feature do? Every box has an on and off switch. It only changes the page, "
          "never the design.")
    zoom_on(power_btn(SUPPRESS_ITEM), 18, 1.3); hush()
    say("Let's switch off the stepper motor screws sketch.")
    press(power_btn(SUPPRESS_ITEM), 0.6, 1.5); press(sel('#fit'), 1.0, 2.0); hush()
    say("Everything Fusion suppressed along with it in the test is crossed out and dashed, and features that would fail "
        "to compute are marked in red. The bar at the top counts them.")
    hover(sel('#simBar'), 1.2, 2.0); hush()
    press("[...document.querySelectorAll('#simBar button')].find(b=>b.textContent==='Reset')", 1.0, 1.5); hush()
    # errors and warnings
    topic("Switching something off does not always just switch other features off. Some of them fail to compute, "
          "or compute with a warning, and the test records that too. Let's switch off the Derive feature that brings "
          "in the parameters.")
    zoom_on(power_btn(BREAK_ITEM), 18, 1.3); hush()
    press(power_btn(BREAK_ITEM), 0.6, 1.5); press(sel('#fit'), 1.0, 2.0)
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
    say("Select in Fusion, in the side panel, selects it in the timeline and the browser.")
    press(by_text('#details button', 'Select in Fusion'), 1.2, 1.5); hush()
    if not DRY:
        activate('Autodesk Fusion'); wait(3.0)
        activate('Safari'); wait(1.0)
    clear_selection()

def step_legend():
    """Legend"""
    topic("And the Legend explains every colour, outline, marker and line.")
    press(sel('#legendBtn'), 1.2, 3.0); hush()
    press(sel('#legendClose'), 1.0, 1.0)

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
    ('Getting around: drag, zoom, Fit', 'folded', step_1),
    ('Linked designs: frames, pictures, unfolding one', 'folded', step_linked),
    ('Layouts: Depth, Components, Timeline, Groups', 'clear', step_layouts),
    ('Hover', 'clear', step_2),
    ('Select an item, side panel, Back', 'clear', step_3),
    ('Display menu: What uses it', 'item', step_4),
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
    if '--probe' in sys.argv:
        probe(); sys.exit()
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
