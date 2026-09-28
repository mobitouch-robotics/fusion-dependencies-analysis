# Fusion Dependencies Analysis

**Dependencies Graph** is an Autodesk Fusion add-in that exports the timeline of a parametric design
as an interactive HTML page: a dependency graph and tree of every timeline item, with thumbnails,
timeline groups, components, parameters and optional suppression tests.

[![Dependencies Graph demo video](https://img.youtube.com/vi/AzQnUs5jFEE/maxresdefault.jpg)](https://youtu.be/AzQnUs5jFEE)

*▶ Watch the demo on YouTube*

## Features

### What is shown
- **Every timeline item** with its category colour and icon: sketches, construction geometry, solid,
  surface, sheet metal, form, mesh and body features, fillets and chamfers, face edits, holes and threads,
  component inserts and Derive features, joints and motion links.
- **Components** as boxes of their own: each hangs under the item that brought it in (insert, New
  Component, a feature set to new component, or a Derive feature) and is the parent of what is built in
  it; joints and motion links hang under the components they connect.
- **Parameters**: user parameters under a common *User Parameters* box in the Depth layout (or under the
  parameter or sketch that drives them), and derived parameters under their Derive feature.
- **Thumbnails** of the model after each timeline step, and one picture per component.
- **Links** for every kind of reference (sketch, profile, plane, faces/edges, body, feature, parameter,
  component, joint, suppression test). *Same body, later* (timeline order only) can be switched on in
  the Display menu.

### Layouts and navigation
- **Depth**, **Groups** (one block per timeline group), **Components** (one block per component) and
  **Timeline** (every item in one row in timeline order, groups as events, longer links arcing above)
  layouts. User parameters have a block of their own (no extra *User Parameters* box there).
- **Folding**: timeline groups, components, *Not in a group* and *User parameters* fold into one box;
  any box can fold what depends on it. *Collapse all* / *Expand all*. A line going into a folded box shows
  how many of the items folded into it the line leads to.
- **Filter menu**: show or hide any kind of item. Hidden items are skipped, not cut out: their links are
  joined through to the items they connect (dotted lines).
- **Selection** highlights what an item or group depends on (blue) and what depends on it (green), and
  pulls related boxes closer together (inside their block in the Groups / Components layouts).
- **Multiple selection**: Cmd+click (Mac) / Ctrl+click adds or removes items; several selected items are
  highlighted and played back together, and for two related items the side panel offers their routes.
- **Hover** a box to highlight it in gold with its direct parents and children and the links between them
  (can be switched off in the Display menu).
- **Routes**: with an item selected, every other box of its tree gets a small route button; click it to
  highlight every route between the two (orange), with the number of routes. `Esc` hides them.
- **Select in Fusion**: the side panel can select the item(s) in Fusion (timeline entry or browser), or the
  whole branch highlighted around the selection. The page talks to the add-in on 127.0.0.1 with a secret key
  written into the page when it is generated; parameters cannot be selected in Fusion.
- **Zoom on select**: to the selected object or to its whole tree (Display menu).
- **Back / forward** through selections, restoring the zoom and position you had.
- **Legend** explaining every colour, icon, outline, marker and line.
- **Display options**: thumbnails, only the selected branch, moving related boxes closer to the selection,
  what the selection highlights around itself (what it depends on / what uses it, each either direct only
  or the whole chain), all links (off by default: only the links of the selection and of the hovered box
  are drawn) and "Same body, later" links.

### History playback
Select an item (or a group, or nothing for the whole design) and press **Play** (or `P`) to animate how
it was built: a dot travels along the links to each next item, which fades in, while the view follows.
`Space` pauses, the right / left arrows (or the buttons) go one step forward / back (animated; while
paused they move one step and stay paused), `Esc` stops; speed 0.5x / 1x / 2x / 4x.

### Suppression tests and health
- Optional tests run in Fusion: *Whole groups* and *Every item* record what really gets suppressed or
  breaks when a group or item is switched off. The page can preview suppressing things without
  touching the design, including groups and items Fusion refuses to suppress (estimated).
- Features that fail to compute or have warnings are marked, with counts in the header.

### Linked designs
- Option *Include linked designs* (off by default): every design brought in with Derive or inserted as a
  linked component is read too, and the designs those link, at any depth. A design used several times
  (from several files, or at different versions) appears once, linked to every place that uses it.
- Each linked design has a connector on its frame: the items a Derive hands over (sketches, bodies,
  parameters) lead into it, and it leads into the Derive feature or the insert item. Linked designs start
  folded into one box; + opens them. Designs without a timeline (e.g. library parts) show only their frame.
- With *Full analysis*, linked designs get the item test; the *Whole groups* test too with *Group test for
  linked designs* (Advanced options, off by default: it takes about as long again; without it a linked
  group's preview adds up its items' results, marked estimated). Each is read and tested in a
  hidden copy of the version that is used, closed without saving; a linked design you have open in a
  tab is read as it is and not tested.
- Fusion keeps every test step's model data until a document is closed, so while a linked design is tested
  its hidden copy is closed and the same version opened again each time Fusion has grown by 4 GB
  (`"memoryRefreshGB"` in `DependenciesGraph/settings.json`; 0 switches it off).
- In the Groups and Components layouts every design is a frame of its own, with its timeline groups and
  a picture of the finished part; the page opens on all of them, then zooms to the design you analysed.
- The page is written while the run goes on: once this design is done and again after each linked design (at
  most every 10 s), over the same file, marked "Still being generated". Open it (or reload it) at any time to see
  what is done; it opens in the browser when the run finishes.
- Cancel stops the whole run; no page is opened. A page already written during the run is brought up to where it
  stopped and marked "Cancelled".

### Test speed-ups

The suppression tests always use these (they started as experiments; there is no toggle in the dialog):

- Fusion's periodic crash-recovery autosave (`Options.CrashRecovery`) and background mass-property calculation
  (`DebugCommands.BodyCacheUpdateMgr`) are switched off while a test runs and back on right after.
- In the item test, the marker move and the suppression (and the three steps of putting an item back) are made
  with `Design.isComputeDeferred` on, so Fusion computes once instead of after each step.
- After a test that took more than 3 s, the design is put back with Fusion's Undo (one step at a time, until
  every item has its original state) instead of switching the item back on, which makes Fusion compute the heavy
  features after it again. Checked; the usual way when Undo does not bring back exactly the original state.

To switch one off (for example to check whether it changes results with `tools/compare_pages.py`), add to
`DependenciesGraph/settings.json`: `"features": {"experimentUndoPutBack": false}` (keys:
`experimentNoCrashRecovery`, `experimentNoBodyCache`, `experimentDeferCompute`, `experimentUndoPutBack`).

### Reusing results

A saved version never changes, so what was read and tested in it is kept and reused: a linked design is tested
once per saved version, and a later run (or another assembly using the same part) takes its result from the
cache. Each design is kept as soon as it is done, so a cancelled run keeps the designs it finished. The cache is
in `~/Library/Application Support/FusionDependenciesGraph/cache` (macOS) or
`%APPDATA%\FusionDependenciesGraph\cache` (Windows); untick *Reuse earlier results* to test everything again.

## Install

1. Copy the `DependenciesGraph` folder into Fusion's add-ins folder:
   - macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   - Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
2. In Fusion open **Utilities > Add-Ins > Scripts and Add-Ins**, select *DependenciesGraph* and run it
   (it is set to run on startup).
3. The **Dependencies Graph** command appears in the Design workspace (Manage tab).

## Use

Open a parametric design and run **Dependencies Graph**. Choose whether to capture thumbnails, then press
**Full analysis** (runs the suppression tests: every link is a real dependency; takes minutes on a small design, much longer on a large assembly with linked designs) or
**Quick estimate** (references only; takes seconds). The design is restored afterwards (the tests suppress and unsuppress items and
the pictures change visibility, so save your work first). The result opens in your browser as a
self-contained HTML file.

## Layout

```
DependenciesGraph/
  DependenciesGraph.py        add-in: data collection in Fusion, tests, writing the page
  page_template.html          the generated page (HTML/CSS/JS); the add-in fills in the data
  progress_panel.html         the progress panel shown in Fusion during a run
  DependenciesGraph.manifest
  resources/DependenciesGraph/ toolbar icons
```

How it works inside (scan, suppression tests and their optimisations, linked designs, cache, memory, the page):
see [dev-documentation.md](dev-documentation.md).
