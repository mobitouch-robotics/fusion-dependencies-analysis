# Fusion Dependencies Analysis

**Dependencies Graph** is an Autodesk Fusion add-in that exports the timeline of a parametric design
as an interactive HTML page: a dependency graph and tree of every timeline item, with thumbnails,
timeline groups, components, parameters and optional suppression tests.

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
- **Hover** a box to highlight it in gold with its direct parents and children and the links between them
  (can be switched off in the Display menu).
- **Routes**: with an item selected, every other box of its tree gets a small route button; click it to
  highlight every route between the two (orange), with the number of routes. `Esc` hides them.
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

## Install

1. Copy the `DependenciesGraph` folder into Fusion's add-ins folder:
   - macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   - Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
2. In Fusion open **Utilities > Add-Ins > Scripts and Add-Ins**, select *DependenciesGraph* and run it
   (it is set to run on startup).
3. The **Dependencies Graph** command appears in the Design workspace (Manage tab).

## Use

Open a parametric design and run **Dependencies Graph**. Choose whether to capture thumbnails, then press
**Full analysis** (runs the suppression tests: every link is a real dependency; takes minutes) or
**Quick estimate** (references only; takes seconds). The design is restored afterwards (the tests suppress and unsuppress items and
the pictures change visibility, so save your work first). The result opens in your browser as a
self-contained HTML file.

## Layout

```
DependenciesGraph/
  DependenciesGraph.py        add-in: data collection in Fusion + the HTML/JS page template
  DependenciesGraph.manifest
  resources/DependenciesGraph/ toolbar icons
```
