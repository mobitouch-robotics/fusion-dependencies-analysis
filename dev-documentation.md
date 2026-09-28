# Dependencies Graph: developer documentation

This document describes how the Dependencies Graph add-in for Autodesk Fusion works inside: its files and
data flow, the dependency scan, the suppression tests and their optimisations, linked designs, the cache,
memory handling, the progress panel, the "Select in Fusion" server, the generated page, and the reasons
behind the less obvious decisions. `README.md` describes the add-in for users.

## Contents

1. [Files](#1-files)
2. [Big picture](#2-big-picture)
3. [Add-in lifecycle and events](#3-add-in-lifecycle-and-events)
4. [The dialog and settings](#4-the-dialog-and-settings)
5. [A run: `generate()`](#5-a-run-generate)
6. [Reading a design: `Collector`](#6-reading-a-design-collector)
7. [Thumbnails and pictures](#7-thumbnails-and-pictures)
8. [Suppression tests](#8-suppression-tests)
9. [Linked designs](#9-linked-designs)
10. [Result cache](#10-result-cache)
11. [Memory](#11-memory)
12. [Test speed-ups](#12-test-speed-ups-former-experiments)
13. [Progress panel](#13-progress-panel)
14. [Select in Fusion (local HTTP server)](#14-select-in-fusion-local-http-server)
15. [The generated page](#15-the-generated-page)
16. [Data format](#16-data-format)
17. [Logs and diagnostics](#17-logs-and-diagnostics)
18. [Tools](#18-tools)
19. [Things that were tried and removed](#19-things-that-were-tried-and-removed)
20. [Unused code and known issues](#20-unused-code-and-known-issues)
21. [Working on the code](#21-working-on-the-code)

---

## 1. Files

```
DependenciesGraph/
  DependenciesGraph.py          the add-in (Python, runs inside Fusion)
  page_template.html            the generated page: HTML + CSS + JS; the add-in fills in the data
  progress_panel.html           the progress palette shown in Fusion during a run
  DependenciesGraph.manifest    Fusion add-in manifest (runOnStartup: true)
  resources/DependenciesGraph/  toolbar icons (16/32/64 px, light and dark, @2x)
  settings.json                 created at run time, not in git (see .gitignore)
tools/
  compare_pages.py              compares the test results of two generated pages
  record_demo.py (+ .command)   macOS script that drives Fusion and Safari for the demo video
README.md                       user documentation
dev-documentation.md            this file
```

`DependenciesGraph.py` is organised top to bottom as:

| Section | What it holds |
|---|---|
| helpers | `_t`, `_safe`, `_has`, category tables (`CAT_BY_TYPE`, `FEATURE_INPUT_ATTRS`, `DEF_INPUT_ATTRS`) |
| `class Collector` | reads one design: nodes, references, outputs, thumbnails, components, parameters, suppression tests |
| linked designs | `_linked_occurrences`, `_derive_features`, `_collect_derived`, `_PlainDesign`, `_CachedDesign` |
| memory | `_process_memory`, `_mem_log`, `_mem_tick` |
| result cache | `_cache_*`, `_design_data` |
| text commands | `_probe_text_commands`, `_tx_*`, experiments (`EXPERIMENTS`, `_experiments_begin/_end`) |
| progress panel | `_PanelCancelHandler`, `_ProgressPanel`, `_prow` |
| run | `generate`, `_write_page` |
| select in Fusion | HTTP server `_SelRequest`, `_sel_start/_stop`, `_do_select`, `_SelHandler` |
| add-in UI | command ids, `_CreatedHandler` (dialog), input/validate/execute handlers, `_RunHandler`, `_Stepper`, `_run_finished`, `_build_ui`, `run`, `stop` |

## 2. Big picture

```
 button ──> dialog (_CreatedHandler) ──> custom event EVENT_ID ──> _RunHandler
                                                                     │
                                            generate() generator, driven by _Stepper
                                                                     │
   ┌─────────────────────────────────────────────────────────────────┤
   │ Collector(des): build_nodes → scan (references, outputs,        │
   │   thumbnails) → part picture → scan_components → scan_parameters│
   │ item suppression test → group suppression test                  │
   │ linked designs (_collect_derived): open → read → test → close   │
   └─────────────────────────────────────────────────────────────────┤
                                                                     │
                              col.result() → data dict → cache → _write_page()
                                                                     │
                     page_template.html + JSON → .html file → browser
                                                                     │
                           _run_finished: reopen the saved version (discard changes)
```

What the add-in produces is a single self-contained HTML page. All data (nodes, links, groups, test results,
thumbnails as data URIs) is embedded as one JSON object; the page needs nothing else except, for "Select in
Fusion", the add-in's local HTTP server.

Two sources of dependencies:

* **References** (Quick estimate and Full analysis): what each feature's API inputs point at (sketches, profiles,
  planes, faces/edges, bodies, components, joints, parameters). Fast, but incomplete: Fusion does not expose
  every internal dependency through the API.
* **Suppression tests** (Full analysis): each item (and each timeline group) is suppressed in Fusion and the
  add-in records what Fusion suppresses with it, what fails to compute and what gets warnings. That is ground
  truth, and it is what most of the complexity and all the performance work is about.

## 3. Add-in lifecycle and events

* `run(context)` builds the UI (`_build_ui`): a button definition `CMD_ID` in the Design workspace, Manage tab,
  in the add-in's own toolbar panel. It registers three custom events and starts the selection server.
  It also switches Fusion's transaction system back on if an old version left it off (see §19), and ends
  experiments an interrupted run left on (§12).
* `stop(context)` removes the UI, unregisters events, stops the server, closes a running generator
  (`_STEPPER.gen.close()`, which runs its `finally` blocks and so restores the design state) and ends leftover
  experiments.
* `_cleanup_ui` also removes buttons and panels of older versions (old ids `claudeHistoryGraphCmd`,
  `claudeHistoryGraphPanel`...): Fusion remembers where a panel id was placed, so retired ids are deleted
  explicitly.

Custom events:

| Event | Handler | Why |
|---|---|---|
| `EVENT_ID` (`claudeDesignGraphRun`) | `_RunHandler` | Starts a run after the dialog has closed. The work cannot run inside the command's execute handler: it would be one long command, and Fusion would undo it or block the UI. |
| `STEP_EVENT_ID` (`claudeDesignGraphStep`) | `_StepHandler` → `_Stepper.step()` | Continues the run generator (see below). |
| `SEL_EVENT_ID` (`claudeDesignGraphSelect`) | `_SelHandler` | Runs a selection request from the HTTP thread on Fusion's main thread. |

### `_Stepper`: why `generate()` is a generator

Some Fusion actions only take effect after the add-in returns control to Fusion, for example executing the
`UndoCommand`. So `generate()` and every step under it (the suppression tests, `_collect_derived`) are
generators. Where they need Fusion to act they `yield`. `_Stepper._step_once()` calls `next(gen)`, then fires
`STEP_EVENT_ID`; Fusion delivers that event after it has processed what was queued, and `_StepHandler`
continues the generator. If firing the event fails, the stepper continues synchronously. When the generator
ends, `done(fin.value)` → `_run_finished` runs. Only the Undo put-back (§12) currently yields;
otherwise the generators run straight through.

`_run_finished(doc, warnings)`: the run moves the marker and suppresses items, so the document ends up
"modified" even though it is back in its original state. The run is only allowed on a saved, unmodified
design (`_unsaved_reason`), so everything that marks it modified came from the run: `_revert` closes it
without saving and opens the same saved version again. This also frees the undo history (memory) the run built
up in that document.

## 4. The dialog and settings

`_CreatedHandler` builds the dialog:

* Information text: design name, timeline entries, groups.
* Options: **Thumbnails**, **Save to** (text + *Choose file...*). The page always goes to a file: when the dialog
  opens, `_default_save_path` proposes `<design> v<version>_dependencies_graph.html` in the folder of the last
  saved page (Downloads the first time) and stores it as `savePath`; *Choose file...* picks another.
* Advanced options (folded): **Include linked designs**, **Group test for linked designs**, **Reuse earlier
  results**. The test speed-ups (§12) have no toggle.
* The OK button is **Full analysis**; **Quick estimate** is a button input. Pressing it cannot end the command
  from its own input event, so `_InputChangedHandler` fires `EVENT_ID` with `closeDialog: True`, and
  `_RunHandler` terminates the dialog first (`terminateActiveCommand`), asks for confirmation, then runs with
  `mode='off'`. Fusion has no API for a button next to OK and Cancel, so it stays in the dialog.
* The Full analysis text gives no fixed time (a small design takes minutes, a large assembly with linked designs
  much longer).
* `_ValidateHandler` disables OK while the design is unsaved or modified.

Checkbox changes are written to `settings.json` immediately (`_InputChangedHandler`, map from input id to
setting key), so they are remembered. `settings.json` sits in the add-in folder:

| Key | Meaning | Default |
|---|---|---|
| `reuse` | Reuse earlier results (cache) | true |
| `savePath` | Page file (proposed when the dialog opens, or chosen with *Choose file...*) | Downloads, named after the design |
| `linkedGroupTest` | Whole groups test on linked designs too | false |
| `memoryRefreshGB` | Reopen a linked design's hidden copy after this growth (0 = off) | 4 |
| `features` | `{"<key>": false}` switches off one of the test speed-ups (§12); the old top-level `experiment...` keys are ignored | all on |
| `transactionsOff` | no longer read (§19) | |

`mode` values: `'both'` (Full analysis: items and groups), `'off'` (Quick estimate). The code also knows
`'items'` and `'groups'`.

## 5. A run: `generate()`

`generate(mode, thumbs, derived)`:

1. Checks there is an active parametric design.
2. Works out the page path (`savePath`; the temporary folder only when it has none or cannot be written).
3. **Whole-result cache**: if the document is not modified, a result generated before for the same saved
   version and options is loaded (`_cache_load('main_<flags>', file id, version)`) and written as the page right
   away. A Full analysis result also answers a Quick estimate. With linked designs and the linked group test
   off, the kind gets an `s` suffix; a result with the linked groups tested also answers it.
4. Creates the progress panel (`_ProgressPanel`, falling back to Fusion's progress dialog).
5. **Steps with weights**: reading (`n_tl * 0.6` with thumbnails, else `0.05`), item test (`n_tl * 1.7`), group
   test (`n_groups * 2.5`), linked designs (placeholder, then re-weighted by `plan()` as designs are found).
   The whole run's progress and `time_left()` (smoothed: `0.8 * old + 0.2 * new`) go to Fusion's progress dialog
   when it is used; the progress panel shows no overall bar, only the time left as the tooltip of its Cancel button.
6. `Collector(des, progress, thumbs)`, `expand_groups()`, then: `build_nodes`, `scan`, part picture, `scan_components`,
   `scan_parameters`, item test, group test, linked designs.
7. `finally`: marker back to where it was (end, or `marker0`), groups collapsed back, thumbnail state restored.
8. Cancelled → no page opened (a page written during the run is updated to where it stopped, marked
   "Cancelled"). Otherwise `col.result()` → cache (unless the design had to be recovered or linked designs failed)
   → `_write_page`.

`_write_page` adds the selection server's port and token to `meta.sel`, reads `page_template.html`, replaces
`/*__DATA__*/null` with the JSON (with `</` escaped so it cannot close the `<script>`), writes the file atomically
(`.tmp` then rename, so a page reloaded while it is overwritten is never half written; temporary folder as
fallback) and opens it in the browser (`open_browser=False` for the pages written during the run).

**The page so far (`snapshot` in `generate`).** With linked designs, the page is written over the same file once
the main design is done and after each linked design (`_collect_derived(snapshot=...)`), at most every
`SNAPSHOT_SECONDS` (10). It is built from a copy of the collected data (`_Snap`: the collector's lists and edges
copied, `Collector.add_edge` / `Collector.result` used on it) plus the linked designs read so far, merged with the
same `merge(target, srcs)` as at the end; `merge` changes neither the queue entries nor anything shared, so it can
run again after every design. The page gets a first warning "Still being generated (N of M linked designs done)".

The **order of tests** is items first, then groups: the item test's results (`item_proofs`) let the group test
stop its walks early.

## 6. Reading a design: `Collector`

### Timeline groups and indexes

`expand_groups()` opens every collapsed timeline group (up to 10 passes, nested groups), remembering them in
`self.collapsed` for `restore_groups()`. With every group open, `timeline.item(i)` enumerates all items
and indexes are stable. Everything in the add-in refers to timeline items by this index `i`; nodes are
`'n%d' % i`.

### `build_nodes()`

* Groups: `G<j>` for the j-th of `timeline.timelineGroups`, with `name`, `first` (smallest member index),
  `parent`. An item's group path `g` is outermost first; the innermost group wins (fewest members).
* One node per non-group item: `id`, `name`, `type` (the API object type), `cat` (from `CAT_BY_TYPE`; surface
  variants of solid features become `surface`), `tl` index, `o` (order), `tok` (entity token, for Select in
  Fusion), `g`, `supp`, `health`, `msg` (error/warning text), `info` (operation, new/linked component).
* `comp_owner`: component name → the item that brought it in (Occurrence, Derive feature).

### `scan()`: one forward walk

For each item `i`, with the marker just before it (`it.rollTo(True)`):

1. The previous item is now computed: `_diff_outputs(prev)` finds the faces it made or changed, `capture(prev)`
   takes its thumbnail, `record_outputs(prev)` records body and face owners.
2. `inputs_of(it)` reads the item's references and adds edges from their owners.

Reading references and outputs in the same walk means each marker position is computed once.

**References** (`inputs_of` → `resolve` → `_resolve`): every attribute in `FEATURE_INPUT_ATTRS` (inputs only,
never outputs such as `.faces` / `.bodies`), recursing through collections and definition objects
(`DEF_INPUT_ATTRS`) up to depth 6. What an object resolves to:

| Object | Link to | Kind |
|---|---|---|
| Sketch | its timeline item (or its component) | `sketch` |
| Profile / sketch entity | its parent sketch | `profile` / `sketch` |
| Construction plane/axis/point | its item | `plane` |
| BRepFace | the item that made the face (`face_owner[token]`), else its body | `geometry` |
| BRepEdge / vertex / loop | the faces around it | `geometry` |
| BRepBody | the item that **created** the body (`body`) plus the last one that changed it (`order`) | `body`, `order` |
| Occurrence / Component | the component node | `component` |
| Joint types | the joint item (or the component holding it) | `joint` |
| Mesh / T-spline body | its base/form feature | `body` |
| any `...Feature` | that feature | `feature` |
| anything inside an inserted/derived component (`external`) | that component | `component` |

`tl_node(obj)` checks that `obj.timelineObject.index` really points at an item of *this* timeline (same name):
objects inside inserted components have their own timelines.

**Outputs**: `_diff_outputs` compares, for each body of the item, the face signatures (area + centroid,
rounded) with the last time an item changed that body (`_body_sigs`). New signatures = faces this item made or
changed. `record_outputs` stores `face_owner[token] = item`, `body_creator` (first), `body_owner` (latest),
component and mesh owners.

**Performance notes** (all measured on real designs):

* Faces are read through the bodies, not the feature's face list: `feature.faces` costs ~2.5 ms per face
  (seconds for a gear); `body.faces` does not (355 s → 59 s on J1 Output Plate - Lower).
* Face signatures are kept only for the bodies each item changes, not every body after every item
  (81 s → 4 s on the Master assembly).
* `entityToken` costs ~3 ms per read. `_face_memo` / `_edge_memo` remember, per item, the token of each face
  (keyed by body name + `tempId`) and each edge's resolved faces; a sketch projecting a gear's curves hits the
  same faces hundreds of times. The memos are reset per item because ids are only valid until the next compute.
* Without thumbnails, the walk hides bodies, sketches and construction geometry (`_hide_display`) so Fusion
  builds no display meshes for every step. Not for a linked design the user has open (`no_roll`).

### `scan_components()`

A node `c:<k>` per component (not the root). Its parent is its `creator` (the Occurrence, New Component,
feature set to "new component", or Derive feature) via a `component` edge, else its parent component
(`incomp`). Items built in a component hang under it (`incomp`). Components inside inserted or derived parts are
not listed: the outermost inserted/derived occurrence stands for the whole part (`comp_ref`). Then
`capture_components()` takes one picture per component.

### `scan_parameters()`

User parameters become nodes `p:<name>`; derived parameters `d:<name>` hang under their Derive feature.
Model parameters are attributed to the feature that created them (`createdBy`). For every parameter, an edge
from the owner of each parameter it depends on (`dependencyParameters`) to its own owner (kind `param`).
User parameters are always kept; derived ones only when linked.

## 7. Thumbnails and pictures

`capture(it, nid)` → `_capture`: one picture per item, taken right after the item is computed during `scan()`.

* `_view_for(it)` decides the view: a sketch is seen straight on (its frame); construction geometry, occurrences,
  derive features and joints show the visible bodies (a construction plane includes a patch of itself);
  features are framed on the faces they made (`_new_faces`, from `_diff_outputs`), looking along the sketch
  normal if they have a sketch, else along the dominant planar normal, flipped to the side with less material
  in front.
* `_camera_for` builds an orthographic camera: up vector kept upright, image proportions follow the item
  (`TH_LONG` 360 px long side, aspect clamped to `TH_MAX_ASPECT`), margin `TH_MARGIN`.
* `_isolate(keep)` hides every other visible body (restored by `_show_bodies`); a hidden tool body the item
  uses is shown. Sketches and construction geometry are forced visible (`_force_visible`). New faces are
  selected (highlighted) when the item is shown with geometry it did not make (a cut, a fillet).
* `_shot` saves the viewport as JPEG (PNG fallback), read back as a data URI.
* `thumbs_end` restores camera, visibility, selection.

`_part_picture()` takes one isometric picture of the whole part (main design and every linked design), used on
the page's design frames.

## 8. Suppression tests

### What is recorded

For every item that is not suppressed in the design:

* `dsupp`: items Fusion suppresses together with it,
* `dbreak`: items that fail to compute (error) that did not before,
* `dwarn`: items with new warnings,
* `fail`: Fusion refused to suppress it even with the marker right after it; `_parse_fail` extracts the
  feature name.

**Refused suppressions.** `Design.setSuppressed` is all or none: it refuses (and rolls back) a suppression that
makes an already computed feature fail. The walk puts the marker at the first possible stop before suppressing,
so features between the item and that stop are computed and can make Fusion refuse. Then the suppression is tried
again with the marker right after the item (nothing after it computed, so nothing can fail), and the walk forward
computes the rest: marker moves are never refused, the failing features simply show errors and are recorded in
`dbreak`. That is exactly what suppressing the item by hand in Fusion shows (the UI does not refuse). The group
test does the same with the marker before the group. Counted in the run log ("refused at a later stop and
walked"). Only when even that is refused does the item or group get `fail`.

Groups get the same fields from the whole groups test. After the item test, `suppress` edges are added from
each item to the items it takes down, keeping only the direct ones (transitive reduction over `desc`).

Both tests hide bodies, sketches, construction geometry and joints of every component first
(`_hide_display`): each recompute otherwise also builds display meshes, and Fusion keeps them. Hidden geometry is
still computed, so results are the same.

### Basic operations

* `_set_suppressed(entities, value)`: `Design.setSuppressed(list, value)` when the Fusion build has it (one
  recompute for many items, all or none), else per item `isSuppressed`. Counts calls and time; then `_breathe`
  gives Fusion `UI_PAUSE` seconds for clicks and redraws (a recompute itself cannot be interrupted).
* `_set_test_marker(m)`: marker so that items `[0, m)` are computed; `moveToEnd` for `m >= count`. Pauses
  `MARKER_PAUSE` so the marker is seen moving.
* `_state(idxs)`: suppressed flag and health of the given items, read once each.
* `_clean(orig, err0)`: every item has its original suppression state and nothing new has an error.

### The item test (`_suppression_test`)

The naive test suppresses item `i` with the marker at the end, reads every item, switches `i` back on: two full
recomputes per item. The actual algorithm computes much less and gives the same result:

**1. Test from the back.** Items are tested last to first. When item `i` is tested, every later item already has
its complete result `known[j]` (what suppressing `j` takes down). These are the "proofs".

**2. Timeline walk (`TIMELINE_WALK`).** Instead of suppressing at the end, the marker is put *before* the part
still to compute, the item is suppressed (costs nothing when the marker is right after it), and the marker then
moves forward in steps. After each step the newly computed items are read:

* an item switched off goes into `casc`, and its own result `known[j]` goes into `covered`: everything it takes
  down is off too;
* an item with a new error goes to `broke`, a new warning to `warned`.

**3. Stopping early with proofs.** When every remaining active item after the marker is in `covered`, the rest is
proven off without computing it (`stopped_early`, `items_not_computed`). If an item that should be off by a
proof turns out on, proofs are not used for the rest of that test (`proof_mismatch`), which then runs to the end.

**4. Where the marker goes (`_next_marker`).** Not a fixed step: the next position where stopping is still
possible, using every known tail. For a candidate stop after position `p`, all active items from `p` on must be
coverable by `covered` or by items before `p` whose tests take them down. Positions where that cannot hold even
in the best case are skipped, so Fusion computes each stretch in one go. `_jump_past_unprovable`: an item that is
in no test's result can never be covered, so the marker goes straight past the last such item. The first marker
position of a test is also chosen this way (not "right after the item").

**5. Upper bounds from blocks (`UPPER_BOUNDS`, `BLOCK_SIZE = 4`).** Neighbouring items (in back-to-front order)
are grouped into blocks. Suppressing less cannot take more down, so whatever a single item makes react lies
within what its whole block makes react. A block is tested first (`probe(block)`), and its reaction becomes
`bound` for each of its items: items outside it are `free`, proven unaffected, and never computed. That is what
saves time on heavy features that do *not* react (proofs only help with items that go off). Blocks are only
tested when useful: `_heavy` (items measured slower than `HEAVY_SECONDS = 2` s, from `_note_cost`) must lie
after the block and outside its static reach (`_static_reach`, from the reference scan); after three blocks that
kept nothing heavy out, no more are tried. If an item outside the bound reacts after all, the bound is dropped
for that test (`bound_mismatch`).

**6. Putting back (`put_back`).** The marker goes back right after the first suppressed item, the item is
switched on, the marker moves to the end. The design is then the original one again and Fusion reuses the
result it already has (measured ~0.1 s instead of ~11 s for a full recompute). If the test ran to the end, the item
is switched back on right there. Then `_clean` is checked. During a test it is strict (`_begin_checks`): the
same suppression, no new error, **no new warning, and every body with the volume it had before**
(`body_signature`, time in the log as "body volume checks"). A feature that lost a reference after being switched
back on often only warns and keeps its last good geometry, or computes a slightly different body; the earlier
check (suppression and errors only) let that through and every later test ran on a changed design.

If the suppression flags differ, `_restore_checked` runs (`_restore`: switch on everything that should be on,
groups included, then switch off what was off; again; then item by item). If the flags are right but the design is
not clean, switching on again cannot help, so it goes straight to **`_repair`**:

1. Undo until clean (`_undo_back(until_clean=True)`, up to `UNDO_REPAIR_STEPS` = 30): Undo brings back the model
   with its references. Every state with the original suppression that is clean is the original design, even one
   from before an earlier test, so undoing further than this test is harmless.
2. For the hidden copy of a linked design: reopen the saved version (`_reopen_hidden`, shared with the memory
   refresh). If even the reopened copy is not clean (the saved version computes differently from when the tests
   started: a feature that is not stable), what it shows becomes the new baseline (`_rebaseline`) with a warning.
3. Otherwise a warning ("Could not put the design back after testing X"), `recovered += 1`.

The main design is never closed and reopened while it is being tested (§19); for it, Undo is the last step. The
item test log line counts repairs with Undo, reopens and designs not put back.

**7. After each put-back:** `_memory_refresh` (§11).

**8. Summary:** stats in the run log (`tests`, `stopped_early`, `items_not_computed`, `proof_mismatch`,
jumps, `n_cert_jumps`, `blocks`, `bound_mismatch`), the time split (first marker move, suppress, walk forward,
put back) and the five slowest items (`_top_costs`). `body_signature()` (body names + volumes) before and after
catches a design that was not put back exactly.

**Why testing from the back and not the front:** each test starts from the original design, so its own cost does
not depend on the order; the order only decides which earlier results can shortcut it. Back to front gives proofs
(what goes off; stops walks). Front to back would instead give upper bounds for the items an earlier test took
down (what can react; skips unaffected heavy features), but no proofs. The blocks give upper bounds within the
back-to-front order.

### The group test (`_group_suppression_test`)

Groups are suppressed as a whole (`_set_suppressed([group])`). Groups that are empty, suppressed, or whose items
are all suppressed are skipped. The marker starts at the first possible stop from the item test's proofs
(`item_proofs`), then `_walk_forward` walks like the item test (proofs from the item test). If Fusion refuses to
suppress the group (a later feature fails), its items are suppressed one by one, as the UI does; if that is also
refused, the group gets `fail`. Put back: marker after the group's first item, group on, marker to the end,
checked.

## 9. Linked designs

With *Include linked designs*, `_collect_derived(main, ...)` also reads every design brought in by a Derive
feature or inserted as a linked component, and the designs those link, at any depth (up to `max_designs = 80`).

**Queue.** `note_links(col)` lists, for an open design, its Derive features (`_derive_features`, with what each
hands over: timeline object names, body names, component names, derived parameter names) and its outermost
linked occurrences (`_linked_occurrences`). Each linked file **and saved version** gets one queue entry, keyed
`<cloud file id>@v<version>` (`dataFile.id`, a `urn:`), never by name: names repeat across folders and projects. A
design linked at two versions is read and tested at both and shown as two frames (they can differ); `e['fid']` is
the file id alone (opening, file check, cache). Each gets a prefix `x<k>:` for its node ids and a group `X<k>` that
becomes its frame on the page.

**One design at a time.** `process(e)`:

1. Cached (same file, same saved version, at least the requested tests) → `_CachedDesign`, links replayed from
   the cache; nothing is opened.
2. Otherwise `open_entry`: opens exactly the version the link uses, in its own visible tab, and activates it so
   you see what is worked on. It verifies that Fusion opened that file id and version; Fusion can hand back a
   different file (e.g. a same-named design elsewhere), which is then left out with a warning.
   **Configurations**: a row of a configured design is a file of its own in Fusion's hidden "System Project -
   CONFIG", but opening it opens the configured design itself (a different file id). `config_row` then finds
   the row (by the file's `configurationRowId` where this Fusion has it, else by name) and activates it; the design
   is read and tested in that configuration and closed without saving. `_memory_refresh` activates the row again
   after reopening (`config_row` on the collector).
3. `mine = False` when Fusion handed back a document the user already has open: that one is read as it is (not
   rolled, groups not expanded) and never tested.
4. A non-parametric design (no timeline, e.g. a library part) → `_PlainDesign`: only its frame, connector and
   picture.
5. Otherwise a `Collector` (`doc = None`, `hidden_doc = doc` for the tests): read, picture, components,
   parameters; its own links are noted while its groups are expanded; with Full analysis the item test (and
   the group test with *Group test for linked designs*).
6. Cached if complete (the tests ran on a hidden copy and neither failed nor was cancelled).
7. `finally`: groups restored, document closed without saving, every Fusion object dropped (`release`), `gc`,
   memory logged. Only one linked design is open at a time.

**Merging into the main graph**, after all are read:

* What a Derive hands over is matched by name in the source design (timeline name → `by_tlname`, body →
  `body_owner`, component → `comp_owner`, parameter → `_source_param`, which also matches renamed derived
  parameters like `Width_Ref` → `Width`).
* Deepest designs first; each gets a group (frame) with `design: True`, its picture and `via` (derive/insert).
  Node ids and group references are prefixed; `o` is shifted (`-1e6 + rank * 1e4`) so linked designs come before
  the main design; `tl` is set to `None` (no Select in Fusion: not in this design's timeline), the original
  index kept as `stl`; the page previews suppressions of any item with `tl` or `stl` (`inTl`). A `fail.node`
  reference gets the design's prefix too.
  User parameters of a linked design only when something uses them.
* Each design gets a **connector** node `x<k>:@` (`type: DerivedDesign`, `port: True`): the items handed over lead
  into it, and it leads into the Derive feature or insert item(s) that use it (`derive` edges).
* Designs with the same name in different files (folders) get their folder in the frame name; one file at two
  versions does not (the names differ by version).
* Groups whose test was skipped get `gskip: True`.

## 10. Result cache

A saved version never changes, so its results stay valid. `_cache_save(kind, file_id, version, data)` writes JSON
files `<kind>_<file id>_v<version>.json` into the user's application data folder
(`~/Library/Application Support/FusionDependenciesGraph/cache` on macOS, `%APPDATA%\FusionDependenciesGraph\cache`
on Windows; temporary folder as fallback). The temporary folder is not used because macOS clears files there it
has not touched for a few days; the add-in folder is not used because it is replaced when a new version is
copied in.

* Kinds: `design` (one linked design: its plain data, links, picture, which tests ran) and `main_<exact><groups>
  <thumbs><derived>[s]` (the whole page data of a main design).
* A more complete result is never replaced by a lesser one (`exact`, `groups`, `pics`).
* `CACHE_VERSION` (now 5) invalidates everything older. Version 4 dropped results tested with Fusion's
  transactions switched off (§19); version 5 results saved although a design was not put back or changed.
* Nothing is saved for a linked design that was not put back after a test (`recovered`), whose bodies changed
  (`bodies_changed`) or whose reopened copy computed differently (`baseline_reset`), nor for one whose test failed
  part-way or was cancelled. Nothing is saved for the whole page when the main design has any of these, when linked
  designs failed, or when any linked design was unclean (`linked_unclean`).
* `reuse: false` (dialog: *Reuse earlier results* unticked) ignores the cache when loading; results are still
  saved.
* Every decision is written to `run_log.txt` (`cache <kind> v<version> (<file id>): ...`): none saved, saved by an
  older add-in version, not enough for this run (which test is missing), saved, not saved and why (
  a more complete result already saved, the design had to be put back the slow way, linked designs failed, write
  error). 

## 11. Memory

**What grows.** Fusion records every suppress, switch-on and marker move as an undo step and keeps the model
data of each step as long as the document is open. Measured on J1 Output Plate - Lower: ~110 MB per
suppress/restore, 7 GB → 29 GB over 200 calls; closing the document gave most of it back within seconds. The
Python side holds only plain data (strings, dicts), tens of MB at most.

**What the add-in does about it:**

* The main design is closed and reopened at the end of every run (`_revert`), which drops its history.
* Linked designs are opened one at a time and closed right after (`release()` drops every Fusion object so the
  document can be freed).
* `_memory_refresh(orig, err0)`: between two tests of a linked design (the design is then in its original state),
  when Fusion's memory has grown by `memoryRefreshGB` (4 GB) since the lowest reading of this design's tests,
  the hidden copy is closed and the same saved version opened again. The code checks it is the same file and
  version, expands the groups, hides the display, moves the marker to the end, and checks that the design is in
  the tested state (`_clean`). Then `des`/`tl` are new objects: the item test updates its local `tl` (`nonlocal`),
  and the group test re-reads its groups. Never for the main design or a design the user has open.
* Display hidden during tests and during the read walk without thumbnails.

**Measuring.** `_process_memory()` reads the process's **physical footprint** on macOS (`proc_pid_rusage`,
which is what Activity Monitor shows; the resident set leaves out compressed and swapped memory, most of it once
Fusion is large) and `PagefileUsage` (private bytes) on Windows. `_mem_log(msg)` appends a line with the time and
memory to `run_log.txt`. `_mem_tick` writes one at most every 30 s during reading and the tests.

## 12. Test speed-ups (former experiments)

Four speed-ups that started as experiment checkboxes. They are now always on, with no toggle in the dialog;
`_feature(key)` reads `settings.json` `{"features": {"<key>": false}}` to switch one off (the old top-level keys the
checkboxes wrote are ignored, so a stale `false` cannot switch one off unseen). Their results are cached like any
other. They have not been validated against a run without them on a large design: if results look wrong, switch
them off one at a time and compare with `tools/compare_pages.py`.

| Setting | What it does | Where |
|---|---|---|
| `experimentNoCrashRecovery` | `Options.CrashRecovery /off` during each test, `/on` after | `EXPERIMENTS`, `_experiments_begin/_end` |
| `experimentNoBodyCache` | `DebugCommands.BodyCacheUpdateMgr /Off` (background mass properties) during each test | same |
| `experimentDeferCompute` | `Design.isComputeDeferred` on around marker move + suppress, and around marker back + switch on + marker to end: one compute per group of steps | `_defer_begin/_end` in `probe` and `put_back_now` |
| `experimentUndoPutBack` | After a test that took ≥ `UNDO_PUT_BACK_MIN` (3 s), put the design back with Fusion's Undo instead of switching the item back on, which would recompute the heavy features after it | `_undo_back` in `put_back` (item test) and the group test |

The two text-command experiments check the current state first and leave a setting alone that Fusion already
reports as off. They record what they switched in `experiments_on.json`, so the add-in can switch it back at the
next start or stop if a run was interrupted.

`_undo_back` is a generator: it executes `UndoCommand` one step at a time, yields until Fusion has run it
(the snapshot of marker position and suppression flags changes), and stops as soon as every item has its original
suppression state (never undoing further, which would undo earlier put-backs). Then marker to the end and
`_clean`. If Undo is unavailable, does nothing, needs more than `UNDO_PUT_BACK_STEPS` (12) steps or does not bring
back the original state, the usual put-back runs from wherever Undo left the design. Undo only works on the active
document tab, so the design's tab is activated first. Whether it saves time depends on whether Fusion's undo record
includes the results computed during the walk forward; the run log shows how many put-backs used Undo and their time.

## 13. Progress panel

`_ProgressPanel` is a Fusion palette showing `progress_panel.html` (loaded from the add-in folder), docked right.
It offers the same interface as Fusion's progress dialog (`message`, `progressValue`, `wasCancelled`, `hide`),
so `generate()` can fall back to `ui.createProgressDialog()`.

* One row per design (`row(key, name, status, frac, state, labels, idx)`): its steps as a segmented bar (Reading /
  Item test / Group test), status text and state (`''` working, `wait`, `done`, `fail`). `_prow()` is the no-op-safe
  wrapper.
* Updates are batched and sent at most every 0.15 s (`sendInfoToHTML('rows'|'all'|'end', json)`): sending is not
  free.
* The page sorts rows: in progress, waiting, finished. Cancel sends `cancel` → `_PanelCancelHandler` sets
  `wasCancelled`; the run checks it through `cancelled()` and stops the whole run: the current test and every step
  after it.

## 14. Select in Fusion (local HTTP server)

The page is a local file in the browser; it asks the add-in to select items in Fusion over HTTP on 127.0.0.1.

* `_sel_start()` starts a `ThreadingHTTPServer` on port 47391 (fixed, so older pages keep working after a
  restart; any free port as fallback) in a daemon thread.
* Every page carries the port and a secret token (`meta.sel`). The token (`_sel_token`) is stored in the temporary
  folder and stays the same between sessions. Requests without it get 403.
* `GET /ping?token=...` checks the connection. `POST /select {token, doc, items[{tl, name, tok} | {occ}], add}`
  queues a job and fires `SEL_EVENT_ID`; the HTTP thread waits up to 15 s for the answer.
* `_do_select` runs on Fusion's main thread: checks the active document is the page's design (name without
  version), finds items by timeline index + name (`_flat_timeline` walks groups without expanding them), else by
  entity token, components by occurrence path, and adds them to the active selection. CORS and
  `Access-Control-Allow-Private-Network` headers let a `file://` page call it.

## 15. The generated page

`page_template.html`: CSS (light/dark), markup, one script (in a closure) with `const D = /*__DATA__*/null;`.
Main parts, in file order:

* **Setup**: categories, icons (one SVG sprite, `ICON_BY_TYPE`), `byId`, synthetic nodes (a common *User
  Parameters* parent), thumbnails `TH`.
* **Suppression simulator** (`simCompute`): the preview of suppressing items and groups without touching Fusion.
  An item uses its recorded `dsupp`/`dbreak`/`dwarn`; a group uses its own test result, or with none (not tested,
  `gskip`) adds up its items' results and marks them *estimated*; untested items follow the links downstream
  (estimated). Items/groups Fusion refused (`fail`) are forced: the named feature is shown broken, what depends on
  it may fail (estimated). Items of linked designs can be previewed like the design's own (`inTl`: `tl` or `stl`);
  whole linked designs (frames) cannot.
* **The selection's whole tree is shown**: while an item, several items or a group is selected, every timeline
  group holding part of its tree (what the Display options highlight around it) is open (`selOpen`, used by
  `isOpen` next to `expanded` and `searchOpen`), so the tree shows as its real boxes and links instead of folded
  boxes. Linked designs keep the state they had (a folded one stays one box in the tree); only the design holding
  a selected item opens, so that item can be shown. Folding one by hand during that selection keeps it folded (`selShut`, forgotten when the
  selection changes); deselecting folds everything back as it was.
* **A linked design's loose items**: its items outside its timeline groups, and its user parameters, get a group
  of their own inside its frame when the page loads (`X8~none` "Not in a group", `X8~params` "User parameters",
  `loose: true`), like this design's pseudo groups `_none` / `_params`: they fold into one box with the block's fold
  button (a block of loose items had only the boxes' own fold buttons, which fold what depends on each box). Not
  suppressible as a group in the preview (not a timeline group).
* **Whole part behind a connector**: when the page loads, an insert's connector gets links (`derive`, `syn: 1`,
  not counted in the header) from its design's final items - those no other item of that design builds on - so
  selecting the insert (or anything using it) opens and highlights every item and link that made the part. A
  Derive keeps its handed-over items as the connector's parents; only when none could be matched (no link into the
  connector from its design) does it get the final items like an insert. User parameters are left out.
* **Hidden connectors**: an expanded linked design's connector box is hidden and its fold button stands in for it.
  The box gets `pt` (the button's centre, relative to the box), and `edgeSegs`/`edgeEnd` treat it as a point:
  links end on the top of the button and leave from its bottom, without spreading their ends.
* **Group level**, **back/forward history** of selections and previews (restoring zoom and position).
* **Select in Fusion** client.
* **Graph** (`renderGraph`, the largest part): layouts *Depth* (rows by dependency depth), *Groups* (`lanes`: one
  block per top-level timeline group), *Components* (`comps`), *Timeline* (one row, longer links arc above).
  Linked designs are frames with their own blocks and a connector. Folding (groups, collapsed boxes that hide
  their downstream; links into folded boxes are joined through and show counts), hidden kinds (links joined through
  as dotted lines), selection highlighting (what it depends on, blue; what depends on it, green) and pulling
  related boxes together, hover (gold), routes between two items, link routing that bends links to avoid lying on
  top of each other and orders link ends on boxes to reduce crossings (cached while boxes stay in place), and
  animated re-rendering (`animatedRerender`).
* **History playback**: dots travel along links to each next item in timeline order; the camera follows.
* **Legend**.

## 16. Data format

```js
D = {
  meta: {doc, exact, gtest, pic, warnings[], date, generationSeconds, sel: {port, token}},
  nodes: [{
    id,            // 'n<i>' item, 'c:<k>' component, 'p:<name>' user parameter, 'd:<name>' derived parameter,
                   // 'x<k>:...' inside linked design k, 'x<k>:@' its connector
    name, type, cat, tl, o, tok, g[], supp, health, msg, info,
    local?,        // Occurrence: made in this design (New Component) vs linked
    occ?,          // component: occurrence path (Select in Fusion)
    dsupp?[], dbreak?[], dwarn?[], fail?{node, name, msg},   // item test
    dsg?, stl?, port?                                        // linked designs
  }],
  groups: [{id, name, first, parent, dsupp?, dbreak?, dwarn?, fail?, empty?,
            design?, pic?, via?[], gskip?}],
  edges: [{s, t, k: [kinds]}],   // kinds: sketch profile plane geometry body order feature component incomp
                                 // joint param derive suppress
  thumbs: {nodeId: 'data:image/jpeg;base64,...'}
}
```

An item that has `dsupp` (even empty) was covered by the item test (`itemTested` on the page).

## 17. Logs and diagnostics

All in the temporary folder, subfolder `FusionDependenciesGraph` (`open "$TMPDIR/FusionDependenciesGraph"` on
macOS, `%TEMP%\FusionDependenciesGraph` on Windows):

| File | Content |
|---|---|
| `run_log.txt` | Overwritten each run: memory at each stage and every 30 s, read times, item test statistics and time split, slowest items, memory refreshes, experiments, the end of the run. |
| `linked_designs_log.txt` | Linked designs opened / from cache / closed, with the number of open documents. |
| `text_commands.txt` | Fusion's full text command list (`TextCommands.List /hidden`), written once per Fusion session. |
| `_thumb.jpg`, `_part.png` | Scratch files for pictures. |
| `.select_token` | Selection server secret. |
| `experiments_on.json`, `transactions_off.flag` | Present only while something must be switched back on. |

## 18. Tools

* `tools/compare_pages.py first.html second.html`: extracts `D` from two pages and prints, per item and group,
  differences in `dsupp`, `dbreak`, `dwarn`, `fail`, `health`, `supp` (and `empty` for groups), then differing
  links and warnings. Exit code 1 when anything differs. Used to validate any change that could affect results.
  Node ids of linked designs depend on the order they were found, so for runs with a different set of linked
  designs, compare by design name + item type + name instead.
* `tools/record_demo.py` (macOS): drives the mouse through Fusion and Safari (Apple Events) to record the demo
  video, with spoken narration. Fusion: the Manage tab button, the dialog (Full analysis / Quick estimate,
  Thumbnails, Advanced options: Include linked designs, Group test for linked designs, Reuse earlier results) and
  the progress panel, at fixed screen points measured from a screenshot of the Mac mini (`FUSION_POINTS`; the
  dialog docked on the right, Advanced options open); `--calibrate` re-measures them elsewhere
  (`tools/demo_positions.json`). "Include linked designs" is ticked when the checkbox's pixels show it is not (a small
  screenshot, read as BMP after `sips`). The dialog step clicks *Choose file...* and saves in Downloads (Cmd+Shift+G `~/Downloads`, Return, Return;
  a "Replace" button, found through System Events, is clicked when the file is already there). The Safari tour
  never fits the whole assembly once a linked design is open (too heavy for the video): `view_design` uses Fit only
  with a selection (it fits the selection), else the page's `dgZoomToDesign` hook zooms to the tour design's frame
  (its width, from its top); `dgFocus` brings a box that is too small or off screen into view. `dgDesignsOnly(keep)` sets the folds in one render (every group open, only that linked design unfolded): doing it with Expand all and one click per design re-rendered the whole assembly each time (33 s in Chromium, and Safari stalled). The finished page is recognised by the first Safari window that opens
  during the run (Safari's windows must be closed before it; the script warns and waits up to 30 s for that; the
  pages saved while the run goes on are not opened). `--skip-generation` (and `--step` with no page open) open the
  newest page, including the one the add-in records in `last_page.json` (next to the cache folder). Safari (`--list` numbers the steps, `--step N` starts at one): getting around,
  linked designs (frames, pictures, unfolding one), layouts, hover, selection and side panel, Display options,
  routes, multi-selection, all links and folding, search, filters, the suppression preview (including a feature
  whose suppression makes others fail or warn, and the broken / warnings buttons), Select in Fusion, legend,
  playback. Set up for the robot arm's Master assembly: the tour works inside one linked design
  (`LINKED_DESIGN`); boxes are found by their full name (`data-name`) and design (`data-id` prefix, frame
  `data-d`), which the page puts on every box and frame for this.

## 19. Things that were tried and removed

* **Fusion's transaction system off (`Options.Transactions /off`)** during the tests, to stop the undo history
  from growing. The run became ~17× faster, but a comparison showed every test found nothing: 0 suppressed / broken /
  warned items against 7586 / 184 / 645. With transactions off, suppressing an item takes nothing down with it (or
  the states read back never update). Removed; `_tx_on` remains to switch it back on after an interrupted run of that
  version; `CACHE_VERSION` 4 discards its cached results. **Lesson: validate every hidden switch with
  `compare_pages.py`.**
* **Memory guard reopening the main design** (every 3 GB): reverted ("Never close and reopen a design while it is
  being worked on"). The current `_memory_refresh` only reopens hidden copies of linked designs.
* **Undo restore with a fixed number of steps** (`_undo_restore`, `UNDO_RESTORE = False`): replaced by the marker
  put-back (no waiting for Fusion). Its step count stopped matching once the timeline walk moved the marker; the
  Undo put-back (§12) undoes step by step instead.
* **Running the work inside the command's preview**: the dialog's docstring still mentions it, but the run
  happens after the dialog closes (custom event).
* **Tail certificates** (`_record_frontier_certificate`, `_frontier_for`, `SUPPRESSION_FRONTIER_ENABLED`):
  superseded by the proof-based walk (`_next_marker`).

## 20. Unused code and known issues

Unused (candidates for removal): `_frontier_items`, `_record_frontier_certificate`, `_frontier_for`,
`_static_dependency_profile`, `_leaf_candidates`, `_undo_restore` (and `_undo_n` bookkeeping, `UNDO_RESTORE`,
`UNDO_MAX_STEPS`), `_open_version`, `batch_hits` (always 0), `_measure` / `_work` (diagnostic stubs),
`_probe_text_commands` (still runs; its purpose, finding memory commands, is done).

Known issues:

* `_panel` is both the progress panel global (`_panel = None`, set to a `_ProgressPanel` in `generate`) and the
  toolbar panel function (`def _panel(create=False)`), which is defined later and so replaces the global at import.
  After a run the name refers to the progress panel instance, so `_cleanup_ui` can no longer find the toolbar
  panel (`_safe(_panel)` fails silently) and `stop()` may leave it in place. Rename one of them.
* `PANEL_ID` is defined twice (`'claudeDesignGraphProgress'` for the palette, `'claudeDesignGraphPanel'` for the
  toolbar panel); the second wins, so the palette uses the toolbar panel's id. They live in different collections, so
  it works, but it is confusing.
* Linked-design node ids (`x<k>:`) depend on the order designs are found, so pages from runs with different linked
  designs cannot be compared by id.
* Configurations are matched to their row by `configurationRowId` or by name; a configuration whose row name
  differs from its file name (and no row id available) is still left out with a warning. The row list is in
  `linked_designs_log.txt`.

## 21. Working on the code

* Every Fusion call that can fail goes through `_safe(lambda: ...)`; Fusion getters raise `RuntimeError`, not
  `AttributeError` (hence `_has` instead of `hasattr`).
* Refer to timeline items by index, with every group expanded. Re-read `self.tl` / `self.des` after anything that can
  reopen a document (`_memory_refresh`).
* Anything that changes the design must be undone in a `finally`; the add-in only runs on a saved design and reverts it
  at the end, which is the last safety net.
* Anything that changes a Fusion-wide setting must also record how to undo it on disk, so an interrupted run can be
  cleaned up at the next start.
* Measure before optimising: `run_log.txt` has the timings; add a line there for anything new.
* Validate result-affecting changes: run Full analysis before and after with *Reuse earlier results* unticked and
  compare with `tools/compare_pages.py`. Algorithm changes can also be checked without Fusion against a fake timeline
  (a stub `adsk` module and a timeline where suppressing an item suppresses its dependents), as was done for the Undo
  put-back.
* The page template is plain HTML/JS; open a generated page's source to test changes, or replace
  `/*__DATA__*/null` in a copy of the template with the JSON from an existing page.
