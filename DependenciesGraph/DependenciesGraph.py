"""Dependencies Graph add-in for Autodesk Fusion.

Adds a "Dependencies Graph" button to Design > Manage > Dependencies Graph.

Reads the active design's timeline, works out which timeline items depend on
which, and writes a self-contained HTML file with an expandable tree and a
zoomable dependency graph.

Two ways to collect dependencies:
  * Quick scan (default, read-only): follows the references Fusion exposes
    through the API - sketch planes and projected geometry, profiles, extent
    and start entities, construction geometry definitions, faces/edges used by
    chamfers, fillets, offsets and similar, bodies used by combines/holes/cuts,
    and parameter expressions.
  * Suppression test (optional, slow): suppresses every timeline item one at a
    time and records what Fusion suppresses with it - the same cascade you see
    when you suppress things by hand. It temporarily changes the design and
    restores it afterwards; save a version before using it.
"""
import adsk.core, adsk.fusion, traceback, json, os, re, time, webbrowser, datetime, tempfile, pathlib, base64

_app = None
_ui = None

# ---------------------------------------------------------------- helpers ---

def _t(obj):
    try:
        return obj.objectType.split('::')[-1]
    except Exception:
        return ''

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _has(obj, name):
    # hasattr() only swallows AttributeError; Fusion getters can raise RuntimeError
    # (e.g. 'InternalValidationError : dataRef' on an unset FromEntityStartDefinition.entity).
    try:
        getattr(obj, name)
        return True
    except AttributeError:
        return False
    except Exception:
        return False

CAT_BY_TYPE = {
    'Sketch': 'sketch',
    'ConstructionPlane': 'construct', 'ConstructionAxis': 'construct', 'ConstructionPoint': 'construct',
    'ExtrudeFeature': 'solid', 'RevolveFeature': 'solid', 'SweepFeature': 'solid', 'LoftFeature': 'solid',
    'RibFeature': 'solid', 'WebFeature': 'solid', 'BoxFeature': 'solid', 'CylinderFeature': 'solid',
    'SphereFeature': 'solid', 'TorusFeature': 'solid', 'CoilFeature': 'solid', 'PipeFeature': 'solid',
    'EmbossFeature': 'solid', 'BoundaryFillFeature': 'solid', 'PatchFeature': 'surface', 'ThickenFeature': 'solid',
    'ChamferFeature': 'finish', 'FilletFeature': 'finish', 'RuleFilletFeature': 'finish', 'FullRoundFilletFeature': 'finish',
    'OffsetFacesFeature': 'offset', 'OffsetFeature': 'surface', 'ShellFeature': 'offset', 'DraftFeature': 'offset',
    'DeleteFaceFeature': 'offset', 'ReplaceFaceFeature': 'offset', 'SplitFaceFeature': 'offset', 'ScaleFeature': 'body',
    'HoleFeature': 'hole', 'ThreadFeature': 'hole',
    'CombineFeature': 'body', 'MirrorFeature': 'body', 'MoveFeature': 'body', 'SplitBodyFeature': 'body',
    'RectangularPatternFeature': 'body', 'CircularPatternFeature': 'body', 'PathPatternFeature': 'body',
    'CopyPasteBody': 'body', 'CutPasteBody': 'body', 'RemoveFeature': 'body', 'SilhouetteSplitFeature': 'body', 'BossFeature': 'solid',
    # surfaces
    'StitchFeature': 'surface', 'UnstitchFeature': 'surface', 'TrimFeature': 'surface', 'UntrimFeature': 'surface',
    'ExtendFeature': 'surface', 'RuledSurfaceFeature': 'surface', 'ReverseNormalFeature': 'surface',
    'SurfaceDeleteFaceFeature': 'surface',
    # sheet metal
    'FlangeFeature': 'sheet', 'HemFeature': 'sheet', 'RipFeature': 'sheet', 'CornerClosureFeature': 'sheet',
    'FoldFeature': 'sheet', 'UnfoldFeature': 'sheet', 'RefoldFeature': 'sheet', 'JoinByBendFeature': 'sheet',
    'LoftedFlangeFeature': 'sheet', 'SheetMetalChamferFeature': 'sheet', 'SheetMetalFilletFeature': 'sheet',
    'FlatPattern': 'sheet',
    # form (T-spline) and direct-edit bodies
    'FormFeature': 'form', 'BaseFeature': 'form',
    # mesh and volumetric
    'MeshCombineFaceGroupsFeature': 'mesh', 'MeshCombineFeature': 'mesh', 'MeshConvertFeature': 'mesh',
    'MeshGenerateFaceGroupsFeature': 'mesh', 'MeshPlaneCutFeature': 'mesh', 'MeshReduceFeature': 'mesh',
    'MeshRemeshFeature': 'mesh', 'MeshRemoveFeature': 'mesh', 'MeshRepairFeature': 'mesh',
    'MeshReverseNormalFeature': 'mesh', 'MeshSeparateFeature': 'mesh', 'MeshShellFeature': 'mesh',
    'MeshSmoothFeature': 'mesh', 'MeshFeature': 'mesh', 'TessellateFeature': 'mesh',
    'VolumetricModelFeature': 'mesh', 'VolumetricModelToMeshFeature': 'mesh', 'VolumetricCustomFeature': 'mesh',
    # items that bring components in
    'Occurrence': 'insert', 'DeriveFeature': 'insert',
    # joints, motion and positions
    'Joint': 'joint', 'AsBuiltJoint': 'joint', 'JointOrigin': 'joint', 'RigidGroup': 'joint', 'MotionLink': 'joint',
    'ContactSet': 'joint', 'ArrangeFeature': 'joint', 'Snapshot': 'joint',
    # reference images, decals and add-in features
    'Canvas': 'other', 'Decal': 'other', 'CustomFeature': 'other',
}
# these make a surface instead of a solid when their isSolid is off
SURFACE_WHEN_NOT_SOLID = ('ExtrudeFeature', 'RevolveFeature', 'SweepFeature', 'LoftFeature', 'PipeFeature', 'CoilFeature')
JOINT_TYPES = ('Joint', 'AsBuiltJoint', 'JointOrigin', 'RigidGroup', 'MotionLink', 'ContactSet')

OP_NAMES = {0: 'join', 1: 'cut', 2: 'intersect', 3: 'new body', 4: 'new component'}

# attributes on features/definitions that hold *inputs* (never outputs such as .faces/.bodies)
FEATURE_INPUT_ATTRS = [
    'profile', 'profiles', 'loftSections', 'centerLineOrRails', 'path', 'guideRail', 'axis',
    'startExtent', 'extentOne', 'extentTwo', 'extentDefinition', 'holePositionDefinition',
    'targetBody', 'toolBodies', 'participantBodies', 'inputEntities', 'inputFaces', 'edgeSets',
    'mirrorPlane', 'splitBodies', 'splittingTool', 'facesToSplit', 'sourceFaces', 'targetFaces',
    'deletedFaces', 'seedFaces', 'definition', 'occurrenceOne', 'occurrenceTwo', 'geometryOrOriginOne',
    'geometryOrOriginTwo', 'directionEntity', 'pathEntity', 'entities', 'plane', 'pullDirection',
    # joints, joint origins, rigid groups, motion links, contact sets
    'geometry', 'occurrences', 'jointOne', 'jointTwo', 'occurencesAndBodies', 'xAxisEntity', 'zAxisEntity',
    # surfaces
    'stitchSurfaces', 'trimTool', 'surfaces', 'tools', 'boundaryCurve', 'interiorRailsAndPoints',
    'facesToUntrim', 'guideSurfaces', 'alternateFace',
    # patterns, scale, remove, draft
    'directionOneEntity', 'directionTwoEntity', 'startPoint', 'point', 'itemToRemove', 'partingLineCurves',
    'movingPartingLineFixedEdges',
    # threads and sheet metal
    'hole', 'inputCylindricalFace', 'inputCylindricalFaces', 'unfoldFeature', 'stationaryFace', 'bendLines',
    'bendFaces', 'edgeOne', 'edgeTwo', 'dominantEdge', 'submissiveEdge',
    # mesh
    'inputBodies', 'mesh',
]
DEF_INPUT_ATTRS = [
    'entity', 'sketchPoint', 'sketchPoints', 'planarEntity', 'linearEntity', 'edge', 'edgeOne', 'edgeTwo',
    'point', 'pointOne', 'pointTwo', 'pointThree', 'circularEntity', 'planarEntityOne', 'planarEntityTwo',
    'linearEntityOne', 'linearEntityTwo', 'edges', 'entityOne', 'entityTwo', 'profile', 'path', 'axis',
    'distanceEntity', 'toEntity', 'fromEntity', 'plane', 'face', 'curve', 'pathEntity',
]


class Collector:
    def __init__(self, design, progress, thumbs=False):
        self.des = design
        self.want_thumbs = thumbs
        self.thumbs = {}             # node id -> PNG data URI
        self._thumb_file = None
        self._cam0 = None
        self.root = design.rootComponent
        self.tl = design.timeline
        self.progress = progress
        self.nodes = []              # list of node dicts
        self.groups = []             # list of group dicts
        self.edges = {}              # (s,t) -> set(kinds)
        self.tl2node = {}            # timeline index -> node id
        self.face_owner = {}         # face entityToken -> node id (latest creator)
        self.body_owner = {}         # body name -> node id (latest feature that changed it)
        self.body_creator = {}       # body name -> node id (feature that created it)
        self.comp_owner = {}         # component name -> node id (occurrence/derive/new-component feature)
        self.mesh_creator = {}       # mesh body name -> node id
        self.comps = {}              # component key -> component node entry (see comp_entry)
        self.doc = _safe(lambda: adsk.core.Application.get().activeDocument)
        self.cur = None
        self.warnings = []
        self._prev_sigs = set()      # face signatures of the state before the item being captured

    # ------------------------------------------------------------ edges ---
    def keep_active(self):
        """Fusion's timeline calls fail when another document tab becomes active during the run."""
        doc = getattr(self, 'doc', None)
        app = adsk.core.Application.get()
        if doc is not None and _safe(lambda: app.activeDocument) != doc:
            _safe(doc.activate)

    def add_edge(self, src, dst, kind):
        if src is None or dst is None or src == dst:
            return
        self.edges.setdefault((src, dst), set()).add(kind)

    def tl_node(self, obj):
        to = _safe(lambda: obj.timelineObject)
        idx = _safe(lambda: to.index) if to is not None else None
        if idx is None:
            return None
        # an object inside an inserted or nested component has a timeline of its own: the index only counts
        # when it points at the same item in this design's timeline
        mine = _safe(lambda: self.tl.item(idx))
        if mine is None or _safe(lambda: mine.name) != _safe(lambda: to.name):
            return None
        return self.tl2node.get(idx)

    def occ_node(self, occ):
        """The timeline item that brought an occurrence (or anything inside it) into this design: its top-level
        occurrence's insert/copy item, the Derive feature that made it, or the feature that made its component."""
        top, guard = occ, 0
        while top is not None and guard < 20:
            up = _safe(lambda: top.assemblyContext)
            if up is None:
                break
            top, guard = up, guard + 1
        for o in ([occ, top] if top is not occ else [occ]):
            df = _safe(lambda: o.deriveFeature)
            if df is not None:
                n = self.tl_node(df)
                if n: return n
        n = self.tl_node(top) if top is not None else None
        if n: return n
        cn = _safe(lambda: top.component.name) if top is not None else None
        return self.comp_owner.get(cn) if cn else None

    def external(self, obj):
        """True when obj sits inside an inserted (referenced) or derived component."""
        ctx = _safe(lambda: obj.assemblyContext)
        guard = 0
        while ctx is not None and guard < 20:
            if _safe(lambda: ctx.isReferencedComponent) or _safe(lambda: ctx.isDerived):
                return ctx
            ctx, guard = _safe(lambda: ctx.assemblyContext), guard + 1
        return None

    # Components are nodes of their own (see scan_components): a reference to a component, or to anything inside
    # it, points at the component node, and the component node hangs under the item that brought it in.
    def comp_key(self, c):
        return _safe(lambda: c.entityToken) or _safe(lambda: c.name)

    def comp_entry(self, c):
        if c is None or c == self.root:
            return None
        k = self.comp_key(c)
        if not k:
            return None
        if k not in self.comps:
            self.comps[k] = {'c': c, 'id': 'c:%d' % len(self.comps), 'members': [], 'creator': None, 'occ': None}
        return self.comps[k]

    def comp_ref(self, occ):
        """Component node for an occurrence or anything inside it. Inside an inserted or derived part the whole
        part counts (its outermost inserted/derived occurrence): its own sub-components are not listed."""
        top, o, guard = None, occ, 0
        while o is not None and guard < 20:
            if _safe(lambda: o.isReferencedComponent) or _safe(lambda: o.isDerived):
                top = o
            o, guard = _safe(lambda: o.assemblyContext), guard + 1
        target = top or occ
        ent = self.comp_entry(_safe(lambda: target.component))
        if ent is None:
            return None
        if ent['occ'] is None:
            ent['occ'] = target
        return ent['id']

    def comp_node(self, obj):
        comp = _safe(lambda: obj.parentComponent) or _safe(lambda: obj.body.parentComponent)
        ctx = _safe(lambda: obj.assemblyContext) or _safe(lambda: obj.body.assemblyContext)
        if ctx is not None:
            return self.comp_ref(ctx)
        # a native object inside another component (e.g. a joint inside a derived part): where is it placed
        if comp and comp != self.root:
            occs = _safe(lambda: self.root.allOccurrencesByComponent(comp))
            first = _safe(lambda: occs.item(0)) if occs is not None and (_safe(lambda: occs.count) or 0) else None
            if first is not None:
                return self.comp_ref(first)
            ent = self.comp_entry(comp)
            return ent['id'] if ent else None
        return None

    def resolve(self, obj, out, kind, depth=0):
        try:
            self._resolve(obj, out, kind, depth)
        except Exception:
            pass

    def _resolve(self, obj, out, kind, depth=0):
        """Collect (node id, kind) pairs that `obj` comes from into `out`."""
        if obj is None or depth > 6:
            return
        t = _t(obj)
        if not t:
            # plain python list / vector
            try:
                for x in obj:
                    self.resolve(x, out, kind, depth + 1)
            except TypeError:
                pass
            return
        native = _safe(lambda: obj.nativeObject) or obj

        if t == 'Sketch':
            n = self.tl_node(native) or self.comp_node(obj)
            if n: out.append((n, 'sketch'))
            return
        if t in ('Profile', 'ProfileLoop', 'ProfileCurve') or (t.startswith('Sketch') and t not in ('SketchDimension',)):
            sk = _safe(lambda: obj.parentSketch)
            if sk is not None:
                self.resolve(sk, out, 'profile' if t.startswith('Profile') else 'sketch', depth + 1)
            return
        if t in ('ConstructionPlane', 'ConstructionAxis', 'ConstructionPoint'):
            n = self.tl_node(native) or self.comp_node(obj)
            if n: out.append((n, 'plane'))
            return
        ext = self.external(obj) if t in ('BRepFace', 'BRepEdge', 'BRepVertex', 'BRepBody') else None
        if ext is not None:
            n = self.comp_ref(ext)
            if n: out.append((n, 'component'))
            return
        if t == 'BRepFace':
            tok = _safe(lambda: obj.entityToken)
            n = self.face_owner.get(tok) if tok else None
            if n:
                out.append((n, 'geometry'))
            else:
                self.resolve(_safe(lambda: obj.body), out, 'body', depth + 1)
            return
        if t == 'BRepEdge':
            for f in (_safe(lambda: list(obj.faces)) or []):
                self.resolve(f, out, 'geometry', depth + 1)
            return
        if t == 'BRepVertex':
            eds = _safe(lambda: list(obj.edges)) or []
            for e in eds[:3]:
                self.resolve(e, out, 'geometry', depth + 1)
            return
        if t in ('BRepLoop', 'BRepCoEdge'):
            self.resolve(_safe(lambda: obj.face) or _safe(lambda: obj.edge), out, 'geometry', depth + 1)
            return
        if t == 'BRepBody':
            # A feature working on a body really depends on the feature that CREATED the body.
            # The last feature that changed it before this step is only an ordering link.
            name = _safe(lambda: obj.name)
            n = self.body_creator.get(name) if name else None
            if n is None:
                n = self.comp_node(obj)
            if n: out.append((n, 'body'))
            last = self.body_owner.get(name) if name else None
            if last and last != n:
                out.append((last, 'order'))
            return
        if t == 'Occurrence':
            n = self.comp_ref(obj)
            if n: out.append((n, 'component'))
            return
        if t == 'Component':
            ent = self.comp_entry(obj)
            if ent: out.append((ent['id'], 'component'))
            return
        if t in ('Path',):
            for pe in (_safe(lambda: list(obj)) or []):
                self.resolve(_safe(lambda: pe.entity), out, 'sketch', depth + 1)
            return
        if t in JOINT_TYPES:
            n = self.tl_node(native)
            if n:
                out.append((n, 'joint'))
            else:
                # a joint inside an inserted component (e.g. one a motion link drives): link that component
                n = self.comp_node(obj)
                if n: out.append((n, 'joint'))
            return
        if t == 'MeshBody':
            n = self.tl_node(_safe(lambda: obj.baseOrFormFeature)) or self.mesh_creator.get(_safe(lambda: obj.name)) or self.comp_node(obj)
            if n: out.append((n, 'body'))
            return
        if t == 'TSplineBody':
            n = self.tl_node(_safe(lambda: obj.parentFormFeature))
            if n: out.append((n, 'body'))
            return
        if t.endswith('Feature') or t.endswith('Joint'):
            n = self.tl_node(native)
            if n: out.append((n, 'feature'))
            return
        # collections
        if t in ('ObjectCollection',) or (_safe(lambda: obj.count) is not None and _has(obj, 'item')):
            cnt = _safe(lambda: obj.count) or 0
            for i in range(min(cnt, 5000)):
                self.resolve(_safe(lambda: obj.item(i)), out, kind, depth + 1)
            if cnt:
                return
        # definition-like objects: look into their input attributes
        for a in DEF_INPUT_ATTRS:
            if _has(obj, a):
                self.resolve(_safe(lambda: getattr(obj, a)), out, kind, depth + 1)

    # -------------------------------------------------------------- scan ---
    def expand_groups(self):
        self.collapsed = []
        for _ in range(10):
            changed = False
            for g in self.tl.timelineGroups:
                if _safe(lambda: g.isCollapsed):
                    self.collapsed.append(g)
                    _safe(lambda: setattr(g, 'isCollapsed', False))
                    changed = True
            if not changed:
                break

    def restore_groups(self):
        for g in reversed(self.collapsed):
            _safe(lambda: setattr(g, 'isCollapsed', True))

    def build_nodes(self):
        tl = self.tl
        # Groups are expanded at this point, and expanded groups do not appear as timeline
        # items, so read them from timelineGroups and map members by timeline index.
        tgroups = _safe(lambda: list(tl.timelineGroups)) or []
        direct = {}          # timeline index -> innermost group id
        gparent = {}         # group id -> parent group id
        gid_by_name = {}
        for j in range(len(tgroups)):
            g = tgroups[j]      # re-read: the list is replaced if the design had to be reopened
            gid = 'G%d' % j
            gid_by_name.setdefault(_safe(lambda: g.name, ''), gid)
        for j in range(len(tgroups)):
            g = tgroups[j]      # re-read: the list is replaced if the design had to be reopened
            gid = 'G%d' % j
            members = []
            for k in range(_safe(lambda: g.count, 0) or 0):
                m = _safe(lambda: g.item(k))
                if m is None:
                    continue
                if _safe(lambda: m.isGroup):
                    continue
                idx = _safe(lambda: m.index)
                if idx is not None:
                    members.append(idx)
            pg = _safe(lambda: g.parentGroup)
            pid = gid_by_name.get(_safe(lambda: pg.name)) if pg else None
            gparent[gid] = pid
            self.groups.append({'id': gid, 'name': _safe(lambda: g.name, gid), 'first': min(members) if members else 10 ** 6, 'parent': pid})
            for idx in members:
                # innermost group wins: a nested group has fewer members than its parent
                prev = direct.get(idx)
                if prev is None or len(members) < prev[1]:
                    direct[idx] = (gid, len(members))
        # nested groups whose members were only other groups: first = min of children
        for g in sorted(self.groups, key=lambda g: -len(g['id'])):
            for c in self.groups:
                if c['parent'] == g['id']:
                    g['first'] = min(g['first'], c['first'])
        order = 0
        for i in range(tl.count):
            it = tl.item(i)
            if it.isGroup:
                continue
            e = _safe(lambda: it.entity)
            t = _t(e) if e else 'Unknown'
            # group path, outermost first
            path = []
            gid = direct.get(i, (None, 0))[0]
            guard = 0
            while gid and guard < 20:
                path.insert(0, gid)
                gid = gparent.get(gid)
                guard += 1
            info = []
            op = _safe(lambda: e.operation)
            if op is not None and t in ('ExtrudeFeature', 'RevolveFeature', 'SweepFeature', 'LoftFeature', 'CombineFeature'):
                info.append(OP_NAMES.get(op, str(op)))
            nid = 'n%d' % i
            cat = CAT_BY_TYPE.get(t, 'other')
            if t in SURFACE_WHEN_NOT_SOLID and _safe(lambda: e.isSolid) is False:
                cat = 'surface'
                info.append('surface')
            node = {'id': nid, 'name': it.name, 'type': t, 'cat': cat, 'tl': i, 'o': order,
                    'g': path, 'supp': bool(_safe(lambda: it.isSuppressed, False)),
                    'health': _safe(lambda: it.healthState, 0),
                    'msg': re.sub(r'<[^>]+>', ' ', _safe(lambda: it.errorOrWarningMessage, '') or '').strip()[:400],
                    'info': ', '.join(info)}
            self.nodes.append(node)
            self.tl2node[i] = nid
            order += 1
            if t == 'Occurrence':
                cname = _safe(lambda: e.component.name)
                if cname: self.comp_owner[cname] = nid
            if t == 'DeriveFeature':
                for c in (_safe(lambda: list(e.derivedComponents), []) or []):
                    cn = _safe(lambda: c.name)
                    if cn: self.comp_owner.setdefault(cn, nid)

    def record_outputs(self, it, nid):
        """Remember which bodies/faces an item produced (state right after it)."""
        e = _safe(lambda: it.entity)
        if e is None or it.isSuppressed:
            return
        for b in (_safe(lambda: list(e.bodies)) or []):
            nm = _safe(lambda: b.name)
            if nm:
                self.body_creator.setdefault(nm, nid)
                self.body_owner[nm] = nid
        for f in (_safe(lambda: list(e.faces)) or []):
            tok = _safe(lambda: f.entityToken)
            if tok: self.face_owner[tok] = nid
        # a feature that makes a new component (e.g. Extrude as new component) owns that component,
        # so joints between components link back to it
        for b in (_safe(lambda: list(e.bodies)) or []):
            comp = _safe(lambda: b.parentComponent)
            if comp is not None and comp != self.root:
                cn = _safe(lambda: comp.name)
                if cn: self.comp_owner.setdefault(cn, nid)
        for mb in (_safe(lambda: list(e.meshBodies)) or []):
            nm = _safe(lambda: mb.name)
            if nm: self.mesh_creator.setdefault(nm, nid)

    def inputs_of(self, it):
        e = _safe(lambda: it.entity)
        if e is None:
            return []
        t = _t(e)
        out = []
        if t == 'Sketch':
            self.resolve(_safe(lambda: e.referencePlane), out, 'plane')
            ents = (_safe(lambda: list(e.sketchCurves)) or []) + (_safe(lambda: list(e.sketchPoints)) or [])
            for s in ents:
                if _safe(lambda: s.isReference) or _safe(lambda: s.isLinked):
                    ref = _safe(lambda: s.referencedEntity)
                    if ref is not None:
                        self.resolve(ref, out, 'sketch')
        elif t in ('ConstructionPlane', 'ConstructionAxis', 'ConstructionPoint'):
            self.resolve(_safe(lambda: e.definition), out, 'plane')
        else:
            for a in FEATURE_INPUT_ATTRS:
                if _has(e, a):
                    self.resolve(_safe(lambda: getattr(e, a)), out, 'feature')
        return out

    # ------------------------------------------------------- thumbnails ---
    # Each thumbnail is framed on the item itself: an orthographic camera looks
    # straight at the plane the item was built on (sketch plane, profile plane,
    # or the dominant planar face it made), zoomed to the item's extent, and
    # the picture's proportions follow the item's shape. Bodies the item does
    # not touch are hidden while the picture is taken and shown again after.
    TH_LONG = 360          # longest side of a thumbnail, px
    TH_MAX_ASPECT = 2.6    # clamp very long/thin items
    TH_MARGIN = 0.10       # empty border around the item

    def thumbs_begin(self):
        if not self.want_thumbs:
            return
        app = adsk.core.Application.get()
        self._vp = app.activeViewport
        self._cam0 = _safe(lambda: self._vp.camera)
        self._hidden = []  # bodies we switched off for the current picture
        self._changed = []  # (object, attribute, old value) switched on for the current picture
        self._thumb_file = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', '_thumb.png')
        os.makedirs(os.path.dirname(self._thumb_file), exist_ok=True)

    def thumbs_end(self):
        if not self.want_thumbs:
            return
        app = adsk.core.Application.get()
        self._show_bodies()
        self._restore_visible()
        _safe(lambda: app.userInterface.activeSelections.clear())
        if self._cam0 is not None:
            cam = self._cam0
            _safe(lambda: setattr(cam, 'isSmoothTransition', False))
            _safe(lambda: setattr(app.activeViewport, 'camera', cam))
        if self._thumb_file and os.path.exists(self._thumb_file):
            _safe(lambda: os.remove(self._thumb_file))
        self._cam0 = None

    # --- geometry helpers
    @staticmethod
    def _vec(x, y, z):
        return adsk.core.Vector3D.create(x, y, z)

    def _visible_bodies(self):
        out = []
        for c in (_safe(lambda: list(self.des.allComponents)) or []):
            for b in (_safe(lambda: list(c.bRepBodies)) or []):
                if _safe(lambda: b.isLightBulbOn):
                    out.append(b)
        return out

    def _isolate(self, keep):
        """Hide visible bodies that are not in `keep` (list of bodies); keep=None shows everything."""
        self._show_bodies()
        if keep is None:
            return
        tokens = set(_safe(lambda: b.entityToken) for b in keep)
        tokens |= set(_safe(lambda: b.nativeObject.entityToken) for b in keep if _safe(lambda: b.nativeObject) is not None)
        for b in keep:
            # the item's own body may be a hidden tool body - show it for the picture
            self._set(b, 'isLightBulbOn', True)
            pc = _safe(lambda: b.parentComponent)
            if pc is not None:
                self._set(pc, 'isBodiesFolderLightBulbOn', True)
        for b in self._visible_bodies():
            if _safe(lambda: b.entityToken) not in tokens:
                if _safe(lambda: setattr(b, 'isLightBulbOn', False), 'fail') != 'fail':
                    self._hidden.append(b)

    def _set(self, obj, attr, value):
        old = _safe(lambda: getattr(obj, attr))
        if old is None or old == value:
            return
        if _safe(lambda: setattr(obj, attr, value), 'fail') != 'fail':
            self._changed.append((obj, attr, old))

    def _force_visible(self, e, folder_attr):
        if not hasattr(self, '_changed'):
            self._changed = []
        comp = _safe(lambda: e.parentComponent) or self.root
        self._set(comp, folder_attr, True)
        self._set(e, 'isLightBulbOn', True)

    def _restore_visible(self):
        for obj, attr, old in reversed(getattr(self, '_changed', [])):
            _safe(lambda: setattr(obj, attr, old))
        self._changed = []

    def _show_bodies(self):
        for b in getattr(self, '_hidden', []):
            _safe(lambda: setattr(b, 'isLightBulbOn', True))
        self._hidden = []

    @staticmethod
    def _corners(bb):
        if bb is None:
            return []
        a, b = bb.minPoint, bb.maxPoint
        return [adsk.core.Point3D.create(x, y, z) for x in (a.x, b.x) for y in (a.y, b.y) for z in (a.z, b.z)]

    @staticmethod
    def _union(boxes):
        out = None
        for bb in boxes:
            if bb is None:
                continue
            if out is None:
                out = bb.copy()
            else:
                out.combine(bb)
        return out

    def _sketch_frame(self, sk):
        xd = _safe(lambda: sk.xDirection)
        yd = _safe(lambda: sk.yDirection)
        if xd is None or yd is None:
            return None
        n = xd.crossProduct(yd)
        n.normalize()
        return n, [xd, yd]

    def _sketch_points(self, sk):
        bb = _safe(lambda: sk.boundingBox)   # sketch space
        pts = []
        for p in self._corners(bb):
            q = _safe(lambda: sk.sketchToModelSpace(p))
            if q is not None:
                pts.append(q)
        return pts

    def _feature_sketch(self, e):
        for a in ('profile', 'profiles'):
            pr = _safe(lambda: getattr(e, a))
            if pr is None:
                continue
            first = pr
            if _safe(lambda: pr.count) is not None:
                first = _safe(lambda: pr.item(0))
            sk = _safe(lambda: first.parentSketch)
            if sk is not None:
                return sk
        hp = _safe(lambda: e.holePositionDefinition)
        for a in ('sketchPoint', 'sketchPoints'):
            sp = _safe(lambda: getattr(hp, a)) if hp is not None else None
            if sp is not None:
                if _safe(lambda: sp.count) is not None:
                    sp = _safe(lambda: sp.item(0))
                sk = _safe(lambda: sp.parentSketch)
                if sk is not None:
                    return sk
        return None

    def _dominant_normal(self, faces):
        """Area-weighted most common outward normal among planar faces."""
        buckets = {}
        for f in faces:
            if _t(_safe(lambda: f.geometry)) != 'Plane':
                continue
            ok, nv = (_safe(lambda: f.evaluator.getNormalAtPoint(f.pointOnFace)) or (False, None))
            if not ok or nv is None:
                continue
            key = (round(nv.x, 2), round(nv.y, 2), round(nv.z, 2))
            area = _safe(lambda: f.area, 0) or 0
            v = buckets.get(key)
            buckets[key] = (nv, (v[1] if v else 0) + area)
        if not buckets:
            return None
        return max(buckets.values(), key=lambda v: v[1])[0]

    def _view_for(self, it):
        """Returns (normal, up_candidates, points, keep_bodies) for an item, or None."""
        e = _safe(lambda: it.entity)
        t = _t(e) if e is not None else ''
        iso = self._vec(-1, -1, 1)
        iso.normalize()
        if t == 'Sketch':
            fr = self._sketch_frame(e)
            pts = self._sketch_points(e)
            if fr and pts:
                return fr[0], fr[1], pts, [], False   # sketch alone, seen from the front
        if t in ('ConstructionPlane', 'ConstructionAxis', 'ConstructionPoint', 'Occurrence', 'DeriveFeature') or t.endswith('Joint'):
            bodies = self._visible_bodies()
            pts = self._corners(self._union(_safe(lambda: b.boundingBox) for b in bodies))
            if t == 'ConstructionPlane':
                g = _safe(lambda: e.geometry)
                if g is not None:
                    # include a patch of the plane itself so it is in frame even with no bodies yet
                    size = 2.5
                    if pts:
                        size = max(1.0, max(pts[0].distanceTo(p) for p in pts) / 3)
                    for su in (-size, size):
                        for sv in (-size, size):
                            q = g.origin.copy()
                            du = g.uDirection.copy(); du.scaleBy(su)
                            dv = g.vDirection.copy(); dv.scaleBy(sv)
                            q.translateBy(du); q.translateBy(dv)
                            pts.append(q)
            if pts:
                g = _safe(lambda: e.geometry) if t == 'ConstructionPlane' else None
                if g is not None:
                    # flat: look straight at the plane, with the model behind it
                    return g.normal, [g.uDirection, g.vDirection], pts, None, False
                return iso, [], pts, None, False
            return None
        faces = self._new_faces(e)
        bodies = _safe(lambda: list(e.bodies)) or []
        if t == 'CombineFeature':
            tb = _safe(lambda: e.targetBody)
            if tb is not None:
                bodies = [tb]
        if not bodies:
            for a in ('targetBody', 'body'):
                b = _safe(lambda: getattr(e, a))
                if b is not None and _t(b) == 'BRepBody':
                    bodies = [b]
                    break
        box = self._union(_safe(lambda: f.boundingBox) for f in faces) or \
            self._union(_safe(lambda: b.boundingBox) for b in bodies)
        pts = self._corners(box)
        if not pts:
            return None
        sk = self._feature_sketch(e)
        fr = self._sketch_frame(sk) if sk is not None else None
        if fr:
            n, ups = fr
            planar = True
        else:
            dn = self._dominant_normal(faces)
            n, ups, planar = (dn or iso), [], dn is not None
        keep = bodies or None
        # Look from the side with less material in front of the feature, so the
        # rest of its body does not hide it.
        shown = keep if keep else self._visible_bodies()
        around = self._corners(self._union(_safe(lambda: b.boundingBox) for b in shown))
        if around:
            c = self._center(pts)
            o = adsk.core.Point3D.create(0, 0, 0)
            fc = o.vectorTo(c).dotProduct(n)
            proj = [o.vectorTo(q).dotProduct(n) for q in around]
            if (max(proj) - fc) > (fc - min(proj)) + 1e-6:
                n = n.copy()
                n.scaleBy(-1)
        # 3D result: a three-quarter view turned from the plane it was built on
        return n, ups, pts, keep, planar

    @staticmethod
    def _center(pts):
        k = float(len(pts))
        return adsk.core.Point3D.create(sum(p.x for p in pts) / k, sum(p.y for p in pts) / k, sum(p.z for p in pts) / k)

    OBLIQUE = (0.55, 0.45)   # sideways / upwards tilt of the 3/4 view, relative to the front view

    def _camera_for(self, n, ups, pts, oblique=False):
        # up: keep the picture upright relative to the world where possible
        z = self._vec(0, 0, 1)
        cands = []
        for u in ups:
            for s in (1, -1):
                v = u.copy()
                v.scaleBy(s)
                cands.append(v)
        if abs(n.dotProduct(z)) < 0.95:
            up = max(cands, key=lambda v: v.dotProduct(z)) if cands else z
            if cands and up.dotProduct(z) < 0.3:
                up = z
        else:
            y = self._vec(0, 1, 0)
            up = max(cands, key=lambda v: v.dotProduct(y)) if cands else y
        # right/up axes of the image
        r = up.crossProduct(n)
        if r.length < 1e-6:
            r = self._vec(1, 0, 0).crossProduct(n)
        r.normalize()
        u = n.crossProduct(r)
        u.normalize()
        if oblique:
            # rotate the view towards the upper right, keeping the same 'up'
            v = n.copy()
            a = r.copy(); a.scaleBy(self.OBLIQUE[0]); v.add(a)
            b = u.copy(); b.scaleBy(self.OBLIQUE[1]); v.add(b)
            v.normalize()
            n = v
            r = u.crossProduct(n)
            r.normalize()
            u = n.crossProduct(r)
            u.normalize()
        c = self._center(pts)
        xs = [c.vectorTo(p).dotProduct(r) for p in pts]
        ys = [c.vectorTo(p).dotProduct(u) for p in pts]
        W = max(max(xs) - min(xs), 1e-3)
        H = max(max(ys) - min(ys), 1e-3)
        # centre on the projected box, not the 3D centre
        c.translateBy(self._vec(*(r.x * (max(xs) + min(xs)) / 2 + u.x * (max(ys) + min(ys)) / 2,
                                  r.y * (max(xs) + min(xs)) / 2 + u.y * (max(ys) + min(ys)) / 2,
                                  r.z * (max(xs) + min(xs)) / 2 + u.z * (max(ys) + min(ys)) / 2)))
        a = max(1.0 / self.TH_MAX_ASPECT, min(self.TH_MAX_ASPECT, W / H))
        if a >= 1:
            iw, ih = self.TH_LONG, max(40, int(round(self.TH_LONG / a)))
        else:
            iw, ih = max(40, int(round(self.TH_LONG * a))), self.TH_LONG
        m = 1 + 2 * self.TH_MARGIN
        scale = min(iw / (W * m), ih / (H * m))            # px per cm
        ext = min(iw, ih) / scale                            # Fusion: the shorter image side spans viewExtents
        cam = self._vp.camera
        cam.isSmoothTransition = False
        cam.cameraType = adsk.core.CameraTypes.OrthographicCameraType
        dist = max(W, H, 1.0) * 20
        eye = c.copy()
        d = n.copy()
        d.scaleBy(dist)
        eye.translateBy(d)
        cam.target = c
        cam.eye = eye
        cam.upVector = u
        cam.isFitView = False
        cam.viewExtents = ext
        return cam, iw, ih

    @staticmethod
    def _face_sig(f):
        c = _safe(lambda: f.centroid)
        a = _safe(lambda: f.area)
        if c is None or a is None:
            return None
        return (round(a, 5), round(c.x, 4), round(c.y, 4), round(c.z, 4))

    def _all_sigs(self):
        out = set()
        for c in self.des.allComponents:
            for b in c.bRepBodies:
                for f in b.faces:
                    out.add(self._face_sig(f))
        return out

    def _new_faces(self, e):
        """Faces of the feature that did not exist before it. Fusion hands faces that a later feature
        only touched (for example a second cut through the same slot) over to that feature, so
        e.faces alone can show geometry an earlier feature made."""
        faces = _safe(lambda: list(e.faces)) or []
        new = [f for f in faces if self._face_sig(f) not in self._prev_sigs]
        return new or faces

    def capture(self, it, nid):
        # Called with the marker right after `it`.
        if not self.want_thumbs:
            return
        try:
            if nid is not None:
                self._capture(it, nid)
        finally:
            self._prev_sigs = _safe(self._all_sigs, set())

    def _capture(self, it, nid):
        app = adsk.core.Application.get()
        sels = app.userInterface.activeSelections
        _safe(sels.clear)
        try:
            view = self._view_for(it)
            if view is None:
                return
            n, ups, pts, keep, oblique = view
            cam, iw, ih = self._camera_for(n, ups, pts, oblique)
            self._isolate(keep)
            e = _safe(lambda: it.entity)
            t = _t(e) if e is not None else ''
            if t == 'Sketch' or t.startswith('Construction'):
                # show the sketch/plane itself even if it is hidden; no selection highlight
                self._force_visible(e, 'isSketchFolderLightBulbOn' if t == 'Sketch' else 'isConstructionFolderLightBulbOn')
            elif keep and e is not None:
                # Highlight only when the feature is shown together with geometry it did not make
                # (cuts, chamfers, fillets, offsets, joins). A new body shown on its own needs no highlight.
                own = self._new_faces(e)
                total = sum((_safe(lambda: b.faces.count, 0) or 0) for b in keep)
                if 0 < len(own) < total:
                    for f in own[:400]:
                        _safe(lambda: sels.add(f))
            self._vp.camera = cam
            adsk.doEvents()
            ok = _safe(lambda: self._vp.saveAsImageFile(self._thumb_file, iw, ih), False)
            if ok and os.path.exists(self._thumb_file):
                with open(self._thumb_file, 'rb') as f:
                    self.thumbs[nid] = 'data:image/png;base64,' + base64.b64encode(f.read()).decode('ascii')
        except Exception as ex:
            if len(self.warnings) < 50:
                self.warnings.append('No thumbnail for %s: %s' % (_safe(lambda: it.name, '?'), ex))
        finally:
            self._show_bodies()
            self._restore_visible()
            _safe(sels.clear)

    def scan(self):
        tl = self.tl
        n_items = tl.count
        prev = None
        self.thumbs_begin()
        for i in range(n_items):
            it = tl.item(i)
            if it.isGroup:
                continue
            nid = self.tl2node.get(i)
            if self.progress:
                self.progress(it.name, i, n_items)
            _safe(lambda: it.rollTo(True))
            self.keep_active()
            if prev is not None:
                self.capture(*prev)
                try:
                    self.record_outputs(*prev)
                except Exception as ex:
                    self.warnings.append('Could not read outputs of %s: %s' % (_safe(lambda: prev[0].name, '?'), ex))
            try:
                links = self.inputs_of(it)
            except Exception as ex:
                links = []
                self.warnings.append('Could not read references of %s: %s' % (_safe(lambda: it.name, '?'), ex))
            for src, kind in links:
                self.add_edge(src, nid, kind)
            prev = (it, nid)
        _safe(lambda: tl.moveToEnd())
        if prev is not None:
            self.capture(*prev)
            _safe(lambda: self.record_outputs(*prev))
        self.thumbs_end()

    def scan_components(self):
        """A node per component (except the root): its parent is the item that brought it in (insert, New
        Component, a feature set to "new component", or a Derive feature), and it is the parent of the timeline
        items built inside it. Components inside inserted or derived parts are not listed separately."""
        byid = {n['id']: n for n in self.nodes}
        comps = self.comps
        comp_entry = self.comp_entry
        ckey = self.comp_key

        for i, nid in sorted(self.tl2node.items()):
            e = _safe(lambda: self.tl.item(i).entity)
            if e is None:
                continue
            if _t(e) == 'Occurrence':
                own = comp_entry(_safe(lambda: e.component))
                if own is not None:
                    own['creator'] = own['creator'] or nid
                    own['occ'] = own['occ'] or e
                cont = _safe(lambda: e.sourceComponent)
                if cont is None:
                    ctx = _safe(lambda: e.assemblyContext)
                    cont = _safe(lambda: ctx.component) if ctx is not None else None
            else:
                cont = _safe(lambda: e.parentComponent)
            ent = comp_entry(cont)
            if ent is not None:
                ent['members'].append(nid)
        # components placed in the design that no timeline item is built in (inserted parts, derived parts)
        for occ in (_safe(lambda: list(self.root.allOccurrences)) or []):
            if self.external(occ) is not None:
                continue          # inside an inserted/derived part
            ent = comp_entry(_safe(lambda: occ.component))
            if ent is not None and ent['occ'] is None:
                ent['occ'] = occ
        for k, ent in comps.items():
            c = ent['c']
            occ = ent['occ']
            if occ is None:
                occs = _safe(lambda: self.root.allOccurrencesByComponent(c))
                occ = _safe(lambda: occs.item(0)) if occs is not None and (_safe(lambda: occs.count) or 0) else None
                ent['occ'] = occ
            if ent['creator'] is None:
                ent['creator'] = self.comp_owner.get(_safe(lambda: c.name)) or (self.occ_node(occ) if occ is not None else None)
            ctx = _safe(lambda: occ.assemblyContext) if occ is not None else None
            ent['parent'] = comps.get(ckey(_safe(lambda: ctx.component))) if ctx is not None else None
        for k, ent in comps.items():
            c = ent['c']
            cr = byid.get(ent['creator']) if ent['creator'] else None
            mem = [byid[m]['o'] for m in ent['members'] if m in byid]
            o = (cr['o'] + 0.0004) if cr else ((min(mem) - 0.5) if mem else -1.5)
            occ = ent['occ']
            info = ['%d item%s' % (len(ent['members']), '' if len(ent['members']) == 1 else 's')]
            if occ is not None and _safe(lambda: occ.isReferencedComponent):
                info.append('inserted')
            if occ is not None and _safe(lambda: occ.isDerived):
                info.append('derived')
            n_occ = _safe(lambda: self.root.allOccurrencesByComponent(c).count)
            if n_occ and n_occ > 1:
                info.append('%d instances' % n_occ)
            self.nodes.append({'id': ent['id'], 'name': _safe(lambda: c.name, 'Component'), 'type': 'Component', 'cat': 'component',
                               'tl': None, 'o': o, 'g': [], 'supp': False, 'health': 0, 'msg': '', 'info': ' · '.join(info)})
            if ent['creator']:
                self.add_edge(ent['creator'], ent['id'], 'component')
            elif ent.get('parent'):
                self.add_edge(ent['parent']['id'], ent['id'], 'incomp')
            for m in ent['members']:
                self.add_edge(ent['id'], m, 'incomp')
        self.capture_components()

    def _occ_bodies(self, occ, depth=0):
        """Bodies of an occurrence and its sub-occurrences, in assembly space."""
        out = list(_safe(lambda: list(occ.bRepBodies)) or [])
        if depth < 8:
            for ch in (_safe(lambda: list(occ.childOccurrences)) or []):
                out += self._occ_bodies(ch, depth + 1)
        return out

    def capture_components(self):
        """One picture per component: only its bodies, three-quarter view, taken at the end of the timeline.
        The item that brought a part in (Derive, component insert) shows the same picture."""
        if not self.want_thumbs or not self.comps:
            return
        self.thumbs_begin()
        app = adsk.core.Application.get()
        try:
            iso = self._vec(-1, -1, 1)
            iso.normalize()
            done_creators = set()
            for k, ent in self.comps.items():
                occ = ent.get('occ')
                if occ is None:
                    continue
                if self.progress:
                    self.progress('Picture of ' + (_safe(lambda: ent['c'].name) or 'component'), 0, 1)
                try:
                    self._set(occ, 'isLightBulbOn', True)
                    bodies = [b for b in self._occ_bodies(occ) if _safe(lambda: b.isValid, True)]
                    pts = self._corners(self._union(_safe(lambda: b.boundingBox) for b in bodies))
                    if not bodies or not pts:
                        continue
                    cam, iw, ih = self._camera_for(iso, [], pts, False)
                    self._isolate(bodies)
                    _safe(lambda: app.userInterface.activeSelections.clear())
                    self._vp.camera = cam
                    adsk.doEvents()
                    ok = _safe(lambda: self._vp.saveAsImageFile(self._thumb_file, iw, ih), False)
                    if ok and os.path.exists(self._thumb_file):
                        with open(self._thumb_file, 'rb') as f:
                            data = 'data:image/png;base64,' + base64.b64encode(f.read()).decode('ascii')
                        self.thumbs[ent['id']] = data
                        cr = ent.get('creator')
                        crn = next((n for n in self.nodes if n['id'] == cr), None) if cr else None
                        if crn and crn['type'] in ('DeriveFeature', 'Occurrence') and cr not in done_creators:
                            self.thumbs[cr] = data
                            done_creators.add(cr)
                except Exception as ex:
                    if len(self.warnings) < 50:
                        self.warnings.append('No picture for component %s: %s' % (_safe(lambda: ent['c'].name, '?'), ex))
                finally:
                    self._show_bodies()
                    self._restore_visible()
        finally:
            self.thumbs_end()

    def scan_parameters(self):
        pnodes = {}
        for p in (_safe(lambda: list(self.des.userParameters)) or []):
            self.param_owner(p, pnodes)
        for p in (_safe(lambda: list(self.des.allParameters)) or []):
            owner = self.param_owner(p, pnodes)
            if owner is None:
                continue
            for q in (_safe(lambda: list(p.dependencyParameters)) or []):
                src = self.param_owner(q, pnodes)
                self.add_edge(src, owner, 'param')
        # keep every user parameter, and derived parameters that ended up linked
        used = set()
        for (s, t) in self.edges:
            used.add(s); used.add(t)
        for nid, node in pnodes.items():
            if nid in used or nid.startswith('p:'):
                self.nodes.append(node)

    def param_owner(self, p, pnodes):
        if _t(p) == 'DerivedParameter':
            # a parameter brought in by a Derive feature: its own node, a child of that Derive feature
            name = _safe(lambda: p.name, '') or ''
            nid = 'd:' + name
            if nid not in pnodes:
                df = _safe(lambda: p.deriveFeature)
                src = self.tl_node(df) if df else None
                srcn = next((n for n in self.nodes if n['id'] == src), None) if src else None
                k = sum(1 for x in pnodes if x.startswith('d:'))
                src_name = _safe(lambda: df.timelineObject.name, '') if df else ''
                pnodes[nid] = {'id': nid, 'name': name, 'type': 'DerivedParameter', 'cat': 'param', 'tl': None,
                               # ordered right after its Derive feature, so it sits below it in the graph
                               'o': (srcn['o'] + 0.001 * (k + 1)) if srcn else -1, 'g': [], 'supp': False, 'health': 0,
                               'msg': '', 'info': '= ' + (_safe(lambda: p.expression, '') or '') + (' · from ' + src_name if src_name else '')}
                if src:
                    self.add_edge(src, nid, 'param')
            return nid
        if _t(p) == 'UserParameter':
            nid = 'p:' + p.name
            if nid not in pnodes:
                pnodes[nid] = {'id': nid, 'name': p.name, 'type': 'UserParameter', 'cat': 'param', 'tl': None,
                               'o': -1, 'g': [], 'supp': False, 'health': 0, 'msg': '',
                               'info': '= ' + (_safe(lambda: p.expression, '') or '')}
            return nid
        cb = _safe(lambda: p.createdBy)
        if cb is None:
            return None
        return self.tl_node(_safe(lambda: cb.nativeObject) or cb)

    # --------------------------------------------------- suppression test ---
    def suppression_test(self, progress, cancelled):
        tl = self.tl
        orig = {}
        for i in range(tl.count):
            it = tl.item(i)
            if not it.isGroup:
                orig[i] = it.isSuppressed
        vol0 = self.body_signature()
        desc = {}
        brk = {}
        fails = {}
        ERR = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
        WARN = adsk.fusion.FeatureHealthStates.WarningFeatureHealthState
        err0 = set(i for i in orig if _safe(lambda: tl.item(i).healthState, 0) == ERR)
        warn0 = set(i for i in orig if _safe(lambda: tl.item(i).healthState, 0) == WARN)
        wrn = {}
        items = [i for i in orig if not orig[i]]
        for k, i in enumerate(items):
            if cancelled():
                self.warnings.append('Suppression test was cancelled; results are partial.')
                break
            it = tl.item(i)
            progress(it.name, k, len(items))
            fail_msg = None
            try:
                it.isSuppressed = True
            except Exception as ex:
                # Fusion raises when later features fail to compute, but usually suppresses anyway
                fail_msg = str(ex)
            nid = self.tl2node.get(i)
            if not _safe(lambda: tl.item(i).isSuppressed, False):
                if nid:
                    fails[nid] = self._parse_fail(fail_msg)
                self._restore_checked(orig, err0, None, it.name)
                tl = self.tl
                continue
            casc = [j for j in orig if j != i and not orig[j] and _safe(lambda: tl.item(j).isSuppressed, False)]
            broke = [j for j in orig if j != i and not orig[j] and j not in err0 and j not in casc
                     and _safe(lambda: tl.item(j).healthState, 0) == ERR]
            warned = [j for j in orig if j != i and not orig[j] and j not in warn0 and j not in casc
                      and _safe(lambda: tl.item(j).healthState, 0) == WARN]
            if nid:
                desc[nid] = [self.tl2node[j] for j in casc if j in self.tl2node]
                brk[nid] = [self.tl2node[j] for j in broke if j in self.tl2node]
                wrn[nid] = [self.tl2node[j] for j in warned if j in self.tl2node]
            name = _safe(lambda: it.name, '')
            self._restore_checked(orig, err0, i, name)
            tl = self.tl
        vol1 = self.body_signature()
        if vol0 != vol1:
            self.warnings.append('Warning: after the suppression test the bodies differ from before '
                                 '(%s vs %s). Check the design, or revert to the saved version.' % (vol0, vol1))
        # direct edges via transitive reduction
        anc = {}
        for s, ds in desc.items():
            for d in ds:
                anc.setdefault(d, set()).add(s)
        for d, ancs in anc.items():
            for s in ancs:
                # s -> d is direct if no other ancestor k of d has s as its ancestor
                if not any((s in anc.get(k, ())) for k in ancs if k != s):
                    self.add_edge(s, d, 'suppress')
        byid = {n['id']: n for n in self.nodes}
        for nid_, b in brk.items():
            if nid_ in byid:
                byid[nid_]['dbreak'] = b
        for nid_, w in wrn.items():
            if nid_ in byid and w:
                byid[nid_]['dwarn'] = w
        for nid_, f in fails.items():
            if nid_ in byid:
                byid[nid_]['fail'] = f
        tested = len(desc) + len(fails)
        if tested < len(items):
            self.warnings.append('Item suppression test: %d of %d items could not be tested.' % (len(items) - tested, len(items)))
        for s, ds in desc.items():
            if s in byid:
                byid[s]['dsupp'] = ds

    def _restore(self, orig, tested=None):
        """Put every item back to its state in orig. Switching the tested item (or group) back on
        brings its whole cascade back in one step. Rolling the marker back and switching items on
        one by one can make Fusion lose edge references in later features, so that is not used."""
        tl = self.tl
        if tested is not None:
            _safe(lambda: setattr(tl.item(tested), 'isSuppressed', False))
        for g in tl.timelineGroups:
            if _safe(lambda: g.isSuppressed):
                _safe(lambda: setattr(g, 'isSuppressed', False))
        for i in sorted(orig):
            if not orig[i] and _safe(lambda: tl.item(i).isSuppressed, False):
                _safe(lambda: setattr(tl.item(i), 'isSuppressed', False))
        # items that were suppressed before must stay suppressed (unsuppressing a group can wake them)
        for i in sorted(orig, reverse=True):
            if orig[i] and not _safe(lambda: tl.item(i).isSuppressed, True):
                _safe(lambda: setattr(tl.item(i), 'isSuppressed', True))
        _safe(lambda: tl.moveToEnd())

    def _clean(self, orig, err0):
        """True when every item is back in its original state and nothing new fails to compute."""
        tl = self.tl
        ERR = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
        for i in orig:
            if _safe(lambda: tl.item(i).isSuppressed, orig[i]) != orig[i]:
                return False
            if i not in err0 and not orig[i] and _safe(lambda: tl.item(i).healthState, 0) == ERR:
                return False
        return True

    def _recover(self):
        """Reopen the saved version (the run only starts on a saved design) and continue on it."""
        app = adsk.core.Application.get()
        doc = app.activeDocument
        df = _safe(lambda: doc.dataFile)
        if df is None:
            return False
        try:
            doc.close(False)
            nd = app.documents.open(df)
            _safe(nd.activate)
        except Exception:
            return False
        self.des = adsk.fusion.Design.cast(app.activeProduct)
        self.tl = self.des.timeline
        self.root = self.des.rootComponent
        self.expand_groups()
        self.recovered = getattr(self, 'recovered', 0) + 1
        return True

    def _restore_checked(self, orig, err0, tested=None, what=''):
        self._restore(orig, tested)
        if self._clean(orig, err0):
            return True
        self._restore(orig)
        if self._clean(orig, err0):
            return True
        if self._recover() and self._clean(orig, err0):
            return True
        self.warnings.append('Could not put the design back after testing %s; later results may be wrong.' % what)
        return False

    def group_suppression_test(self, progress, cancelled):
        """Suppress each timeline group as a whole and record which items outside it Fusion suppresses too."""
        tl = self.tl
        orig = {}
        for i in range(tl.count):
            it = tl.item(i)
            if not it.isGroup:
                orig[i] = it.isSuppressed
        node_tl = {n['id']: n['tl'] for n in self.nodes if n.get('tl') is not None}
        members = {}
        for n in self.nodes:
            for gid in n.get('g') or []:
                members.setdefault(gid, set()).add(n['tl'])
        tgroups = _safe(lambda: list(tl.timelineGroups)) or []
        vol0 = self.body_signature()
        byg = {g['id']: g for g in self.groups}
        err0 = set(i for i in orig if _safe(lambda: tl.item(i).healthState, 0) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState)
        WARN = adsk.fusion.FeatureHealthStates.WarningFeatureHealthState
        warn0 = set(i for i in orig if _safe(lambda: tl.item(i).healthState, 0) == WARN)
        for j in range(len(tgroups)):
            g = tgroups[j]      # re-read: the list is replaced if the design had to be reopened
            gid = 'G%d' % j
            if cancelled():
                self.warnings.append('Group suppression test was cancelled; results are partial.')
                break
            progress(_safe(lambda: g.name) or gid, j, len(tgroups))
            inside = members.get(gid, set())
            if not inside:
                if gid in byg:
                    byg[gid]['empty'] = True
                continue
            if _safe(lambda: g.isSuppressed):
                continue
            if all(orig.get(i) for i in inside):
                continue     # everything in it is already suppressed
            fail_msg = None
            try:
                g.isSuppressed = True
            except Exception as ex:
                # Fusion reports downstream compute failures as an error; see below.
                fail_msg = str(ex)
            if not _safe(lambda: g.isSuppressed):
                # Fusion refused the group as a whole (a later feature failed to compute).
                # Suppress its items one by one instead, which is what happens in the UI.
                for i in sorted(inside, reverse=True):
                    if not orig.get(i):
                        try:
                            tl.item(i).isSuppressed = True
                        except Exception:
                            pass
                if not all(_safe(lambda: tl.item(i).isSuppressed, False) for i in inside):
                    # Fusion rolls the suppression back because a later feature fails to compute.
                    if gid in byg:
                        byg[gid]['fail'] = self._parse_fail(fail_msg)
                    self._restore_checked(orig, err0, None, _safe(lambda: g.name, gid))
                    tl = self.tl
                    tgroups = _safe(lambda: list(tl.timelineGroups)) or tgroups
                    continue
            casc = [i for i in orig if i not in inside and not orig[i] and _safe(lambda: tl.item(i).isSuppressed)]
            broke = [i for i in orig if i not in inside and not orig[i] and i not in err0
                     and not _safe(lambda: tl.item(i).isSuppressed, True)
                     and _safe(lambda: tl.item(i).healthState, 0) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState]
            if gid in byg:
                byg[gid]['dsupp'] = [self.tl2node[i] for i in casc if i in self.tl2node]
                byg[gid]['dbreak'] = [self.tl2node[i] for i in broke if i in self.tl2node]
                warned = [i for i in orig if i not in inside and not orig[i] and i not in warn0
                          and not _safe(lambda: tl.item(i).isSuppressed, True)
                          and _safe(lambda: tl.item(i).healthState, 0) == WARN]
                if warned:
                    byg[gid]['dwarn'] = [self.tl2node[i] for i in warned if i in self.tl2node]
            gname = _safe(lambda: g.name, gid)
            _safe(lambda: setattr(g, 'isSuppressed', False))
            self._restore_checked(orig, err0, None, gname)
            if self.tl is not tl:
                tl = self.tl
                tgroups = _safe(lambda: list(tl.timelineGroups)) or tgroups
        self.gtested = True
        vol1 = self.body_signature()
        if vol0 != vol1:
            self.warnings.append('Warning: after the group suppression test the bodies differ from before '
                                 '(%s vs %s). Check the design, or revert to the saved version.' % (vol0, vol1))

    def _parse_fail(self, msg):
        """'3 : Feature_Name / Compute Failed // DETAIL - text' -> {'node', 'name', 'msg'}"""
        msg = msg or ''
        body = re.sub(r'^\s*\d+\s*:\s*', '', msg)
        name = body.split(' / ', 1)[0].strip() if ' / ' in body else ''
        node = None
        for n in self.nodes:
            if name and n['name'].strip() == name:
                node = n['id']
                break
        detail = body.split(' / ', 1)[1] if ' / ' in body else body
        detail = re.sub(r'\s+', ' ', detail).strip()
        return {'node': node, 'name': name, 'msg': detail[:300]}

    def body_signature(self):
        sig = []
        for c in self.des.allComponents:
            for b in c.bRepBodies:
                sig.append((b.name, round(_safe(lambda: b.volume, 0) * 1000, 1)))
        return sorted(sig)

    def result(self, doc_name, exact):
        edges = [{'s': s, 't': t, 'k': sorted(k)} for (s, t), k in self.edges.items()]
        return {'meta': {'doc': doc_name, 'exact': exact, 'gtest': getattr(self, 'gtested', False), 'warnings': self.warnings,
                         'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')},
                'nodes': self.nodes, 'groups': self.groups, 'edges': edges, 'thumbs': self.thumbs}


# ------------------------------------------------------------------- run ---

def generate(mode='both', thumbs=True):
    exact = mode in ('items', 'both')
    groups_test = mode in ('groups', 'both')
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    progress_dlg = None
    try:
        des = adsk.fusion.Design.cast(_app.activeProduct)
        if not des:
            _ui.messageBox('Open a design (Design workspace) first.')
            return
        if des.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            _ui.messageBox('Dependencies graph needs a parametric design (with a timeline).')
            return
        doc_name = _app.activeDocument.name


        out_dir = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph')
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, '%s_dependencies_graph_%s.html' % (
            re.sub(r'[^\w\- ]+', '_', doc_name).strip() or 'design',
            datetime.datetime.now().strftime('%Y%m%d_%H%M%S')))

        progress_dlg = _ui.createProgressDialog()
        progress_dlg.isCancelButtonShown = True
        progress_dlg.show('Dependencies graph', 'Starting...', 0, 1000)

        # One bar for the whole run: every step gets a share of it sized by how long it usually takes.
        steps = []                      # [name, weight]
        cur = {'k': 0, 'base': 0.0}

        def set_step(k):
            total = sum(w for _, w in steps) or 1.0
            cur['k'] = k
            cur['base'] = sum(w for _, w in steps[:k]) / total
            cur['span'] = steps[k][1] / total

        def progress(msg, i, n):
            frac = cur['base'] + cur.get('span', 0) * min(1.0, i / max(1, n))
            # %p is filled in by Fusion with the bar's percentage
            progress_dlg.message = ('%%p%%  ·  step %d of %d: %s  (%d/%d)\n%s' % (
                cur['k'] + 1, len(steps), steps[cur['k']][0] if steps else '', min(i + 1, n), n,
                msg.replace('%', ' percent')))[:200]
            progress_dlg.progressValue = int(1000 * frac)
            adsk.doEvents()

        def cancelled():
            adsk.doEvents()
            return progress_dlg.wasCancelled

        tl = des.timeline
        marker0 = tl.markerPosition
        at_end = marker0 >= tl.count
        col = Collector(des, progress, thumbs)
        col.expand_groups()
        n_tl = tl.count
        n_groups = len(_safe(lambda: list(tl.timelineGroups)) or [])
        steps.append(['Reading references' + (' and thumbnails' if thumbs else ''), n_tl * (0.6 if thumbs else 0.05)])
        if groups_test:
            steps.append(['Whole groups test', n_groups * 2.5])
        if exact:
            steps.append(['Every item test', n_tl * 1.7])
        t0 = time.time()
        try:
            set_step(0)
            col.build_nodes()
            col.scan()
            _safe(col.scan_components)
            col.scan_parameters()
            k = 1
            if groups_test:
                set_step(k); k += 1
                col.group_suppression_test(progress, cancelled)
            if exact:
                set_step(k)
                col.suppression_test(progress, cancelled)
        finally:
            tl = col.tl
            if at_end:
                _safe(lambda: tl.moveToEnd())
            else:
                _safe(lambda: setattr(tl, 'markerPosition', marker0))
            if getattr(col, 'recovered', 0):
                col.warnings.append('The design was reopened from its saved version %d time(s) during the test, '
                                    'because switching a feature back on made Fusion lose references.' % col.recovered)
            col.restore_groups()
            _safe(col.thumbs_end)
        data = col.result(doc_name, exact)
        progress_dlg.hide()
        progress_dlg = None

        html = TEMPLATE.replace('/*__DATA__*/null', json.dumps(data).replace('</', '<\\/'))
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
        webbrowser.open(pathlib.Path(path).as_uri())
        return data['meta']['warnings']
    except Exception:
        if progress_dlg:
            _safe(lambda: progress_dlg.hide())
        if _ui:
            _ui.messageBox('Dependencies graph failed:\n{}'.format(traceback.format_exc()))



# ------------------------------------------------------------ add-in UI ---

CMD_ID = 'claudeDesignGraphCmd'
OLD_CMD_ID = 'claudeHistoryGraphCmd'
CMD_NAME = 'Dependencies Graph'
CMD_TIP = ('Export the timeline as an interactive dependency tree and graph (HTML). '
           'The page is saved to a temporary folder and opened in your browser.')
EVENT_ID = 'claudeDesignGraphRun'
WORKSPACE_ID = 'FusionSolidEnvironment'
TAB_ID = 'ManageTab'
OLD_TAB_ID = 'ToolsTab'
PANEL_ID = 'claudeDesignGraphPanel'   # own panel: changing the ADD-INS panel closes the
                                        # Scripts and Add-Ins dialog, which lives in that panel
OLD_PANEL_ID = 'SolidScriptsAddinsPanel'
_handlers = []
_custom_event = None
_state = {'go': False}


class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    """The dialog. All the work runs inside the command's preview: Fusion throws away everything
    a preview changed when the command ends, so timeline rolls, suppressions and visibility
    changes never reach the design."""
    def notify(self, args):
        try:
            cmd = args.command
            cmd.isRepeatable = False
            cmd.isOKButtonVisible = True
            cmd.okButtonText = 'Generate graph'
            cmd.cancelButtonText = 'Cancel'
            inputs = cmd.commandInputs
            _safe(lambda: cmd.setDialogInitialSize(470, 460))
            _safe(lambda: cmd.setDialogMinimumSize(420, 400))

            # --- what this does
            doc = _safe(lambda: _app.activeDocument)
            des = adsk.fusion.Design.cast(_safe(lambda: _app.activeProduct))
            n_items = _safe(lambda: des.timeline.count, 0) if des else 0
            n_groups = _safe(lambda: des.timeline.timelineGroups.count, 0) if des else 0
            inputs.addTextBoxCommandInput('hgInfo', '',
                '<b>%s</b><br>%s timeline entries, %s timeline groups<br><br>'
                'Builds an interactive dependency graph of the timeline and opens it in your browser.'
                % (_safe(lambda: doc.name, 'No design'), n_items, n_groups), 4, True)

            # --- options
            og = inputs.addGroupCommandInput('hgOpts', 'Options')
            og.isExpanded = True
            oc = og.children
            th = oc.addBoolValueInput('hgThumbs', 'Thumbnails', True, '', True)
            th.tooltip = 'A picture of every timeline step'
            th.tooltipDescription = ('Each item is photographed straight on (sketches, planes) or in a three-quarter '
                                     'view (3D features), zoomed to the item. Adds about 20 seconds on a large design.')
            # Deep analysis = both suppression tests (every item, then every timeline group)
            mins = max(1, int(round((n_items * 1.7 + n_groups * 2.5) / 60.0)))
            da = oc.addBoolValueInput('hgDeep', 'Deep analysis', True, '', True)
            da.tooltip = 'Ask Fusion what suppressing really does'
            da.tooltipDescription = ('Suppresses every timeline item, then every timeline group, one at a time, records '
                                     'what else Fusion suppresses or breaks, and puts everything back.')
            oc.addTextBoxCommandInput('hgDeepInfo', '', _deep_info(True, mins), 4, True)

            # --- saved-state check
            why = _unsaved_reason()
            if why:
                # The run moves the timeline marker, which marks the design as modified. Only allow it
                # on a saved design, so closing without saving afterwards can't lose real work.
                inputs.addTextBoxCommandInput('hgSaveFirst', '',
                    '<b><span style="color:#c0392b">%s</span></b><br>Save it (File &gt; Save), then open '
                    'Dependencies Graph again.' % why, 3, True)
            else:
                inputs.addTextBoxCommandInput('hgSavedNote', '',
                    '<span style="color:#6b6a64">Afterwards the saved version is reopened, so the design is left '
                    'exactly as it was.</span>', 2, True)

            for ev, h in ((cmd.inputChanged, _InputChangedHandler()),
                          (cmd.validateInputs, _ValidateHandler()),
                          (cmd.execute, _ExecuteHandler())):
                ev.add(h)
                _handlers.append(h)
        except Exception:
            _ui.messageBox('Dependencies graph failed:\n{}'.format(traceback.format_exc()))


def _deep_info(on, mins):
    if on:
        return ('<span style="color:#6b6a64">On: suppresses every item and every timeline group in turn, so every link '
                'is a real dependency and the page can preview suppressions. About %d min for this design.</span>' % mins)
    return ('<span style="color:#6b6a64">Off: uses only what each feature references (sketches, planes, bodies, faces, '
            'parameters). Takes seconds, but some real dependencies are missing and a few links may be wrong.</span>')


def _options(inputs):
    di = inputs.itemById('hgDeep')
    mode = 'both' if (di is None or di.value) else 'off'
    ti = inputs.itemById('hgThumbs')
    return mode, (bool(ti.value) if ti else True)


def _revert(doc):
    """Close the document without saving and open the same saved version again."""
    df = _safe(lambda: doc.dataFile)
    if df is None:
        return False
    try:
        doc.close(False)
    except Exception:
        return False
    try:
        nd = _app.documents.open(df)
        _safe(nd.activate)
        return True
    except Exception:
        _ui.messageBox('Dependencies graph closed the design to discard the timeline changes, but could not '
                       'reopen it. Open "%s" again from the Data Panel.' % _safe(lambda: df.name, 'it'),
                       'Dependencies graph')
        return True


def _unsaved_reason():
    doc = _safe(lambda: _app.activeDocument)
    if doc is None:
        return 'No design is open.'
    if not _safe(lambda: doc.isSaved, True):
        return 'This design has never been saved.'
    if _safe(lambda: doc.isModified, False):
        return 'This design has unsaved changes.'
    return None


class _InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        if args.input.id == 'hgDeep':
            info = args.inputs.itemById('hgDeepInfo')
            if info:
                des = adsk.fusion.Design.cast(_safe(lambda: _app.activeProduct))
                n_items = _safe(lambda: des.timeline.count, 0) if des else 0
                n_groups = _safe(lambda: des.timeline.timelineGroups.count, 0) if des else 0
                mins = max(1, int(round((n_items * 1.7 + n_groups * 2.5) / 60.0)))
                info.formattedText = _deep_info(bool(args.input.value), mins)


class _ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        # Generate is only allowed on a saved design (the run is undone by reopening the saved version)
        args.areInputsValid = _unsaved_reason() is None


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        mode, thumbs = _options(args.command.commandInputs)
        # run after the dialog has closed, outside the command
        _app.fireCustomEvent(EVENT_ID, json.dumps({'mode': mode, 'thumbs': thumbs}))


class _RunHandler(adsk.core.CustomEventHandler):
    """Runs the scan, opens the page, then reopens the saved version so the design shows no changes."""
    def notify(self, args):
        try:
            opts = json.loads(args.additionalInfo or '{}')
        except Exception:
            opts = {}
        try:
            if _unsaved_reason():
                return
            doc = _app.activeDocument
            w = list(generate(opts.get('mode', 'both'), bool(opts.get('thumbs', True))) or [])
            doc = _app.activeDocument      # the test may have reopened the design
            if _safe(lambda: doc.isModified):
                # The design was saved when the run started and nothing else could edit it during the run,
                # so everything that marks it modified came from the run itself (marker moves etc.).
                if not _revert(doc):
                    w.append('Fusion marks the design as modified because the timeline marker was moved, '
                             'and reopening the saved version did not work. Nothing in the design was '
                             'changed; you can close it without saving.')
            if w:
                _ui.messageBox('Dependencies graph opened in your browser, with warnings:\n\n' +
                               '\n'.join(w[:15]), 'Dependencies graph')
        except Exception:
            _ui.messageBox('Dependencies graph failed:\n{}'.format(traceback.format_exc()))


def _tab():
    ws = _ui.workspaces.itemById(WORKSPACE_ID)
    return ws.toolbarTabs.itemById(TAB_ID) if ws else None


def _panel(create=False):
    tab = _tab()
    if not tab:
        return None
    panel = tab.toolbarPanels.itemById(PANEL_ID)
    if panel is None and create:
        panel = tab.toolbarPanels.add(PANEL_ID, 'DEPENDENCIES GRAPH', '', False)
    return panel


def _cleanup_ui(remove_panel=False):
    # earlier versions put the button in the ADD-INS panel
    old = _safe(lambda: _ui.allToolbarPanels.itemById(OLD_PANEL_ID))
    ctrl = _safe(lambda: old.controls.itemById(CMD_ID)) if old else None
    if ctrl:
        _safe(ctrl.deleteMe)
    # the previous version had its own panel on the Utilities tab; Fusion remembers where a
    # panel id was placed, so that id is retired and removed wherever it still is
    for old_pid in ('claudeHistoryGraphPanel', 'claudeHistoryGraphManagePanel'):
        op = _safe(lambda: _ui.allToolbarPanels.itemById(old_pid))
        if op:
            for c in (_safe(lambda: list(op.controls)) or []):
                _safe(c.deleteMe)
            _safe(op.deleteMe)
    ocd = _safe(lambda: _ui.commandDefinitions.itemById(OLD_CMD_ID))
    if ocd:
        _safe(ocd.deleteMe)
    opanel = None
    if opanel:
        c = _safe(lambda: opanel.controls.itemById(CMD_ID))
        if c:
            _safe(c.deleteMe)
        _safe(opanel.deleteMe)
    panel = _safe(_panel)
    ctrl = _safe(lambda: panel.controls.itemById(CMD_ID)) if panel else None
    if ctrl:
        _safe(ctrl.deleteMe)
    if remove_panel and panel:
        _safe(panel.deleteMe)
    cd = _safe(lambda: _ui.commandDefinitions.itemById(CMD_ID))
    if cd:
        _safe(cd.deleteMe)


def _build_ui():
    """Creates the command and the toolbar button."""
    global _custom_event
    _cleanup_ui()
    res = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'DependenciesGraph')
    cd = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TIP, res)
    on_created = _CreatedHandler()
    cd.commandCreated.add(on_created)
    _handlers.append(on_created)
    _safe(lambda: _app.unregisterCustomEvent(EVENT_ID))
    _custom_event = _app.registerCustomEvent(EVENT_ID)
    on_run = _RunHandler()
    _custom_event.add(on_run)
    _handlers.append(on_run)
    panel = _panel(create=True)
    if panel:
        ctrl = panel.controls.addCommand(cd)
        ctrl.isPromoted = True
        ctrl.isPromotedByDefault = True


def run(context):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        _build_ui()
    except Exception:
        if _ui:
            _ui.messageBox('Dependencies graph add-in failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _custom_event
    try:
        _cleanup_ui(remove_panel=True)
        if _custom_event:
            _safe(lambda: _app.unregisterCustomEvent(EVENT_ID))
            _custom_event = None
        _handlers.clear()
    except Exception:
        pass


TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dependencies graph</title>
<style>
:root{
  --bg:#f7f7f5;--panel:#ffffff;--panel2:#f1efe8;--text:#1f1f1d;--muted:#6b6a64;--border:#d9d7cf;--accent:#185fa5;
  --up:#185fa5;--down:#1f8a4c;--sel:#7a3fd1;--hov:#c9950c;--err:#c0392b;--warn:#b7791f;--edge:#b4b2a9;
  --c-sketch:#0f6e56;--c-sketch-bg:#e1f5ee;--c-construct:#5f5e5a;--c-construct-bg:#f1efe8;
  --c-solid:#185fa5;--c-solid-bg:#e6f1fb;--c-finish:#854f0b;--c-finish-bg:#faeeda;
  --c-offset:#534ab7;--c-offset-bg:#eeedfe;--c-hole:#993c1d;--c-hole-bg:#faece7;
  --c-body:#993556;--c-body-bg:#fbeaf0;--c-param:#3b6d11;--c-param-bg:#eaf3de;
  --c-other:#444441;--c-other-bg:#ecebe6;--c-group:#444441;--c-group-bg:#e4e2da;
  --supp:#8a8a86;--supp-bg:#dcdcd8;
  --c-joint:#c2410c;--c-joint-bg:#fdebe0;--c-insert:#be185d;--c-insert-bg:#fce6f0;--c-surface:#0e7490;--c-surface-bg:#dff3f7;
  --c-sheet:#475569;--c-sheet-bg:#e6eaef;--c-component:#9a6700;--c-component-bg:#fdf3d7;--c-form:#a21caf;--c-form-bg:#f8e5fa;--c-mesh:#65730f;--c-mesh-bg:#eff3d8;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#1b1b1a;--panel:#242422;--panel2:#2d2d2a;--text:#ecebe6;--muted:#a3a19a;--border:#3d3c38;--accent:#85b7eb;
  --up:#85b7eb;--down:#6fd39a;--sel:#afa9ec;--hov:#f2c14e;--err:#f09595;--warn:#ef9f27;--edge:#5f5e5a;
  --c-sketch:#9fe1cb;--c-sketch-bg:#08352c;--c-construct:#d3d1c7;--c-construct-bg:#34332f;
  --c-solid:#b5d4f4;--c-solid-bg:#0c2f52;--c-finish:#fac775;--c-finish-bg:#3d2a07;
  --c-offset:#cecbf6;--c-offset-bg:#26215c;--c-hole:#f5c4b3;--c-hole-bg:#4a1b0c;
  --c-body:#f4c0d1;--c-body-bg:#4b1528;--c-param:#c0dd97;--c-param-bg:#173404;
  --c-other:#d3d1c7;--c-other-bg:#2c2c2a;--c-group:#ecebe6;--c-group-bg:#3a3935;
  --supp:#7c7b77;--supp-bg:#3a3a38;
  --c-joint:#fdba8c;--c-joint-bg:#4a1c06;--c-insert:#f9a8d4;--c-insert-bg:#4a0a2a;--c-surface:#86d8ea;--c-surface-bg:#07313b;
  --c-sheet:#cbd5e1;--c-sheet-bg:#27303d;--c-component:#f5c94c;--c-component-bg:#382800;--c-form:#f0abfc;--c-form-bg:#3b0a42;--c-mesh:#d6e48c;--c-mesh-bg:#283005;
}}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:10px 14px;border-bottom:1px solid var(--border);background:var(--panel)}
header h1{font-size:16px;font-weight:600;margin:0;white-space:nowrap}
header .hl{display:flex;flex-direction:column;min-width:0;margin-right:6px}
header .hl{max-width:300px}header .hl .meta{font-size:11px;line-height:1.3}
.seg{display:inline-flex;align-items:stretch;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--panel)}
.seg .cap{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);padding:0 8px;display:flex;align-items:center;background:var(--panel2);border-right:1px solid var(--border)}
.seg button{border:0;border-radius:0;padding:5px 11px}
.seg button:disabled{opacity:.35;cursor:default}
#hBack,#hNext{font-size:18px;line-height:1;padding:3px 11px}.seg button+button{border-left:1px solid var(--border)}
.seg select{border:0;border-radius:0;padding:4px 8px;background:var(--panel)}
.srch{display:flex;align-items:center;gap:6px;flex:0 1 260px;min-width:160px}.srch input{flex:1;width:auto;min-width:120px}
.tools{display:flex;gap:6px;align-items:center;margin-left:auto}
.pop{position:relative}.popbox{display:none;position:absolute;right:0;top:calc(100% + 6px);z-index:30;min-width:220px;background:var(--panel);border:1px solid var(--border);border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.16);padding:8px 12px}
.pop.open .popbox{display:block}.pop.open>button{border-color:var(--accent)}
.popbox .ph{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:6px 0 4px}
.popbox label.chk{display:flex;padding:3px 0;font-size:13px;color:var(--text)}
#cats label.chk{align-items:center;gap:6px;white-space:nowrap}#cats .pill{font-size:12px}#cats .cn{margin-left:auto;padding-left:12px;font-size:11px;color:var(--muted)}
.fbtns{display:flex;gap:6px;margin:8px 0 4px}.fbtns button{padding:2px 10px;font-size:12px}.fnote{font-size:11px;max-width:240px;margin-top:4px}
svg .edge.bridge{stroke-dasharray:1.5 4}
#kinds{display:flex;flex-direction:column}
.gtools .sep{width:1px;align-self:stretch;background:var(--border);margin:0 2px}
header .meta{color:var(--muted);font-size:12px}
.ctrl{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
button,select,input[type=search]{font:inherit;font-size:13px;color:var(--text);background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:4px 9px}
button{cursor:pointer}button:hover{border-color:var(--muted)}
button.on{background:var(--accent);color:var(--panel);border-color:var(--accent)}
input[type=search]{width:220px}
label.chk{font-size:12px;color:var(--muted);display:inline-flex;align-items:center;gap:4px;cursor:pointer}
body{display:flex;flex-direction:column}
header,#simBar{flex:none}
main{display:flex;flex:1 1 auto;min-height:0}
#left{flex:1;min-width:0;position:relative;overflow:hidden}
#details{width:360px;max-width:40%;border-left:1px solid var(--border);background:var(--panel);overflow-y:auto;overflow-x:hidden;padding:12px 14px}
#details h2,#details .name{overflow-wrap:anywhere}
body.nosel #details{display:none}
#gpanel{position:absolute;left:10px;top:10px;width:300px;max-height:calc(100% - 20px);display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--border);border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.1);z-index:5}
#gpanel .gph{display:flex;align-items:center;gap:6px;padding:6px 8px;border-bottom:1px solid var(--border);font-size:13px}
#gpanel .gph button{margin-left:auto;padding:0 8px}
#gpanel.min #gpList{display:none}#gpanel.min .gph{border-bottom:0}
#gpList{overflow:auto;padding:4px 0}
.gprow{display:flex;align-items:center;gap:6px;padding:3px 8px;font-size:12px;cursor:pointer;white-space:nowrap}
.gprow:hover{background:var(--panel2)}.gprow.sel{background:var(--panel2);box-shadow:inset 3px 0 0 var(--sel)}
.gprow .gn{overflow:hidden;text-overflow:ellipsis;font-weight:600;flex:1;min-width:0}
.gprow .gt{font-size:11px;color:var(--muted)}.gprow .gt.bad{color:var(--err)}.gprow .gt.ok{color:var(--c-sketch)}
.gprow.dim .gn{opacity:.5;text-decoration:line-through}
#info{position:fixed;left:12px;right:12px;bottom:12px;z-index:15;max-height:38vh;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.18);padding:12px 44px 12px 16px;display:none}
body.infoopen #info{display:block}
#legend{position:fixed;right:12px;top:64px;bottom:12px;width:min(560px,calc(100vw - 24px));z-index:16;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.22);padding:12px 16px 20px;display:none}
body.legendopen #legend{display:block}
#legend .x{position:absolute;top:8px;right:8px;width:28px;height:28px;padding:0;font-size:16px;line-height:1}
#legend h2{font-size:16px;margin:0 0 4px}#legend h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:18px 0 6px;border-bottom:1px solid var(--border);padding-bottom:4px}
#legend .lgintro{margin-bottom:6px}
.lgrow{display:flex;gap:12px;align-items:center;padding:5px 0}.lgrow .lgsw{flex:none;width:118px;display:flex;align-items:center;gap:6px;white-space:nowrap;flex-wrap:wrap}
.lgrow b{font-size:13px}.lgrow .kv{font-size:12px;color:var(--muted)}
.pill.lgk{background:var(--panel2);color:var(--text)}
.lgbtn{display:inline-flex;align-items:center;gap:5px;font-weight:600;border-radius:14px;padding:2px 8px;font-size:12px;pointer-events:none}
.lgbtn.hb{color:var(--err);border-color:var(--err)}.lgbtn.hw{color:var(--warn);border-color:var(--warn)}
.lgbtn .ic{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;border-radius:50%;background:var(--err);color:#fff;font-size:10px;font-weight:800}
.lgbtn.hw .ic{background:none;border-radius:0;width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-bottom:12px solid var(--warn)}
.lgbtn small{font-weight:400;color:var(--muted)}
#info .x{position:absolute;top:8px;right:8px;width:28px;height:28px;padding:0;font-size:16px;line-height:1}
#info h2{font-size:14px;margin:0 0 4px}
#info .cols{display:flex;gap:24px;flex-wrap:wrap}#info .cols>div{flex:1 1 320px;min-width:0}
#details li{min-width:0}
#tree{position:absolute;inset:0;overflow:auto;padding:8px 10px 40px}
#graphwrap{position:absolute;inset:0;display:none}
#graph{width:100%;height:100%;cursor:grab;user-select:none}
#graph.drag{cursor:grabbing}
.row{display:flex;align-items:center;gap:6px;padding:2px 4px;border-radius:5px;white-space:nowrap}
.row:hover{background:var(--panel2)}
.row.sel{outline:2px solid var(--sel);outline-offset:-2px}
.tog{width:16px;text-align:center;color:var(--muted);cursor:pointer;flex:none;font-size:11px}
.tog.empty{visibility:hidden}
.kids{margin-left:18px;border-left:1px dotted var(--border);padding-left:4px}
.name{cursor:pointer}
.name:hover{text-decoration:underline}
.pill{font-size:11px;padding:0 6px;border-radius:9px;border:1px solid transparent;flex:none}
.cnt{font-size:11px;color:var(--muted)}
.grp{font-weight:600}
.lbl{font-size:12px;color:var(--muted);font-style:italic}
.st-sup{color:var(--supp)!important;text-decoration:line-through}
img.thumb.supp,svg .suppimg{filter:grayscale(1);opacity:.55}
.st-warn{color:var(--warn)}.st-err{color:var(--err)}
.st-est{font-style:italic}
.simtog{font-size:11px;padding:0 6px;line-height:18px;border-radius:9px;min-width:34px;cursor:pointer;background:var(--panel);color:var(--muted)}
.simtog.off{background:var(--err);border-color:var(--err);color:#fff}
.simb{font-size:11px}.simb.s{color:var(--muted)}.simb.b{color:var(--err)}
#simBar{display:none;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 14px;border-bottom:1px solid var(--border);background:var(--panel2);font-size:13px}
#simBar b{font-weight:600}
body.simon #simBar{display:flex}
.hit{background:#fff3b0;color:#000;border-radius:3px}
@media (prefers-color-scheme: dark){.hit{background:#6b5a00;color:#fff}}
#details h2{font-size:15px;margin:0 0 4px}
#details h3{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:14px 0 4px}
#details .kv{font-size:12px;color:var(--muted)}
#details ul{list-style:none;padding:0;margin:0}
#details li{padding:2px 0;font-size:13px;display:flex;gap:6px;align-items:baseline}
#details li .k{font-size:11px;color:var(--muted)}
#details .msg{font-size:12px;color:var(--err);white-space:pre-wrap;margin-top:6px}
.legend{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.hint{font-size:12px;color:var(--muted);margin-top:10px}
.gtools{position:absolute;right:10px;top:10px;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;align-items:center;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:4px 6px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.gtools button{background:var(--panel)}
.gtools button.on{background:var(--accent);color:var(--panel);border-color:var(--accent)}
img.thumb{width:40px;height:30px;object-fit:contain;border-radius:4px;border:1px solid var(--border);background:var(--panel2);flex:none;cursor:zoom-in}
body.nothumbs img.thumb{display:none}
#details img.big{display:block;width:100%;max-height:420px;object-fit:contain;border-radius:6px;border:1px solid var(--border);background:var(--panel2);margin:8px auto 4px}
#peek{position:fixed;z-index:20;pointer-events:none;display:none;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:6px;box-shadow:0 4px 16px rgba(0,0,0,.25)}
#peek img{display:block;max-width:360px;max-height:360px;border-radius:4px;background:var(--panel2)}
#peek div{font-size:12px;margin-top:4px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
svg text{font:12px system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--text)}
svg .nd rect{stroke-width:1}
svg .nd{cursor:pointer}
svg .edge{fill:none;stroke:var(--edge);stroke-width:1.1;pointer-events:none;stroke-linejoin:round;stroke-linecap:round}
svg .edge.order{stroke-dasharray:4 4}
svg .edge.contain{stroke-width:2.4;stroke-opacity:.85;stroke-linecap:round}
svg .edge.up{stroke:var(--up);stroke-width:1.8}
svg .edge.down{stroke:var(--down);stroke-width:1.8}
svg .dim{opacity:.18}
svg .edge.up:not(.ind),svg .edge.down:not(.ind){stroke-width:2.3}
svg .edge.insel{stroke:var(--sel);stroke-width:2.3}
svg .edge.ind{stroke-opacity:.3}   /* further ancestors and descendants (not direct) */
svg .ehit{fill:none;stroke:transparent;stroke-width:12;pointer-events:stroke;cursor:pointer}
svg .ehit.off{pointer-events:none}   /* links greyed out by the selection do not react */
svg .edge.hov{stroke-width:3.4!important;stroke-opacity:1!important;opacity:1!important}
svg .edge.hov{stroke:var(--hov)!important}   /* hovered link: gold, same colour as the rings on its ends */
svg .nd.hov{opacity:1!important}
svg .hovring{fill:none;stroke:var(--hov);stroke-width:5;pointer-events:none;transition:opacity .2s ease}
svg .edge{transition:stroke .2s ease,stroke-width .2s ease,stroke-opacity .2s ease,opacity .2s ease}
svg .nd{transition:opacity .2s ease}
svg .selglow{fill:var(--sel);fill-opacity:.2;stroke:var(--sel);stroke-opacity:.75;stroke-width:3;pointer-events:none}
svg .ctog{cursor:pointer}
svg .act{cursor:pointer}
svg .act rect{fill:var(--panel);stroke:var(--border);stroke-width:1}
svg .act path{fill:none;stroke:var(--muted);stroke-width:1.8;stroke-linecap:round}
svg .act:hover rect{stroke:var(--err)}svg .act:hover path{stroke:var(--err)}
svg .act.on rect{fill:var(--err);stroke:var(--err)}svg .act.on path{stroke:#fff}
svg .ctogc{fill:var(--panel);stroke:var(--muted);stroke-width:1}
svg .ctogc.col{fill:var(--accent);stroke:var(--accent)}
svg .ctog:hover .ctogc{stroke:var(--text)}
svg .ctogt{font-size:12px;font-weight:700;fill:var(--muted);pointer-events:none}
svg .ctogc.col+.ctogt{fill:var(--panel)}
svg .ctogn{font-size:11px;fill:var(--accent);pointer-events:none}
svg .gtoggle{font-weight:700}
.ico{vertical-align:-2px;flex:none}.pill .ico{margin-right:4px;vertical-align:-2px}.icw{display:inline-flex;margin-right:7px;vertical-align:-3px}svg .nico{pointer-events:none}
#legend .icgrid{display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;margin-top:6px}#legend .icgrid div{display:flex;gap:8px;align-items:center;font-size:12px;min-width:0}#legend .icgrid span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#health{display:flex;gap:6px;align-items:center}
#health button{display:inline-flex;align-items:center;gap:6px;font-weight:600;border-radius:14px;padding:3px 10px}
#health .hb{color:var(--err);border-color:var(--err)}#health .hw{color:var(--warn);border-color:var(--warn)}
#health .ic{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:var(--err);color:#fff;font-size:11px;font-weight:800}
#health .hw .ic{background:none;border-radius:0;width:0;height:0;border-left:8px solid transparent;border-right:8px solid transparent;border-bottom:14px solid var(--warn);position:relative}
#health .hw .ic::after{content:'!';position:absolute;left:-2px;top:2px;color:#fff;font-size:10px;font-weight:800}
#health small{font-weight:400;color:var(--muted)}
svg .brk circle{fill:var(--err);stroke:var(--panel);stroke-width:2}svg .brk text{fill:#fff;font-size:13px;font-weight:800}svg .brk.est circle{fill:var(--panel);stroke:var(--err);stroke-dasharray:3 2}svg .brk.est text{fill:var(--err)}
svg .wrn path{fill:var(--warn);stroke:var(--panel);stroke-width:2;stroke-linejoin:round}svg .wrn text{fill:#fff;font-size:12px;font-weight:800}.simb.w{color:var(--warn)}
/* history playback */
svg.playing .nd,svg.playing .edge{transition:none!important}
svg.playing #vp *{pointer-events:none!important}   /* no hover highlights or clicks on boxes/links while playing */
#pbCard{position:absolute;right:14px;bottom:18px;z-index:11;width:230px;display:none;background:var(--panel);border:1px solid var(--border);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.18);padding:6px;pointer-events:none}
body.pbon.pbthumbs #pbCard{display:block}
#pbCard img{display:block;width:100%;height:160px;object-fit:contain;border-radius:6px;background:var(--panel2)}
#pbCard .cap{font-size:12px;margin-top:6px}#pbCard .cap .ty{font-size:11px;color:var(--muted)}#pbCard .cap .nm{font-weight:600;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#pbCard .cap small{color:var(--muted);display:block;font-size:11px}
#pbCard.swap img,#pbCard.swap .cap{animation:pbswap .45s ease}
@keyframes pbswap{from{opacity:0;transform:scale(.97)}to{opacity:1;transform:none}}
svg .pblit{fill:none;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
svg .pblit.glow{stroke:var(--hov);stroke-opacity:.28}
svg .pblit.base{stroke:var(--hov);stroke-opacity:.35}
svg .pblit.trail{stroke:var(--hov)}
svg .pbhalo{fill:var(--up);fill-opacity:.22;pointer-events:none}
svg .pbcore{fill:var(--up);stroke:var(--panel);stroke-width:2;pointer-events:none;vector-effect:non-scaling-stroke}
svg .pbpulse{fill:none;stroke:var(--up);stroke-width:4;pointer-events:none;vector-effect:non-scaling-stroke}
#pbBar{position:absolute;left:50%;bottom:18px;transform:translateX(-50%);z-index:12;display:none;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--border);border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.18);padding:6px 8px 9px;max-width:calc(100% - 24px);overflow:hidden}
#pbBar button{min-width:36px;padding:4px 10px;font-size:14px;line-height:1.2}
#pbBar #pbStop{font-size:13px;white-space:nowrap}
#pbInfo{font-size:13px;padding:0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:420px;min-width:120px}
#pbTrack{position:absolute;left:0;right:0;bottom:0;height:3px;background:var(--panel2)}#pbProg{height:100%;width:0;background:var(--up);transition:width .3s ease}
#pbStart:disabled{opacity:.4;cursor:default}
body.pbon .gtools #pbStart{background:var(--accent);color:var(--panel);border-color:var(--accent)}
</style>
</head>
<body>
<header>
  <div class="hl"><h1 id="title">Dependencies graph</h1><div class="meta" id="meta"></div></div>
  <div class="seg"><button id="hBack" title="Back (Alt+←)">‹</button><button id="hNext" title="Forward (Alt+→)">›</button></div>
  <div class="seg"><span class="cap">View</span><button id="vGraph">Graph</button><button id="vTree" class="on">Tree</button></div>
  <div class="seg" id="layoutSeg"><span class="cap">Layout</span><button id="layDeps" title="Boxes arranged by dependency depth">Depth</button><button id="laneBtn" class="on" title="Boxes arranged by timeline group: one block per group, in a grid">Groups</button><button id="compBtn" title="Boxes arranged by component: one block per component, in a grid">Components</button></div>
  <div class="seg" id="treeCtrl"><span class="cap">Tree</span>
    <select id="treeMode">
      <option value="timeline">By timeline group</option>
      <option value="roots">From roots down</option>
      <option value="leaves">From end results up</option>
    </select>
  </div>
  <div class="srch"><input type="search" id="search" placeholder="Search features…"><span id="sNav" style="display:none;gap:4px;align-items:center"><span id="sCount" class="cnt"></span><button id="sPrev" title="Previous match (Shift+Enter)">‹</button><button id="sNext" title="Next match (Enter)">›</button></span></div>
  <div id="health" style="display:none"></div>
  <div class="tools">
    <div class="pop"><button id="filterBtn" title="Which kinds of items to show">Filter ▾</button>
      <div class="popbox" id="filterBox"><div class="ph">Show items</div><div id="cats"></div><div class="fbtns"><button id="catAll">All</button><button id="catNone">None</button></div><div class="kv fnote">Hidden items are skipped, not cut out: their links are joined through to the items they connect (dotted lines).</div></div></div>
    <div class="pop"><button id="dispBtn" title="Display options">Display ▾</button>
      <div class="popbox" id="dispBox"><div class="ph">Boxes</div><label class="chk" id="thumbCtrl" style="display:none"><input type="checkbox" id="showThumbs" checked> Thumbnails</label><label class="chk"><input type="checkbox" id="focus"> Only the selected branch</label><label class="chk" title="When something is selected, the boxes related to it move next to it (inside their block in the Groups and Components layouts)"><input type="checkbox" id="pullTog" checked> Move related boxes closer to the selection</label><div class="ph">Lines</div><label class="chk" title="Earlier features that changed the same body before this one. Timeline order, not a dependency: suppressing them does not suppress this."><input type="checkbox" id="showOrder"> “Same body, later” links (dashed)</label></div></div>
    <button id="legendBtn" title="What the colours, outlines, markers and lines mean">Legend</button>
    <button id="infoBtn" title="How to use, warnings">Info</button>
  </div>
</header>
<div id="simBar"></div>
<div id="peek"><img alt=""><div></div></div>
<main>
  <div id="left">
    <div id="tree"></div>
    <div id="graphwrap">
      <svg id="graph" xmlns="http://www.w3.org/2000/svg"><defs>
      </defs><g id="vp"></g></svg>
      <div id="gpanel"><div class="gph"><b>Timeline groups</b><span class="cnt" id="gpCount"></span><button id="gpToggle" title="Show/hide the group list">–</button></div><div id="gpList"></div></div>
      <div class="gtools">
        <button id="expAll" title="Expand all timeline groups">Expand all</button><button id="colAll" title="Collapse all timeline groups">Collapse all</button><span class="sep"></span><button id="pbStart" title="Play the whole history of the design (P)">▶ Play</button><span class="sep"></span><button id="fit" title="Fit the whole graph, or the selection and everything highlighted with it">Fit</button>
      </div>
      <div id="pbCard"><img alt=""><div class="cap"></div></div><div id="pbBar"><button id="pbPlay" title="Pause (Space)">❚❚</button><button id="pbNext" title="Skip to the next step (→)">⏭</button><button id="pbSpeed" title="Playback speed">1×</button><span id="pbInfo"></span><button id="pbStop" title="Stop (Esc)">■ Stop</button><div id="pbTrack"><div id="pbProg"></div></div></div>
    </div>
  </div>
  <aside id="details"></aside>
</main>
<div id="info"><button class="x" id="infoClose" title="Close">×</button><div id="infoBody"></div></div>
<div id="legend"><button class="x" id="legendClose" title="Close (Esc)">×</button><h2>Legend</h2><div id="legendBody"></div></div>
<script>
const D = /*__DATA__*/null;
(function(){
if(!D){document.body.innerHTML='<p style="padding:20px">No data embedded.</p>';return;}
const CAT={sketch:'Sketch',construct:'Construction',solid:'Solid feature',finish:'Chamfer / fillet',offset:'Offset / face',hole:'Hole / thread',body:'Body operation',param:'Parameter',component:'Component',surface:'Surface',sheet:'Sheet metal',form:'Form / base',mesh:'Mesh / volumetric',insert:'Insert / derive',joint:'Joint / motion',other:'Other'};
// ---------- icons: one small line icon per kind of item (original drawings, 16x16, current colour) ----------
const ICONS={
  sketch:'<rect x="2" y="3" width="10" height="10" rx="1" stroke-dasharray="2 1.5"/><path d="M8.5 10.5l5.5-5.5-1.5-1.5-5.5 5.5-.5 2z"/>',
  plane:'<path d="M1.5 11.5l3.5-7h9.5l-3.5 7z"/>',
  axis:'<path d="M2 14L14 2"/><path d="M11 2h3v3"/><circle cx="8" cy="8" r="1.2" fill="currentColor"/>',
  point:'<circle cx="8" cy="8" r="2" fill="currentColor"/><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3"/>',
  extrude:'<path d="M2.5 11.5l3-2h8l-3 2z"/><path d="M8 9V2.5M5.5 5L8 2.5 10.5 5"/>',
  revolve:'<path d="M8 1.5v13" stroke-dasharray="1.5 1.5"/><path d="M12.5 6.5A5 2.5 0 1 1 5 5.3"/><path d="M5 3.3l.3 2.1-2 .5"/>',
  sweep:'<path d="M2 13c3 0 3-9 8-9h4"/><rect x="1" y="10.5" width="3" height="3" fill="currentColor" stroke="none"/>',
  loft:'<rect x="2" y="10" width="5" height="4"/><circle cx="11.5" cy="4" r="2.5"/><path d="M7 10l2.8-4.3M2 10l7.7-6.8"/>',
  rib:'<path d="M2 13h12M2 13V4M2 4l10 9"/><path d="M4.5 13l5.5-5" stroke-width="2.4" stroke-opacity=".5"/>',
  box:'<path d="M2.5 5.5l5.5-3 5.5 3v6l-5.5 3-5.5-3z"/><path d="M2.5 5.5l5.5 3 5.5-3M8 8.5v6"/>',
  cylinder:'<ellipse cx="8" cy="4" rx="5" ry="2"/><path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4"/>',
  sphere:'<circle cx="8" cy="8" r="6"/><ellipse cx="8" cy="8" rx="6" ry="2.2"/>',
  torus:'<ellipse cx="8" cy="8" rx="6.5" ry="4"/><ellipse cx="8" cy="7.5" rx="2.5" ry="1.2"/>',
  coil:'<path d="M3 3h10M3 3c0 1.5 10 1.5 10 3S3 7.5 3 9s10 1.5 10 3S3 13.5 3 13"/>',
  pipe:'<path d="M2 12c0-5 3-8 9-8"/><path d="M2 12c0-5 3-8 9-8" stroke-width="4" stroke-opacity=".3"/><circle cx="12.5" cy="4" r="2"/>',
  emboss:'<path d="M1.5 13.5h13"/><path d="M4.5 12L8 3l3.5 9M6 9h4"/>',
  thicken:'<path d="M2 6c3-3 9-3 12 0"/><path d="M2 10c3-3 9-3 12 0"/><path d="M8 4.2v4" stroke-dasharray="1 1"/>',
  fill:'<path d="M2 3h12v10H2z" stroke-dasharray="2 1.5"/><path d="M4 5h8v6H4z" fill="currentColor" fill-opacity=".35"/>',
  fillet:'<path d="M2 14V7a5 5 0 0 1 5-5h7"/><path d="M2 14h12V2" stroke-opacity=".45"/>',
  chamfer:'<path d="M2 14V6l4-4h8"/><path d="M2 14h12V2" stroke-opacity=".45"/>',
  offsetface:'<path d="M2 12h12" /><path d="M2 8h12" stroke-dasharray="2 1.5"/><path d="M8 11.5V4.5M6 6.5L8 4.5l2 2"/>',
  shell:'<rect x="2" y="3" width="12" height="10" rx="1"/><rect x="4.5" y="3" width="7" height="7.5" rx=".5"/>',
  draft:'<path d="M3 14l2-11h6l2 11z"/><path d="M8 1.5v13" stroke-dasharray="1.5 1.5" stroke-opacity=".6"/>',
  deleteface:'<rect x="2" y="4" width="9" height="9" rx="1"/><path d="M10 2l4 4M14 2l-4 4"/>',
  replaceface:'<rect x="2" y="6" width="9" height="8" rx="1"/><path d="M5 3.5c2-2 6-2 8 1M13 1.5v3h-3"/>',
  splitface:'<rect x="2" y="3" width="12" height="10" rx="1"/><path d="M5 3l6 10" stroke-dasharray="2 1.2"/>',
  scale:'<rect x="2" y="7" width="7" height="7"/><path d="M8 8l6-6M10 2h4v4"/>',
  hole:'<rect x="2" y="2" width="12" height="12" rx="1.5"/><circle cx="8" cy="8" r="3"/><path d="M8 3.5v1.5M8 11v1.5M3.5 8H5M11 8h1.5" stroke-opacity=".5"/>',
  thread:'<path d="M4.5 2v12M11.5 2v12"/><path d="M4.5 4l7 2M4.5 7l7 2M4.5 10l7 2"/>',
  combine:'<rect x="1.5" y="4.5" width="8" height="8" rx="1"/><rect x="6.5" y="1.5" width="8" height="8" rx="1" fill="currentColor" fill-opacity=".3"/>',
  mirror:'<path d="M8 1.5v13" stroke-dasharray="1.5 1.5"/><path d="M6 4L2 12h4z"/><path d="M10 4l4 8h-4z" fill="currentColor" fill-opacity=".35"/>',
  rectpattern:'<rect x="2" y="2" width="4" height="4"/><rect x="10" y="2" width="4" height="4"/><rect x="2" y="10" width="4" height="4"/><rect x="10" y="10" width="4" height="4"/>',
  circpattern:'<circle cx="8" cy="8" r="1"/><circle cx="8" cy="2.8" r="1.6"/><circle cx="13" cy="9.5" r="1.6"/><circle cx="3" cy="9.5" r="1.6"/><circle cx="8" cy="13.4" r="1.2" stroke-opacity=".5"/>',
  pathpattern:'<path d="M1.5 13c4-1 5-9 13-10" stroke-dasharray="1.5 1.5"/><rect x="1" y="10.5" width="3" height="3"/><rect x="6.5" y="5.5" width="3" height="3"/><rect x="12" y="1.5" width="3" height="3"/>',
  move:'<path d="M8 1.5v13M1.5 8h13M6 3.5l2-2 2 2M6 12.5l2 2 2-2M3.5 6l-2 2 2 2M12.5 6l2 2-2 2"/>',
  splitbody:'<path d="M2 4h5.5v9H2zM9.5 3h4.5v9H9.5z"/><path d="M8.5 1.5v13" stroke-dasharray="1.5 1.2"/>',
  copy:'<rect x="1.5" y="4.5" width="8" height="10" rx="1"/><rect x="5.5" y="1.5" width="8" height="10" rx="1" fill="currentColor" fill-opacity=".15"/>',
  remove:'<rect x="2.5" y="3.5" width="11" height="11" rx="1" stroke-dasharray="2 1.5"/><path d="M5.5 6.5l5 5M10.5 6.5l-5 5"/>',
  param:'<path d="M5.5 13.5c1.2 0 1.5-1 1.8-3l1-6.5c.3-2 .8-2.5 2.2-2.5M4.5 6.5h5.5"/><path d="M10.5 9l3.5 4.5M14 9l-3.5 4.5"/>',
  dparam:'<path d="M4.5 13.5c1 0 1.3-1 1.5-3l.8-6c.3-2 .7-2.5 2-2.5M3.5 6.5h5"/><path d="M10 10.5a2 2 0 0 1 2-2h1a2 2 0 0 1 0 4M12 14.5h-1a2 2 0 0 1 0-4"/>',
  params:'<path d="M2 3.5h2M2 8h2M2 12.5h2M6 3.5h8M6 8h8M6 12.5h8"/>',
  component:'<path d="M2.5 5l5.5-3 5.5 3v6.5l-5.5 3-5.5-3z"/><path d="M2.5 5l5.5 3 5.5-3M8 8v6.5"/><path d="M5.2 3.5l5.5 3" stroke-opacity=".5"/>',
  insert:'<path d="M5 6l3.5-2 5 2.8v5.7l-5 2.5-3.5-2"/><path d="M1 9.5h6.5M5 7l2.5 2.5L5 12"/>',
  derive:'<path d="M2.5 5.5l4-2.2 4 2.2v4.7l-4 2.3-4-2.3z"/><path d="M10 11.5a2 2 0 0 1 2-2h1a2 2 0 0 1 0 4M12 15.5h-1a2 2 0 0 1 0-4" transform="translate(0 -1)"/>',
  joint:'<circle cx="4.5" cy="11.5" r="2.5"/><circle cx="11.5" cy="4.5" r="2.5"/><path d="M6.3 9.7l3.4-3.4"/><circle cx="8" cy="8" r=".9" fill="currentColor"/>',
  jointorigin:'<circle cx="8" cy="8" r="5.5"/><circle cx="8" cy="8" r="2"/><path d="M8 1v3M8 12v3M1 8h3M12 8h3"/>',
  rigid:'<rect x="3" y="7" width="10" height="7" rx="1"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/>',
  motion:'<circle cx="5" cy="9.5" r="3.5"/><circle cx="11.5" cy="5" r="2.5"/><path d="M5 6v-1M5 14v-1M1.5 9.5h-1M9.5 9.5h-1M11.5 2.5v-1M11.5 8.5v-1"/>',
  contact:'<circle cx="5.5" cy="8" r="4"/><circle cx="11.5" cy="8" r="3"/><path d="M9.5 5.5v5" stroke-opacity=".6"/>',
  snapshot:'<rect x="1.5" y="4.5" width="13" height="9" rx="1.5"/><circle cx="8" cy="9" r="2.5"/><path d="M5.5 4.5l1-2h3l1 2"/>',
  arrange:'<rect x="1.5" y="1.5" width="5.5" height="5.5"/><rect x="9" y="1.5" width="5.5" height="5.5"/><rect x="1.5" y="9" width="5.5" height="5.5"/><rect x="9" y="9" width="5.5" height="5.5" stroke-dasharray="1.5 1.2"/>',
  surface:'<path d="M1.5 11c2-4 4.5 1 6.5-3s4.5 1 6.5-3"/><path d="M1.5 11l1.5 3M14.5 5L13 8" stroke-opacity=".5"/>',
  patch:'<path d="M2 5c3-2 9-2 12 0v7c-3-2-9-2-12 0z" stroke-dasharray="2 1.5"/><path d="M4 6.5c2.5-1 5.5-1 8 0v4c-2.5-1-5.5-1-8 0z" fill="currentColor" fill-opacity=".3" stroke="none"/>',
  stitch:'<path d="M2 3h5v10H2zM9 3h5v10H9z"/><path d="M6 5.5h4M6 8h4M6 10.5h4"/>',
  trim:'<path d="M2 12c3-5 9-5 12 0"/><circle cx="4" cy="4" r="1.8"/><circle cx="4" cy="9" r="1.8" stroke-opacity="0"/><path d="M5.3 5.3L14 2M5.5 3.2L14 6"/>',
  extend:'<path d="M2 10c2-3 5-3 7-1"/><path d="M9 9c2 2 4 1 5-1" stroke-dasharray="1.5 1.5"/><path d="M11.5 5.5l2.5 2.5-3 1"/>',
  sheet:'<path d="M2 4.5h7.5a2 2 0 0 1 2 2v8"/><path d="M2 2.5h7.5a4 4 0 0 1 4 4v8"/>',
  flat:'<rect x="2" y="4" width="12" height="8"/><path d="M6 4v8M10 4v8" stroke-dasharray="1.5 1.2"/>',
  form:'<path d="M3 11c-1-4 2-8 6-8s5 3 4 6-3 5-6 4-3-1-4-2z"/><circle cx="6" cy="6" r=".8" fill="currentColor"/><circle cx="10.5" cy="5.5" r=".8" fill="currentColor"/><circle cx="9" cy="10.5" r=".8" fill="currentColor"/>',
  base:'<path d="M2.5 5l5.5-3 5.5 3v6.5l-5.5 3-5.5-3z" stroke-dasharray="2 1.3"/><path d="M8 5.5v6M5 8.5h6"/>',
  mesh:'<path d="M2 13L8 2l6 11z"/><path d="M5 7.5h6M8 13L5 7.5M8 13l3-5.5"/>',
  group:'<path d="M1.5 4a1 1 0 0 1 1-1h3.5l1.5 1.5h6a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z"/>',
  canvas:'<rect x="2" y="3" width="12" height="10" rx="1"/><path d="M3.5 11.5l3-4 2.5 3 1.5-1.5 2 2.5"/><circle cx="11" cy="6" r="1"/>',
  custom:'<path d="M8 1.5l1.6 3.3 3.6.5-2.6 2.5.6 3.6L8 9.7l-3.2 1.7.6-3.6-2.6-2.5 3.6-.5z"/>',
  other:'<circle cx="8" cy="8" r="5.5" stroke-dasharray="2 1.5"/><circle cx="8" cy="8" r="1.2" fill="currentColor"/>',
};
const ICON_BY_TYPE={Sketch:'sketch',ConstructionPlane:'plane',ConstructionAxis:'axis',ConstructionPoint:'point',
  ExtrudeFeature:'extrude',RevolveFeature:'revolve',SweepFeature:'sweep',LoftFeature:'loft',RibFeature:'rib',WebFeature:'rib',
  BoxFeature:'box',CylinderFeature:'cylinder',SphereFeature:'sphere',TorusFeature:'torus',CoilFeature:'coil',PipeFeature:'pipe',
  EmbossFeature:'emboss',BoundaryFillFeature:'fill',ThickenFeature:'thicken',BossFeature:'cylinder',
  FilletFeature:'fillet',RuleFilletFeature:'fillet',FullRoundFilletFeature:'fillet',ChamferFeature:'chamfer',
  OffsetFacesFeature:'offsetface',ShellFeature:'shell',DraftFeature:'draft',DeleteFaceFeature:'deleteface',ReplaceFaceFeature:'replaceface',
  SplitFaceFeature:'splitface',ScaleFeature:'scale',HoleFeature:'hole',ThreadFeature:'thread',
  CombineFeature:'combine',MirrorFeature:'mirror',RectangularPatternFeature:'rectpattern',CircularPatternFeature:'circpattern',
  PathPatternFeature:'pathpattern',MoveFeature:'move',SplitBodyFeature:'splitbody',SilhouetteSplitFeature:'splitbody',
  CopyPasteBody:'copy',CutPasteBody:'copy',RemoveFeature:'remove',
  UserParameter:'param',DerivedParameter:'dparam',UserParameters:'params',Component:'component',
  Occurrence:'insert',DeriveFeature:'derive',Joint:'joint',AsBuiltJoint:'joint',JointOrigin:'jointorigin',RigidGroup:'rigid',
  MotionLink:'motion',ContactSet:'contact',Snapshot:'snapshot',ArrangeFeature:'arrange',
  PatchFeature:'patch',StitchFeature:'stitch',UnstitchFeature:'stitch',TrimFeature:'trim',UntrimFeature:'trim',ExtendFeature:'extend',
  OffsetFeature:'surface',RuledSurfaceFeature:'surface',ReverseNormalFeature:'surface',SurfaceDeleteFaceFeature:'deleteface',
  FlangeFeature:'sheet',HemFeature:'sheet',RipFeature:'sheet',CornerClosureFeature:'sheet',FoldFeature:'sheet',UnfoldFeature:'flat',
  RefoldFeature:'sheet',JoinByBendFeature:'sheet',LoftedFlangeFeature:'sheet',SheetMetalChamferFeature:'chamfer',SheetMetalFilletFeature:'fillet',
  FlatPattern:'flat',FormFeature:'form',BaseFeature:'base',Canvas:'canvas',Decal:'canvas',CustomFeature:'custom'};
const ICON_BY_CAT={sketch:'sketch',construct:'plane',solid:'extrude',finish:'fillet',offset:'offsetface',hole:'hole',body:'combine',
  param:'param',surface:'surface',sheet:'sheet',form:'form',mesh:'mesh',component:'component',insert:'insert',joint:'joint',group:'group',other:'other'};
const ICON_CATS={construct:'plane axis point',solid:'extrude revolve sweep loft rib box cylinder sphere torus coil pipe emboss thicken fill',finish:'fillet chamfer',
      offset:'offsetface shell draft deleteface replaceface splitface',hole:'hole thread',body:'scale combine mirror rectpattern circpattern pathpattern move splitbody copy remove',
      param:'param dparam params',component:'component',insert:'insert derive',joint:'joint jointorigin rigid motion contact snapshot arrange',surface:'patch stitch trim extend surface',
      sheet:'sheet flat',form:'form base',mesh:'mesh',sketch:'sketch'};
function catOfIcon(k){return Object.keys(ICON_CATS).find(c=>ICON_CATS[c].split(' ').includes(k))||'other';}
function iconName(n){if(!n)return 'other';if(ICON_BY_TYPE[n.type])return ICON_BY_TYPE[n.type];if(/^Mesh|Tessellate|Volumetric/.test(n.type))return 'mesh';return ICON_BY_CAT[n.cat]||'other';}
// one hidden sprite with every icon; everything else refers to it with <use>
(function(){const NS='http://www.w3.org/2000/svg';const sp=document.createElementNS(NS,'svg');sp.setAttribute('width','0');sp.setAttribute('height','0');sp.style.position='absolute';
  sp.innerHTML='<defs>'+Object.keys(ICONS).map(k=>'<symbol id="ic-'+k+'" viewBox="0 0 16 16"><g fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round">'+ICONS[k]+'</g></symbol>').join('')+'</defs>';
  document.body.insertBefore(sp,document.body.firstChild);})();
function iconHTML(name,cat,size){size=size||14;return '<svg class="ico" width="'+size+'" height="'+size+'" style="color:var(--c-'+(cat||'other')+')" aria-hidden="true"><use href="#ic-'+name+'"/></svg>';}
function iconEl(n,size){const s=document.createElement('span');s.className='icw';s.innerHTML=iconHTML(iconName(n),n?n.cat:'other',size);return s;}
function iconUse(name,x,y,size,color){const NS='http://www.w3.org/2000/svg';const u=document.createElementNS(NS,'use');u.setAttribute('href','#ic-'+name);u.setAttribute('x',x);u.setAttribute('y',y);u.setAttribute('width',size);u.setAttribute('height',size);u.setAttribute('class','nico');u.style.color=color;return u;}
const KIND={sketch:'Sketch',profile:'Profile',plane:'Plane / axis / point',geometry:'Faces / edges',body:'Body',feature:'Feature',param:'Parameter',component:'Component',incomp:'In component',joint:'Joint / motion',suppress:'Suppression test',order:'Same body, later'};
const nodes=D.nodes, byId={}; nodes.forEach(n=>byId[n.id]=n);
// graphs made before the split had one "assembly" category
nodes.forEach(n=>{if(n.cat==='assembly'||(n.cat==='other'&&ICON_BY_TYPE[n.type]&&catOfIcon(ICON_BY_TYPE[n.type])!=='other'))n.cat=catOfIcon(ICON_BY_TYPE[n.type]||'other')==='other'?((n.type==='Occurrence'||n.type==='DeriveFeature')?'insert':'joint'):catOfIcon(ICON_BY_TYPE[n.type]);});
// one common parent for all user parameters (derived parameters stay under their Derive feature)
{const ups=nodes.filter(n=>n.type==='UserParameter');
  if(ups.length&&!byId['up:all']){const r={id:'up:all',name:'User Parameters',type:'UserParameters',cat:'param',tl:null,o:-2,g:[],supp:false,health:0,msg:'',info:ups.length+' parameter'+(ups.length===1?'':'s')};
    nodes.push(r);byId[r.id]=r;// only parameters with no other parent (not driven by another parameter) hang directly under it
    // a parameter driven by other parameters sits below them: order user parameters by their depth in the
    // parameter chain (all still before the first timeline item)
    // a parameter driven by anything else (another parameter, a sketch dimension...) is placed right after
    // its latest parent, so it sits below it; free parameters come first, before the first timeline item
    const uid=new Set(ups.map(n=>n.id)),oo={};const pin={};D.edges.forEach(e=>{if(uid.has(e.t)&&byId[e.s])(pin[e.t]=pin[e.t]||[]).push(e.s);});
    const ord=(id,seen)=>{if(!uid.has(id))return byId[id].o;if(oo[id]!=null)return oo[id];if(seen.has(id))return -1;seen.add(id);
      const ps=pin[id]||[];return oo[id]=ps.length?Math.max(...ps.map(s=>ord(s,seen)))+0.0005:-1;};
    ups.forEach(n=>{n.o=ord(n.id,new Set());});
    const hasParent=new Set(D.edges.map(e=>e.t));ups.forEach(n=>{if(!hasParent.has(n.id))D.edges.push({s:r.id,t:n.id,k:['param'],syn:1});});}}
const TH=D.thumbs||{};const hasThumbs=Object.keys(TH).length>0;
// ---------- suppression simulator ----------
// Uses the recorded suppression tests: a group's own cascade (Whole groups test) or an item's
// cascade (Every item test). Without a recorded cascade it follows the dependency links
// downstream and marks the result as estimated.
let simOn=false,simState=null;const sim={items:new Set(),groups:new Set(),un:new Set()};
const canItems=!!D.meta.exact, canGroups=!!(D.meta.gtest||D.meta.exact);
function simCompute(){
  if(!simOn){simState=null;return;}
  const why={},est=new Set(),broken={},bEst=new Set(),warned={},refused=[];
  const add=(id,src)=>{if(!byId[id])return;if(!why[id])why[id]=src;};
  // Fusion refuses to suppress this on its own because a later feature (fail.node) then fails to compute.
  // The preview still suppresses it: the named feature is shown broken (Fusion reported it), what depends
  // on that feature may fail too (estimated), and the rest of the cascade is estimated from the links.
  const forced=(ids,fail,src)=>{const fn=fail&&fail.node&&byId[fail.node]?fail.node:null;
    const bad=new Set(fn?[fn,...closure(fn,'down')]:[]);
    if(fn&&!broken[fn]){broken[fn]=src;bEst.delete(fn);}
    bad.forEach(i=>{if(i!==fn&&!broken[i]){broken[i]=src;bEst.add(i);}});
    ids.forEach(id=>{const n=byId[id];
      if(canItems&&itemTested(n)){n.dsupp.forEach(i=>{if(!bad.has(i))add(i,src);});(n.dbreak||[]).forEach(i=>{if(!broken[i])broken[i]=src;});}
      else closure(id,'down').forEach(i=>{if(!bad.has(i)&&!why[i]&&!ids.includes(i)){add(i,src);est.add(i);}});});};
  sim.groups.forEach(g=>{const G=groups[g];if(!G)return;const mem=groupMembers(g);
    mem.forEach(n=>add(n.id,{g}));
    if(G.fail){refused.push(g);forced(mem.map(n=>n.id),G.fail,{g});return;}
    if(G.dsupp)G.dsupp.forEach(i=>add(i,{g}));
    else if(canItems)mem.forEach(n=>(n.dsupp||[]).forEach(i=>add(i,{g})));
    (G.dbreak||[]).forEach(i=>{if(!broken[i])broken[i]={g};});(G.dwarn||[]).forEach(i=>{if(!warned[i])warned[i]={g};});});
  if(canItems)sim.items.forEach(id=>{const n=byId[id];if(!n)return;
    add(id,{i:id});
    if(n.fail){refused.push('i:'+id);forced([id],n.fail,{i:id});return;}
    if(itemTested(n)){n.dsupp.forEach(i=>add(i,{i:id}));(n.dbreak||[]).forEach(i=>{if(!broken[i])broken[i]={i:id};});(n.dwarn||[]).forEach(i=>{if(!warned[i])warned[i]={i:id};});}
    else closure(id,'down').forEach(i=>{if(!why[i]){add(i,{i:id});est.add(i);}});});
  nodes.forEach(n=>{if(n.supp&&!(canItems&&sim.un.has(n.id)))add(n.id,{d:1});});
  Object.keys(broken).forEach(i=>{if(why[i]){delete broken[i];bEst.delete(i);}});
  Object.keys(warned).forEach(i=>{if(why[i]||broken[i])delete warned[i];});
  simState={why,est,broken,bEst,warned,refused};
}
function itemTested(n){return !!n&&Array.isArray(n.dsupp);}
function isSupp(n){return simState?!!simState.why[n.id]:!!n.supp;}
// 'sim': fails in the preview (from the test) · 'est': may fail in the preview (estimated) · 'design': fails in the design
function brokenKind(n){if(!n)return null;if(simState&&simState.broken[n.id])return simState.bEst.has(n.id)?'est':'sim';
  if(n.health===2&&!isSupp(n))return 'design';return null;}
function isBroken(n){return !!brokenKind(n);}
// 'sim': gets a warning in the preview (from the test) · 'design': has a warning in the design
function warnKind(n){if(!n||isBroken(n)||isSupp(n))return null;if(simState&&simState.warned&&simState.warned[n.id])return 'sim';return n.health===1?'design':null;}
function warnText(n){const k=warnKind(n);return k==='design'?'Has a warning in the design':k?'Would get a warning':'';}
function brokenText(n){const k=brokenKind(n);return k==='design'?'Fails to compute in the design':k==='est'?'May fail to compute (estimated)':k?'Would fail to compute':'';}
function isExplicit(n){return sim.items.has(n.id)||(n.supp&&!sim.un.has(n.id));}
function whyText(n){if(!simState)return '';const w=simState.why[n.id];if(!w)return '';if(w.d)return 'suppressed in the design';
  if(w.g)return (sim.groups.has(w.g)&&groupMembers(w.g).some(m=>m.id===n.id)?'in suppressed group ':'with group ')+(groups[w.g]?groups[w.g].name:w.g);
  if(w.i===n.id)return 'suppressed by you';return 'with '+(byId[w.i]?byId[w.i].name:w.i);}
function simToggleItem(id){if(!canItems)return;const n=byId[id];if(n.supp){sim.un.has(id)?sim.un.delete(id):sim.un.add(id);}else{sim.items.has(id)?sim.items.delete(id):sim.items.add(id);}simUpdate();}
function simToggleGroup(g){sim.groups.has(g)?sim.groups.delete(g):sim.groups.add(g);simUpdate();}
function simReset(){sim.items.clear();sim.groups.clear();sim.un.clear();simUpdate();}
function simButton(on,title,fn){const b=document.createElement('button');b.className='simtog'+(on?' off':'');b.textContent=on?'off':'on';b.title=title;b.onclick=e=>{e.stopPropagation();fn();};return b;}
function simUpdate(){saveView();setTimeout(pushHist,0);simCompute();paintTree();renderDetails();if(view==='graph')renderGraph(false);renderSimBar();renderGroupPanel();}
// one colour per top-level timeline group (timeline order)
const GCOL=['#4e79a7','#f28e2b','#59a14f','#e15759','#76b7b2','#edc948','#b07aa1','#ff9da7','#9c755f','#8cd17d','#86bcb6','#f1ce63','#d37295','#a0cbe8','#ffbe7d','#499894','#b6992d','#79706e','#d4a6c8','#fabfd2'];
const gColor={};D.groups.filter(g=>!g.parent).sort((a,b)=>a.first-b.first).forEach((g,i)=>gColor[g.id]=GCOL[i%GCOL.length]);
function topGroup(n){return (n&&n.g&&n.g.length)?n.g[0]:null;}
function colorOfGroup(gid){const p=groupPath(gid);let g=groups[gid];let guard=0;while(g&&g.parent&&guard++<20)g=groups[g.parent];return g?gColor[g.id]:null;}
function groupTag(g){if(g.fail)return ['breaks '+(g.fail.node&&byId[g.fail.node]?byId[g.fail.node].name:(g.fail.name||'a feature')),'bad'];if(g.empty)return ['empty',''];if(!g.dsupp)return ['',''];
  const b=(g.dbreak||[]).length;if(!g.dsupp.length&&!b)return ['independent','ok'];return [(g.dsupp.length?'+'+g.dsupp.length+' outside':'')+(b?(g.dsupp.length?', ':'')+b+' fail':''),b?'bad':''];}
function renderGroupPanel(){const L=$('gpList');if(!L)return;L.innerHTML='';const gs=D.groups.slice().sort((a,b)=>a.first-b.first);$('gpCount').textContent=gs.length;
  $('gpanel').style.display=gs.length?'':'none';
  gs.forEach(g=>{const mem=groupMembers(g.id);if(!mem.length&&!g.empty)return;const r=document.createElement('div');r.className='gprow'+(selGroup===g.id?' sel':'');
    const depth=groupPath(g.id).length-1;r.style.paddingLeft=(8+depth*14)+'px';
    if(simOn){r.appendChild(simButton(sim.groups.has(g.id),'Switch this whole group off/on in the simulation',()=>simToggleGroup(g.id)));}
    const dot=document.createElement('span');dot.style.cssText='width:9px;height:9px;border-radius:50%;flex:none;background:'+(colorOfGroup(g.id)||'transparent');r.appendChild(dot);
    const n=document.createElement('span');n.className='gn';n.textContent=g.name;n.title=g.name+' ('+mem.length+' items)';r.appendChild(n);
    const [txt,cls]=groupTag(g);const t=document.createElement('span');t.className='gt '+cls;
    if(simState){const k=mem.filter(isSupp).length;if(k===mem.length&&mem.length){r.classList.add('dim');}t.textContent=k?k+'/'+mem.length+' off':(txt||mem.length+' items');}else t.textContent=txt||mem.length+' items';
    r.appendChild(t);r.onclick=()=>{if(selGroup===g.id)clearSel();else selectGroup(g.id);};L.appendChild(r);});}
// header chips: how many items fail / have warnings right now (in the design, or in the suppression preview).
// Clicking a chip steps through those items.
let healthIdx={b:-1,w:-1};
function renderHealth(){const h=$('health');if(!h)return;h.innerHTML='';
  const its=nodes.filter(n=>n.tl!=null||n.cat==='param');const br=its.filter(n=>brokenKind(n)&&brokenKind(n)!=='est'),be=its.filter(n=>brokenKind(n)==='est'),wr=its.filter(n=>warnKind(n));
  const prev=!!(simOn&&(sim.groups.size||sim.items.size||sim.un.size));
  if(!br.length&&!be.length&&!wr.length){h.style.display='none';return;}h.style.display='';
  const chip=(cls,ic,txt,list,key,tip)=>{const b=document.createElement('button');b.className=cls;b.innerHTML='<span class="ic">'+ic+'</span>'+txt;b.title=tip+' · click to go through them';
    b.onclick=()=>{if(!list.length)return;healthIdx[key]=(healthIdx[key]+1)%list.length;const n=list[healthIdx[key]];(n.g||[]).forEach(g=>expanded.add(g));if(view!=='graph'){setView('graph');renderGraph(false);}select(n.id);};h.appendChild(b);};
  if(br.length||be.length){const all=[...br,...be];chip('hb','!',(br.length?br.length+' broken':'')+(be.length?(br.length?' ':'')+'<small>'+(br.length?'+':'')+be.length+' may fail</small>':''),all,'b',(prev?'In this suppression preview: ':'In the design: ')+br.length+' fail to compute'+(be.length?', '+be.length+' may fail (estimated)':''));}
  if(wr.length)chip('hw','',wr.length+' warning'+(wr.length===1?'':'s'),wr,'w',(prev?'In this suppression preview: ':'In the design: ')+wr.length+' with warnings');}
function renderSimBar(){renderHealth();const bar=$('simBar');bar.innerHTML='';
  const any=simOn&&(sim.groups.size||sim.items.size||sim.un.size);document.body.classList.toggle('simon',!!any);if(!any)return;
  const nItems=nodes.filter(n=>n.tl!=null);const sup=nItems.filter(isSupp).length;const br=simState?Object.keys(simState.broken).length:0;
  const offG=[...sim.groups].map(g=>groups[g]?groups[g].name:g),offI=[...sim.items].map(i=>byId[i].name),onI=[...sim.un].map(i=>byId[i].name);
  const t=document.createElement('span');t.innerHTML='<b>Suppression preview</b>';bar.appendChild(t);
  const w=document.createElement('span');w.textContent='Off: '+[...offG,...offI].join(', ')+(onI.length?' · back on: '+onI.join(', '):'');w.className='cnt';w.style.maxWidth='50vw';w.style.overflow='hidden';w.style.textOverflow='ellipsis';w.style.whiteSpace='nowrap';w.title=w.textContent;bar.appendChild(w);
  const r=document.createElement('span');r.innerHTML='<b>'+sup+'</b> of '+nItems.length+' items suppressed'+(br?' · <span class="st-err"><b>'+br+'</b> would fail</span>':'');bar.appendChild(r);
  if(simState&&simState.refused.length){const x=document.createElement('span');x.className='st-err';x.textContent='Breaks a later feature (estimated): '+simState.refused.map(g=>String(g).startsWith('i:')?byId[g.slice(2)].name:groups[g].name).join(', ');bar.appendChild(x);}
  if(simState&&simState.est.size){const x=document.createElement('span');x.className='cnt';x.textContent=simState.est.size+' estimated (item not covered by the test)';bar.appendChild(x);}
  const rb=document.createElement('button');rb.textContent='Reset';rb.style.marginLeft='auto';rb.onclick=simReset;bar.appendChild(rb);}
function paintTree(){if(view!=='tree')return;
  document.querySelectorAll('#tree .row').forEach(r=>{const id=r.dataset.id,gid=r.dataset.gid;
    if(id&&byId[id]){const n=byId[id];const nm=r.querySelector('.name');if(nm)nm.className='name '+stateCls(n);const ti=r.querySelector('img.thumb');if(ti)ti.classList.toggle('supp',isSupp(n));
      const b=r.querySelector('.simb');if(b){const s=isSupp(n),k=isBroken(n);b.className='simb'+(k?' b':s?' s':'');const wk=!k&&warnKind(n);if(wk)b.className='simb w';b.textContent=k?'● '+({design:'fails in the design',est:'may fail (estimated)',sim:'would fail'}[brokenKind(n)]):wk?'▲ '+(wk==='design'?'warning in the design':'would get a warning'):(s?(simState&&simState.est.has(n.id)?'suppressed (estimated) · ':'suppressed · ')+whyText(n):(simOn?'':(n.supp?'suppressed':'')));
        if(!simOn&&n.supp)b.textContent='suppressed';}
      const tg=r.querySelector('.simtog');if(tg){const on=isExplicit(n);tg.className='simtog'+(on?' off':'');tg.textContent=on?'off':'on';tg.style.display=simOn?'':'none';}}
    else if(gid){const tg=r.querySelector('.simtog');if(tg){const on=sim.groups.has(gid);tg.className='simtog'+(on?' off':'');tg.textContent=on?'off':'on';tg.style.display=simOn?'':'none';}
      const b=r.querySelector('.simb');if(b){const mem=groupMembers(gid);const k=simOn&&simState?mem.filter(isSupp).length:0;b.className='simb s';b.textContent=k?k+'/'+mem.length+' suppressed':'';}}});}
function peekShow(id,ev){const src=TH[id];if(!src||!showThumbs)return;const p=document.getElementById('peek');p.querySelector('img').src=src;p.querySelector('div').textContent=byId[id]?byId[id].name:'';p.style.display='block';peekMove(ev);}
function peekMove(ev){const p=document.getElementById('peek');if(p.style.display!=='block')return;const w=p.offsetWidth,h=p.offsetHeight;let x=ev.clientX+16,y=ev.clientY+16;if(x+w>innerWidth-8)x=ev.clientX-w-16;if(y+h>innerHeight-8)y=Math.max(8,innerHeight-h-8);p.style.left=x+'px';p.style.top=y+'px';}
function peekHide(){document.getElementById('peek').style.display='none';}
function thumbImg(id){const src=TH[id];if(!src)return null;const im=document.createElement('img');im.className='thumb';im.loading='lazy';im.src=src;im.alt='';
  im.onmouseenter=e=>peekShow(id,e);im.onmousemove=peekMove;im.onmouseleave=peekHide;im.onclick=()=>select(id);return im;}
const groups={}; (D.groups||[]).forEach(g=>groups[g.id]=g);
// items outside any timeline group and the user parameters can be folded like groups too
const isUserParam=n=>n.type==='UserParameter'||n.type==='UserParameters';
function pseudoOf(n){if(!n||(n.g&&n.g.length))return null;return isUserParam(n)?'_params':'_none';}
[['_none','Not in a group'],['_params','User parameters']].forEach(([id,name])=>{const mem=nodes.filter(n=>pseudoOf(n)===id);
  if(mem.length)groups[id]={id,name,parent:null,pseudo:true,first:Math.min(...mem.map(n=>n.tl!=null?n.tl:1e6))};});
const kindsPresent=[...new Set(D.edges.flatMap(e=>e.k))].filter(k=>KIND[k]);
const kindOn={}; kindsPresent.forEach(k=>kindOn[k]=k!=='order');  // every kind is always on; only "Same body, later" can be switched (Display menu)
// links found by the suppression test are real dependencies: always shown, no toggle
let showThumbs=true,pullTogether=true;const catOn={};nodes.forEach(n=>{catOn[n.cat]=true;});let view='tree', selected=null, selGroup=null, focus=false, search='';
const expanded=new Set(Object.keys(groups)); // graph starts with every group expanded
const $=id=>document.getElementById(id);

$('title').textContent='Dependencies graph: '+D.meta.doc;document.title='Dependencies graph: '+D.meta.doc;
$('meta').textContent=nodes.filter(n=>n.cat!=='param'&&n.cat!=='component').length+' items · '+D.edges.filter(e=>!e.syn).length+' links · '+('references'+(D.meta.gtest?' + group suppression test':'')+(D.meta.exact?' + item suppression test':''))+' · '+D.meta.date;

// Filter menu: one checkbox per kind of item present in the design
function renderCatFilter(){const cd=$('cats');cd.innerHTML='';const cnt={};nodes.forEach(n=>{cnt[n.cat]=(cnt[n.cat]||0)+1;});
  Object.keys(CAT).filter(c=>cnt[c]).forEach(c=>{const l=document.createElement('label');l.className='chk';
    l.innerHTML='<input type="checkbox"'+(catOn[c]?' checked':'')+'> <span class="pill" style="color:var(--c-'+c+');background:var(--c-'+c+'-bg)">'+iconHTML(ICON_BY_CAT[c]||'other',c,12)+CAT[c]+'</span><span class="cn">'+cnt[c]+'</span>';
    l.firstChild.onchange=e=>{catOn[c]=e.target.checked;filterChanged();};cd.appendChild(l);});}
function filterChanged(){updateFilterBtn();if(selected&&byId[selected]&&!visibleNode(byId[selected]))selected=null;
  if(view==='graph'&&Object.keys(pos).length){if(PB)stopPlay();animatedRerender(()=>{buildAdj();renderDetails();},{dur:480});}else refresh();}

function visibleNode(n){return !!n&&catOn[n.cat]!==false;}
// Links between the items on show. A hidden item does not break a chain: its parents are joined straight to its
// children (through any number of hidden items). Such joined links are marked via (drawn dotted).
let EFF=[];
function computeEff(){const adj={},out=new Map();
  D.edges.forEach(e=>{if(!byId[e.s]||!byId[e.t])return;const ks=e.k.filter(k=>kindOn[k]!==false);if(!ks.length)return;(adj[e.s]=adj[e.s]||[]).push({t:e.t,k:ks});});
  const add=(s,t,ks,via)=>{if(s===t)return;const key=s+'>'+t;let x=out.get(key);if(!x){x={s,t,k:[],via:true};out.set(key,x);}ks.forEach(k=>{if(!x.k.includes(k))x.k.push(k);});if(!via)x.via=false;};
  nodes.forEach(n=>{if(!visibleNode(n))return;const st=(adj[n.id]||[]).map(a=>({t:a.t,k:a.k,via:false}));const seen=new Set();
    while(st.length){const a=st.pop();if(visibleNode(byId[a.t])){add(n.id,a.t,a.k,a.via);continue;}if(seen.has(a.t))continue;seen.add(a.t);
      (adj[a.t]||[]).forEach(b=>st.push({t:b.t,k:[...new Set([...a.k,...b.k])],via:true}));}});
  EFF=[...out.values()];
  // a suppression-test link that another chain of links already implies (e.g. Derive -> component -> joint) adds nothing
  const adj2={};EFF.forEach(e=>{(adj2[e.s]=adj2[e.s]||[]).push(e);});
  EFF=EFF.filter(e=>{if(!(e.k.length===1&&e.k[0]==='suppress'))return true;const seen=new Set([e.s]),st=[e.s];
    while(st.length){const x=st.pop();for(const f of adj2[x]||[]){if(f===e)continue;if(f.t===e.t)return false;if(!seen.has(f.t)){seen.add(f.t);st.push(f.t);}}}return true;});}
function edgeOn(e){return true;}
let preds={},succs={};
function buildAdj(){computeEff();preds={};succs={};nodes.forEach(n=>{preds[n.id]=[];succs[n.id]=[];});
  EFF.forEach(e=>{preds[e.t].push(e);succs[e.s].push(e);});
  for(const id in preds){preds[id].sort((a,b)=>byId[a.s].o-byId[b.s].o);succs[id].sort((a,b)=>byId[a.t].o-byId[b.t].o);}}
function closure(id,dir){const seen=new Set(),st=[id];while(st.length){const x=st.pop();for(const e of (dir==='up'?preds[x]:succs[x])){const y=dir==='up'?e.s:e.t;if(!seen.has(y)){seen.add(y);st.push(y);}}}return seen;}

function pill(n){const s=document.createElement('span');s.className='pill';s.innerHTML=iconHTML(iconName(n),n.cat,12);s.appendChild(document.createTextNode(n.type.replace(/Feature$/,'')));s.style.color='var(--c-'+n.cat+')';s.style.background='var(--c-'+n.cat+'-bg)';return s;}
function stateCls(n){if(simState){if(isBroken(n))return 'st-err'+(brokenKind(n)==='est'?' st-est':'');if(isSupp(n))return 'st-sup'+(simState.est.has(n.id)?' st-est':'');return n.health===2?'st-err':(n.health===1?'st-warn':'');}return n.supp?'st-sup':(n.health===2?'st-err':(n.health===1?'st-warn':''));}
function hl(text){if(!search)return document.createTextNode(text);const i=text.toLowerCase().indexOf(search);if(i<0)return document.createTextNode(text);
  const f=document.createDocumentFragment();f.append(text.slice(0,i));const m=document.createElement('span');m.className='hit';m.textContent=text.slice(i,i+search.length);f.append(m,text.slice(i+search.length));return f;}
function matches(n){return !search||n.name.toLowerCase().includes(search)||n.type.toLowerCase().includes(search);}

function row(opts){ // {label(node|string), node, count, childrenFn, open, cls}
  const wrap=document.createElement('div');
  const r=document.createElement('div');r.className='row';
  const t=document.createElement('span');t.className='tog';
  const kids=document.createElement('div');kids.className='kids';kids.style.display='none';
  let built=false;
  const has=!!opts.childrenFn;
  if(!has)t.classList.add('empty');
  t.textContent=has?'▸':'';
  function setOpen(o){if(!has)return;if(o&&!built){opts.childrenFn(kids);built=true;}kids.style.display=o?'':'none';t.textContent=o?'▾':'▸';}
  t.onclick=()=>setOpen(kids.style.display==='none');
  r.appendChild(t);
  if(opts.node){const n=opts.node;r.dataset.id=n.id;if(selected===n.id)r.classList.add('sel');
    if(n.tl!=null&&canItems){const tg=simButton(isExplicit(n),'Switch this item off/on in the simulation',()=>simToggleItem(n.id));tg.style.display=simOn?'':'none';r.appendChild(tg);}
    const ti=thumbImg(n.id);if(ti)r.appendChild(ti);
    r.appendChild(pill(n));const nm=document.createElement('span');nm.className='name '+stateCls(n);nm.appendChild(hl(n.name));nm.onclick=()=>select(n.id);r.appendChild(nm);
    if(n.health===2||n.health===1){const w=document.createElement('span');w.className=n.health===2?'st-err':'st-warn';w.textContent=n.health===2?'● error':'● warning';w.style.fontSize='11px';r.appendChild(w);}
    const sb=document.createElement('span');sb.className='simb s';r.appendChild(sb);
  }else{if(opts.gid){r.dataset.gid=opts.gid;if(selGroup===opts.gid)r.classList.add('sel');const gg=opts.gid;const tg=simButton(sim.groups.has(gg),'Switch this whole group off/on in the simulation',()=>simToggleGroup(gg));tg.style.display=simOn?'':'none';r.appendChild(tg);}
    if(opts.gid){const c=colorOfGroup(opts.gid);if(c){const dot=document.createElement('span');dot.style.cssText='width:9px;height:9px;border-radius:50%;flex:none;background:'+c;r.appendChild(dot);}}
    const l=document.createElement('span');l.className=opts.cls||'grp';l.appendChild(typeof opts.label==='string'?hl(opts.label):opts.label);if(opts.onLabel){l.style.cursor='pointer';l.onclick=opts.onLabel;}r.appendChild(l);}
  if(opts.count!=null){const c=document.createElement('span');c.className='cnt';c.textContent=opts.count;r.appendChild(c);}
  if(opts.gid){const sb=document.createElement('span');sb.className='simb s';r.appendChild(sb);}
  wrap.append(r,kids);if(opts.open)setOpen(true);return wrap;
}
function depRows(n,container,path){ // children of an item: depends on / used by
  const p=preds[n.id],s=succs[n.id];
  if(p.length)container.appendChild(row({label:'Depends on',cls:'lbl',count:p.length,childrenFn:k=>p.forEach(e=>k.appendChild(itemRow(byId[e.s],path,e)))}));
  if(s.length)container.appendChild(row({label:'Used by',cls:'lbl',count:s.length,childrenFn:k=>s.forEach(e=>k.appendChild(itemRow(byId[e.t],path,e)))}));
}
function itemRow(n,path,edge){
  const cyc=path.has(n.id);const np=new Set(path);np.add(n.id);
  const w=row({node:n,childrenFn:(!cyc&&(preds[n.id].length||succs[n.id].length))?k=>depRows(n,k,np):null});
  if(edge){const k=document.createElement('span');k.className='cnt';k.textContent='· '+edge.k.map(x=>KIND[x]||x).join(', ');w.firstChild.appendChild(k);}
  return w;
}
// ---------- group level (units: a top-level group, or an item outside any group) ----------
let level='items';
function unitOf(n){return (n.g&&n.g.length)?'g'+n.g[0]:n.id;}
// Group-level links taken from the Whole groups suppression test: A -> B when suppressing
// group A suppresses something in B. Reduced to direct links (A->C dropped if A->B->C).
function testUnitLinks(){const L=new Map();
  D.groups.forEach(g=>{if(!g.dsupp)return;if(groupPath(g.id).length!==1)return;const a='g'+g.id;
    g.dsupp.forEach(i=>{const n=byId[i];if(!n||!visibleNode(n))return;const b=unitOf(n);if(b===a)return;if(!L.has(a))L.set(a,new Map());L.get(a).set(b,(L.get(a).get(b)||0)+1);});});
  const red=new Map();L.forEach((m,a)=>{const keep=new Map();m.forEach((c,b)=>{let via=false;m.forEach((c2,x)=>{if(x!==b&&L.has(x)&&L.get(x).has(b))via=true;});if(!via)keep.set(b,c);});red.set(a,keep);});
  return red;}
const useTestLinks=()=>false;   // links between group boxes are the links of their items
function unitAdj(){if(useTestLinks()){const up={},dn={};nodes.filter(visibleNode).forEach(n=>{const u=unitOf(n);up[u]=up[u]||new Map();dn[u]=dn[u]||new Map();});
    testUnitLinks().forEach((m,a)=>m.forEach((c,b)=>{if(!dn[a]||!up[b])return;dn[a].set(b,c);up[b].set(a,c);}));return {up,dn};}
const up={},dn={};const vis=nodes.filter(visibleNode);vis.forEach(n=>{const u=unitOf(n);up[u]=up[u]||new Map();dn[u]=dn[u]||new Map();});
  EFF.forEach(e=>{const a=byId[e.s],b=byId[e.t];if(!a||!b||!visibleNode(a)||!visibleNode(b))return;const ua=unitOf(a),ub=unitOf(b);if(ua===ub)return;
    dn[ua].set(ub,(dn[ua].get(ub)||0)+1);up[ub].set(ua,(up[ub].get(ua)||0)+1);});return {up,dn};}
function unitOrder(u){return u[0]==='g'&&groups[u.slice(1)]?Math.min(...groupMembers(u.slice(1)).map(n=>n.o).concat([1e9])):(byId[u]?byId[u].o:1e9);}
function unitMatches(u){if(!search)return true;if(u[0]==='g'&&groups[u.slice(1)]){const g=groups[u.slice(1)];return g.name.toLowerCase().includes(search)||groupMembers(g.id).some(matches);}return byId[u]?matches(byId[u]):false;}
function unitRow(u,extra){if(u[0]==='g'&&groups[u.slice(1)]){const g=groups[u.slice(1)];const mem=groupMembers(g.id);const [txt]=groupTag(g);
    return row(Object.assign({label:g.name,gid:g.id,onLabel:()=>selectGroup(g.id),count:mem.length+' items'+(txt?' · '+txt:'')},extra||{}));}
  return row(Object.assign({node:byId[u]},extra||{}));}
function unitChainRow(u,dir,path,A){const m=(dir==='down'?A.dn:A.up)[u]||new Map();const list=[...m.keys()].sort((a,b)=>unitOrder(a)-unitOrder(b));const cyc=path.has(u);const np=new Set(path);np.add(u);
  const w=unitRow(u,{childrenFn:(!cyc&&list.length)?k=>list.forEach(x=>k.appendChild(unitChainRow(x,dir,np,A))):null});
  if(list.length){const c=document.createElement('span');c.className='cnt';c.textContent=(dir==='down'?'→ ':'← ')+list.length;w.firstChild.appendChild(c);}return w;}
function chainRow(n,dir,path){
  const list=dir==='down'?succs[n.id]:preds[n.id];const cyc=path.has(n.id);const np=new Set(path);np.add(n.id);
  return row({node:n,count:list.length?(dir==='down'?'→ '+list.length:'← '+list.length):null,childrenFn:(!cyc&&list.length)?k=>list.forEach(e=>k.appendChild(chainRow(byId[dir==='down'?e.t:e.s],dir,np))):null});
}
function groupPathIds(n){return n.g||[];}
function renderTree(){
  const tr=$('tree');tr.innerHTML='';const mode=$('treeMode').value;
  const vis=nodes.filter(visibleNode);
  if(mode==='timeline'){
    // nested groups
    const top=[];const gk={};
    function gnode(gid){if(!gk[gid])gk[gid]={g:groups[gid],items:[],sub:[],o:1e9};return gk[gid];}
    vis.forEach(n=>{const gp=groupPathIds(n);if(!gp.length){top.push({n,o:n.o});return;}
      for(let i=0;i<gp.length;i++){const gn=gnode(gp[i]);gn.o=Math.min(gn.o,n.o);if(i===0){if(!top.find(x=>x.gn===gn))top.push({gn,o:gn.o});}else{const par=gnode(gp[i-1]);if(!par.sub.includes(gn))par.sub.push(gn);}}
      gnode(gp[gp.length-1]).items.push(n);});
    top.forEach(x=>{if(x.gn)x.o=x.gn.o;});top.sort((a,b)=>a.o-b.o);
    function countMatch(gn){return gn.items.filter(matches).length+gn.sub.reduce((a,s)=>a+countMatch(s),0);}
    function renderG(gn,cont){
      const m=countMatch(gn);if(search&&!m)return;
      const entries=[...gn.items.map(n=>({n,o:n.o})),...gn.sub.map(s=>({gn:s,o:s.o}))].sort((a,b)=>a.o-b.o);
      const total=gn.items.length+gn.sub.reduce((a,s)=>a+s.items.length,0);
      const gb=gn.g&&gn.g.dbreak?gn.g.dbreak.length:0;const gf=gn.g&&gn.g.fail;
      const gtag=gn.g&&gn.g.dsupp?((gn.g.dsupp.length?' · suppresses '+gn.g.dsupp.length+' outside':(gb?'':' · independent'))+(gb?' · breaks '+gb:'')):(gf?' · breaks '+(gf.node&&byId[gf.node]?byId[gf.node].name:(gf.name||'a feature')):'');
      cont.appendChild(row({label:gn.g?gn.g.name:'?',gid:gn.g?gn.g.id:null,onLabel:gn.g?()=>selectGroup(gn.g.id):null,count:(search?m+' match':total+' items')+gtag,open:!!search,childrenFn:k=>entries.forEach(x=>{if(x.gn)renderG(x.gn,k);else if(matches(x.n))k.appendChild(itemRow(x.n,new Set()));})}));
    }
    top.forEach(x=>{if(x.gn)renderG(x.gn,tr);else if(matches(x.n))tr.appendChild(itemRow(x.n,new Set()));});
  }else{
    const dir=mode==='roots'?'down':'up';
    if(level==='groups'){const A=unitAdj();const units=Object.keys(A.up).sort((a,b)=>unitOrder(a)-unitOrder(b));
      const roots=units.filter(u=>(dir==='down'?A.up[u]:A.dn[u]).size===0&&(dir==='down'?A.dn[u]:A.up[u]).size>0);
      const alone=units.filter(u=>A.up[u].size===0&&A.dn[u].size===0);
      const hdr=document.createElement('div');hdr.className='hint';hdr.textContent=dir==='down'?'Groups (and items outside groups) that depend on nothing else. Expand to see what is built on them.':'Groups (and items outside groups) nothing else depends on. Expand to see what they are built from.';tr.appendChild(hdr);
      roots.filter(unitMatches).forEach(u=>tr.appendChild(unitChainRow(u,dir,new Set(),A)));
      if(alone.length)tr.appendChild(row({label:'Independent (no links to other groups)',cls:'lbl',count:alone.length,childrenFn:k=>alone.filter(unitMatches).forEach(u=>k.appendChild(unitRow(u)))}));
      paintTree();return;}
    const roots=vis.filter(n=>(dir==='down'?preds[n.id]:succs[n.id]).length===0&&(dir==='down'?succs[n.id]:preds[n.id]).length>0);
    const alone=vis.filter(n=>preds[n.id].length===0&&succs[n.id].length===0);
    const hdr=document.createElement('div');hdr.className='hint';hdr.textContent=dir==='down'?'Roots: items that depend on nothing. Expand to see what is built on them.':'End results: items nothing depends on. Expand to see what they are built from.';tr.appendChild(hdr);
    roots.filter(n=>!search||matches(n)||[...closure(n.id,dir)].some(i=>matches(byId[i]))).forEach(n=>tr.appendChild(chainRow(n,dir,new Set())));
    if(alone.length)tr.appendChild(row({label:'Independent items (no links)',cls:'lbl',count:alone.length,childrenFn:k=>alone.filter(matches).forEach(n=>k.appendChild(row({node:n})))}));
  }
  paintTree();
}

// ---------- back / forward: selection and suppression-preview history ----------
const hist=[];let hIdx=-1,hMute=false;
function snap(){return {sel:selected,grp:selGroup,items:[...sim.items].sort(),groups:[...sim.groups].sort(),un:[...sim.un].sort()};}
// each history step also remembers the graph's zoom and position while it was the current one
const histKey=o=>JSON.stringify({sel:o.sel,grp:o.grp,items:o.items,groups:o.groups,un:o.un});
function saveView(){if(hMute||hIdx<0||!hist[hIdx]||view!=='graph'||!Object.keys(pos).length)return;hist[hIdx].view={x:T.x,y:T.y,k:T.k,layout:layoutMode};}
function pushHist(){if(hMute)return;const st=snap();if(hIdx>=0&&histKey(hist[hIdx])===histKey(st))return;
  hist.splice(hIdx+1);hist.push(st);if(hist.length>300)hist.shift();hIdx=hist.length-1;updHistBtns();}
function viewTo(v,dur){if(!v)return;if(anim)cancelAnimationFrame(anim);const s0={x:T.x,y:T.y,k:T.k},t0=performance.now();dur=dur||450;
  const step=now=>{const a=Math.min(1,(now-t0)/dur),e=a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;T.k=s0.k+(v.k-s0.k)*e;T.x=s0.x+(v.x-s0.x)*e;T.y=s0.y+(v.y-s0.y)*e;applyT();if(a<1)anim=requestAnimationFrame(step);else anim=null;};
  anim=requestAnimationFrame(step);}
function updHistBtns(){const b=$('hBack'),n=$('hNext');if(b)b.disabled=hIdx<=0;if(n)n.disabled=hIdx>=hist.length-1;}
function goHist(d){const j=hIdx+d;if(j<0||j>=hist.length)return;saveView();hIdx=j;const st=hist[j];hMute=true;
  try{sim.items=new Set(st.items);sim.groups=new Set(st.groups);sim.un=new Set(st.un);simCompute();renderSimBar();paintTree();renderGroupPanel();
    if(st.sel&&byId[st.sel])select(st.sel);else if(st.grp&&groups[st.grp])selectGroup(st.grp);else clearSel();}
  finally{hMute=false;}updHistBtns();
  // back where we were: the zoom and position from then (after the selection's own zoom has started)
  const v=st.view;if(v&&view==='graph'&&v.layout===layoutMode)requestAnimationFrame(()=>requestAnimationFrame(()=>viewTo(v,480)));}
function select(id){saveView();setTimeout(pushHist,0);peekHide();setTimeout(renderGroupPanel,0);selected=id;selGroup=null;renderDetails();if(view==='tree')document.querySelectorAll('#tree .row').forEach(r=>r.classList.toggle('sel',!!id&&r.dataset.id===id));else{animatedRerender(()=>{},{});if(id&&byId[id])requestAnimationFrame(()=>focusOn([rep(byId[id])]));}}
function selectGroup(gid){saveView();setTimeout(pushHist,0);peekHide();selected=null;setTimeout(renderGroupPanel,0);selGroup=gid;renderDetails();if(view==='tree')document.querySelectorAll('#tree .row').forEach(r=>r.classList.toggle('sel',!!gid&&r.dataset.gid===gid));else{animatedRerender(()=>{},{});if(gid)requestAnimationFrame(()=>focusOn([...new Set(groupMembers(gid).filter(visibleNode).map(rep))]));}}
function clearSel(){saveView();setTimeout(pushHist,0);setTimeout(renderGroupPanel,0);selected=null;selGroup=null;renderDetails();if(view==='graph'){if(Object.keys(pos).length)animatedRerender(()=>{},{});else renderGraph(false);}else document.querySelectorAll('#tree .row.sel').forEach(r=>r.classList.remove('sel'));}
function groupUpIds(gid){const mem=groupMembers(gid);const mset=new Set(mem.map(n=>n.id));
  const links=mem.flatMap(n=>[...closure(n.id,'up')]);
  const test=D.meta.gtest?D.groups.filter(o=>o.id!==gid&&o.dsupp&&o.dsupp.some(id=>mset.has(id))).flatMap(o=>groupMembers(o.id).map(n=>n.id)):[];
  return [...new Set([...links,...test])].filter(i=>!mset.has(i)&&byId[i]&&visibleNode(byId[i]));}
function groupMembers(gid){if(groups[gid]&&groups[gid].pseudo)return nodes.filter(n=>pseudoOf(n)===gid);return nodes.filter(n=>(n.g||[]).includes(gid));}
function groupPath(gid){const out=[];let g=groups[gid];let guard=0;while(g&&guard++<20){out.unshift(g.name);g=groups[g.parent];}return out;}
function groupsSuppressing(id){return D.groups.filter(g=>g.dsupp&&g.dsupp.includes(id));}
function itemList(ids){const ul=document.createElement('ul');if(!ids.length){ul.innerHTML='<li class="kv">none</li>';return ul;}
  ids.forEach(id=>{const x=byId[id];if(!x)return;const li=document.createElement('li');const ti=thumbImg(x.id);if(ti)li.appendChild(ti);li.appendChild(pill(x));const a=document.createElement('span');a.className='name '+stateCls(x);a.textContent=x.name;a.onclick=()=>select(id);li.appendChild(a);
    ul.appendChild(li);});return ul;}
function groupLinks(list){const ul=document.createElement('ul');if(!list.length){ul.innerHTML='<li class="kv">none</li>';return ul;}
  list.forEach(([gid,cnt])=>{const g=groups[gid];const li=document.createElement('li');const a=document.createElement('span');{const gm=g?groupMembers(gid):[];a.className='name grp'+(gm.length&&gm.every(isSupp)?' st-sup':'');}a.textContent=g?g.name:gid;a.onclick=()=>selectGroup(gid);li.appendChild(a);if(cnt!=null){const k=document.createElement('span');k.className='k';k.textContent=cnt+' item'+(cnt===1?'':'s');li.appendChild(k);}ul.appendChild(li);});return ul;}
function renderGroupDetails(d){
  const g=groups[selGroup];const mem=groupMembers(selGroup);const mset=new Set(mem.map(n=>n.id));
  const h=document.createElement('h2');h.textContent=g.name;h.insertAdjacentHTML('afterbegin','<span class="icw">'+iconHTML('group','group',18)+'</span>');d.appendChild(h);
  {const last=[...mem].sort((a,b)=>b.o-a.o).find(n=>TH[n.id]);if(last&&showThumbs){const im=document.createElement('img');im.className='big';im.src=TH[last.id];im.alt='Model after '+last.name;im.title='Model after the last item in the group: '+last.name;d.appendChild(im);}}
  if(simOn){const b=document.createElement('button');const on=sim.groups.has(selGroup);b.textContent=on?'Switch group back on (simulation)':'Suppress group (simulation)';b.style.margin='6px 0';b.onclick=()=>simToggleGroup(selGroup);d.appendChild(b);}
  const kv=document.createElement('div');kv.className='kv';
  ['Timeline group'+(groupPath(selGroup).length>1?' in '+groupPath(selGroup).slice(0,-1).join(' › '):''),mem.length+' items'].forEach(t=>{const x=document.createElement('div');x.textContent=t;kv.appendChild(x);});d.appendChild(kv);
  if(g.dsupp){
    const h3=document.createElement('h3');h3.textContent='Suppressing this group also suppresses ('+g.dsupp.length+')';d.appendChild(h3);
    if(g.dbreak&&g.dbreak.length){const hb=document.createElement('h3');hb.style.color='var(--err)';hb.textContent='Suppressing it breaks ('+g.dbreak.length+')';const w=document.createElement('div');w.className='kv';w.textContent='These stay on but fail to compute (lost references or failed geometry) when this group is suppressed.';d.append(hb,w,itemList(g.dbreak));}
    if(!g.dsupp.length&&!(g.dbreak&&g.dbreak.length)){const ok=document.createElement('div');ok.className='kv';ok.textContent='Nothing outside the group. It can be suppressed on its own.';d.appendChild(ok);}
    else if(!g.dsupp.length){const ok=document.createElement('div');ok.className='kv';ok.textContent='Nothing else is suppressed with it.';d.appendChild(ok);}
    else{const by={};g.dsupp.forEach(id=>{const x=byId[id];const k=x&&(x.g||[]).length?x.g[0]:'';(by[k]=by[k]||[]).push(id);});
      Object.keys(by).forEach(k=>{const sub=document.createElement('div');sub.className='kv';sub.style.marginTop='6px';sub.textContent=k?(groups[k]?groups[k].name:k):'Not in a group';if(k){sub.style.cursor='pointer';sub.onclick=()=>selectGroup(k);}d.append(sub,itemList(by[k]));});}
    const hb=document.createElement('h3');const inb=D.groups.filter(o=>o.id!==selGroup&&o.dsupp&&o.dsupp.some(id=>mset.has(id))).map(o=>[o.id,o.dsupp.filter(id=>mset.has(id)).length]);
    hb.textContent='Suppressed along with these groups ('+inb.length+')';d.append(hb,groupLinks(inb));
  }else if(g.fail){const h3=document.createElement('h3');h3.style.color='var(--err)';h3.textContent='Suppressing it breaks a later feature';d.appendChild(h3);
    const x=document.createElement('div');x.className='kv';x.append('Fusion refuses to suppress this group through the API because this feature then fails to compute. The preview can still suppress it: that feature is marked broken, the rest is estimated from the links. ');
    if(g.fail.node&&byId[g.fail.node]){const a=document.createElement('span');a.className='name st-err';a.textContent=byId[g.fail.node].name;a.onclick=()=>select(g.fail.node);x.appendChild(a);}else x.append(g.fail.name||'unknown feature');
    d.appendChild(x);if(g.fail.msg){const m=document.createElement('div');m.className='msg';m.textContent=g.fail.msg;d.appendChild(m);}
  }else if(g.pseudo){const x=document.createElement('div');x.className='hint';x.textContent=g.id==='_params'?'All user parameters, shown as one box. Expand it (+) to see them one by one.':'Items that are not in any timeline group, shown as one box. Expand it (+) to see them one by one.';d.appendChild(x);
  }else{const x=document.createElement('div');x.className='hint';x.textContent=g.empty?'This group is empty.':(D.meta.gtest?'This group was not tested (it could not be suppressed, or everything in it was already suppressed).':'Run Dependencies Graph with Deep analysis on to see what suppressing this group does.');d.appendChild(x);}
  // reference links crossing the group boundary, counted per other group
  const out={},inn={};
  EFF.forEach(e=>{const a=mset.has(e.s),b=mset.has(e.t);if(a===b)return;const other=byId[a?e.t:e.s];if(!other||!visibleNode(other))return;const og=(other.g||[]).length?other.g[0]:'-';const m=a?inn:out;m[og]=(m[og]||0)+1;});
  const toList=m=>Object.entries(m).filter(([k])=>k!=='-').sort((x,y)=>y[1]-x[1]);
  const h4=document.createElement('h3');h4.textContent='References into other groups';d.append(h4,groupLinks(toList(out)));
  const h5=document.createElement('h3');h5.textContent='Referenced from other groups';d.append(h5,groupLinks(toList(inn)));
  const det=document.createElement('details');det.style.marginTop='10px';const sm=document.createElement('summary');sm.textContent='Items in the group ('+mem.length+')';det.appendChild(sm);det.appendChild(itemList(mem.map(n=>n.id)));d.appendChild(det);
  const b=document.createElement('div');b.style.marginTop='12px';b.style.display='flex';b.style.gap='6px';b.style.flexWrap='wrap';
  const bg=document.createElement('button');bg.textContent='Show in graph';bg.onclick=()=>showInGraph(()=>groupMembers(selGroup).filter(visibleNode).map(rep));b.appendChild(bg);
  if(view==='graph'){const be=document.createElement('button');be.textContent=expanded.has(selGroup)?'Collapse group':'Expand group';be.onclick=()=>{if(expanded.has(selGroup))expanded.delete(selGroup);else expanded.add(selGroup);renderGraph(false);renderDetails();};b.appendChild(be);}
  const bp=document.createElement('button');bp.textContent='▶ Play history';bp.title='Animate how this group was built from its dependencies (P)';bp.onclick=()=>playHistory();b.appendChild(bp);
  const bx=document.createElement('button');bx.textContent='Clear selection';bx.onclick=clearSel;b.appendChild(bx);d.appendChild(b);
}
function linkList(list,dir){const ul=document.createElement('ul');if(!list.length){ul.innerHTML='<li class="kv">none</li>';return ul;}
  list.forEach(e=>{const n=byId[dir==='up'?e.s:e.t];const li=document.createElement('li');li.appendChild(pill(n));const a=document.createElement('span');a.className='name '+stateCls(n);a.textContent=n.name;a.onclick=()=>select(n.id);li.appendChild(a);const k=document.createElement('span');k.className='k';k.textContent=e.k.map(x=>KIND[x]||x).join(', ');li.appendChild(k);li.addEventListener('mouseenter',()=>panelLinkHover(e.s,e.t,true));li.addEventListener('mouseleave',()=>panelLinkHover(e.s,e.t,false));ul.appendChild(li);});return ul;}
function setInfo(open){if(open&&document.body.classList.contains('legendopen'))setLegend(false);document.body.classList.toggle('infoopen',!!open);$('infoBtn').classList.toggle('on',!!open);}
function renderInfo(){const b=$('infoBody');b.innerHTML='';const cols=document.createElement('div');cols.className='cols';
  const c1=document.createElement('div');c1.innerHTML='<h2>Dependencies graph</h2><div class="kv">Click an item or group in the tree, or a box in the graph, to see its details. Click empty space in the graph to deselect.</div>';
  const lg=document.createElement('div');lg.className='legend';Object.keys(CAT).forEach(c=>{if(!nodes.some(n=>n.cat===c))return;const s=document.createElement('span');s.className='pill';s.textContent=CAT[c];s.style.color='var(--c-'+c+')';s.style.background='var(--c-'+c+'-bg)';lg.appendChild(s);});c1.appendChild(lg);
  const h=document.createElement('div');h.className='hint';h.innerHTML='Graph: drag to pan, scroll to zoom, click a grey group box to expand it. Blue links lead to what the selection depends on, green links to what depends on it. <b>▶ Play</b> (or P) animates how the selected item was built from its dependencies, or the whole history when nothing is selected: Space pauses, → skips to the next step, Esc stops.'+(D.meta.exact||D.meta.gtest?'':'<br>Links come from references the add-in could read (sketches, profiles, planes, faces/edges, bodies, parameters). Turn on <b>Deep analysis</b> in the add-in to get Fusion\'s real dependencies and the suppression preview.');c1.appendChild(h);
  if(canGroups){const x=document.createElement('div');x.className='hint';x.innerHTML='<b>Suppression preview:</b> use the on/off buttons (groups panel, tree, details) or Shift+click a box in the graph'+(canItems?'':' (switches its whole group - this page has the group test only)')+'. The preview bar appears as soon as something is switched off.'+
      '<br><b>How exact it is:</b> one item switched off'+(D.meta.gtest?', or one whole timeline group,':'')+' shows exactly what Fusion did in the suppression test. '+
      'With several things switched off at once, the preview adds up their single results. Features that fail or switch off only when those things are off <i>together</i> are not shown, so treat that result as an estimate.';c1.appendChild(x);}
  cols.appendChild(c1);
  if(D.meta.warnings&&D.meta.warnings.length){const c2=document.createElement('div');c2.innerHTML='<h2>Warnings</h2>';const w=document.createElement('div');w.className='msg';w.textContent=D.meta.warnings.join('\n');c2.appendChild(w);cols.appendChild(c2);}
  b.appendChild(cols);}
function renderDetails(){
  pbSync();const d=$('details');d.innerHTML='';
  if(selGroup&&groups[selGroup]){document.body.classList.remove('nosel');setInfo(false);renderGroupDetails(d);return;}
  document.body.classList.toggle('nosel',!selected);
  if(!selected){renderInfo();return;}
  setInfo(false);
  const n=byId[selected];
  const h=document.createElement('h2');h.textContent=n.name;h.className=stateCls(n);h.prepend(iconEl(n,18));d.appendChild(h);
  if(TH[n.id]&&showThumbs){const im=document.createElement('img');im.className='big';im.src=TH[n.id];im.alt='Model after '+n.name;d.appendChild(im);}
  const kv=document.createElement('div');kv.className='kv';
  [n.type+(n.info?' · '+n.info:''),n.tl!=null?'Timeline position '+(n.tl+1):'',isSupp(n)?'Suppressed':(isBroken(n)?brokenText(n):warnKind(n)?warnText(n):'OK')].filter(Boolean).forEach(t=>{const x=document.createElement('div');x.textContent=t;kv.appendChild(x);});
  if(!(n.g&&n.g.length)){const gl=document.createElement('div');gl.textContent='Not in a timeline group';kv.insertBefore(gl,kv.children[1]||null);}
  d.appendChild(kv);
  if(simOn&&n.tl!=null&&!canItems&&isSupp(n)){const x=document.createElement('div');x.className='kv';x.style.margin='8px 0';x.textContent='Suppressed: '+whyText(n);d.appendChild(x);}
  if(simOn&&n.tl!=null&&canItems){const box=document.createElement('div');box.style.margin='8px 0';box.style.display='flex';box.style.gap='8px';box.style.alignItems='center';box.style.flexWrap='wrap';
    const b=document.createElement('button');const ex=isExplicit(n);b.textContent=ex?'Switch back on (simulation)':'Suppress (simulation)';b.onclick=()=>simToggleItem(n.id);box.appendChild(b);
    const st=document.createElement('span');st.className=isBroken(n)?'st-err':(warnKind(n)?'st-warn':'kv');st.textContent=isBroken(n)?brokenText(n):warnKind(n)?warnText(n):(isSupp(n)?'Suppressed: '+whyText(n)+(simState&&simState.est.has(n.id)?' (estimated)':''):'Active');box.appendChild(st);d.appendChild(box);
}
  if(n.g&&n.g.length){const h3=document.createElement('h3');h3.textContent='Timeline group';d.appendChild(h3);const ul=document.createElement('ul');
    n.g.forEach((gid,i)=>{const g=groups[gid];if(!g)return;const li=document.createElement('li');li.style.paddingLeft=(i*14)+'px';li.style.alignItems='center';
      if(simOn&&canGroups)li.appendChild(simButton(sim.groups.has(gid),'Switch this whole group off/on in the simulation',()=>simToggleGroup(gid)));
      const a=document.createElement('span');a.className='name grp';a.textContent=g.name;a.onclick=()=>selectGroup(gid);li.appendChild(a);
      const [txt,cls]=groupTag(g);const k=document.createElement('span');k.className='k'+(cls==='bad'?' st-err':'');k.textContent=groupMembers(gid).length+' items'+(txt?' · '+txt:'');li.appendChild(k);ul.appendChild(li);});d.appendChild(ul);}
  if(D.meta.gtest){const bg2=D.groups.filter(g=>g.dbreak&&g.dbreak.includes(n.id));if(bg2.length){const h3=document.createElement('h3');h3.style.color='var(--err)';h3.textContent='Breaks when you suppress group ('+bg2.length+')';d.append(h3,groupLinks(bg2.map(g=>[g.id,null])));}}
  if(D.meta.gtest){const sg=groupsSuppressing(n.id);if(sg.length){const h3=document.createElement('h3');h3.textContent='Suppressed when you suppress group ('+sg.length+')';d.append(h3,groupLinks(sg.map(g=>[g.id,null])));}}
  if(n.msg){const m=document.createElement('div');m.className='msg';m.textContent=n.msg;d.appendChild(m);}
  const up=closure(n.id,'up'),down=closure(n.id,'down');
  const h3a=document.createElement('h3');h3a.textContent='Depends on ('+preds[n.id].length+' direct, '+up.size+' total)';d.append(h3a,linkList(preds[n.id],'up'));
  const h3b=document.createElement('h3');h3b.textContent='Used by ('+succs[n.id].length+' direct, '+down.size+' total)';d.append(h3b,linkList(succs[n.id],'down'));
  if(canItems&&n.tl!=null&&!n.supp){
    if(n.fail){const h3=document.createElement('h3');h3.style.color='var(--err)';h3.textContent='Suppressing it breaks a later feature';d.appendChild(h3);const w=document.createElement('div');w.className='kv';w.textContent='Fusion refuses to suppress this item through the API'+(n.fail.name?' because '+n.fail.name+' then fails to compute':'')+'. The preview can still suppress it: that feature is marked broken, the rest is estimated from the links.';d.appendChild(w);if(n.fail.msg){const m=document.createElement('div');m.className='msg';m.textContent=n.fail.msg;d.appendChild(m);}}
    else if(!itemTested(n)){const w=document.createElement('div');w.className='kv';w.style.marginTop='10px';w.textContent='Not covered by the Every item test. The preview estimates its effect from the dependency links.';d.appendChild(w);}
    if(n.dbreak&&n.dbreak.length){const hb=document.createElement('h3');hb.style.color='var(--err)';hb.textContent='Suppressing it breaks ('+n.dbreak.length+')';const w=document.createElement('div');w.className='kv';w.textContent='These stay on but fail to compute when this item is suppressed.';d.append(hb,w,itemList(n.dbreak));}}
  if(n.dsupp){const h3=document.createElement('h3');h3.textContent='Suppressing it also suppresses ('+n.dsupp.length+')';d.appendChild(h3);const ul=document.createElement('ul');n.dsupp.forEach(id=>{const x=byId[id];if(!x)return;const li=document.createElement('li');li.appendChild(pill(x));const a=document.createElement('span');a.className='name '+stateCls(x);a.textContent=x.name;a.onclick=()=>select(id);li.appendChild(a);ul.appendChild(li);});d.appendChild(ul);}
  const b=document.createElement('div');b.style.marginTop='12px';b.style.display='flex';b.style.gap='6px';b.style.flexWrap='wrap';
  const bg=document.createElement('button');bg.textContent='Show in graph';bg.onclick=()=>{(n.g||[]).forEach(g=>expanded.add(g));showInGraph(()=>[rep(n)]);};b.appendChild(bg);
  if(n.g&&n.g.length&&view==='graph'){const bc=document.createElement('button');bc.textContent='Collapse group';bc.onclick=()=>{expanded.delete(n.g[n.g.length-1]);renderGraph(false);};b.appendChild(bc);}
  const bp=document.createElement('button');bp.textContent='▶ Play history';bp.title='Animate how this item was built from its dependencies (P)';bp.onclick=()=>playHistory();b.appendChild(bp);
  const bx=document.createElement('button');bx.textContent='Clear selection';bx.onclick=clearSel;b.appendChild(bx);
  d.appendChild(b);
}

// ---------- graph ----------
const svg=$('graph'),vp=$('vp');let T={x:20,y:20,k:1},pos={};
function applyT(){vp.setAttribute('transform','translate('+T.x+','+T.y+') scale('+T.k+')');}
let nodeEls={},edgeEls=[],graphAnim=null,nodeBtnEls={};
let hovState=null,hovPin=null,routeCache=null,selRelated=[];
// clicked link: stays highlighted until the mouse really moves (not just the view moving under it)
window.addEventListener('mousemove',ev=>{if(!hovPin)return;if(Math.hypot(ev.clientX-hovPin.x,ev.clientY-hovPin.y)<5)return;hovPin=null;edgeHover(null,null,null,false);});
function edgeHover(p,s,t,on){
  if(on&&PB)return;             // no hover highlights while the history is playing
  if(!on&&hovPin)return;        // a clicked link stays highlighted until the mouse moves
  if(hovState){const h=hovState;if(h.p){h.p.classList.remove('hov');if(h.parent)h.parent.insertBefore(h.p,h.next);}
    h.rings.forEach(r=>{r.style.opacity='0';setTimeout(()=>r.remove(),220);});h.nodes.forEach(g=>g.classList.remove('hov'));hovState=null;}
  if(!on)return;const vpEl=$('vp');const st={p,parent:p?p.parentNode:null,next:p?p.nextSibling:null,rings:[],nodes:[]};
  if(p){p.classList.add('hov');vpEl.insertBefore(p,vpEl.querySelector(':scope > .btnlayer'));}    // on top of the other links while hovered (below the +/- buttons)
  [s,t].forEach(id=>{const g=nodeEls[id];if(!g)return;g.classList.add('hov');st.nodes.push(g);
    const r=document.createElementNS('http://www.w3.org/2000/svg','rect');r.setAttribute('class','hovring');r.setAttribute('x',-6);r.setAttribute('y',-6);r.setAttribute('width',NW+12);r.setAttribute('height',NH+12);r.setAttribute('rx',9);r.style.opacity='0';requestAnimationFrame(()=>requestAnimationFrame(()=>{r.style.opacity='';}));g.appendChild(r);st.rings.push(r);});
  hovState=st;}
// hovering a link in the side panel highlights the same link in the graph
function panelLinkHover(s,t,on){if(view!=='graph'||!byId[s]||!byId[t])return;
  if(!on){edgeHover(null,null,null,false);return;}
  const a=rep(byId[s]),b=rep(byId[t]);const x=edgeEls.find(x=>x.s===a&&x.t===b);edgeHover(x?x.el:null,a,b,true);}
// Links: the curve itself (edgeCurve) plus an arrowhead drawn as part of the same path (edgeD), so the arrow
// takes the line's colour, width, hover and animation in every browser (Safari ignores context-stroke markers).
const AH=6;
function arrowAt(x,y,dx,dy){const px=-dy,py=dx,w=AH*0.7;return ' M'+(x-dx*AH+px*w)+','+(y-dy*AH+py*w)+' L'+x+','+y+' L'+(x-dx*AH-px*w)+','+(y-dy*AH-py*w);}
function edgeEnd(a,b,o2){const x2=b.x+NW/2+(o2||0),y2=b.y;if(y2>a.y+NH)return [x2,y2,0,1];const by=b.y+NH/2;
  if(b.x>=a.x+NW)return [b.x,by,1,0];if(b.x+NW<=a.x)return [b.x+NW,by,-1,0];return [b.x+NW,by,-1,0];}
function edgeD(a,b,o1,o2,bow){const e=edgeEnd(a,b,o2);return edgeCurve(a,b,o1,o2,bow)+arrowAt(e[0],e[1],e[2],e[3]);}
// A link as a list of cubic Bezier segments [p0,c1,c2,p3]; used both to draw it and to sample it
// for the crossing checks (sampling in plain maths is much faster than asking the browser).
function edgeSegs(a,b,o1,o2,bow){const x1=a.x+NW/2+(o1||0),y1=a.y+NH,x2=b.x+NW/2+(o2||0),y2=b.y;
  // a long link that would lie on top of another link bends sideways a little in the middle
  if(y2>y1&&bow){const xm=(x1+x2)/2+bow,ym=(y1+y2)/2,h=y2-y1;
    return [[[x1,y1],[x1,y1+YG*0.5],[xm,ym-h*0.3],[xm,ym]],[[xm,ym],[xm,ym+h*0.3],[x2,y2-YG*0.5],[x2,y2]]];}
  if(y2>y1)return [[[x1,y1],[x1,y1+YG*0.6],[x2,y2-YG*0.6],[x2,y2]]];
  // not downwards (same row, or a row above in the Lanes grid): leave and enter through the sides that face
  // each other; only a link back up the same column loops round the right-hand side
  const ay=a.y+NH/2,by=b.y+NH/2;
  if(b.x>=a.x+NW){const x1s=a.x+NW,x2s=b.x,m=Math.max(30,(x2s-x1s)*0.45);return [[[x1s,ay],[x1s+m,ay],[x2s-m,by],[x2s,by]]];}
  if(b.x+NW<=a.x){const x1s=a.x,x2s=b.x+NW,m=Math.max(30,(x1s-x2s)*0.45);return [[[x1s,ay],[x1s-m,ay],[x2s+m,by],[x2s,by]]];}
  return [[[a.x+NW,ay],[a.x+NW+60,ay],[b.x+NW+60,by],[b.x+NW,by]]];}
function edgeCurve(a,b,o1,o2,bow){const sg=edgeSegs(a,b,o1,o2,bow);
  return 'M'+sg[0][0][0]+','+sg[0][0][1]+sg.map(q=>' C'+q[1][0]+','+q[1][1]+' '+q[2][0]+','+q[2][1]+' '+q[3][0]+','+q[3][1]).join('');}
function edgeSamples(a,b,o1,o2,bow,n){const sg=edgeSegs(a,b,o1,o2,bow),out=[],per=Math.max(4,Math.round(n/sg.length));
  sg.forEach((q,si)=>{for(let k=(si?1:0);k<=per;k++){const t=k/per,u=1-t,A=u*u*u,B=3*u*u*t,C=3*u*t*t,E=t*t*t;
    out.push([A*q[0][0]+B*q[1][0]+C*q[2][0]+E*q[3][0],A*q[0][1]+B*q[1][1]+C*q[2][1]+E*q[3][1]]);}});return out;}
// Re-render after `change()` and animate: boxes glide from their old place, new boxes grow out of the
// box they were folded into, removed boxes slide into the box that now contains them and fade out.
// opts.keepOld/keepNew: keep that box fixed on screen;  opts.fit: refit the view (animated) instead.
function animatedRerender(change,opts){opts=opts||{};
  const prev={};Object.keys(pos).forEach(id=>prev[id]={x:pos[id].x,y:pos[id].y});const T0={x:T.x,y:T.y,k:T.k};
  const oldEdges=edgeEls.map(x=>({k:x.s+'>'+x.t,el:x.el}));const prevEK=new Set(oldEdges.map(x=>x.k));const oldLanes=lanes.length?vp.firstChild:null;
  const oldEls=nodeEls;const prevRep={};nodes.forEach(n=>{prevRep[n.id]=rep(n);});
  const o=opts.keepOld?pos[opts.keepOld]:null;
  change();renderGraph(false);
  const newRep={};nodes.forEach(n=>{newRep[n.id]=rep(n);});
  let T1;
  if(opts.fit){const Ts={x:T.x,y:T.y,k:T.k};(opts.fit==='initial'?fitInitial:fit)();T1={x:T.x,y:T.y,k:T.k};T.x=Ts.x;T.y=Ts.y;T.k=Ts.k;}
  else{const n2=pos[opts.keepNew]||pos[opts.keepOld];if(o&&n2){T.x=T0.x+o.x*T0.k-n2.x*T.k;T.y=T0.y+o.y*T0.k-n2.y*T.k;applyT();}T1={x:T.x,y:T.y,k:T.k};}
  // positions are compared in graph coordinates of the final view
  const toNew=p=>({x:(T0.x+p.x*T0.k-T1.x)/T1.k,y:(T0.y+p.y*T0.k-T1.y)/T1.k});
  const animT=!!opts.fit;const TA=animT?T0:T1;
  const origin=id=>{if(prev[id])return prev[id];
    if(id[0]==='h'&&prev['g'+id.slice(1)])return prev['g'+id.slice(1)];
    if(id[0]==='g'&&prev['h'+id.slice(1)])return prev['h'+id.slice(1)];
    const n=byId[id];if(n&&prevRep[id]&&prev[prevRep[id]])return prev[prevRep[id]];
    const mem=(id[0]==='h'||id[0]==='g')?groupMembers(id.slice(1)):[];for(const m of mem){if(prev[prevRep[m.id]])return prev[prevRep[m.id]];}
    return opts.keepOld&&prev[opts.keepOld]?prev[opts.keepOld]:null;};
  const start={},fresh=new Set();
  Object.keys(pos).forEach(id=>{const p0=origin(id);if(p0){const q=animT?p0:toNew(p0);start[id]=q;if(!prev[id]&&!(id[0]==='h'&&prev['g'+id.slice(1)])&&!(id[0]==='g'&&prev['h'+id.slice(1)]))fresh.add(id);}else{start[id]=pos[id];fresh.add(id);}});
  // boxes that went away: slide into whatever contains them now, fading out
  const ghosts=[];const gl=document.createElementNS('http://www.w3.org/2000/svg','g');vp.appendChild(gl);
  Object.keys(prev).forEach(id=>{if(pos[id]||!oldEls[id])return;
    if(id[0]==='h'&&pos['g'+id.slice(1)])return;if(id[0]==='g'&&pos['h'+id.slice(1)])return;
    let tgt=null;const n=byId[id];if(n&&pos[newRep[id]])tgt=newRep[id];
    if(!tgt&&(id[0]==='h'||id[0]==='g')){const mem=groupMembers(id.slice(1));for(const m of mem){if(pos[newRep[m.id]]){tgt=newRep[m.id];break;}}}
    if(!tgt&&opts.keepNew&&pos[opts.keepNew])tgt=opts.keepNew;
    const el=oldEls[id];el.style.pointerEvents='none';gl.appendChild(el);
    ghosts.push({el,from:animT?prev[id]:toNew(prev[id]),to:tgt?pos[tgt]:(animT?prev[id]:toNew(prev[id]))});});
  // links that went away fade out where they were; new links fade in
  const newEK=new Set(edgeEls.map(x=>x.s+'>'+x.t));const gEdges=oldEdges.filter(x=>!newEK.has(x.k));
  gEdges.forEach(x=>{x.el.style.pointerEvents='none';gl.insertBefore(x.el,gl.firstChild);});
  const freshE=new Set(edgeEls.filter(x=>!prevEK.has(x.s+'>'+x.t)));
  // block backgrounds (Groups / Components layouts): old ones fade out, new ones fade in
  const newLanes=lanes.length?vp.firstChild:null;if(oldLanes&&oldLanes!==newLanes){oldLanes.style.pointerEvents='none';vp.insertBefore(oldLanes,vp.firstChild);}
  if(graphAnim)cancelAnimationFrame(graphAnim);const t0=performance.now(),dur=opts.dur||(opts.fit?420:320);
  const step=now=>{const a=Math.min(1,(now-t0)/dur),e=a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;const cur={};
    if(animT){T.x=TA.x+(T1.x-TA.x)*e;T.y=TA.y+(T1.y-TA.y)*e;T.k=TA.k+(T1.k-TA.k)*e;applyT();}
    Object.keys(pos).forEach(id=>{const s0=start[id],t1=pos[id];const c={x:s0.x+(t1.x-s0.x)*e,y:s0.y+(t1.y-s0.y)*e};cur[id]=c;const el=nodeEls[id];
      if(el){el.setAttribute('transform','translate('+c.x+','+c.y+')');if(fresh.has(id))el.style.opacity=a<1?String(e):'';}
      const bw=nodeBtnEls[id];if(bw){bw.setAttribute('transform','translate('+c.x+','+c.y+')');if(fresh.has(id))bw.style.opacity=a<1?String(e):'';}});
    edgeEls.forEach(x=>{if(cur[x.s]&&cur[x.t]){{const dd=edgeD(cur[x.s],cur[x.t],x.o1,x.o2,x.bow);x.el.setAttribute('d',dd);if(x.hit)x.hit.setAttribute('d',dd);}if(fresh.has(x.s)||fresh.has(x.t)||freshE.has(x))x.el.style.opacity=a<1?String(e):'';}});
    gEdges.forEach(x=>{x.el.style.opacity=String(1-e);});
    if(newLanes)newLanes.style.opacity=a<1?String(e):'';if(oldLanes&&oldLanes!==newLanes){oldLanes.style.opacity=String(1-e);if(a>=1)oldLanes.remove();}
    ghosts.forEach(g=>{const c={x:g.from.x+(g.to.x-g.from.x)*e,y:g.from.y+(g.to.y-g.from.y)*e};g.el.setAttribute('transform','translate('+c.x+','+c.y+')');g.el.style.opacity=String(1-e);});
    if(a<1)graphAnim=requestAnimationFrame(step);else{graphAnim=null;gl.remove();}};
  graphAnim=requestAnimationFrame(step);}
let searchOpen=new Set();let layoutMode='lanes',lanes=[];const collapsedNodes=new Set();let collapseEverything=false;
// block layouts: 'lanes' = one block per top-level timeline group, 'comps' = one block per component
const isLanes=()=>layoutMode==='lanes'||layoutMode==='comps';
const compOf={};D.edges.forEach(e=>{if(e.k.includes('incomp')&&byId[e.s]&&byId[e.s].cat==='component')compOf[e.t]=e.s;});
nodes.forEach(n=>{if(n.cat==='component')compOf[n.id]=n.id;});
const cColor={};nodes.filter(n=>n.cat==='component').sort((a,b)=>a.o-b.o).forEach((n,i)=>cColor[n.id]=GCOL[(i+3)%GCOL.length]);
// user parameters get a block of their own; a derived parameter stays with its Derive feature
const dparamOf={};D.edges.forEach(e=>{if(byId[e.t]&&byId[e.t].type==='DerivedParameter'&&byId[e.s]&&byId[e.s].tl!=null)dparamOf[e.t]=e.s;});
function laneKey(n){if(!n)return null;if(n.type==='UserParameter'||n.type==='UserParameters')return '_params';
  if(n.type==='DerivedParameter'&&dparamOf[n.id])return laneKey(byId[dparamOf[n.id]]);
  if(layoutMode==='comps')return compOf[n.id]||'_root';return topGroup(n)||'_none';}
function rep(n){const gp=n.g||[];for(const g of gp){if(!expanded.has(g)&&!searchOpen.has(g))return 'g'+g;}
  const ps=pseudoOf(n);if(ps&&groups[ps]&&!expanded.has(ps)&&!searchOpen.has(ps))return 'g'+ps;return n.id;}
let NW=210,NH=26;const XG=18,YG=64;// top-down: XG = gap between boxes in a row, YG = gap between rows
function renderGraph(fitAfter,centerId){
  if(PB)stopPlay();   // a re-render replaces the boxes the playback is animating
  const gth=hasThumbs&&showThumbs;NW=gth?240:210;NH=gth?50:26;
  searchOpen=new Set();let hitReps=null;
  if(search){nodes.filter(n=>visibleNode(n)&&matches(n)).forEach(n=>{(n.g||[]).forEach(g=>searchOpen.add(g));const ps=pseudoOf(n);if(ps)searchOpen.add(ps);});}
  const vis=nodes.filter(visibleNode);
  let keep=null;
  if(focus&&selected){keep=new Set([selected,...closure(selected,'up'),...closure(selected,'down')]);}
  else if(focus&&selGroup&&groups[selGroup]){const mem=groupMembers(selGroup).map(n=>n.id);const g=groups[selGroup];
    keep=new Set([...mem,...(g.dsupp||mem.flatMap(i=>[...closure(i,'down')])),...mem.flatMap(i=>[...closure(i,'up')])]);}
  const R={};// rep -> {id,label,o,members,isGroup}
  vis.forEach(n=>{if(keep&&!keep.has(n.id))return;const r=rep(n);if(!R[r]){R[r]={id:r,o:n.o,members:[],isGroup:r!==n.id};}R[r].members.push(n);R[r].o=Math.min(R[r].o,n.o);});
  let reps=Object.values(R).sort((a,b)=>a.o-b.o);
  if(search){hitReps=new Set(reps.filter(r=>r.members.some(matches)).map(r=>r.id));}
  graphHits=hitReps?reps.filter(r=>hitReps.has(r.id)).map(r=>r.id):[];
  const ek={};let redges=[];
  EFF.forEach(e=>{if(keep&&(!keep.has(e.s)||!keep.has(e.t)))return;const a=rep(byId[e.s]),b=rep(byId[e.t]);if(a===b||!R[a]||!R[b])return;const k=a+'>'+b;if(!ek[k]){ek[k]={s:a,t:b,n:0,src:[]};redges.push(ek[k]);}ek[k].n++;ek[k].src.push(e);});
  if(useTestLinks()){redges=redges.filter(e=>{const gg=R[e.s].isGroup&&R[e.t].isGroup;if(gg)delete ek[e.s+'>'+e.t];return !gg;});
    testUnitLinks().forEach((m,a)=>{if(!R[a]||!R[a].isGroup)return;m.forEach((c,b)=>{if(!R[b])return;const k=a+'>'+b;if(ek[k]){ek[k].src.push({s:a,t:b,k:['suppress']});return;}const x={s:a,t:b,n:c,src:[{s:a,t:b,k:['suppress']}]};ek[k]=x;redges.push(x);});});}
  // collapsed boxes hide everything that depends on them (their whole downstream)
  const kids={};redges.forEach(e=>{(kids[e.s]=kids[e.s]||[]).push(e.t);});
  // the collapse button only appears where collapsing would hide something: in the Groups layout that means
  // something depending on the box inside its own timeline group (a collapsed box always keeps its button)
  const laneOfR=id=>R[id]?laneKey(R[id].members[0]):null;
  const canCollapse=id=>{if(!kids[id]||!kids[id].length)return false;if(!isLanes()||collapsedNodes.has(id))return true;
    const l=laneOfR(id),st=[...kids[id]],seen=new Set();while(st.length){const x=st.pop();if(seen.has(x)||x===id)continue;seen.add(x);if(laneOfR(x)===l)return true;(kids[x]||[]).forEach(y=>st.push(y));}return false;};
  if(collapseEverything){collapseEverything=false;reps.forEach(r=>{if(canCollapse(r.id))collapsedNodes.add(r.id);});}
  const hiddenBy={};collapsedNodes.forEach(c=>{if(!R[c])return;const st=[...(kids[c]||[])];const seen=new Set();while(st.length){const x=st.pop();if(seen.has(x)||x===c)continue;seen.add(x);(kids[x]||[]).forEach(y=>st.push(y));}
    // in Lanes view a collapsed box only hides what depends on it inside its own timeline group
    const lane=id=>R[id]?laneKey(R[id].members[0]):null;const cl=lane(c);
    seen.forEach(x=>{if(isLanes()&&lane(x)!==cl)return;(hiddenBy[x]=hiddenBy[x]||[]).push(c);});});
  const hiddenCount={};collapsedNodes.forEach(c=>{hiddenCount[c]=0;});Object.keys(hiddenBy).forEach(x=>hiddenBy[x].forEach(c=>hiddenCount[c]++));
  Object.keys(hiddenBy).forEach(x=>{if(collapsedNodes.has(x)&&hiddenBy[x].every(c=>c===x))delete hiddenBy[x];});
  reps=reps.filter(r=>!hiddenBy[r.id]);
  // links touching a hidden box are kept and attached to the collapsed box that hides it, so what is still
  // on screen keeps its dependency (merged with any link that already joins the same two boxes)
  {const vis=x=>{if(!hiddenBy[x])return x;const c=hiddenBy[x].find(c=>!hiddenBy[c]);return c||null;};const m={};const out=[];
    redges.forEach(e=>{const a=vis(e.s),b=vis(e.t);if(!a||!b||a===b)return;const k=a+'>'+b;
      if(a===e.s&&b===e.t&&!m[k]){m[k]=e;out.push(e);return;}
      if(m[k]){m[k].n+=e.n;m[k].src.push(...e.src);if(a===e.s&&b===e.t)m[k].via=false;return;}
      const x={s:a,t:b,n:e.n,src:[...e.src],via:true};m[k]=x;out.push(x);});
    redges=out;}
  const pr={};reps.forEach(r=>pr[r.id]=[]);redges.forEach(e=>{pr[e.t].push(e.s);});
  const layer={};reps.forEach(r=>{let l=0;pr[r.id].forEach(s=>{if(R[s].o<r.o&&layer[s]!=null)l=Math.max(l,layer[s]+1);});layer[r.id]=l;});
  // expanded timeline groups keep a header box above their items, linked to the group's first items
  const headers=[];const openG=g=>expanded.has(g)||searchOpen.has(g);
  // group boxes for expanded groups only in Groups mode; Items mode is a pure item graph
  if(!isLanes()){const repSet=new Set(reps.map(r=>r.id));   // Depth layout: every open timeline group has a header box above its items
    D.groups.forEach(g=>{if(!openG(g.id))return;let p=g.parent,ok=true,guard=0;while(p&&guard++<20){if(!openG(p)){ok=false;break;}p=groups[p]?groups[p].parent:null;}if(!ok)return;
      const ch=new Set(),mem=[];groupMembers(g.id).forEach(n=>{const r=rep(n);if(!repSet.has(r))return;mem.push(n);const path=n.g||[];const nx=path[path.indexOf(g.id)+1];ch.add(nx&&openG(nx)?'h'+nx:r);});
      if(mem.length)headers.push({id:'h'+g.id,gid:g.id,depth:groupPath(g.id).length,ch:[...ch],mem});});
    const hset=new Set(headers.map(h=>h.id));headers.sort((a,b)=>b.depth-a.depth);
    headers.forEach(h=>{h.ch=h.ch.filter(c=>c[0]!=='h'||hset.has(c));
      const inG=new Set([...h.mem.map(n=>rep(n)),...h.ch]);
      h.targets=h.ch.filter(c=>c[0]==='h'||!pr[c]||!pr[c].some(s=>inG.has(s)));if(!h.targets.length)h.targets=h.ch.slice(0,1);
      R[h.id]={id:h.id,o:Math.min(...h.mem.map(n=>n.o))-0.5,members:h.mem,isGroup:true,header:true};reps.push(R[h.id]);pr[h.id]=[];
      layer[h.id]=Math.min(...h.targets.map(t=>layer[t]!=null?layer[t]:0))-1;});}
  const cols={};reps.forEach(r=>{(cols[layer[r.id]]=cols[layer[r.id]]||[]).push(r);});
  pos={};lanes=[];const Ls=Object.keys(cols).map(Number).sort((a,b)=>a-b);
  const laneLevels=[];
  if(isLanes()){
    // one column per top-level timeline group, in timeline order; rows inside a lane follow the
    // dependencies between that lane's own boxes, so each lane starts at the top
    const laneOf=r=>laneKey(r.members[0]);const LN={};
    reps.forEach(r=>{const k=laneOf(r);(LN[k]=LN[k]||{id:k,o:1e9,items:[]});LN[k].o=Math.min(LN[k].o,r.o);LN[k].items.push(r);});
    const order=Object.values(LN).sort((a,b)=>a.o-b.o);const LG=46,TOP=44,SUBG=YG*0.45;
    // 1) each lane on its own: rows by dependency depth, long rows wrap into a small grid
    const built=order.map(l=>{const inL=new Set(l.items.map(r=>r.id));const ll={};l.items.sort((a,b)=>a.o-b.o).forEach(r=>{let d=0;pr[r.id].forEach(s=>{if(inL.has(s)&&ll[s]!=null&&R[s].o<r.o)d=Math.max(d,ll[s]+1);});ll[r.id]=d;});
      const rows={};l.items.forEach(r=>{(rows[ll[r.id]]=rows[ll[r.id]]||[]).push(r);});
      const maxc=Math.max(2,Math.min(5,Math.ceil(Math.sqrt(l.items.length))));
      const loc={};let y=TOP,w=1;const levels=[];
      Object.keys(rows).map(Number).sort((a,b)=>a-b).forEach((L,li)=>{const a=rows[L];if(li)y+=YG;const lv={ids:a.map(r=>r.id),ys:[],home:{}};levels.push(lv);
        for(let i=0;i<a.length;i+=maxc){if(i)y+=SUBG;const ch=a.slice(i,i+maxc);lv.ys.push(y);ch.forEach((r,k)=>{loc[r.id]={x:k*(NW+XG),y:y};lv.home[r.id]=(lv.ys.length-1)*1000+k;});w=Math.max(w,ch.length);y+=NH;}});
      // the places a row may use when the selection pulls boxes together: every column of the block, on the row's own lines
      levels.forEach(lv=>{lv.slots=[];lv.ys.forEach((yy,ri)=>{for(let k=0;k<w;k++)lv.slots.push({x:k*(NW+XG),y:yy,key:ri*1000+k});});});
      return {l,loc,levels,w:w*(NW+XG)-XG,h:y+18};});
    // 2) lanes packed left to right into rows of lanes, aiming at a roughly 16:10 overall shape
    const area=built.reduce((a,b)=>a+(b.w+LG)*(b.h+LG),0);const target=Math.max(Math.max(...built.map(b=>b.w)),Math.sqrt(area*1.6));
    let lx=0,ly=0,rowH=0;
    built.forEach(b=>{if(lx>0&&lx+b.w>target){lx=0;ly+=rowH+LG;rowH=0;}
      Object.keys(b.loc).forEach(id=>{pos[id]={x:lx+b.loc[id].x,y:ly+b.loc[id].y};});
      b.levels.forEach(lv=>laneLevels.push({ids:lv.ids,home:lv.home,slots:lv.slots.map(q=>({x:lx+q.x,y:ly+q.y,key:q.key}))}));
      lanes.push({id:b.l.id,x:lx-14,w:b.w+28,y0:ly,y1:ly+b.h});lx+=b.w+LG;rowH=Math.max(rowH,b.h);});
  }else
  Ls.forEach(L=>{const c=cols[L];c.forEach(r=>{const xs=pr[r.id].map(s=>pos[s]?pos[s].x+NW/2:null).filter(v=>v!=null);r.bc=xs.length?xs.reduce((a,b)=>a+b,0)/xs.length:null;});
    const withBc=c.filter(r=>r.bc!=null);const avg=withBc.length?withBc.reduce((a,r)=>a+r.bc,0)/withBc.length:0;
    c.forEach(r=>{if(r.bc==null)r.bc=avg+r.o*0.001;});
    c.sort((a,b)=>a.bc-b.bc||a.o-b.o);let x=-1e9;const y=L*(NH+YG);
    c.forEach(r=>{x=Math.max(x,r.bc-NW/2);pos[r.id]={x:x,y:y};x+=NW+XG;});
    // shift the row so on average boxes sit under their parents
    const sh=c.reduce((a,r)=>a+(r.bc-(pos[r.id].x+NW/2)),0)/c.length;c.forEach(r=>{pos[r.id].x+=sh;});});
  if(headers.length){headers.forEach(h=>{const xs=h.targets.map(t=>pos[t]?pos[t].x:null).filter(v=>v!=null);if(xs.length&&pos[h.id])pos[h.id].x=xs.reduce((a,b)=>a+b,0)/xs.length;});
    const rows={};Object.keys(pos).forEach(id=>{(rows[pos[id].y]=rows[pos[id].y]||[]).push(id);});
    Object.values(rows).forEach(ids=>{ids.sort((a,b)=>pos[a].x-pos[b].x);for(let i=1;i<ids.length;i++){const m=pos[ids[i-1]].x+NW+XG;if(pos[ids[i]].x<m)pos[ids[i]].x=m;}});
    headers.forEach(h=>h.targets.forEach(t=>{if(pos[t])redges.push({s:h.id,t:t,n:0,src:[],contain:true});}));}
  // relation sets for highlight
  let up=null,down=null,selRep=null,selSet=null;selRelated=[];
  if(selected&&byId[selected]){const s=byId[selected];selRep=rep(s);selSet=new Set([selRep]);up=new Set([...closure(selected,'up')].map(i=>rep(byId[i])));down=new Set([...closure(selected,'down')].map(i=>rep(byId[i])));}
  else if(selGroup&&groups[selGroup]){const mem=groupMembers(selGroup);const mset=new Set(mem.map(n=>n.id));selSet=new Set(mem.map(n=>rep(n)));if(R['h'+selGroup])selSet.add('h'+selGroup);selRep='__group__';
    // a group depends on everything any of its items depends on (links, incl. user parameters and items outside
    // groups), plus, with the group test, the groups whose suppression takes items of this group with them
    const g=groups[selGroup];const dnLinks=[...new Set(mem.flatMap(n=>[...closure(n.id,'down')]))];
    const dn=[...new Set([...(g.dsupp?[...g.dsupp,...(g.dbreak||[])]:[]),...dnLinks])].filter(i=>!mset.has(i));
    down=new Set(dn.filter(i=>byId[i]&&visibleNode(byId[i])).map(i=>rep(byId[i])));
    const upIds=groupUpIds(selGroup);
    up=new Set(upIds.filter(i=>byId[i]).map(i=>rep(byId[i])));selSet.forEach(r=>{up.delete(r);down.delete(r);});}
  if(selSet)selRelated=[...new Set([...selSet,...(up||[]),...(down||[])])].filter(r=>r!=='__group__');
  // selection: pull the related boxes together in each row, centred under the selection;
  // unrelated boxes in those rows move aside (rows themselves stay where they are)
  // Groups / Components layouts: boxes stay inside their block, but within each block the related boxes take the
  // places nearest to the selection (each dependency level of a block keeps its own places)
  if(selSet&&isLanes()&&!focus&&pullTogether){const rel=new Set([...selSet,...(up||[]),...(down||[])]);const sx=[...selSet].map(i=>pos[i]).filter(Boolean);
    if(sx.length&&rel.size>1){const cx=sx.reduce((a,p)=>a+p.x+NW/2,0)/sx.length,cy=sx.reduce((a,p)=>a+p.y+NH/2,0)/sx.length;
      const dist=q=>Math.abs(q.x+NW/2-cx)+0.6*Math.abs(q.y+NH/2-cy);
      laneLevels.forEach(L=>{const ids=L.ids.filter(i=>pos[i]);const inR=ids.filter(i=>rel.has(i));if(!inR.length||inR.length===ids.length)return;
        const slots=L.slots.map((q,i)=>({q,i}));const near=slots.slice().sort((a,b)=>dist(a.q)-dist(b.q)||a.i-b.i);
        const taken=new Set();inR.forEach((id,k)=>{const sl=near[k];taken.add(sl.i);pos[id]={x:sl.q.x,y:sl.q.y};});
        // the others keep their own place when it is still free, otherwise take the first free one
        const others=ids.filter(i=>!rel.has(i));const byKey={};slots.forEach(sl=>{byKey[sl.q.key]=sl;});const wait=[];
        others.forEach(id=>{const sl=byKey[L.home[id]];if(sl&&!taken.has(sl.i)){taken.add(sl.i);pos[id]={x:sl.q.x,y:sl.q.y};}else wait.push(id);});
        const free=slots.filter(sl=>!taken.has(sl.i));wait.forEach((id,k)=>{const sl=free[k];if(sl){pos[id]={x:sl.q.x,y:sl.q.y};}});});}}
  if(selSet&&!isLanes()&&!focus&&pullTogether){const rel=new Set([...selSet,...(up||[]),...(down||[])]);
    const hdrOf={};Object.keys(pos).forEach(id=>{if(id[0]==='h'&&R[id]&&R[id].members.some(m=>rel.has(rep(m))))rel.add(id);});
    const sx=[...selSet].map(i=>pos[i]).filter(Boolean);
    if(sx.length&&rel.size>1){const cx=sx.reduce((a,p)=>a+p.x+NW/2,0)/sx.length;const rows={};
      Object.keys(pos).forEach(id=>{(rows[pos[id].y]=rows[pos[id].y]||[]).push(id);});
      Object.values(rows).forEach(ids=>{const inR=ids.filter(i=>rel.has(i)).sort((a,b)=>pos[a].x-pos[b].x);if(!inR.length)return;
        const w=inR.length*(NW+XG)-XG;let x=cx-w/2;const x0=x,x1=x0+w;inR.forEach(i=>{pos[i]={x:x,y:pos[i].y};x+=NW+XG;});
        const left=ids.filter(i=>!rel.has(i)&&pos[i].x+NW/2<cx).sort((a,b)=>pos[b].x-pos[a].x);let cur=x0-XG-NW;
        left.forEach(i=>{const nx=Math.min(pos[i].x,cur);pos[i]={x:nx,y:pos[i].y};cur=nx-XG-NW;});
        const right=ids.filter(i=>!rel.has(i)&&pos[i].x+NW/2>=cx).sort((a,b)=>pos[a].x-pos[b].x);cur=x1+XG;
        right.forEach(i=>{const nx=Math.max(pos[i].x,cur);pos[i]={x:nx,y:pos[i].y};cur=nx+NW+XG;});});}}
  const hdrRelated=r=>!!selSet&&r.members.some(m=>{const x=rep(m);return selSet.has(x)||(up&&up.has(x))||(down&&down.has(x))||m.id===selected;});
  const NS='http://www.w3.org/2000/svg';hovState=null;hovPin=null;vp.innerHTML='';
  // +/- buttons live in a layer above everything, so links never cover them; each box's buttons follow the box
  const btnLayer=document.createElementNS(NS,'g');btnLayer.setAttribute('class','btnlayer');nodeBtnEls={};
  const addBtn=(id,bt)=>{let w=nodeBtnEls[id];if(!w){w=document.createElementNS(NS,'g');w.setAttribute('transform','translate('+pos[id].x+','+pos[id].y+')');nodeBtnEls[id]=w;btnLayer.appendChild(w);}w.appendChild(bt);};
  if(lanes.length){const gl=document.createElementNS(NS,'g');vp.appendChild(gl);
    const CM=layoutMode==='comps';const noLane=id=>id==='_none'||id==='_root'||(id==='_params'&&!byId['up:all']);
    lanes.forEach(l=>{const col=l.id==='_params'?'var(--c-param)':noLane(l.id)?'var(--muted)':((CM?cColor[l.id]:gColor[l.id])||'var(--muted)');const bg=document.createElementNS(NS,'rect');bg.setAttribute('x',l.x);bg.setAttribute('y',l.y0);bg.setAttribute('width',l.w);bg.setAttribute('height',l.y1-l.y0);bg.setAttribute('rx',10);
      bg.setAttribute('style','fill:'+col+';fill-opacity:.07;stroke:'+col+';stroke-opacity:.45;stroke-width:1.5');
      const tx=document.createElementNS(NS,'text');tx.setAttribute('x',l.x+12);tx.setAttribute('y',l.y0+24);tx.setAttribute('style','font-size:15px;font-weight:700;fill:'+col);{const full=l.id==='_params'?'User parameters':l.id==='_none'?'Not in a group':l.id==='_root'?'Root component':CM?(byId[l.id]?byId[l.id].name:l.id):(groups[l.id]?groups[l.id].name:l.id);const mc=Math.max(4,Math.floor((l.w-24)/9));tx.textContent=full.length>mc?full.slice(0,mc-1)+'…':full;const tt=document.createElementNS(NS,'title');tt.textContent=full;tx.appendChild(tt);}
      // fold button on the block title: a whole timeline group (Groups layout) or a whole component (Components layout)
      {const gidL=CM?null:l.id,cidL=CM&&byId[l.id]?l.id:null,psL=(l.id==='_params'||(l.id==='_none'&&!CM))?l.id:null;const gk=psL||gidL;
        const can=(gk&&groups[gk])||(cidL&&canCollapse(cidL))||(cidL&&collapsedNodes.has(cidL));
        if(can){const open=cidL?!collapsedNodes.has(cidL):(expanded.has(gk)||searchOpen.has(gk));const bt=document.createElementNS(NS,'g');bt.setAttribute('class','ctog');bt.setAttribute('transform','translate('+(l.x+l.w/2)+','+(l.y1+1)+')');   // bottom centre, like the boxes' buttons
          const c=document.createElementNS(NS,'circle');c.setAttribute('r',open?8:9);c.setAttribute('class','ctogc'+(open?'':' col'));const t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','middle');t.setAttribute('y',4);t.setAttribute('class','ctogt');t.textContent=open?'−':'+';
          const tt=document.createElementNS(NS,'title');tt.textContent=open?(cidL?'Fold this component into its box':'Fold this whole group into one box'):(cidL?'Show what is in this component':'Show the items of this group');bt.append(c,t,tt);
          bt.addEventListener('mousedown',ev=>ev.stopPropagation());
          bt.addEventListener('click',ev=>{ev.stopPropagation();animatedRerender(()=>{if(cidL){if(open)collapsedNodes.add(cidL);else collapsedNodes.delete(cidL);}else if(open){expanded.delete(gk);Object.keys(groups).forEach(x=>{let q=groups[x].parent,gd=0;while(q&&gd++<20){if(q===gk){expanded.delete(x);break;}q=groups[q]?groups[q].parent:null;}});}else expanded.add(gk);},{});});
          l.btn=bt;}}
      const lg=document.createElementNS(NS,'g');lg.style.cursor=noLane(l.id)?'default':'pointer';lg.append(bg,tx);if(l.btn)btnLayer.appendChild(l.btn);if(!noLane(l.id))lg.addEventListener('click',ev=>{ev.stopPropagation();if(moved)return;if(l.id==='_params')select('up:all');else if(CM)select(l.id);else selectGroup(l.id);});gl.appendChild(lg);});}
  const ge=document.createElementNS(NS,'g');vp.appendChild(ge);const geHi=document.createElementNS(NS,'g');
  nodeEls={};edgeEls=[];const brkBadges=[];
  // Link routing (bends and the order of link ends on boxes) depends only on where the boxes are, so it is
  // remembered and reused when a re-render (a click, a hover list, the preview) leaves every box in place.
  const ek2=e=>e.s+'>'+e.t+(e.contain?'#c':'');
  const routeKey=NW+'|'+NH+'|'+redges.map(e=>{const a=pos[e.s],b=pos[e.t];return a&&b?ek2(e)+'@'+Math.round(a.x)+','+Math.round(a.y)+';'+Math.round(b.x)+','+Math.round(b.y):'';}).join('|');
  const routeHit=!!(routeCache&&routeCache.key===routeKey);
  // Links spanning more than one row that would lie on top of another link (same column, overlapping
  // height) bend sideways a little. Boxes may be crossed; only lines are kept apart.
  const bowOf=new Map();if(routeHit)redges.forEach(e=>{const v=routeCache.bow[ek2(e)];if(v)bowOf.set(e,v);});else{const cx=id=>pos[id].x+NW/2;
    const segs=redges.filter(e=>pos[e.s]&&pos[e.t]&&pos[e.t].y>pos[e.s].y);
    segs.forEach(e=>{if(e.contain)return;const a=pos[e.s],b=pos[e.t];if(b.y-a.y<=NH+YG+1)return;
      if(Math.abs(cx(e.s)-cx(e.t))>NW*0.6)return;
      const y1=a.y+NH,y2=b.y,xmid=(cx(e.s)+cx(e.t))/2;
      const hit=segs.some(f=>f!==e&&!(pos[f.t].y<=y1||pos[f.s].y+NH>=y2)&&Math.abs((cx(f.s)+cx(f.t))/2-xmid)<NW*0.25&&(pos[f.t].y-pos[f.s].y)<(b.y-a.y));
      if(hit)bowOf.set(e,-Math.min(70,NW*0.3));});}
  // shared helpers: sample a link's drawn curve, and test two sampled curves for crossings
  const ptsCache=new Map();
  const pts=(e,o1,o2,bow)=>{const k=e.s+'>'+e.t+'|'+(o1||0)+'|'+(o2||0)+'|'+(bow||0);let P=ptsCache.get(k);if(P)return P;
    P=edgeSamples(pos[e.s],pos[e.t],o1,o2,bow,28);let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;P.forEach(q=>{if(q[0]<x0)x0=q[0];if(q[0]>x1)x1=q[0];if(q[1]<y0)y0=q[1];if(q[1]>y1)y1=q[1];});
    P.bb=[x0,y0,x1,y1];ptsCache.set(k,P);return P;};
  const segX=(a,b,c,d)=>{const o=(p,q,r)=>(q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0]);
    return o(a,b,c)*o(a,b,d)<0&&o(c,d,a)*o(c,d,b)<0;};
  const cross=(A,B)=>{if(A.bb&&B.bb&&(A.bb[2]<B.bb[0]||B.bb[2]<A.bb[0]||A.bb[3]<B.bb[1]||B.bb[3]<A.bb[1]))return false;for(let i=0;i<A.length-1;i++)for(let j=0;j<B.length-1;j++)if(segX(A[i],A[i+1],B[j],B[j+1]))return true;return false;};
  const near=(A,B)=>{if(A.bb&&B.bb&&(A.bb[2]+7<B.bb[0]||B.bb[2]+7<A.bb[0]||A.bb[3]+7<B.bb[1]||B.bb[3]+7<A.bb[1]))return false;for(let i=6;i<A.length-6;i++)for(let j=0;j<B.length;j++){const dx=A[i][0]-B[j][0],dy=A[i][1]-B[j][1];if(dx*dx+dy*dy<49)return true;}return false;};
  // bent links: try a few bends on both sides and keep the one that crosses the fewest links sharing
  // an end with it (and does not lie on top of one); smaller bends win ties
  if(!routeHit){const down=redges.filter(e=>!e.contain&&pos[e.s]&&pos[e.t]&&pos[e.t].y>pos[e.s].y);const byNode={};
    down.forEach(e=>{(byNode[e.s]=byNode[e.s]||[]).push(e);(byNode[e.t]=byNode[e.t]||[]).push(e);});
    const cands=[0.3,0.45,0.6,0.8].flatMap(f=>[-f*NW,f*NW]).map(v=>Math.round(v));
    for(let pass=0;pass<2;pass++)[...bowOf.keys()].forEach(e=>{const rel=[...new Set([...(byNode[e.s]||[]),...(byNode[e.t]||[])])].filter(f=>f!==e).slice(0,40);
      let best=null;cands.forEach(b=>{const P=pts(e,0,0,b);let sc=0;rel.forEach(f=>{const Q=pts(f,0,0,bowOf.get(f)||0);if(cross(P,Q))sc+=10;else if(near(P,Q))sc+=3;});
        sc+=Math.abs(b)/NW;if(!best||sc<best.sc)best={b,sc};});bowOf.set(e,best.b);});}
  // spread the ends of the links along the bottom/top of each box, ordered by the side each link
  // really comes from (a bent link counts from its bend), so links meeting at a box do not cross
  const port=new Map();if(routeHit)redges.forEach(e=>{const v=routeCache.port[ek2(e)];if(v)port.set(e,{o1:v[0],o2:v[1]});});else{const outs={},ins={};
    redges.forEach(e=>{const a=pos[e.s],b=pos[e.t];if(!a||!b||b.y<=a.y)return;(outs[e.s]=outs[e.s]||[]).push(e);(ins[e.t]=ins[e.t]||[]).push(e);});
    const spread=(list,side)=>{const n=list.length;if(n<2)return;const step=Math.min(12,NW*0.5/(n-1));
      list.forEach((e,i)=>{const o=(i-(n-1)/2)*step;const m=port.get(e)||{};m[side]=o;port.set(e,m);});};
    // order by the direction each link leaves/arrives from: sideways distance per unit of height, measured
    // to the other end (or to the bend of a bent link). A flatter link sits further out, so none cross.
    const bw=e=>bowOf.get(e)||0;
    // For each pair of links meeting at a box, try both orders on the real curves and keep the one where
    // they do not cross; if both (or neither) work, the link that is further out one gap away goes outside.
    const xAt=(e,atY)=>{const P=pts(e,0,0,bw(e));for(let k=1;k<P.length;k++){const a=P[k-1],b=P[k];if((a[1]-atY)*(b[1]-atY)<=0){const t=(atY-a[1])/((b[1]-a[1])||1);return a[0]+(b[0]-a[0])*t;}}return P[P.length-1][0];};
    const order=(list,end)=>{const n=list.length;if(n<2)return list;const step=Math.min(12,NW*0.5/(n-1)),h=step/2;
      const many=n>12;   // many links at one box: order them by direction only (comparing every pair of curves is too slow)
      const key=new Map(list.map(e=>[e,end==='in'?xAt(e,pos[e.t].y-Math.min(YG*0.9,(pos[e.t].y-pos[e.s].y-NH)*0.5)):xAt(e,pos[e.s].y+NH+Math.min(YG*0.9,(pos[e.t].y-pos[e.s].y-NH)*0.5))]));
      const P=(e,o)=>end==='in'?pts(e,0,o,bw(e)):pts(e,o,0,bw(e));const memo=new Map();
      const cmp=(e,f)=>{const k=e.s+'>'+e.t+'|'+f.s+'>'+f.t;if(memo.has(k))return memo.get(k);
        const ef=cross(P(e,-h),P(f,h)),fe=cross(P(f,-h),P(e,h));const r=ef&&!fe?1:(!ef&&fe?-1:(key.get(e)-key.get(f)));memo.set(k,r);return r;};
      // insertion sort: stable with a comparator that may not be perfectly transitive
      if(many)return list.slice().sort((e,f)=>key.get(e)-key.get(f));
      const out=[];list.slice().sort((e,f)=>key.get(e)-key.get(f)).forEach(e=>{let i=out.length;while(i>0&&cmp(out[i-1],e)>0)i--;out.splice(i,0,e);});return out;};
    Object.values(outs).forEach(l=>spread(order(l,'out'),'o1'));
    Object.values(ins).forEach(l=>spread(order(l,'in'),'o2'));
    // repair: the order at the start and at the end of a link are chosen separately and can disagree.
    // For every pair of links that still cross, try swapping their ends at the shared box and keep the
    // swap if it lowers the number of crossings among the links around them.
    const P2=e=>{const m=port.get(e)||{};return pts(e,m.o1||0,m.o2||0,bw(e));};
    const nb={};redges.forEach(e=>{if(!pos[e.s]||!pos[e.t]||pos[e.t].y<=pos[e.s].y)return;(nb[e.s]=nb[e.s]||[]).push(e);(nb[e.t]=nb[e.t]||[]).push(e);});
    const around=(e,f)=>[...new Set([...(nb[e.s]||[]),...(nb[e.t]||[]),...(nb[f.s]||[]),...(nb[f.t]||[])])];
    // only e and f change when their ends swap, so only their crossings need counting
    const count2=(e,f,list)=>{const A=P2(e),B=P2(f);let c=cross(A,B)?1:0;list.forEach(g=>{if(g===e||g===f)return;const G=P2(g);if(cross(A,G))c++;if(cross(B,G))c++;});return c;};
    const swapEnd=(e,f,side)=>{const a=port.get(e)||{},b=port.get(f)||{};const t=a[side];a[side]=b[side];b[side]=t;port.set(e,a);port.set(f,b);};
    for(let pass=0;pass<(redges.length>1500?0:3);pass++){let changed=false;
      [[ins,'o2'],[outs,'o1']].forEach(([grp,side])=>Object.values(grp).forEach(l=>{if(l.length>12)return;
        for(let i=0;i<l.length;i++)for(let j=i+1;j<l.length;j++){const e=l[i],f=l[j];if(!cross(P2(e),P2(f)))continue;
          const ar=around(e,f);const before=count2(e,f,ar);swapEnd(e,f,side);if(count2(e,f,ar)<before)changed=true;else swapEnd(e,f,side);}}));
      if(!changed)break;}}
  if(!routeHit){const bow={},prt={};bowOf.forEach((v,e)=>{bow[ek2(e)]=v;});port.forEach((v,e)=>{prt[ek2(e)]=[v.o1||0,v.o2||0];});routeCache={key:routeKey,bow,port:prt};}

  redges.forEach(e=>{const a=pos[e.s],b=pos[e.t];if(!a||!b)return;const p=document.createElementNS(NS,'path');
    const pm=port.get(e)||{};const bow=bowOf.get(e)||0;
    const d=edgeD(a,b,pm.o1,pm.o2,bow);edgeEls.push({el:p,s:e.s,t:e.t,o1:pm.o1,o2:pm.o2,bow});
    p.setAttribute('d',d);let cls=e.contain?'edge contain':'edge'+(e.src.every(x=>x.k.every(k=>k==='order'||(k!=='suppress'&&!kindOn[k])))?' order':'')+(!e.contain&&(e.via||e.src.length&&e.src.every(x=>x.via))?' bridge':'');
    if(e.contain){const gc=colorOfGroup(e.s.slice(1));if(gc)p.setAttribute('style','stroke:'+gc);}
    if(selRep&&e.contain){if(!(selSet.has(e.s)||selSet.has(e.t)||up.has(e.t)||down.has(e.t)))cls+=' dim';}
    else if(selRep){
      if(selSet.has(e.s)&&selSet.has(e.t))cls+=' insel';   // a link inside the selection (between items of a selected group)
      else if(selSet.has(e.t)&&up.has(e.s))cls+=' up';else if(selSet.has(e.s)&&down.has(e.t))cls+=' down';else if(up.has(e.s)&&up.has(e.t))cls+=' up ind';else if(down.has(e.s)&&down.has(e.t))cls+=' down ind';else cls+=' dim';}
    if(!selSet&&hitReps&&!hitReps.has(e.s)&&!hitReps.has(e.t))cls+=' dim';
    p.setAttribute('class',cls);const lift=/ (up|down|insel)\b/.test(cls);
    const t=document.createElementNS(NS,'title');t.textContent=e.contain?'Part of timeline group '+(groups[e.s.slice(1)]?groups[e.s.slice(1)].name:''):[...new Set(e.src.flatMap(x=>x.k))].map(x=>KIND[x]||x).join(', ')+(e.n>1?' ('+e.n+' links)':'');if(!e.contain&&e.via)t.textContent+=' (through collapsed items)';else if(!e.contain&&/ bridge/.test(cls))t.textContent+=' (through hidden items)';p.appendChild(t);const layer=lift?geHi:ge;layer.appendChild(p);
    // wide invisible twin that catches the mouse; hovering shows the link bold and rings both ends
    const h=document.createElementNS(NS,'path');h.setAttribute('class','ehit'+(/\bdim\b/.test(cls)?' off':''));h.setAttribute('d',d);h.appendChild(t.cloneNode(true));layer.appendChild(h);
    edgeEls[edgeEls.length-1].hit=h;const es=e.s,et=e.t;
    h.addEventListener('mouseenter',()=>{if(drag||hovPin)return;edgeHover(p,es,et,true);});
    // click: zoom to show both boxes, keep the link highlighted until the mouse moves
    h.addEventListener('click',ev=>{ev.stopPropagation();if(moved)return;hovPin=null;edgeHover(p,es,et,true);hovPin={x:ev.clientX,y:ev.clientY};focusOn([es,et],450);});
    h.addEventListener('mouseleave',()=>edgeHover(p,es,et,false));});
  // layers, bottom to top: other links, dimmed boxes, links of the selection, boxes of the selection.
  // Links of the selected item run above boxes that are not part of its history.
  const gnDim=document.createElementNS(NS,'g'),gn=document.createElementNS(NS,'g');vp.append(gnDim,geHi,gn);
  reps.forEach(r=>{const p=pos[r.id];const g=document.createElementNS(NS,'g');nodeEls[r.id]=g;g.setAttribute('class','nd');g.setAttribute('transform','translate('+p.x+','+p.y+')');
    const rect=document.createElementNS(NS,'rect');rect.setAttribute('width',NW);rect.setAttribute('height',NH);rect.setAttribute('rx',5);
    let label,cat;
    // Groups layout, whole block folded: the block itself stands for the group; inside it only a short note
    const laneFold=r.isGroup&&!r.header&&layoutMode==='lanes'&&laneKey(r.members[0])===r.id.slice(1);
    if(r.isGroup){const gid=r.id.slice(1);label=laneFold?(r.members.length+' item'+(r.members.length===1?'':'s')+' folded'):(r.header?'▾ ':'▸ ')+(groups[gid]?groups[gid].name:'group')+'  ('+r.members.length+')';cat='group';rect.setAttribute('stroke-dasharray','4 3');}
    else{const n=r.members[0];label=n.name;cat=n.cat;}
    const gsupp=!r.isGroup?isSupp(r.members[0]):!!(simState&&r.members.length&&r.members.every(isSupp));
    if(r.isGroup&&simState){const k=r.members.filter(isSupp).length;if(k){label+=' · '+k+' off';}}
    if(gsupp)cat='supp_';rect.setAttribute('fill',gsupp?'var(--supp-bg)':'var(--c-'+cat+'-bg)');if(gsupp)rect.setAttribute('stroke-dasharray','5 3');rect.setAttribute('stroke',gsupp?'var(--supp)':(!r.isGroup&&r.members[0].health===2)?'var(--err)':(!r.isGroup&&r.members[0].health===1)?'var(--warn)':'var(--c-'+cat+')');
    if(laneFold){rect.setAttribute('fill','transparent');rect.setAttribute('stroke','transparent');rect.removeAttribute('stroke-dasharray');}
    const bk=r.members.map(brokenKind).filter(Boolean);
    if(bk.length){const est=bk.every(k=>k==='est');rect.setAttribute('stroke','var(--err)');rect.setAttribute('stroke-width','3');rect.setAttribute('stroke-dasharray',est?'6 4':'');
      const bb=document.createElementNS(NS,'g');bb.setAttribute('class','brk'+(est?' est':''));bb.setAttribute('transform','translate('+(NW-3)+',3)');
      const c=document.createElementNS(NS,'circle');c.setAttribute('r',9);const t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','middle');t.setAttribute('y',4.5);t.textContent='!';
      const tt=document.createElementNS(NS,'title');tt.textContent=r.isGroup?(bk.length+' item'+(bk.length===1?'':'s')+' in this group fail to compute'+(est?' (estimated)':'')):brokenText(r.members[0]);bb.append(c,t,tt);brkBadges.push([g,bb]);}
    else{const wk=r.members.map(warnKind).filter(Boolean);
      if(wk.length){rect.setAttribute('stroke','var(--warn)');rect.setAttribute('stroke-width','2.5');
        const bb=document.createElementNS(NS,'g');bb.setAttribute('class','wrn');bb.setAttribute('transform','translate('+(NW-3)+',3)');
        const tri=document.createElementNS(NS,'path');tri.setAttribute('d','M0,-10 L10,8 L-10,8 Z');const t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','middle');t.setAttribute('y',6);t.textContent='!';
        const tt=document.createElementNS(NS,'title');tt.textContent=r.isGroup?(wk.length+' item'+(wk.length===1?'':'s')+' in this group with warnings'):warnText(r.members[0]);bb.append(tri,t,tt);brkBadges.push([g,bb]);}}
    if(selSet&&selSet.has(r.id)){rect.setAttribute('stroke','var(--sel)');rect.setAttribute('stroke-width','3.5');
      if(selRep!=='__group__'||r.header){const gl=document.createElementNS(NS,'rect');gl.setAttribute('class','selglow');gl.setAttribute('x',-9);gl.setAttribute('y',-9);gl.setAttribute('width',NW+18);gl.setAttribute('height',NH+18);gl.setAttribute('rx',12);g.appendChild(gl);}}
    else if(selGroup&&down&&down.has(r.id)){rect.setAttribute('stroke','var(--down)');rect.setAttribute('stroke-width','2.5');}
    else if(selGroup&&up&&up.has(r.id)){rect.setAttribute('stroke','var(--up)');rect.setAttribute('stroke-width','2');}
    const lastM=r.isGroup?[...r.members].sort((a,b)=>b.o-a.o).find(m=>TH[m.id]):r.members[0];const nth=(!laneFold&&gth&&lastM)?TH[lastM.id]:null;const tx0=nth?66:8;
    // action buttons on the right edge of the box (for now: suppress in the preview)
    const acts=[];const gidA=r.isGroup?r.id.slice(1):null;
    if(laneFold){}
    else if(simOn&&r.isGroup&&canGroups&&groups[gidA]&&!groups[gidA].pseudo)acts.push({kind:'supp',on:sim.groups.has(gidA),title:(sim.groups.has(gidA)?'Switch this timeline group back on':'Suppress this timeline group')+' (preview)',fn:()=>simToggleGroup(gidA)});
    else if(simOn&&!r.isGroup&&canItems&&r.members[0].tl!=null){const it=r.members[0];acts.push({kind:'supp',on:isExplicit(it),title:(isExplicit(it)?'Switch this item back on':'Suppress this item')+' (preview'+(it.fail?' · breaks '+(it.fail.name||'a later feature')+', estimated':(!it.supp&&!itemTested(it)?' · estimated, not tested':''))+')',fn:()=>simToggleItem(it.id)});}
    const maxc=(nth?22:(gth?32:28))-acts.length*(r.isGroup?5:3);
    const icoN=r.isGroup?'group':iconName(r.members[0]);const icoS=gth?18:15;
    const tx=document.createElementNS(NS,'text');tx.setAttribute('x',tx0+icoS+5);tx.setAttribute('y',NH/2+4);tx.setAttribute('style','fill:'+(gsupp?'var(--supp);text-decoration:line-through':'var(--c-'+cat+')')+(r.isGroup?';font-weight:600':''));
    tx.textContent=label.length>maxc?label.slice(0,maxc-1)+'…':label;
    if(laneFold){tx.setAttribute('x',NW/2);tx.setAttribute('text-anchor','middle');tx.setAttribute('style','fill:var(--muted);font-style:italic'+(gsupp?';text-decoration:line-through':''));}
    const ti=document.createElementNS(NS,'title');ti.textContent=r.isGroup?(label+'\n'+r.members.slice(0,25).map(m=>'• '+m.name).join('\n')+(r.members.length>25?'\n…':'')+'\nClick to expand'):(r.members[0].name+'\n'+r.members[0].type);
    g.append(rect);
    {const gid=topGroup(r.members[0]);const col=gid&&!laneFold?gColor[gid]:null;if(col){const st=document.createElementNS(NS,'rect');st.setAttribute('x',0);st.setAttribute('y',0);st.setAttribute('width',6);st.setAttribute('height',NH);st.setAttribute('rx',3);st.setAttribute('fill',col);
      const tt=document.createElementNS(NS,'title');tt.textContent='Timeline group: '+(groups[gid]?groups[gid].name:gid);st.appendChild(tt);g.append(st);}}
    if(nth){const bg=document.createElementNS(NS,'rect');bg.setAttribute('x',4);bg.setAttribute('y',4);bg.setAttribute('width',56);bg.setAttribute('height',NH-8);bg.setAttribute('rx',3);bg.setAttribute('style','fill:var(--panel2)');
      const im=document.createElementNS(NS,'image');im.setAttribute('x',4);im.setAttribute('y',4);im.setAttribute('width',56);im.setAttribute('height',NH-8);im.setAttribute('preserveAspectRatio','xMidYMid meet');im.setAttribute('href',nth);if(gsupp)im.setAttribute('class','suppimg');
      const mid=lastM.id;const hot=document.createElementNS(NS,'rect');hot.setAttribute('x',4);hot.setAttribute('y',4);hot.setAttribute('width',56);hot.setAttribute('height',NH-8);hot.setAttribute('fill','transparent');hot.style.cursor='zoom-in';
      hot.addEventListener('mouseenter',ev=>{if(!drag)peekShow(mid,ev);});hot.addEventListener('mousemove',peekMove);hot.addEventListener('mouseleave',peekHide);
      g.append(bg,im,hot);}
    if(!laneFold)g.append(iconUse(icoN,tx0,(NH-icoS)/2,icoS,gsupp?'var(--supp)':'var(--c-'+cat+')'));
    g.append(tx);if(!nth)g.append(ti);
    brkBadges.filter(x=>x[0]===g).forEach(x=>g.appendChild(x[1]));
    acts.forEach((a,i)=>{const bs=gth?24:18;const bx=NW-6-bs-i*(bs+4),by=(NH-bs)/2;const bt=document.createElementNS(NS,'g');bt.setAttribute('class','act'+(a.on?' on':''));bt.setAttribute('transform','translate('+bx+','+by+')');
      const bg=document.createElementNS(NS,'rect');bg.setAttribute('width',bs);bg.setAttribute('height',bs);bg.setAttribute('rx',5);
      // power symbol
      const c=bs/2,rr=bs*0.26;const arc=document.createElementNS(NS,'path');arc.setAttribute('d','M'+(c-rr*0.7)+','+(c-rr*0.7)+' A'+rr+','+rr+' 0 1 0 '+(c+rr*0.7)+','+(c-rr*0.7));
      const ln=document.createElementNS(NS,'path');ln.setAttribute('d','M'+c+','+(c-rr*1.25)+' L'+c+','+(c-rr*0.1));
      const tt=document.createElementNS(NS,'title');tt.textContent=a.title;bt.append(bg,arc,ln,tt);
      ['mousedown','dblclick'].forEach(ev=>bt.addEventListener(ev,e=>e.stopPropagation()));
      bt.addEventListener('click',e=>{e.stopPropagation();peekHide();a.fn();});g.appendChild(bt);});
    if(laneFold){}
    else if(r.isGroup){const gid=r.id.slice(1);const open=!!r.header;const bt=document.createElementNS(NS,'g');bt.setAttribute('class','ctog');bt.setAttribute('transform','translate('+(NW/2)+','+(NH+1)+')');
      const c=document.createElementNS(NS,'circle');c.setAttribute('r',open?7:9);c.setAttribute('class','ctogc'+(open?'':' col'));const t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','middle');t.setAttribute('y',4);t.setAttribute('class','ctogt');t.textContent=open?'−':'+';
      const tt=document.createElementNS(NS,'title');tt.textContent=open?'Collapse this timeline group into one box':'Expand this timeline group to show its items';bt.append(c,t,tt);
      bt.addEventListener('mousedown',ev=>ev.stopPropagation());
      bt.addEventListener('click',ev=>{ev.stopPropagation();const newId=(open?'g':'h')+gid;
        animatedRerender(()=>{if(open){expanded.delete(gid);Object.keys(groups).forEach(x=>{if(groupPath(x).length>1&&groups[x].parent&&(function up(y){let q=groups[y].parent,gd=0;while(q&&gd++<20){if(q===gid)return true;q=groups[q]?groups[q].parent:null;}return false;})(x))expanded.delete(x);});}else expanded.add(gid);},{keepOld:r.id,keepNew:newId});});addBtn(r.id,bt);}
    else if(canCollapse(r.id)){const col=collapsedNodes.has(r.id);const bt=document.createElementNS(NS,'g');bt.setAttribute('class','ctog');bt.setAttribute('transform','translate('+(NW/2)+','+(NH+1)+')');
      const c=document.createElementNS(NS,'circle');c.setAttribute('r',col?9:7);c.setAttribute('class','ctogc'+(col?' col':''));
      const t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','middle');t.setAttribute('y',4);t.setAttribute('class','ctogt');t.textContent=col?'+':'−';
      const tt=document.createElementNS(NS,'title');tt.textContent=col?('Expand: show the '+(hiddenCount[r.id]||0)+' hidden items that depend on this'):(isLanes()?'Collapse: hide what depends on this in the same timeline group':'Collapse: hide everything that depends on this');bt.append(c,t,tt);
      if(col&&hiddenCount[r.id]){const cn=document.createElementNS(NS,'text');cn.setAttribute('x',13);cn.setAttribute('y',4);cn.setAttribute('class','ctogn');cn.textContent=hiddenCount[r.id]+' hidden';bt.appendChild(cn);}
      bt.addEventListener('mousedown',ev=>ev.stopPropagation());
      bt.addEventListener('click',ev=>{ev.stopPropagation();const rid=r.id;animatedRerender(()=>{if(col)collapsedNodes.delete(rid);else collapsedNodes.add(rid);},{keepOld:rid,keepNew:rid});});addBtn(r.id,bt);}
    if(selSet&&!selSet.has(r.id)&&!up.has(r.id)&&!down.has(r.id)&&!(r.header&&hdrRelated(r)))g.classList.add('dim');
    if(hitReps){if(hitReps.has(r.id)){rect.setAttribute('stroke','var(--sel)');rect.setAttribute('stroke-width','3');if(r.id===graphHits[hitIdx]){rect.setAttribute('stroke','var(--down)');rect.setAttribute('stroke-width','4');}}else if(!selSet)g.classList.add('dim');}

    g.addEventListener('click',ev=>{ev.stopPropagation();if(moved)return;if(simOn&&(ev.shiftKey||ev.altKey)){if(r.isGroup)simToggleGroup(r.id.slice(1));else if(canItems)simToggleItem(r.members[0].id);else{const gg=(r.members[0].g||[]);if(gg.length)simToggleGroup(gg[gg.length-1]);}return;}if(r.isGroup){selectGroup(r.id.slice(1));}else select(r.members[0].id);});
    (selSet&&g.classList.contains('dim')?gnDim:gn).appendChild(g);if(nodeBtnEls[r.id]&&g.classList.contains('dim'))nodeBtnEls[r.id].classList.add('dim');});
  vp.appendChild(btnLayer);
  if(fitAfter)fit(centerId?rep(byId[centerId]):null);
}
function fit(centerRep,only){const ids=(only&&only.length?only:Object.keys(pos)).filter(i=>pos[i]);if(!ids.length)return;const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;const W=(svg.clientWidth||800)-LP,H=svg.clientHeight||600;
  if(centerRep&&pos[centerRep]){T.k=1;T.x=LP+W/2-(pos[centerRep].x+NW/2);T.y=H/2-(pos[centerRep].y+NH/2);applyT();return;}
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;ids.forEach(i=>{const p=pos[i];x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});
  const k=Math.min(1.2,Math.min((W-40)/(x1-x0||1),(H-80)/(y1-y0||1)));T.k=Math.max(0.03,k);const cw=(x1-x0)*T.k,ch=(y1-y0)*T.k;T.x=LP+(cw<W-40?(W-cw)/2-x0*T.k:20-x0*T.k);T.y=only&&ch<H-100?50+(H-50-ch)/2-y0*T.k:60-y0*T.k;applyT();}
// zoom and centre on the given boxes (animated); a single item is shown at a readable size
let anim=null;
function focusOn(repIds,dur0,alignTop){const ps=repIds.map(r=>pos[r]).filter(Boolean);if(!ps.length)return;
  const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;
  const W=(svg.clientWidth||800)-LP,H=svg.clientHeight||600;
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;ps.forEach(p=>{x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});
  let k=Math.min(Math.max(T.k,1.2),1.6,(W-80)/(x1-x0||1),(H-140)/(y1-y0||1));k=Math.max(k,0.05);
  const cx=(x0+x1)/2,cy=(y0+y1)/2;const tx=LP+W/2-cx*k,ty=alignTop?70-y0*k:30+H/2-cy*k;
  const s0={x:T.x,y:T.y,k:T.k},t0=performance.now(),dur=dur0||280;if(anim)cancelAnimationFrame(anim);
  const step=now=>{const a=Math.min(1,(now-t0)/dur),e=a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;
    T.k=s0.k+(k-s0.k)*e;T.x=s0.x+(tx-s0.x)*e;T.y=s0.y+(ty-s0.y)*e;applyT();if(a<1)anim=requestAnimationFrame(step);else anim=null;};
  anim=requestAnimationFrame(step);}
// first view: like Fit, but never zoomed out further than MIN_START_ZOOM (top of the graph, centred)
const MIN_START_ZOOM=0.55;
function fitInitial(){const ids=Object.keys(pos);if(!ids.length)return;const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;
  const W=(svg.clientWidth||800)-LP,H=svg.clientHeight||600;let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;ids.forEach(i=>{const p=pos[i];x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});
  const k=Math.max(MIN_START_ZOOM,Math.min(1.2,(W-40)/(x1-x0||1),(H-80)/(y1-y0||1)));T.k=k;T.x=LP+(W-(x1-x0)*k)/2-x0*k;T.y=60-y0*k;applyT();}
// switch to the graph and zoom/centre on the given boxes, same as selecting in the graph
function showInGraph(getReps){setView('graph');renderGraph(false);
  let ids=[...new Set(getReps())];if(ids.some(r=>!pos[r])&&collapsedNodes.size){collapsedNodes.clear();renderGraph(false);ids=[...new Set(getReps())];}
  requestAnimationFrame(()=>focusOn(ids));}
function fitWidth(only){const ids=(only&&only.length?only:Object.keys(pos)).filter(i=>pos[i]);if(!ids.length)return;const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;const W=(svg.clientWidth||800)-LP;
  let x0=1e9,y0=1e9,x1=-1e9;ids.forEach(i=>{const p=pos[i];x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);});
  T.k=Math.max(0.03,Math.min(1.5,(W-40)/(x1-x0||1)));T.x=LP+20-x0*T.k;T.y=60-y0*T.k;applyT();}
// Fit / Fit width: with something selected, fit the selection and everything highlighted with it; animated
function fitAnimated(fn){const s0={x:T.x,y:T.y,k:T.k};fn();const t1={x:T.x,y:T.y,k:T.k};T.x=s0.x;T.y=s0.y;T.k=s0.k;
  const t0=performance.now(),dur=420;if(anim)cancelAnimationFrame(anim);
  const step=now=>{const a=Math.min(1,(now-t0)/dur),e=a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;T.k=s0.k+(t1.k-s0.k)*e;T.x=s0.x+(t1.x-s0.x)*e;T.y=s0.y+(t1.y-s0.y)*e;applyT();if(a<1)anim=requestAnimationFrame(step);else anim=null;};
  anim=requestAnimationFrame(step);}
let drag=null,moved=false;
svg.addEventListener('mousedown',e=>{drag={x:e.clientX,y:e.clientY,tx:T.x,ty:T.y};moved=false;svg.classList.add('drag');});
window.addEventListener('mousemove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3)moved=true;T.x=drag.tx+dx;T.y=drag.ty+dy;applyT();});
window.addEventListener('mouseup',()=>{drag=null;svg.classList.remove('drag');setTimeout(()=>moved=false,0);});
svg.addEventListener('wheel',e=>{e.preventDefault();const r=svg.getBoundingClientRect();const mx=e.clientX-r.left,my=e.clientY-r.top;const f=Math.exp(-e.deltaY*0.0015);const k=Math.min(4,Math.max(0.05,T.k*f));T.x=mx-(mx-T.x)*(k/T.k);T.y=my-(my-T.y)*(k/T.k);T.k=k;applyT();},{passive:false});
svg.addEventListener('click',()=>{if(moved)return;if(selected||selGroup)clearSel();});

// ---------- history playback ----------
// Select an item and press Play: the view shows the item and everything it depends on, dims it all,
// then walks the history in timeline order. For each step a dot flies along the links from the
// items already built to the next one, which then fades in. The camera follows the dots.
let PB=null;const PBS=[0.5,1,2,4];
const PB_A={pre:0.3,other:0.06,edgePre:0.16,edgeOther:0.03};
function pbLP(){const gp=$('gpanel');return (gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;}
function pbSync(){const b=$('pbStart');if(!b)return;const ok=!!(selected&&byId[selected]);b.disabled=false;
  if(!ok&&selGroup&&groups[selGroup]){b.title='Play how this timeline group was built from its dependencies (P)';return;}
  b.title=ok?'Play how the selected item was built from its dependencies (P)':'Play the whole history of the design (P). Select an item first to play only how that item was built.';}
// a link as a polyline with its cumulative length, so a dot can move along it at constant speed
function pbPath(x){const P=edgeSamples(pos[x.s],pos[x.t],x.o1,x.o2,x.bow,48);const L=[0];
  for(let i=1;i<P.length;i++)L.push(L[i-1]+Math.hypot(P[i][0]-P[i-1][0],P[i][1]-P[i-1][1]));return {P,L,len:L[L.length-1]};}
function pbAt(pp,f){const d=f*pp.len;let i=1;while(i<pp.L.length-1&&pp.L[i]<d)i++;const a=pp.P[i-1],b=pp.P[i],s=(d-pp.L[i-1])/((pp.L[i]-pp.L[i-1])||1);
  return [a[0]+(b[0]-a[0])*s,a[1]+(b[1]-a[1])*s];}
function pbBox(ids,pts){let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;(ids||[]).forEach(i=>{const p=pos[i];if(!p)return;x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});
  (pts||[]).forEach(q=>{x0=Math.min(x0,q[0]);y0=Math.min(y0,q[1]);x1=Math.max(x1,q[0]);y1=Math.max(y1,q[1]);});return x0>x1?null:{x0,y0,x1,y1};}
function pbArea(){const LP=pbLP();const bar=$('pbBar');const bh=bar&&bar.offsetHeight?bar.offsetHeight+28:0;
  const RP=document.body.classList.contains('pbthumbs')?250:0;
  return {LP,W:Math.max(200,(svg.clientWidth||800)-LP-RP),H:Math.max(200,(svg.clientHeight||600)-bh-52),top:52};}
function pbView(b,kmin,kmax,pad){const A=pbArea();pad=pad==null?90:pad;
  let k=Math.min(kmax,(A.W-2*pad)/Math.max(1,b.x1-b.x0),(A.H-2*pad)/Math.max(1,b.y1-b.y0));k=Math.max(kmin,k);
  return {cx:(b.x0+b.x1)/2,cy:(b.y0+b.y1)/2,k};}
// ease the camera towards a view; tau: time constant in ms (smaller = snappier)
function pbCam(v,dt,tau){const A=pbArea();const cxW=A.LP+A.W/2,cyW=A.top+A.H/2;
  const cx=(cxW-T.x)/T.k,cy=(cyW-T.y)/T.k;const f=1-Math.exp(-dt/Math.max(16,tau));
  const k=Math.exp(Math.log(T.k)+(Math.log(v.k)-Math.log(T.k))*f);const nx=cx+(v.cx-cx)*f,ny=cy+(v.cy-cy)*f;
  T.k=k;T.x=cxW-nx*k;T.y=cyW-ny*k;applyT();}
function pbEase(a){a=Math.max(0,Math.min(1,a));return a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;}

function playHistory(){
  stopPlay();setInfo(false);peekHide();
  if(view!=='graph'){setView('graph');renderGraph(false);}
  if(selected&&byId[selected]&&!pos[rep(byId[selected])]&&collapsedNodes.size){collapsedNodes.clear();renderGraph(false);}
  if(anim){cancelAnimationFrame(anim);anim=null;}
  // wait for a running re-layout (selection glide) to settle, so the boxes are where pos says
  const go=()=>{if(graphAnim){setTimeout(go,60);return;}pbBegin();};go();}
function pbBegin(){let order;const grp=!selected&&selGroup&&groups[selGroup]?selGroup:null;const whole=!(selected&&byId[selected])&&!grp;
  if(whole){// nothing selected: the whole history, every box on screen in timeline order
    const ord={};nodes.filter(visibleNode).forEach(n=>{const r=rep(n);if(pos[r])ord[r]=Math.min(ord[r]==null?1e9:ord[r],n.o);});
    order=Object.keys(ord).sort((a,b)=>ord[a]-ord[b]);if(!order.length)return;}
  else if(grp){// a timeline group: everything its items depend on, then its own items, in timeline order
    const mem=groupMembers(grp).filter(visibleNode);const ord={};const add=n=>{const r=rep(n);if(pos[r])ord[r]=Math.min(ord[r]==null?1e9:ord[r],n.o);};
    groupUpIds(grp).forEach(i=>add(byId[i]));const upR=Object.keys(ord);const mo={};mem.forEach(n=>{const r=rep(n);if(pos[r])mo[r]=Math.min(mo[r]==null?1e9:mo[r],n.o);});
    const memR=Object.keys(mo).sort((a,b)=>mo[a]-mo[b]);const ms=new Set(memR);
    order=upR.filter(r=>!ms.has(r)).sort((a,b)=>ord[a]-ord[b]).concat(memR);if(!order.length)return;}
  else{const selR=rep(byId[selected]);if(!pos[selR])return;
    const up=[...closure(selected,'up')].filter(i=>byId[i]&&visibleNode(byId[i]));
    const ord={};const setR=new Set(up.map(i=>rep(byId[i])).filter(r=>pos[r]));setR.delete(selR);
    nodes.forEach(n=>{const r=rep(n);if(r===selR||setR.has(r))ord[r]=Math.min(ord[r]==null?1e9:ord[r],n.o);});
    order=[...setR].sort((a,b)=>ord[a]-ord[b]);order.push(selR);}
  const S=new Set(order);const idx={};order.forEach((r,i)=>idx[r]=i);
  const edges=edgeEls.filter(x=>S.has(x.s)&&S.has(x.t)&&x.s!==x.t);
  const steps=order.map((r,i)=>({id:r,inc:edges.filter(x=>x.t===r&&idx[x.s]<i)}));
  const ov=document.createElementNS('http://www.w3.org/2000/svg','g');ov.setAttribute('class','pbov');vp.appendChild(ov);
  const lits=document.createElementNS('http://www.w3.org/2000/svg','g');ov.appendChild(lits);
  edgeHover(null,null,null,false);hovPin=null;if(hovState)edgeHover(null,null,null,false);
  PB={whole,grp,order,S,idx,edges,steps,k:-1,phase:'intro',vt:0,ph0:0,dur:1800,speed:PB_speed,paused:false,
    alpha:{},ealpha:new Map(),ov,lits,fading:[],dots:[],pulses:[],userCam:false,last:performance.now(),raf:0,shown:new Set()};
  Object.keys(nodeEls).forEach(id=>PB.alpha[id]=1);edgeEls.forEach(x=>PB.ealpha.set(x,1));
  svg.classList.add('playing');document.body.classList.add('pbon');document.body.classList.toggle('pbthumbs',hasThumbs&&showThumbs);pbUI();
  PB.raf=requestAnimationFrame(pbFrame);}
let PB_speed=1;
function stopPlay(){if(!PB)return;cancelAnimationFrame(PB.raf);PB.ov.remove();
  Object.values(nodeEls).forEach(g=>{g.style.opacity='';});edgeEls.forEach(x=>{x.el.style.opacity='';});
  PB=null;svg.classList.remove('playing');document.body.classList.remove('pbon','pbthumbs');$('pbCard').dataset.id='';pbUI();}
function pbUI(){const bar=$('pbBar');if(!bar)return;bar.style.display=PB?'flex':'none';pbSync();if(!PB)return;
  $('pbPlay').textContent=PB.phase==='done'?'↺':(PB.paused?'▶':'❚❚');
  $('pbPlay').title=PB.phase==='done'?'Replay':(PB.paused?'Resume (Space)':'Pause (Space)');
  $('pbNext').disabled=PB.phase==='done';$('pbSpeed').textContent=PB.speed+'×';
  const n=PB.order.length;let t;
  if(PB.phase==='intro')t=PB.whole?'Whole history · <b>'+n+'</b> step'+(n===1?'':'s'):PB.grp?'<b>'+n+'</b> step'+(n===1?'':'s')+' to build group <b>'+esc(groups[PB.grp]?groups[PB.grp].name:'')+'</b>':'<b>'+n+'</b> step'+(n===1?'':'s')+' to build <b>'+esc(byId[selected]?byId[selected].name:'')+'</b>';
  else if(PB.phase==='done')t='Done · '+n+' step'+(n===1?'':'s');
  else{const r=PB.order[PB.k];t='Step <b>'+(PB.k+1)+'</b> / '+n+' · '+esc(pbName(r));}
  $('pbInfo').innerHTML=t;
  pbCardShow(PB.phase==='intro'?null:(PB.phase==='done'?PB.order[n-1]:PB.order[PB.k]));
  const pr=$('pbProg');if(pr)pr.style.width=(PB.phase==='done'?100:Math.max(0,PB.k+1)/n*100)+'%';}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
// small card with the thumbnail of the item being built (the model after that timeline step)
function pbThumbOf(r){if(byId[r])return TH[r]?r:null;const g=groups[r.slice(1)];if(!g)return null;const m=groupMembers(g.id).filter(x=>TH[x.id]).sort((a,b)=>b.o-a.o)[0];return m?m.id:null;}
function pbCardShow(r){const c=$('pbCard');if(!c)return;const key=r||'';if(c.dataset.id===key)return;c.dataset.id=key;
  const im=c.querySelector('img'),cap=c.querySelector('.cap');if(!r){im.style.display='none';cap.innerHTML='<small>Starting…</small>';return;}
  const tid=pbThumbOf(r);im.style.display=tid?'':'none';if(tid)im.src=TH[tid];
  const n=byId[r];const cat=n?n.cat:'group';const lab=n?(CAT[n.cat]||n.cat):'Timeline group';
  cap.innerHTML='<span class="pill" style="color:var(--c-'+cat+');background:var(--c-'+cat+'-bg)">'+iconHTML(n?iconName(n):'group',cat,12)+esc(lab)+'</span> '+(n?'<span class="ty">'+esc(n.type.replace(/Feature$/,''))+'</span>':'')+'<div class="nm">'+esc(pbName(r))+'</div><small>'+(n&&n.tl!=null?'Timeline position '+(n.tl+1):'')+(tid?'':(n&&n.tl!=null?' · ':'')+'no thumbnail')+'</small>';
  c.classList.remove('swap');void c.offsetWidth;c.classList.add('swap');}
function pbName(r){if(byId[r])return byId[r].name;const g=groups[r.slice(1)];return g?g.name+' (group)':r;}
function pbSetPhase(ph,dur){PB.phase=ph;PB.ph0=PB.vt;PB.dur=dur;}
function pbStep(k){PB.k=k;PB.userCam=false;pbDropDots();if(PB.steps[k])PB.steps[k].pulsed=false;
  if(k>=PB.order.length){pbSetPhase('done',1600);pbUI();return;}   // shows the whole result briefly, then closes
  const st=PB.steps[k];
  if(st.inc.length){let mx=0;st.inc.forEach(x=>{const pp=pbPath(x);mx=Math.max(mx,pp.len);
      const NS2='http://www.w3.org/2000/svg';const d0=edgeCurve(pos[x.s],pos[x.t],x.o1,x.o2,x.bow);
      const lit=document.createElementNS(NS2,'g');const mk=c=>{const q=document.createElementNS(NS2,'path');q.setAttribute('class','pblit '+c);q.setAttribute('d',d0);lit.appendChild(q);return q;};
      const lb=mk('base'),lg=mk('glow'),lt=mk('trail');PB.lits.appendChild(lit);const ll=lt.getTotalLength()||pp.len;
      const g=document.createElementNS('http://www.w3.org/2000/svg','g');g.setAttribute('class','pbdot');
      const h=document.createElementNS('http://www.w3.org/2000/svg','circle');h.setAttribute('class','pbhalo');
      const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('class','pbcore');g.append(h,c);PB.ov.appendChild(g);
      PB.dots.push({x,pp,el:g,h,c,lit,lb,lg,lt,ll,dotted:x.el.classList.contains('bridge')});});
    pbSetPhase('fly',Math.max(1400,Math.min(3400,1100+mx*1.0)));}
  else pbSetPhase('fade',k===0?1400:1100);
  pbUI();}
// dots are removed at once; their lit links stay fully drawn and fade out
// the lit part of a link, from its start up to `on`: a solid line, or a dotted one for a dotted (indirect) link
function pbDash(d,on,w){const L=d.ll;if(!d.dotted)return on+' '+(L+10);const step=w*2.4;const n=Math.floor(on/step);
  let a=[];for(let i=0;i<n;i++)a.push('0.01',String(step-0.01));const r=on-n*step;if(r>0)a.push('0.01',String(r));a.push('0',String(L+10));return a.join(' ');}
function pbDropDots(){PB.dots.forEach(d=>{d.el.remove();const L=d.ll,w=4.5/T.k;d.lt.setAttribute('stroke-dasharray',pbDash(d,L,w));d.lg.setAttribute('stroke-dasharray',pbDash(d,L,w));PB.fading.push({el:d.lit,t0:PB.vt});});PB.dots=[];}
function pbPulse(id){const p=pos[id];if(!p)return;const r=document.createElementNS('http://www.w3.org/2000/svg','rect');r.setAttribute('class','pbpulse');r.setAttribute('rx',9);PB.ov.appendChild(r);PB.pulses.push({el:r,id,t0:PB.vt});}
// finish the running step at once (Next)
function pbSkip(){if(!PB||PB.phase==='done')return;
  if(PB.phase==='intro'){pbStep(0);return;}
  const r=PB.order[PB.k];PB.shown.add(r);PB.steps[PB.k].inc.forEach(x=>PB.ealpha.set(x,1));PB.alpha[r]=1;pbPulse(r);pbStep(PB.k+1);}
function pbFrame(now){if(!PB)return;const dt=Math.min(80,now-PB.last);PB.last=now;
  if(!PB.paused)PB.vt+=dt*PB.speed;
  const p=(PB.vt-PB.ph0)/PB.dur,e=pbEase(p);const tau=Math.max(120,480/Math.sqrt(PB.speed));
  // target opacities
  const na={},ea=new Map();
  Object.keys(nodeEls).forEach(id=>{na[id]=PB.S.has(id)?(PB.shown.has(id)?1:PB_A.pre):PB_A.other;});
  edgeEls.forEach(x=>{ea.set(x,PB.S.has(x.s)&&PB.S.has(x.t)?(PB.shown.has(x.t)&&PB.shown.has(x.s)?1:PB_A.edgePre):PB_A.edgeOther);});
  let camIds=null,camPts=null,kmin=0.3,kmax=1.25,pad=90;
  if(PB.phase==='intro'){// dim everything while zooming out to the whole history
    Object.keys(na).forEach(id=>{na[id]=1+(na[id]-1)*e;});ea.forEach((v,x)=>ea.set(x,1+(v-1)*e));
    camIds=PB.order;kmin=0.02;kmax=1.1;pad=60;if(p>=1)pbStep(0);}
  else if(PB.phase==='fly'){const st=PB.steps[PB.k];const f=pbEase(Math.min(1,p));camPts=[];
    const rad=Math.min(40,Math.max(5,7/T.k));
    PB.dots.forEach(d=>{const q=pbAt(d.pp,f);camPts.push(q);d.c.setAttribute('cx',q[0]);d.c.setAttribute('cy',q[1]);d.c.setAttribute('r',rad);
      d.h.setAttribute('cx',q[0]);d.h.setAttribute('cy',q[1]);d.h.setAttribute('r',rad*(2.1+0.35*Math.sin(PB.vt/140)));
      ea.set(d.x,PB_A.edgePre+(1-PB_A.edgePre)*f);
      const px=1/T.k;d.lb.setAttribute('stroke-width',3*px);d.lg.setAttribute('stroke-width',13*px);d.lt.setAttribute('stroke-width',4.5*px);
      const L=d.ll,on=f*L;d.lt.setAttribute('stroke-dasharray',pbDash(d,on,4.5*px));d.lg.setAttribute('stroke-dasharray',pbDash(d,on,4.5*px));
      if(d.dotted)d.lb.setAttribute('stroke-dasharray',pbDash(d,L,4.5*px));
      d.lb.style.opacity=String(Math.min(1,p*4));});
    camIds=[st.id];if(p>=1){pbDropDots();pbPulse(st.id);pbSetPhase('fade',1000);}}
  else if(PB.phase==='fade'){const st=PB.steps[PB.k];na[st.id]=PB_A.pre+(1-PB_A.pre)*e;st.inc.forEach(x=>ea.set(x,1));
    camIds=[st.id];kmax=1.3;if(!st.inc.length&&!st.pulsed&&p>0.15){st.pulsed=true;pbPulse(st.id);}
    if(p>=1){PB.shown.add(st.id);pbSetPhase('hold',st.inc.length?500:650);}}
  else if(PB.phase==='hold'){camIds=[PB.order[PB.k]];kmax=1.3;if(p>=1)pbStep(PB.k+1);}
  else if(PB.phase==='done'){camIds=PB.order;kmin=0.02;kmax=1.1;pad=60;if(p>=1){stopPlay();return;}}
  // apply opacities
  Object.keys(nodeEls).forEach(id=>{const g=nodeEls[id];const v=na[id];if(PB.alpha[id]!==v){PB.alpha[id]=v;g.style.opacity=String(v);}});
  edgeEls.forEach(x=>{const v=ea.get(x);if(PB.ealpha.get(x)!==v){PB.ealpha.set(x,v);x.el.style.opacity=String(v);}});
  PB.fading=PB.fading.filter(q=>{const a=(PB.vt-q.t0)/900;if(a>=1){q.el.remove();return false;}q.el.style.opacity=String(1-pbEase(a));return true;});
  // arrival pulses: a ring that grows out of the box and fades
  PB.pulses=PB.pulses.filter(q=>{const a=(PB.vt-q.t0)/1100;const pp=pos[q.id];if(a>=1||!pp){q.el.remove();return false;}
    const g=6+22*pbEase(a);q.el.setAttribute('x',pp.x-g);q.el.setAttribute('y',pp.y-g);q.el.setAttribute('width',NW+2*g);q.el.setAttribute('height',NH+2*g);q.el.style.opacity=String(1-a);return true;});
  // camera: follow the dots and the box being built
  if(!PB.userCam&&!PB.paused){const b=pbBox(camIds,camPts);if(b)pbCam(pbView(b,kmin,kmax,pad),dt,PB.phase==='intro'||PB.phase==='done'?tau*1.6:tau);}
  PB.raf=requestAnimationFrame(pbFrame);}
svg.addEventListener('mousedown',()=>{if(PB)PB.userCam=true;},true);
svg.addEventListener('click',e=>{if(PB){e.stopImmediatePropagation();}},true);
svg.addEventListener('wheel',()=>{if(PB)PB.userCam=true;},{capture:true,passive:true});
$('pbStart').onclick=()=>playHistory();
$('pbStop').onclick=()=>stopPlay();
$('pbNext').onclick=()=>{if(PB){PB.paused=false;pbSkip();pbUI();}};
$('pbPlay').onclick=()=>{if(!PB)return;if(PB.phase==='done'){playHistory();return;}PB.paused=!PB.paused;PB.userCam=false;pbUI();};
$('pbSpeed').onclick=()=>{PB_speed=PBS[(PBS.indexOf(PB_speed)+1)%PBS.length];if(PB)PB.speed=PB_speed;pbUI();};
window.addEventListener('keydown',e=>{if(e.target&&(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||e.target.tagName==='TEXTAREA'))return;
  if(!PB){if((e.key==='p'||e.key==='P')&&!e.metaKey&&!e.ctrlKey&&!e.altKey&&view==='graph'){e.preventDefault();playHistory();}return;}
  if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();stopPlay();}
  else if(e.key===' '){e.preventDefault();$('pbPlay').onclick();}
  else if(e.key==='ArrowRight'&&!e.altKey){e.preventDefault();$('pbNext').onclick();}},true);
// ---------- legend: what the colours, outlines, markers and lines mean ----------
const CAT_DESC={sketch:'Sketches.',construct:'Construction planes, axes and points.',
  solid:'Features that create or add solid material: extrude, revolve, sweep, loft, rib, web, emboss, coil, pipe, boss, primitives, thicken, boundary fill.',
  finish:'Fillets and chamfers.',offset:'Features that edit faces: offset face, shell, draft, delete face, replace face, split face.',
  hole:'Holes and threads.',body:'Body operations: combine, split body, move, mirror, patterns, scale, copy/paste, remove.',
  param:'User parameters (under "User Parameters", or under what drives them) and derived parameters (under their Derive feature).',
  surface:'Surface features: patch, stitch, unstitch, trim, untrim, extend, offset, ruled surface, reverse normal, and extrude/revolve/sweep/loft set to make a surface.',
  sheet:'Sheet metal: flange, hem, rip, corner closure, fold, unfold/refold, join by bend, lofted flange, flat pattern.',
  form:'T-spline form features and base (direct edit) features.',mesh:'Mesh and volumetric features.',
  component:'A component of the design. Its parent is the item that brought it in (insert, New Component, a feature set to new component, or a Derive feature); its children are the items built inside it.',
  insert:'Items that bring a component into the design: component inserts, New Component, copy/paste of components, Derive features.',
  joint:'Joints, as-built joints, joint origins, rigid groups, motion links, contact sets, Arrange and captured positions (snapshots).',
  other:'Anything else (canvases, decals, add-in features, items the Fusion API does not describe).'};
const KIND_DESC={sketch:'uses a sketch (or geometry projected/included into it)',profile:'uses a sketch profile',plane:'built on or references a construction plane, axis or point',
  geometry:'references faces, edges or vertices made by that item',body:'works on a body that item created',feature:'references that feature directly',
  param:'a parameter drives it (expression or dimension)',component:'references a component, or something inside a component, that item brought in',
  incomp:'is built inside that component (sketches, features, joints and sub-components of a component)',
  joint:'references a joint, joint origin or rigid group',suppress:'suppression test: suppressing the upper item suppresses the lower one too, even without a reference Fusion reports',
  order:'works on the same body after that item changed it; only timeline order, not a real reference'};
function renderLegend(){const b=$('legendBody');if(!b)return;
  const sv=(inner,w)=>'<svg width="'+(w||70)+'" height="30" viewBox="0 0 '+(w||70)+' 30" aria-hidden="true">'+inner+'</svg>';
  const box=(fill,stroke,o)=>{o=o||{};return sv('<rect x="3" y="4" width="64" height="22" rx="5" fill="'+fill+'" stroke="'+stroke+'" stroke-width="'+(o.w||1)+'"'+(o.dash?' stroke-dasharray="'+o.dash+'"':'')+(o.op?' opacity="'+o.op+'"':'')+'/>'+(o.extra||''));};
  const line=cls=>sv('<path class="edge '+cls+'" d="M4,15 L60,15 M53,10 L60,15 L53,20"/>');
  const row=(sw,t,d)=>'<div class="lgrow"><div class="lgsw">'+sw+'</div><div><b>'+t+'</b><div class="kv">'+d+'</div></div></div>';
  const sec=(t,rows,intro)=>'<h3>'+t+'</h3>'+(intro?'<div class="kv lgintro">'+intro+'</div>':'')+rows.join('');
  let h='';
  h+=sec('Box colours: what kind of item',Object.keys(CAT).map(c=>row(box('var(--c-'+c+'-bg)','var(--c-'+c+')',{extra:'<use href="#ic-'+(ICON_BY_CAT[c]||'other')+'" x="9" y="7" width="16" height="16" style="color:var(--c-'+c+')"/><rect x="30" y="13" width="28" height="4" rx="2" style="fill:var(--c-'+c+');opacity:.5"/>'}),CAT[c],CAT_DESC[c]||'')),
    'The fill and text colour of a box show its category. Hover a box for its full name and Fusion type.');
  const G=GCOL[0];
  {const by={};Object.keys(ICON_BY_TYPE).forEach(t=>{const k=ICON_BY_TYPE[t];(by[k]=by[k]||[]).push(t==='Occurrence'?'Component insert / new component':t.replace(/Feature$/,'').replace(/([a-z])([A-Z])/g,'$1 $2'));});

    h+='<h3>Icons: what type of item</h3><div class="kv lgintro">Shown on the boxes, in the tree, in the details and in the lists. Types without their own icon use their category\'s icon.</div><div class="icgrid">'+
      Object.keys(by).map(k=>'<div>'+iconHTML(k,catOfIcon(k),18)+'<span title="'+by[k].join(', ')+'">'+by[k].join(', ')+'</span></div>').join('')+'<div>'+iconHTML('group','group',18)+'<span>Timeline group</span></div></div>';}
  h+=sec('Box parts and outlines',[
    row(box('var(--c-solid-bg)','var(--c-solid)',{extra:'<rect x="3" y="4" width="5" height="22" rx="2" fill="'+G+'"/>'}),'Coloured strip on the left','The top-level timeline group the item belongs to. Each group has its own colour (also used in the groups panel and in the Groups layout).'),
    row(box('var(--c-solid-bg)','var(--c-solid)',{extra:'<rect x="9" y="7" width="18" height="16" rx="2" fill="var(--panel2)"/><rect x="13" y="11" width="10" height="8" fill="var(--c-body)"/>'}),'Thumbnail','The model right after this item, framed on the item. Hover it for a bigger picture.'),
    row(box('var(--c-group-bg)','var(--c-group)',{dash:'4 3',extra:'<text x="9" y="19" style="font-size:10px;font-weight:600">▸ (12)</text>'}),'Group box (collapsed)','A whole timeline group (or "Not in a group" / "User parameters") folded into one box, with its number of items. Click it to select the group, use + to open it. In the Groups and Components layouts the −/+ at the bottom of a block folds the whole group or component.'),
    row(box('var(--c-group-bg)','var(--c-group)',{dash:'4 3',extra:'<text x="9" y="19" style="font-size:10px;font-weight:600">▾ Group</text>'}),'Group header (expanded)','Depth layout: sits above the items of an open timeline group, as their parent. Thick coloured lines join it to the group\'s first items; its − folds the group into one box.'),
    row(sv('<rect x="3" y="3" width="64" height="24" rx="7" style="fill:'+G+';fill-opacity:.07;stroke:'+G+';stroke-opacity:.45;stroke-width:1.5"/><text x="9" y="15" style="font-size:9px;font-weight:700;fill:'+G+'">Name</text>'),'Coloured block','Groups layout: one block per top-level timeline group. Components layout: one block per component (items outside components sit in "Root component"). In both, user parameters have a block of their own. Click the block title to select the group, component or User Parameters.'),
    row(sv('<circle cx="22" cy="15" r="8" class="ctogc col"/><text x="22" y="19" text-anchor="middle" class="ctogt">+</text><circle cx="48" cy="15" r="7" class="ctogc"/><text x="48" y="19" text-anchor="middle" class="ctogt">−</text>'),'+ / − under a box','Expand a group, or collapse/expand everything that depends on this box. A collapsed box shows how many items it hides.'),
    row(box('var(--c-solid-bg)','var(--sel)',{w:3,extra:'<rect x="-1" y="0" width="72" height="30" rx="8" class="selglow" style="stroke-width:2"/>'}),'Selected','Purple outline with a glow. With a timeline group selected, all its boxes get the purple outline.'),
    row(box('var(--c-solid-bg)','var(--up)',{w:2}),'What the selected group depends on','Blue outline (when a whole group is selected).'),
    row(box('var(--c-solid-bg)','var(--down)',{w:2.5}),'What depends on the selected group','Green outline (when a whole group is selected).'),
    row(box('var(--c-solid-bg)','var(--c-solid)',{op:.25}),'Faded box','Not related to the selection (or not a search match). During playback: not built yet (a little faded) or not part of the history being played (very faded).'),
    row(box('var(--c-solid-bg)','var(--sel)',{w:3}),'Search match','Matches the search text. The current match (Enter / ›) has a thick green outline.'),
    row(box('var(--supp-bg)','var(--supp)',{dash:'5 3',extra:'<text x="10" y="19" style="font-size:10px;fill:var(--supp);text-decoration:line-through">Aa</text>'}),'Suppressed','Grey, dashed, crossed out: suppressed in the design, or in the suppression preview. In italics when the preview only estimates it.'),
    row(box('var(--c-solid-bg)','var(--err)',{w:3,extra:'<g class="brk" transform="translate(64,6)"><circle r="7"/><text text-anchor="middle" y="4" style="font-size:10px">!</text></g>'}),'Fails to compute','Red outline and a red ! : an error in the design, or it would fail in the suppression preview (Fusion reported it in the test).'),
    row(box('var(--c-solid-bg)','var(--err)',{w:3,dash:'6 4',extra:'<g class="brk est" transform="translate(64,6)"><circle r="7"/><text text-anchor="middle" y="4" style="font-size:10px">!</text></g>'}),'May fail (estimated)','Dashed red: in the preview it depends on a feature that fails, so it probably fails too. Not tested.'),
    row(box('var(--c-solid-bg)','var(--warn)',{w:2.5,extra:'<g class="wrn" transform="translate(64,7)"><path d="M0,-7 L7,6 L-7,6 Z"/><text text-anchor="middle" y="5" style="font-size:9px">!</text></g>'}),'Warning','Orange outline and triangle: a warning in the design, or one it would get in the suppression preview.'),
    row(sv('<g class="act"><rect x="22" y="4" width="22" height="22" rx="5"/><path d="M28.5,11.5 A5.5,5.5 0 1 0 37.5,11.5"/><path d="M33,8.5 L33,14.5"/></g>'),'Power button','Suppress / switch back on in the suppression preview (never changes the design). Red when switched off.'),
  ]);
  h+=sec('Lines (links)',[
    row(line(''),'Grey line','A link: the lower item uses the upper one. Arrows point from what is used to what uses it.'),
    row(line('up'),'Blue, thick','A direct parent of the selection: the selected item uses it directly.'),
    row(line('up ind'),'Blue, faint','A further ancestor: a link between two items the selection depends on.'),
    row(line('down'),'Green, thick','A direct child: it uses the selected item directly.'),
    row(line('down ind'),'Green, faint','A further descendant: a link between two items that depend on the selection.'),
    row(line('insel'),'Purple','A link inside the selection (between items of the selected timeline group).'),
    row(line('dim'),'Very faint','Not related to the selection.'),
    row(line('hov'),'Gold, thick','The link under the mouse, or the one you clicked (stays until the mouse moves). Both its ends get a gold ring.'),
    row(line('order'),'Dashed','"Same body, later": only timeline order on the same body, not a real reference. Off by default (Display menu).'),
    row(sv('<path d="M4,15 L60,15" fill="none" stroke="'+G+'" stroke-width="2.4" stroke-opacity=".85" stroke-linecap="round"/>'),'Thick coloured line','Joins a group header to the group\'s items (Depth layout).'),
    row(line('bridge'),'Dotted','Not a direct link: it goes through items that are hidden by the Filter menu or folded into a collapsed box ("N hidden"). The chain continues through them.'),
    row(sv('<path class="edge" d="M4,24 C22,24 20,6 38,6 S60,6 64,6"/>'),'Curves and bends','Only routing: long links bend sideways so they do not lie on top of each other. The shape means nothing.'),
  ],'Each line may stand for several references between the same two items; hover it to see which kinds.');
  h+=sec('Link kinds',Object.keys(KIND).filter(k=>KIND_DESC[k]).map(k=>row('<span class="pill lgk">'+KIND[k]+'</span>',KIND[k],KIND_DESC[k])),
    'Why the lower item uses the upper one. All kinds are always shown, except "Same body, later" (Display menu).');
  h+=sec('History playback',[
    row(sv('<path class="pblit base" d="M4,15 L60,15" style="stroke-width:3"/><path class="pblit trail" d="M4,15 L36,15" style="stroke-width:4.5"/><circle cx="36" cy="15" r="9" class="pbhalo"/><circle cx="36" cy="15" r="5" class="pbcore"/>'),'Blue dot and gold line','Travels from items already built to the next one; the link it follows lights up in gold.'),
    row(box('var(--c-solid-bg)','var(--c-solid)',{extra:'<rect x="0" y="1" width="70" height="28" rx="8" class="pbpulse" style="stroke-width:2.5"/>'}),'Blue ring','An item appears (fades in to full colour).'),
    row(sv('<rect x="10" y="2" width="50" height="26" rx="4" fill="var(--panel)" stroke="var(--border)"/><rect x="14" y="5" width="42" height="14" rx="2" fill="var(--panel2)"/><rect x="14" y="21" width="16" height="5" rx="2" fill="var(--c-solid-bg)"/>'),'Card (bottom right)','Thumbnail, type and name of the item being built.'),
  ],'▶ Play (or P) with an item selected plays how it was built; with a group selected, how the group was built; with nothing selected, the whole history. Space pauses, → skips, Esc stops.');
  h+=sec('Header, panels and tree',[
    row('<button class="lgbtn hb"><span class="ic">!</span>2 broken</button>','Broken chip','Items that fail to compute now (in the design, or in the suppression preview), plus how many may fail (estimated). Click to step through them.'),
    row('<button class="lgbtn hw"><span class="ic"></span>1 warning</button>','Warning chip','Items with warnings. Click to step through them.'),
    row('<span class="kv">+12 outside</span>','"+N outside" (groups panel)','Suppressing the group also suppresses N items outside it (group test).'),
    row('<span class="kv" style="color:var(--c-sketch)">independent</span>','"independent"','The group can be suppressed on its own.'),
    row('<span class="st-err" style="font-size:12px">breaks X</span>','"breaks X"','Fusion refuses to suppress it because X then fails; the preview can still suppress it (estimated).'),
    row('<span class="kv">3/5 off</span>','"N/M off"','In the suppression preview, N of the group\'s M items are off.'),
    row('<span class="simb b">● fails</span><span class="simb w">▲ warning</span>','Tree markers','Same meaning as the red and orange markers on the boxes.'),
  ]);
  b.innerHTML=h;}
function setLegend(open){document.body.classList.toggle('legendopen',!!open);$('legendBtn').classList.toggle('on',!!open);if(open){setInfo(false);renderLegend();}}
$('legendBtn').onclick=()=>setLegend(!document.body.classList.contains('legendopen'));
$('legendClose').onclick=()=>setLegend(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('legendopen')){e.stopImmediatePropagation();setLegend(false);}},true);
function setView(v){view=v;$('vTree').classList.toggle('on',v==='tree');$('vGraph').classList.toggle('on',v==='graph');$('tree').style.display=v==='tree'?'':'none';$('graphwrap').style.display=v==='graph'?'block':'none';$('treeCtrl').style.display=v==='tree'?'':'none';$('layoutSeg').style.display=v==='graph'?'':'none';renderDetails();if(v==='tree')renderTree();setTimeout(updateSearchNav,0);}
function refresh(){buildAdj();renderDetails();if(view==='tree')renderTree();else renderGraph(false);}
$('vTree').onclick=()=>setView('tree');
$('vGraph').onclick=()=>{setView('graph');renderGraph(true);};
$('treeMode').onchange=renderTree;
renderCatFilter();
$('catAll').onclick=()=>{Object.keys(catOn).forEach(c=>catOn[c]=true);renderCatFilter();filterChanged();};
$('catNone').onclick=()=>{Object.keys(catOn).forEach(c=>catOn[c]=false);renderCatFilter();filterChanged();};
$('showOrder').onchange=e=>{const v=e.target.checked;if(view==='graph'&&Object.keys(pos).length)animatedRerender(()=>{kindOn.order=v;buildAdj();renderDetails();},{dur:420});else{kindOn.order=v;refresh();}};
if(!kindsPresent.includes('order')){const l=$('showOrder').parentNode;l.style.display='none';if(l.previousElementSibling)l.previousElementSibling.style.display='none';}
if(hasThumbs)$('thumbCtrl').style.display='';
$('infoBtn').onclick=()=>{const open=!document.body.classList.contains('infoopen');if(open){renderInfo();}setInfo(open);};
$('infoClose').onclick=()=>setInfo(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(document.body.classList.contains('infoopen'))setInfo(false);else if(selected||selGroup)clearSel();}});
$('gpToggle').onclick=()=>{const m=$('gpanel').classList.toggle('min');$('gpToggle').textContent=m?'+':'–';};
$('showThumbs').onchange=e=>{const v=e.target.checked;const ch=()=>{showThumbs=v;document.body.classList.toggle('nothumbs',!showThumbs);peekHide();renderDetails();};
  if(view==='graph'&&Object.keys(pos).length)animatedRerender(ch,{fit:'fit',dur:480});else{ch();if(view==='graph')renderGraph(true);}};
$('pullTog').onchange=e=>{const v=e.target.checked;if(view==='graph'&&Object.keys(pos).length)animatedRerender(()=>{pullTogether=v;},{dur:450});else pullTogether=v;};
$('focus').onchange=e=>{const v=e.target.checked;if(view==='graph'&&Object.keys(pos).length)animatedRerender(()=>{focus=v;},{fit:'fit'});else{focus=v;renderGraph(true,selected);}};
$('expAll').onclick=()=>{setLevel('items');};
$('colAll').onclick=()=>{collapseEverything=true;setLevel('groups');};
$('fit').onclick=()=>{const r=(selected||selGroup)?selRelated:null;fitAnimated(()=>fit(null,r));};

$('hBack').onclick=()=>goHist(-1);$('hNext').onclick=()=>goHist(1);
document.addEventListener('keydown',e=>{if(e.target&&(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'))return;if(e.altKey&&e.key==='ArrowLeft'){e.preventDefault();goHist(-1);}else if(e.altKey&&e.key==='ArrowRight'){e.preventDefault();goHist(1);}});
window.addEventListener('mouseup',e=>{if(e.button===3){e.preventDefault();goHist(-1);}else if(e.button===4){e.preventDefault();goHist(1);}});
document.querySelectorAll('.pop>button').forEach(b=>b.onclick=e=>{e.stopPropagation();const p=b.parentElement;const o=!p.classList.contains('open');document.querySelectorAll('.pop.open').forEach(x=>x.classList.remove('open'));if(o)p.classList.add('open');});
document.querySelectorAll('.popbox').forEach(x=>x.addEventListener('click',e=>e.stopPropagation()));
document.addEventListener('mousedown',e=>{if(!e.target.closest||!e.target.closest('.pop'))document.querySelectorAll('.pop.open').forEach(x=>x.classList.remove('open'));},true);
function updateFilterBtn(){const cs=Object.keys(catOn);const off=cs.filter(c=>!catOn[c]);const def=!off.length;$('filterBtn').textContent='Filter '+(def?'':'('+(cs.length-off.length)+'/'+cs.length+') ')+'▾';}
function updateLinksBtn(){updateFilterBtn();}
function setLayout(m){layoutMode=m;$('laneBtn').classList.toggle('on',m==='lanes');$('compBtn').classList.toggle('on',m==='comps');$('layDeps').classList.toggle('on',m==='deps');renderGraph(false);fitInitial();}
$('layDeps').onclick=()=>setLayout('deps');
$('laneBtn').onclick=()=>{setLayout('lanes');};
$('compBtn').onclick=()=>{if(catOn.component===false){catOn.component=true;renderCatFilter();updateFilterBtn();buildAdj();}setLayout('comps');};
function setLevel(l){
  const apply=()=>{collapsedNodes.clear();if(l==='groups')expanded.clear();else Object.keys(groups).forEach(g=>expanded.add(g));};
  if(view==='graph'&&Object.keys(pos).length)animatedRerender(apply,{fit:'initial'});else{apply();if(view==='graph')renderGraph(true);else renderTree();}}
let graphHits=[],hitIdx=0;
function updateSearchNav(){const nav=$('sNav');const n=view==='graph'?graphHits.length:nodes.filter(x=>visibleNode(x)&&matches(x)).length;
  nav.style.display=search?'inline-flex':'none';$('sCount').textContent=search?(n?(view==='graph'&&n?(hitIdx+1)+'/'+n:n+' match'+(n===1?'':'es')):'no matches'):'';
  $('sPrev').style.display=$('sNext').style.display=(view==='graph'&&n>1)?'':'none';}
function goHit(step){if(view!=='graph'||!graphHits.length)return;hitIdx=(hitIdx+step+graphHits.length)%graphHits.length;renderGraph(false);const id=graphHits[hitIdx];if(pos[id]){const W=svg.clientWidth||800,H=svg.clientHeight||600;T.k=Math.max(T.k,1);T.x=W/2-(pos[id].x+NW/2)*T.k;T.y=H/2-(pos[id].y+NH/2)*T.k;applyT();}updateSearchNav();}
let st;$('search').oninput=e=>{clearTimeout(st);st=setTimeout(()=>{search=e.target.value.trim().toLowerCase();hitIdx=0;if(view==='tree')renderTree();else{renderGraph(false);if(graphHits.length)goHit(0);}updateSearchNav();},150);};
$('search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();goHit(e.shiftKey?-1:1);}});
$('sPrev').onclick=()=>goHit(-1);$('sNext').onclick=()=>goHit(1);
if(canGroups){simOn=true;simCompute();renderSimBar();}
renderHealth();updateLinksBtn();hist.push(snap());hIdx=0;updHistBtns();
applyT();buildAdj();renderGroupPanel();setView('graph');renderGraph(false);fit();setInfo(true);
// opening: show the whole graph first, then glide in to the top row, at its middle (nothing gets selected)
{// the box in the top row that is closest to the horizontal middle of the graph
  const ids=Object.keys(pos);let first=null;
  if(ids.length){const top=Math.min(...ids.map(i=>pos[i].y));const x0=Math.min(...ids.map(i=>pos[i].x)),x1=Math.max(...ids.map(i=>pos[i].x+NW));const mid=(x0+x1)/2;
    first=ids.filter(i=>Math.abs(pos[i].y-top)<1).sort((a,b)=>Math.abs(pos[a].x+NW/2-mid)-Math.abs(pos[b].x+NW/2-mid))[0];}
  if(first)setTimeout(()=>{if(!selected&&!selGroup)focusOn([first],1100,true);},650);}
})();
</script>
</body>
</html>
'''
