# Fusion Dependencies Analysis

**Dependencies Graph** is an Autodesk Fusion add-in that exports the timeline of a parametric design
as an interactive HTML page: a dependency tree and graph of every timeline item, with thumbnails,
timeline groups and suppression tests.

## Features

- **Graph and tree views** of what each feature depends on and what depends on it
  (sketches, profiles, planes, faces/edges, bodies, features, parameters, components).
- **Thumbnails** of the model after each timeline step.
- **Timeline groups**: show single items or whole groups, a Depth layout or a Groups layout.
- **Suppression tests** (optional, run in Fusion): the *Whole groups* test and the *Every item* test
  record what really gets suppressed or breaks when a group or item is switched off, and the page
  can preview suppressing things without touching the design.
- **History playback**: select an item and press **▶ Play** (or `P`) to animate how it was built
  from its dependencies, step by step in timeline order. `Space` pauses, `→` skips to the next step,
  `Esc` stops; speed 1× / 2× / 4×.

## Install

1. Copy the `DependenciesGraph` folder into Fusion's add-ins folder:
   - macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   - Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
2. In Fusion open **Utilities → Add-Ins → Scripts and Add-Ins**, select *DependenciesGraph* and run it
   (it is set to run on startup).
3. The **Dependencies Graph** command appears in the Design workspace (Manage tab).

## Use

Open a parametric design and run **Dependencies Graph**. Choose whether to run the suppression tests
and capture thumbnails. The design is restored afterwards (the tests suppress and unsuppress items,
so save your work first). The result opens in your browser as a self-contained HTML file.

## Layout

```
DependenciesGraph/
  DependenciesGraph.py        add-in: data collection in Fusion + the HTML/JS page template
  DependenciesGraph.manifest
  resources/DependenciesGraph/ icons
```
