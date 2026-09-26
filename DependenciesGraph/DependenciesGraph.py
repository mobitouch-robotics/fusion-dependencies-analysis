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
import threading, secrets, http.server, gc, sys

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
            local = None
            if t == 'Occurrence':
                # a component made in this design (New Component) or one linked from another file (Insert)
                local = not _safe(lambda: e.isReferencedComponent, False)
                if local:
                    info.append('new component')
                else:
                    fname = _safe(lambda: e.component.parentDesign.parentDocument.name)
                    info.append('linked from ' + fname if fname else 'linked component')
            nid = 'n%d' % i
            cat = CAT_BY_TYPE.get(t, 'other')
            if t in SURFACE_WHEN_NOT_SOLID and _safe(lambda: e.isSolid) is False:
                cat = 'surface'
                info.append('surface')
            node = {'id': nid, 'name': it.name, 'type': t, 'cat': cat, 'tl': i, 'o': order,
                    'tok': _safe(lambda: e.entityToken) or '',   # to select it in Fusion from the page
                    'g': path, 'supp': bool(_safe(lambda: it.isSuppressed, False)),
                    'health': _safe(lambda: it.healthState, 0),
                    'msg': re.sub(r'<[^>]+>', ' ', _safe(lambda: it.errorOrWarningMessage, '') or '').strip()[:400],
                    'info': ', '.join(info)}
            if local is not None:
                node['local'] = local
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
        self._thumb_file = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', '_thumb.jpg')   # JPEG: a fraction of PNG's size
        os.makedirs(os.path.dirname(self._thumb_file), exist_ok=True)

    def _shot(self, iw, ih):
        """The viewport as a data URI: JPEG (much smaller), PNG when this Fusion cannot write JPEG."""
        for path, mime in ((self._thumb_file, 'image/jpeg'), (self._thumb_file[:-4] + '.png', 'image/png')):
            _safe(lambda: os.remove(path))
            if _safe(lambda: self._vp.saveAsImageFile(path, iw, ih), False) and os.path.exists(path):
                with open(path, 'rb') as f:
                    return 'data:%s;base64,%s' % (mime, base64.b64encode(f.read()).decode('ascii'))
        return None

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
            data = self._shot(iw, ih)
            if data:
                self.thumbs[nid] = data
        except Exception as ex:
            if len(self.warnings) < 50:
                self.warnings.append('No thumbnail for %s: %s' % (_safe(lambda: it.name, '?'), ex))
        finally:
            self._show_bodies()
            self._restore_visible()
            _safe(sels.clear)

    def scan(self):
        """One forward walk: with the marker just before each item, its references are read; the previous item is
        computed by then, so its outputs (face/body owners) are recorded and its picture taken in the same step."""
        tl = self.tl
        n_items = tl.count
        stop = getattr(self, 'cancelled', None)
        prev = None
        self.thumbs_begin()
        for i in range(n_items):
            if stop and i % 5 == 0 and stop():
                break
            it = tl.item(i)
            if it.isGroup:
                continue
            nid = self.tl2node.get(i)
            if self.progress:
                self.progress(it.name, i, n_items)
            if not getattr(self, 'no_roll', False):
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
        if not getattr(self, 'no_roll', False):
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
                               'tl': None, 'o': o, 'g': [], 'supp': False, 'health': 0, 'msg': '', 'info': ' · '.join(info),
                               'occ': (_safe(lambda: occ.fullPathName) or '') if occ is not None else ''})
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
                    data = self._shot(iw, ih)
                    if data:
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
    def _set_suppressed(self, entities, value):
        """Change several suppression states in one Fusion recompute.

        Fusion 360 September 2026+ exposes Design.setSuppressed(), which is
        considerably faster than assigning isSuppressed repeatedly. Keep a
        small fallback for older Fusion builds so the add-in remains usable.
        """
        es = [e for e in (entities or []) if e is not None]
        if not es:
            return True
        fn = getattr(self.des, 'setSuppressed', None)
        if fn is not None:
            try:
                fn(es, bool(value))
                return True
            except Exception as ex:
                # Fusion raises when later features fail to compute, usually after doing the change anyway.
                # Callers check the real state; the message is what the page shows as the reason.
                if all(_safe(lambda: e.isSuppressed, None) == bool(value) for e in es):
                    raise
                first = ex
        else:
            first = None
        err = first
        for e in es:
            try:
                setattr(e, 'isSuppressed', bool(value))
            except Exception as ex:
                err = err or ex
        if err is not None and not all(_safe(lambda: e.isSuppressed, None) == bool(value) for e in es):
            raise err
        if err is not None and first is not None:
            raise first
        return True

    def _leaf_candidates(self, items):
        """Items the reference scan found nothing built on (no link out of them): likely leaves. Only a hint for
        how to group the tests; every result still comes from Fusion."""
        has_out = set(sa for (sa, _t_), k in self.edges.items() if k - {'order'})
        return [i for i in items if self.tl2node.get(i) not in has_out]

    def suppression_test(self, progress, cancelled):
        """Suppress each item and record what Fusion suppresses, breaks or warns about with it.

        Likely leaves are tested in batches first: suppressing a batch that makes nothing outside it change proves
        every item in it has no effect (suppression effects add up), with one recompute instead of two per item.
        A batch where something reacts is split in halves; single items that react are tested on their own, like
        every other item."""
        self._mg_base = _process_memory()
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
        wrn = {}
        ERR = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
        WARN = adsk.fusion.FeatureHealthStates.WarningFeatureHealthState
        st0 = self._state(orig)
        err0 = set(i for i in orig if st0[i][1] == ERR)
        warn0 = set(i for i in orig if st0[i][1] == WARN)
        items = [i for i in orig if not orig[i]]
        total = len(items)
        done = [0]

        def effects(tested):
            """Items outside `tested` that Fusion suppressed, broke or warned about (one read of the timeline)."""
            st = self._state(orig)
            casc = [j for j in orig if j not in tested and not orig[j] and st[j][0]]
            broke = [j for j in orig if j not in tested and not orig[j] and j not in err0 and not st[j][0] and st[j][1] == ERR]
            warned = [j for j in orig if j not in tested and not orig[j] and j not in warn0 and not st[j][0] and st[j][1] == WARN]
            return st, casc, broke, warned

        # --- 1. likely leaves, in batches (the tail of the timeline has nothing after it, so it is among them)
        singles = []
        batch_hits = 0

        def batch(idxs):
            """Suppress the batch; if nothing outside it reacts, switch its items back on one at a time from the
            last: an item that comes back clean (not suppressed, no new error or warning) is not affected by the
            earlier items still off, and it affects nothing later (those came back clean while it was off) and
            nothing outside. So every item proven this way has no effect at all, exactly as its own test would
            show, for one recompute each instead of two."""
            nonlocal batch_hits
            idxs = sorted(idxs)
            if cancelled() or len(idxs) < 2:
                singles.extend(idxs)
                return
            progress('%d items at once' % len(idxs), done[0], total)
            try:
                self._set_suppressed([tl.item(i) for i in idxs], True)
            except Exception:
                pass
            st, casc, broke, warned = effects(set(idxs))
            if not (all(st[i][0] for i in idxs) and not casc and not broke and not warned):
                # something outside reacts: find which items with a few halvings, small groups go one by one
                self._restore_checked(orig, err0, None, '%d items' % len(idxs))
                if len(idxs) <= 6:
                    singles.extend(idxs)
                    return
                h = len(idxs) // 2
                batch(idxs[:h])
                batch(idxs[h:])
                return
            for k in range(len(idxs) - 1, -1, -1):
                i = idxs[k]
                try:
                    self._set_suppressed([tl.item(i)], False)
                except Exception:
                    pass
                s1 = self._state([i])[i]
                nid = self.tl2node.get(i)
                if nid:
                    desc[nid], brk[nid], wrn[nid] = [], [], []
                done[0] += 1
                batch_hits += 1
                if s1[0] or (s1[1] == ERR and i not in err0) or (s1[1] == WARN and i not in warn0):
                    # an earlier item of the batch affects this one: those are tested again, without it
                    self._restore_checked(orig, err0, None, '%d items' % len(idxs))
                    singles.extend(idxs[:k])
                    return
            self._restore_checked(orig, err0, None, '%d items' % len(idxs))

        cands = self._leaf_candidates(items)
        for c in range(0, len(cands), 12):
            batch(cands[c:c + 12])
            tl = self.tl
        rest = sorted(set(items) - set(cands) | set(singles))

        # --- 2. every other item on its own
        for i in rest:
            if cancelled():
                self.warnings.append('Suppression test was cancelled; results are partial.')
                break
            it = tl.item(i)
            progress(it.name, done[0], total)
            done[0] += 1
            fail_msg = None
            try:
                self._set_suppressed([it], True)
            except Exception as ex:
                # Fusion raises when later features fail to compute, but usually suppresses anyway
                fail_msg = str(ex)
            nid = self.tl2node.get(i)
            st, casc, broke, warned = effects({i})
            if not st[i][0]:
                if nid:
                    fails[nid] = self._parse_fail(fail_msg)
                self._restore_checked(orig, err0, None, it.name)
                tl = self.tl
                continue
            if nid:
                desc[nid] = [self.tl2node[j] for j in casc if j in self.tl2node]
                brk[nid] = [self.tl2node[j] for j in broke if j in self.tl2node]
                wrn[nid] = [self.tl2node[j] for j in warned if j in self.tl2node]
            name = _safe(lambda: it.name, '')
            self._restore_checked(orig, err0, i, name)
            tl = self.tl
        self.batched = batch_hits
        vol1 = self.body_signature()
        if vol0 != vol1:
            self.warnings.append('Warning: after the suppression test the bodies differ from before '
                                 '(%s vs %s). Check the design, or revert to the saved version.' % (vol0, vol1))
        # direct edges via transitive reduction
        anc = {}
        for s_, ds in desc.items():
            for d in ds:
                anc.setdefault(d, set()).add(s_)
        for d, ancs in anc.items():
            for s_ in ancs:
                # s -> d is direct if no other ancestor k of d has s as its ancestor
                if not any((s_ in anc.get(k, ())) for k in ancs if k != s_):
                    self.add_edge(s_, d, 'suppress')
        byid = {n['id']: n for n in self.nodes}
        for nid_, b_ in brk.items():
            if nid_ in byid and b_:
                byid[nid_]['dbreak'] = b_
        for nid_, w in wrn.items():
            if nid_ in byid and w:
                byid[nid_]['dwarn'] = w
        for nid_, f in fails.items():
            if nid_ in byid:
                byid[nid_]['fail'] = f
        tested = len(desc) + len(fails)
        if tested < len(items):
            self.warnings.append('Item suppression test: %d of %d items could not be tested.' % (len(items) - tested, len(items)))
        for s_, ds in desc.items():
            if s_ in byid:
                byid[s_]['dsupp'] = ds

    def _state(self, idxs):
        """Suppressed flag and health of the given timeline items, read once each (a table instead of a read per
        question)."""
        tl = self.tl
        out = {}
        for i in idxs:
            it = _safe(lambda: tl.item(i))
            out[i] = (bool(_safe(lambda: it.isSuppressed, False)), _safe(lambda: it.healthState, 0)) if it is not None else (False, 0)
        return out

    def _restore(self, orig, tested=None):
        """Put every item back to its state in orig. Switching the tested item (or group) back on
        brings its whole cascade back in one step. Rolling the marker back and switching items on
        one by one can make Fusion lose edge references in later features, so that is not used."""
        tl = self.tl
        if tested is not None:
            _safe(lambda: self._set_suppressed([tl.item(tested)], False))
        # everything still off goes back on in one recompute (setSuppressed), then the items that were off
        # before go back off in one more; _restore_checked verifies the result and falls back if needed
        groups_on = [g for g in (_safe(lambda: list(tl.timelineGroups)) or []) if _safe(lambda: g.isSuppressed)]
        if groups_on:
            _safe(lambda: self._set_suppressed(groups_on, False))
        on = [tl.item(i) for i in sorted(orig) if not orig[i] and _safe(lambda: tl.item(i).isSuppressed, False)]
        if on:
            _safe(lambda: self._set_suppressed(on, False))
        # items that were suppressed before must stay suppressed (unsuppressing a group can wake them)
        off = [tl.item(i) for i in sorted(orig, reverse=True) if orig[i] and not _safe(lambda: tl.item(i).isSuppressed, True)]
        if off:
            _safe(lambda: self._set_suppressed(off, True))
        _safe(lambda: tl.moveToEnd())

    def _clean(self, orig, err0):
        """True when every item is back in its original state and nothing new fails to compute."""
        ERR = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
        st = self._state(orig)
        for i in orig:
            sup, h = st[i]
            if sup != orig[i]:
                return False
            if i not in err0 and not orig[i] and h == ERR:
                return False
        return True

    MEM_GROWTH_LIMIT = 3 * 1024 ** 3        # bytes Fusion may grow by during the tests before the design is reopened

    def _memory_guard(self, orig, err0):
        """Every suppress/restore leaves undo history and cached geometry behind in the document, and Fusion keeps
        it as long as the document is open, so memory grows with every test. Every few tests Fusion's memory is
        checked; when it has grown too much the design is reopened from its saved version (nothing is lost: it
        is in its original state after every test), which drops all of that."""
        now = time.time()
        if now - getattr(self, '_mg_t', 0) < 1.0:
            return
        self._mg_t = now
        rss = _process_memory()
        if rss is None:
            return
        if getattr(self, '_mg_base', None) is None:
            self._mg_base = rss
            return
        if rss - self._mg_base < self.MEM_GROWTH_LIMIT:
            return
        name = _safe(lambda: self.des.parentDocument.name, 'the design')
        if self._recover() and self._clean(orig, err0):
            self.refreshed = getattr(self, 'refreshed', 0) + 1
            gc.collect()
            adsk.doEvents()
            after = _process_memory()
            _mem_log('reopened %s to free memory: %.1f GB -> %s GB' % (name, rss / 1024 ** 3,
                                                                       '%.1f' % (after / 1024 ** 3) if after else '?'))
            self._mg_base = after or rss

    def _recover(self):
        """Reopen the saved version (the run only starts on a saved design) and continue on it."""
        app = adsk.core.Application.get()
        hd = getattr(self, 'hidden_doc', None)
        if hd is not None:
            # a derived design read in a hidden document: reopen that version, hidden again
            df = _safe(lambda: hd.dataFile)
            active = _safe(lambda: app.activeDocument)
            try:
                hd.close(False)
                nd = app.documents.open(df, False)
            except Exception:
                return False
            if active is not None and _safe(lambda: app.activeDocument) != active:
                _safe(active.activate)
            self.hidden_doc = nd
            self.des = adsk.fusion.Design.cast(nd.products.itemByProductType('DesignProductType'))
            self.tl = self.des.timeline
            self.root = self.des.rootComponent
            self.expand_groups()
            self.recovered = getattr(self, 'recovered', 0) + 1
            return True
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
        if getattr(self, 'doc', None) is not None:
            self.doc = nd
        self.des = adsk.fusion.Design.cast(app.activeProduct)
        self.tl = self.des.timeline
        self.root = self.des.rootComponent
        self.expand_groups()
        self.recovered = getattr(self, 'recovered', 0) + 1
        return True

    def _restore_checked(self, orig, err0, tested=None, what=''):
        self._restore(orig, tested)
        if self._clean(orig, err0):
            self._memory_guard(orig, err0)
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
        self._mg_base = _process_memory()
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
                self._set_suppressed([g], True)
            except Exception as ex:
                # Fusion reports downstream compute failures as an error; see below.
                fail_msg = str(ex)
            if not _safe(lambda: g.isSuppressed):
                # Fusion refused the group as a whole (a later feature failed to compute).
                # Suppress its items one by one instead, which is what happens in the UI.
                for i in sorted(inside, reverse=True):
                    if not orig.get(i):
                        try:
                            self._set_suppressed([tl.item(i)], True)
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
            st = self._state(orig)      # one read of the whole timeline
            ERR_ = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
            casc = [i for i in orig if i not in inside and not orig[i] and st[i][0]]
            broke = [i for i in orig if i not in inside and not orig[i] and i not in err0
                     and not st[i][0] and st[i][1] == ERR_]
            if gid in byg:
                byg[gid]['dsupp'] = [self.tl2node[i] for i in casc if i in self.tl2node]
                byg[gid]['dbreak'] = [self.tl2node[i] for i in broke if i in self.tl2node]
                warned = [i for i in orig if i not in inside and not orig[i] and i not in warn0
                          and not st[i][0] and st[i][1] == WARN]
                if warned:
                    byg[gid]['dwarn'] = [self.tl2node[i] for i in warned if i in self.tl2node]
            gname = _safe(lambda: g.name, gid)
            _safe(lambda: self._set_suppressed([g], False))
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

    def result(self, doc_name, exact, generation_seconds=None):
        edges = [{'s': s, 't': t, 'k': sorted(k)} for (s, t), k in self.edges.items()]
        return {'meta': {'doc': doc_name, 'exact': exact, 'pic': getattr(self, 'part_pic', None), 'gtest': getattr(self, 'gtested', False), 'warnings': self.warnings,
                         'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                         'generationSeconds': round(float(generation_seconds), 1) if generation_seconds is not None else None},
                'nodes': self.nodes, 'groups': self.groups, 'edges': edges, 'thumbs': self.thumbs}


# ------------------------------------------------------- derived designs ---
# With "Include derived designs", every design brought in with a Derive feature is read too (references only:
# it is not the active document, so it is not suppression-tested), and so are the designs those derive from, at
# any depth. Each one becomes a timeline group of its own ('X1', 'X2'...) holding its items and its own timeline
# groups, so the page shows it as one block named after the design. Its links end in the Derive feature: from the
# items it hands over (the source entities, bodies) and from its parameters to the derived parameters.

def _has_geometry(des):
    for c in (_safe(lambda: list(des.allComponents)) or []):
        if (_safe(lambda: c.bRepBodies.count, 0) or 0) or (_safe(lambda: c.meshBodies.count, 0) or 0):
            return True
    return False


def _part_picture(w=640, h=420):
    """A picture of the whole part in the active window: isometric view, fitted, camera put back afterwards."""
    app = adsk.core.Application.get()
    vp = _safe(lambda: app.activeViewport)
    if vp is None:
        return None
    cam0 = _safe(lambda: vp.camera)
    path = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', '_part.png')
    try:
        cam = vp.camera
        cam.viewOrientation = adsk.core.ViewOrientations.IsoTopRightViewOrientation
        cam.isFitView = True
        cam.isSmoothTransition = False
        vp.camera = cam
        adsk.doEvents()
        vp.fit()
        adsk.doEvents()
        if not vp.saveAsImageFile(path, w, h) or not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            return 'data:image/png;base64,' + base64.b64encode(f.read()).decode('ascii')
    except Exception:
        return None
    finally:
        if cam0 is not None:
            _safe(lambda: setattr(vp, 'camera', cam0))


class _PlainDesign:
    """Stand-in collector for a linked design without a timeline (a direct-modelling design, e.g. a library part):
    no items of its own, only its frame, connector and picture."""
    def __init__(self, des):
        self.des = des
        self.root = _safe(lambda: des.rootComponent)
        self.tl = None
        self.nodes, self.groups, self.edges, self.warnings = [], [], {}, []
        self.tl2node, self.body_owner, self.comp_owner, self.by_tlname = {}, {}, {}, {}

    def restore_groups(self):
        pass

    def occ_node(self, occ):
        return None


def _linked_occurrences(des):
    """The outermost linked (inserted, referenced) components of a design: the ones inside a linked design are
    found when that design itself is read."""
    out = []
    for o in (_safe(lambda: list(des.rootComponent.allOccurrences)) or []):
        if not _safe(lambda: o.isReferencedComponent, False):
            continue
        a, inner, guard = _safe(lambda: o.assemblyContext), False, 0
        while a is not None and guard < 20:
            if _safe(lambda: a.isReferencedComponent, False):
                inner = True
                break
            a, guard = _safe(lambda: a.assemblyContext), guard + 1
        if not inner:
            out.append(o)
    return out


def _derive_features(col):
    out = []
    for idx, nid in sorted(col.tl2node.items()):
        e = _safe(lambda: col.tl.item(idx).entity)
        if e is not None and _t(e) == 'DeriveFeature':
            out.append((nid, e))
    return out


def _source_param(name, src_names):
    """The source parameter a derived parameter comes from: the same name, or the longest source name it starts
    with (derived parameters are often renamed with a suffix, e.g. Width -> Width_Ref)."""
    if name in src_names:
        return name
    best = None
    for n in src_names:
        if name.startswith(n) and (best is None or len(n) > len(best)):
            best = n
    return best


def _open_version(sd):
    """Opens the saved version a Derive feature uses as a hidden document of its own, so it can be read (and
    its timeline stepped through) without touching the copy the open design references. None if it cannot."""
    app = adsk.core.Application.get()
    ref_doc = _safe(lambda: sd.parentDocument)
    dfile = _safe(lambda: ref_doc.dataFile)
    if dfile is None:
        return None
    ver = _safe(lambda: dfile.versionNumber)
    target = dfile
    for v in (_safe(lambda: list(dfile.versions)) or []):
        if _safe(lambda: v.versionNumber) == ver:
            target = v
            break
    active = _safe(lambda: app.activeDocument)
    before = list(_safe(lambda: list(app.documents)) or [])
    try:
        doc = app.documents.open(target, False)
    except Exception:
        return None
    if active is not None and _safe(lambda: app.activeDocument) != active:
        _safe(active.activate)
    # Fusion hands back a document that is already open (e.g. a tab of the user's) instead of a new one: that one
    # must be neither closed nor changed
    mine = not any(_safe(lambda: d == doc, False) for d in before)
    return doc, mine


# ------------------------------------------------------------ memory ---
def _process_memory():
    """Memory Fusion uses now (resident set, bytes); None when it cannot be read."""
    try:
        if sys.platform == 'darwin':
            import subprocess
            out = subprocess.run(['ps', '-o', 'rss=', '-p', str(os.getpid())], capture_output=True, text=True,
                                 timeout=5, env={'PATH': '/bin:/usr/bin'}).stdout.strip()
            return int(out) * 1024 if out else None
        if sys.platform.startswith('win'):
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            h = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return int(c.WorkingSetSize)
    except Exception:
        pass
    return None


def _mem_log(msg):
    """One line in the run's log (temporary folder / FusionDependenciesGraph / run_log.txt)."""
    try:
        p = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', 'run_log.txt')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        m = _process_memory()
        with open(p, 'a', encoding='utf-8') as f:
            f.write('%s  %s  (Fusion memory: %s)\n' % (datetime.datetime.now().strftime('%H:%M:%S'), msg,
                                                        '%.1f GB' % (m / 1024 ** 3) if m else '?'))
    except Exception:
        pass


# ------------------------------------------------------------ result cache ---
# A saved version of a design never changes, so what was read and tested in it can be kept and reused: a later run
# (or another assembly using the same part) takes it from here instead of opening and testing the design again.
CACHE_VERSION = 2


def _cache_dir():
    d = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', 'cache')
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(kind, file_id, ver):
    safe = re.sub(r'[^\w\-]+', '_', str(file_id))[-80:]
    return os.path.join(_cache_dir(), '%s_%s_v%s.json' % (kind, safe, ver))


def _cache_load(kind, file_id, ver):
    if not file_id or ver is None or not _settings().get('reuse', True):
        return None
    try:
        with open(_cache_path(kind, file_id, ver), 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if d.get('cv') == CACHE_VERSION else None
    except Exception:
        return None


def _cache_save(kind, file_id, ver, d):
    if not file_id or ver is None:
        return
    old = _cache_load(kind, file_id, ver)
    if old and any(old.get(f) and not d.get(f) for f in ('exact', 'groups', 'pics')):
        return          # never replace a more complete result (e.g. a Full analysis) with a lesser one
    try:
        d = dict(d)
        d['cv'] = CACHE_VERSION
        tmp = _cache_path(kind, file_id, ver) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f)
        os.replace(tmp, _cache_path(kind, file_id, ver))
    except Exception:
        pass


class _CachedDesign:
    """A linked design as read (and tested) in an earlier run: plain data only, nothing is opened."""
    def __init__(self, d):
        self.nodes = d.get('nodes') or []
        self.groups = d.get('groups') or []
        self.edges = {(a, b): set(k) for a, b, k in (d.get('edges') or [])}
        self.warnings = list(d.get('warnings') or [])
        self.by_tlname = d.get('by_tlname') or {}
        self.body_owner = d.get('body_owner') or {}
        self.comp_owner = d.get('comp_owner') or {}
        self.tl2node = {int(k): v for k, v in (d.get('tl2node') or {}).items()}
        self.gtested = d.get('gtested', False)

    def restore_groups(self):
        pass


def _design_data(col):
    """The plain data of a read design, as the cache keeps it."""
    return {'nodes': col.nodes, 'groups': col.groups, 'edges': [[a, b, sorted(k)] for (a, b), k in col.edges.items()],
            'warnings': list(col.warnings), 'by_tlname': getattr(col, 'by_tlname', {}) or {},
            'body_owner': getattr(col, 'body_owner', {}) or {}, 'comp_owner': getattr(col, 'comp_owner', {}) or {},
            'tl2node': {str(k): v for k, v in (getattr(col, 'tl2node', {}) or {}).items()},
            'gtested': getattr(col, 'gtested', False)}


def _collect_derived(main, progress, cancelled, exact=False, groups_test=False, pictures=False, max_designs=80, plan=None):
    """Linked designs, each opened once: open -> read -> test (Full analysis) -> note the designs it links -> close,
    then the next one from a queue. What a Derive hands over is noted by name while the deriving design is open
    and matched once the source design has been read (names are stable within a saved version)."""
    sources = []            # read designs, in the order they were processed
    by_key = {}             # file id -> entry (queued or read)
    queue = []
    links = []              # (source id, target id) across designs, ids already prefixed
    app = adsk.core.Application.get()

    capped = [False]

    def step_labels(testing=True):
        return ['Reading'] + (['Group test'] if groups_test and testing else []) + (['Item test'] if exact and testing else [])

    def entry_for(sd, depth):
        """The queue entry for a linked design (one per file), created when first seen."""
        ref_doc = _safe(lambda: sd.parentDocument)
        name = _safe(lambda: ref_doc.name) or 'Linked design'
        dfile = _safe(lambda: ref_doc.dataFile)
        return add_entry(_safe(lambda: dfile.id) or name, name, dfile, _safe(lambda: dfile.versionNumber), depth)

    def add_entry(key, name, dfile, ver, depth):
        e = by_key.get(key)
        if e is None:
            if len(by_key) >= max_designs:
                if not capped[0]:
                    capped[0] = True
                    main.warnings.append('More than %d linked designs: the rest were left out.' % max_designs)
                return None
            k = len(by_key) + 1
            e = {'key': key, 'name': name, 'data_file': dfile, 'read_ver': ver, 'depth': depth,
                 'versions': {ver} if ver is not None else set(), 'prefix': 'x%d:' % k, 'gid': 'X%d' % k,
                 'col': None, 'doc': None, 'specs': [], 'into': set(), 'targets': set(), 'via': set()}
            by_key[key] = e
            queue.append(e)
            _prow(key, name, 'Waiting', 0, 'wait', step_labels())
        else:
            e['depth'] = max(e['depth'], depth)
            if ver is not None:
                e['versions'].add(ver)
        return e

    def apply_link(rec, prefix, depth):
        """One link of a design to a design it derives or inserts: queue that design, add the arrows."""
        e = add_entry(rec['key'], rec['name'], rec.get('dfile'), rec['ver'], depth)
        if e is None:
            return
        e['via'].add(rec['via'])
        for t in rec['targets']:
            e['targets'].add(prefix + t)
        e['specs'].extend(tuple(x) for x in rec['specs'])

    def note_links(col, prefix, depth, out=None):
        """While a design is open: the designs it derives from or inserts, and what each Derive hands over.
        `out` collects the same links as plain data, for the cache."""
        def link(sd, via, target, specs):
            ref_doc = _safe(lambda: sd.parentDocument)
            name = _safe(lambda: ref_doc.name) or 'Linked design'
            dfile = _safe(lambda: ref_doc.dataFile)
            rec = {'key': _safe(lambda: dfile.id) or name, 'name': name, 'ver': _safe(lambda: dfile.versionNumber),
                   'via': via, 'targets': [target] if target else [], 'specs': specs}
            if out is not None:
                out.append(dict(rec))
            rec['dfile'] = dfile
            apply_link(rec, prefix, depth)

        for nid, df in _derive_features(col):
            if cancelled():
                return
            sd = _safe(lambda: df.sourceDesign)
            if sd is None:
                main.warnings.append('The source design of %s could not be read.' % _safe(lambda: df.name, 'a Derive feature'))
                continue
            specs = []
            for se in (_safe(lambda: list(df.sourceEntities)) or []):
                t = _t(se)
                specs.append(('tl', _safe(lambda: se.timelineObject.name)))
                if t == 'BRepBody':
                    specs.append(('body', _safe(lambda: se.name)))
                if t in ('Component', 'Occurrence'):
                    specs.append(('comp', _safe(lambda: se.name) or _safe(lambda: se.component.name)))
            for b in (_safe(lambda: list(df.bodies)) or []):
                sb = _safe(lambda: df.getSourceEntity(b))
                if sb is not None:
                    specs.append(('body', _safe(lambda: sb.name)))
            dname = _safe(lambda: df.timelineObject.name)
            for p in (_safe(lambda: list(col.des.allParameters)) or []):
                if _t(p) == 'DerivedParameter' and _safe(lambda: p.deriveFeature.timelineObject.name) == dname:
                    specs.append(('param', _safe(lambda: p.name, '') or ''))
            link(sd, 'derive', nid, specs)
        for occ in _linked_occurrences(col.des):
            if cancelled():
                return
            sd = _safe(lambda: occ.component.parentDesign)
            if sd is None:
                continue
            t = _safe(lambda: col.occ_node(occ)) or _safe(lambda: col.comp_ref(occ))
            link(sd, 'insert', t, [])

    def open_entry(e):
        """Opens the version the link uses, hidden. mine=False: Fusion handed back a document the user has open."""
        dfile = e['data_file'] or _safe(lambda: app.data.findFileById(e['key']))
        if dfile is None:
            return None, False, None
        target = dfile
        for v in (_safe(lambda: list(dfile.versions)) or []):
            if _safe(lambda: v.versionNumber) == e['read_ver']:
                target = v
                break
        active = _safe(lambda: app.activeDocument)
        before = list(_safe(lambda: list(app.documents)) or [])
        try:
            doc = app.documents.open(target, False)
        except Exception:
            return None, False, None
        if active is not None and _safe(lambda: app.activeDocument) != active:
            _safe(active.activate)
        mine = not any(_safe(lambda: d == doc, False) for d in before)
        des = _safe(lambda: adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType')))
        if des is None and mine:
            _safe(lambda: doc.close(False))
        return doc, mine, des

    log_path = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', 'linked_designs_log.txt')

    def log(msg):
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write('%s  %s  (documents open in Fusion: %s)\n' % (
                    datetime.datetime.now().strftime('%H:%M:%S'), msg, _safe(lambda: app.documents.count, '?')))
        except Exception:
            pass

    def release(col):
        """Drops every Fusion object a read design still holds, so the closed document can be freed; only plain
        data (names, ids, links, test results) is kept for the page."""
        if col is None:
            return
        for a in ('des', 'root', 'tl', 'doc', 'hidden_doc', '_vp', '_cam0'):
            if hasattr(col, a):
                setattr(col, a, None)
        for a in ('collapsed', '_hidden', '_changed'):
            if hasattr(col, a):
                setattr(col, a, [])
        for ent in (getattr(col, 'comps', None) or {}).values():
            ent['c'] = None
            ent['occ'] = None

    def picture(doc, des, sc, mine):
        if not (pictures and not cancelled() and _has_geometry(des)):
            return None
        back = _safe(lambda: app.activeDocument)
        if mine and getattr(sc, 'tl', None) is not None:
            _safe(lambda: sc.tl.moveToEnd())
        pic = None
        if _safe(doc.activate) is not False:
            adsk.doEvents()
            pic = _part_picture()
        if back is not None:
            _safe(back.activate)
            adsk.doEvents()
        return pic

    def process(e, n_done):
        name = e['name']
        # read and tested in an earlier run (same saved version, at least the same checks): nothing to open
        c = _cache_load('design', e['key'], e['read_ver'])
        if c and (not exact or c.get('exact')) and (not groups_test or c.get('gtest')) and (not pictures or c.get('pics')):
            e['col'] = _CachedDesign(c)
            e['pic'] = c.get('pic')
            _prow(e['key'], name, 'Taken from an earlier run (same saved version)', 1, 'done', ['From an earlier run'])
            for rec in c.get('links') or []:
                apply_link(rec, e['prefix'], e['depth'] + 1)
            log('from cache %s' % name)
            return
        if progress:
            progress('Opening ' + name, n_done, n_done + len(queue) + 1)
        _prow(e['key'], name, 'Opening...', 0.02, '', step_labels(), 0)
        doc, mine, des = open_entry(e)
        if des is None:
            main.warnings.append('Could not open %s to read it.' % name)
            _prow(e['key'], name, 'Could not open it', 1, 'fail')
            return
        e['doc'] = doc if mine else None
        try:
            if _safe(lambda: des.designType) != adsk.fusion.DesignTypes.ParametricDesignType:
                sc = _PlainDesign(des)
                e['col'] = sc
                e['pic'] = picture(doc, des, sc, mine)
                links = []
                note_links(sc, e['prefix'], e['depth'] + 1, links)
                if mine and not cancelled():
                    d = _design_data(sc)
                    d.update({'links': links, 'pic': e['pic'], 'pics': pictures, 'exact': True, 'gtest': True})
                    _cache_save('design', e['key'], e['read_ver'], d)
                return
            sc = Collector(des, None, False)
            sc.cancelled = cancelled
            sc.doc = None                        # hidden: the main design stays the active one
            sc.no_roll = not mine                # a design the user has open is read as it is
            e['col'] = sc
            if progress:
                progress('Reading ' + name, n_done, n_done + len(queue) + 1)
            testing = bool((exact or groups_test) and mine)
            span = 0.25 if testing else 1.0          # share of this design's bar the reading takes

            def rprog(msg, i, n):
                _prow(e['key'], name, 'Reading: %s (%d/%d)' % (msg, min(i + 1, n), n), min(1.0, (i + 1) / max(1, n)), '',
                      step_labels(testing), 0)
                adsk.doEvents()
            sc.progress = rprog
            if mine:
                sc.expand_groups()
            else:
                sc.collapsed = []
            sc.build_nodes()
            sc.scan()
            e['pic'] = picture(doc, des, sc, mine)
            _safe(sc.scan_components)
            sc.scan_parameters()
            if mine:
                _safe(lambda: sc.tl.moveToEnd())
            elif any(_safe(lambda: g.isCollapsed, False) for g in (_safe(lambda: list(sc.tl.timelineGroups)) or [])):
                main.warnings.append('%s is open in Fusion, so it was read as it is: items inside its collapsed timeline '
                                     'groups are left out. Close it and generate again for the full picture.' % name)
            sc.by_tlname = {}
            for n in sc.nodes:
                if n.get('tl') is not None:
                    sc.by_tlname.setdefault(n['name'], n['id'])
            # its own links, while it is open and its groups are expanded (timeline indexes are read from them)
            links = []
            note_links(sc, e['prefix'], e['depth'] + 1, links)
            tested = False
            if (exact or groups_test) and not cancelled():
                if not mine:
                    main.warnings.append('%s is open in Fusion, so it was not suppression-tested (that would change it). '
                                         'Close it and generate again to test it too.' % name)
                else:
                    # the same tests as on the main design, on this hidden copy (closed without saving)
                    sc.hidden_doc = doc
                    stage = {'k': 0}
                    n_stages = (1 if groups_test else 0) + (1 if exact else 0)

                    def prog(msg, i, n):
                        _prow(e['key'], name, '%s: %s (%d/%d)' % ('Group test' if (groups_test and stage['k'] == 0) else 'Item test', msg, min(i + 1, n), n),
                              min(1.0, (i + 1) / max(1, n)), '', step_labels(True), 1 + stage['k'])
                        if progress:
                            progress('%s: %s' % (name, msg), i, n)
                    _safe(lambda: sc.tl.moveToEnd())
                    if groups_test:
                        try:
                            sc.group_suppression_test(prog, cancelled)
                        except Exception as ex:
                            main.warnings.append('%s: the whole groups test failed: %s' % (name, ex))
                    if groups_test:
                        stage['k'] = 1
                    if exact and not cancelled():
                        try:
                            sc.suppression_test(prog, cancelled)
                        except Exception as ex:
                            main.warnings.append('%s: the item test failed: %s' % (name, ex))
                    tested = not cancelled()
            # kept for later runs: only a complete result from a hidden copy of the saved version
            if mine and not cancelled():
                d = _design_data(sc)
                d.update({'links': links, 'pic': e['pic'], 'pics': pictures,
                          'exact': bool(exact and tested), 'gtest': bool(groups_test and tested)})
                _cache_save('design', e['key'], e['read_ver'], d)
        finally:
            # closed right away: only one linked design is open at a time
            hd = getattr(e['col'], 'hidden_doc', None) or e['doc']
            if hd is not None:
                _safe(e['col'].restore_groups) if e['col'] is not None else None
                _safe(lambda: hd.close(False))
            e['doc'] = None
            if e['col'] is not None:
                _prow(e['key'], name, 'Cancelled' if cancelled() else ('Done' + ('' if mine or isinstance(e['col'], _PlainDesign) else ' (open in Fusion: read as it is, not tested)')), 1,
                      'fail' if cancelled() else 'done')
            release(e['col'])
            des = doc = hd = None
            gc.collect()
            adsk.doEvents()
            log('closed %s' % name)
            _mem_log('closed %s' % name)

    active = _safe(lambda: app.activeDocument)
    try:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            open(log_path, 'w').close()
        except Exception:
            pass
        log('start')
        note_links(main, '', 1)
        n_done = 0
        while queue and not cancelled():
            e = queue.pop(0)
            process(e, n_done)
            if e['col'] is not None:
                sources.append(e)
            n_done += 1
            if plan:
                # the workload known so far: designs read plus those found but not read yet (average size)
                read_cols = [x['col'] for x in sources if isinstance(x['col'], Collector)]
                avg_i = (sum(len(c.tl2node) for c in read_cols) / len(read_cols)) if read_cols else 20
                avg_g = (sum(len(c.groups) for c in read_cols) / len(read_cols)) if read_cols else 2
                total = n_done + len(queue)
                plan(avg_i * total, avg_g * total, total)
            if progress:
                progress('Finished ' + e['name'], n_done, n_done + len(queue))
    finally:
        for e in by_key.values():
            hd = e.get('doc') or getattr(e.get('col'), 'hidden_doc', None)
            if hd is not None and _safe(lambda: hd.isValid, False):
                _safe(lambda: hd.close(False))
        if active is not None and _safe(lambda: app.activeDocument) != active:
            _safe(active.activate)
    if not sources:
        return

    # what each Derive hands over, matched by name now that the source designs have been read
    for e in sources:
        sc, sp = e['col'], e['prefix']
        src_names = [n['name'] for n in sc.nodes if n['type'] == 'UserParameter']
        for kind, val in e['specs']:
            if not val:
                continue
            n = None
            if kind == 'tl':
                n = sc.by_tlname.get(val)
            elif kind == 'body':
                n = sc.body_owner.get(val)
            elif kind == 'comp':
                n = sc.comp_owner.get(val)
            elif kind == 'param':
                sn = _source_param(val, src_names)
                n = 'p:' + sn if sn else None
            if n:
                e['into'].add(sp + n)

    # deepest sources first, then the main design (its items have o >= 0 and user parameters o = -1..)
    order = sorted(sources, key=lambda s: -s['depth'])
    for rank, src in enumerate(order):
        sc, sp, gid = src['col'], src['prefix'], src['gid']
        base = -1e6 + rank * 1e4
        if len(src['versions']) > 1:
            base_name = re.sub(r'\s+v\d+$', '', src['name'])
            src['name'] = '%s (v%s; read v%s)' % (base_name, ', v'.join(str(v) for v in sorted(src['versions'])), src['read_ver'])
            main.warnings.append('%s is linked at more than one version; it is shown once, read at v%s.' % (base_name, src['read_ver']))
        main.groups.append({'id': gid, 'name': src['name'], 'first': base, 'parent': None, 'design': True,
                            'pic': src.get('pic'), 'via': sorted(src.get('via') or [])})
        # a source design's user parameters only when something uses them (a big design can have hundreds)
        used = set(a for a, _ in sc.edges) | set(b for _, b in sc.edges) | set(a[len(sp):] for a in src['into'])
        pre = lambda ids: [sp + x for x in ids]
        for g in sc.groups:
            ng = {'id': sp + g['id'], 'name': g['name'], 'first': base + 1 + (g['first'] if g['first'] < 10 ** 6 else 9000),
                  'parent': sp + g['parent'] if g.get('parent') else gid}
            for f in ('dsupp', 'dbreak', 'dwarn'):
                if g.get(f): ng[f] = pre(g[f])
            for f in ('fail', 'empty'):
                if f in g: ng[f] = g[f]
            main.groups.append(ng)
        for n in sc.nodes:
            if n['type'] == 'UserParameter' and n['id'] not in used:
                continue
            m = dict(n)
            m['id'] = sp + n['id']
            for f in ('dsupp', 'dbreak', 'dwarn'):
                if n.get(f): m[f] = pre(n[f])
            m['o'] = base + 5 + (n['o'] if n['o'] is not None and n['o'] >= 0 else 0) + (0 if n['o'] is None or n['o'] >= 0 else n['o'] * 0.001)
            m['g'] = [gid] + [sp + x for x in (n.get('g') or [])]
            m['dsg'] = src['name']
            if n.get('tl') is not None:
                m['stl'] = n['tl']
            m['tl'] = None                   # not in this design's timeline: no suppression preview, no Select in Fusion
            m['tok'] = ''
            m.pop('occ', None)
            m['info'] = ((n.get('info') or '') + (' · ' if n.get('info') else '') + 'in ' + src['name']).strip()
            main.nodes.append(m)
        for (a, b), k in sc.edges.items():
            main.edges.setdefault((sp + a, sp + b), set()).update(k)
        # the design's connector: the whole design as one item, on its frame; its parents are the items the
        # Derive features hand over, its children are those Derive features
        port = sp + '@'
        main.nodes.append({'id': port, 'name': src['name'], 'type': 'DerivedDesign', 'cat': 'insert', 'tl': None,
                           'o': base + 9990, 'g': [gid], 'dsg': src['name'], 'port': True, 'tok': '', 'supp': False,
                           'health': 0, 'msg': '', 'info': 'the whole design, as ' + ('it is inserted' if src.get('via') == {'insert'} else 'the Derive features bring it in')})
        links.extend((a, port) for a in src['into'])
        links.extend((port, t) for t in src['targets'])
        for w in sc.warnings[:5]:
            main.warnings.append('%s: %s' % (src['name'], w))
    ids = {n['id'] for n in main.nodes}
    for a, b in links:
        if a in ids and b in ids:
            main.add_edge(a, b, 'derive')


# ------------------------------------------------------------ progress panel ---
# A Fusion palette (a small HTML panel) with one line per design: its name, what is happening, and its own bar,
# plus the whole run's bar, time left and Cancel. Behaves like Fusion's progress dialog (message, progressValue,
# wasCancelled, hide) so the run can fall back to that dialog when a palette cannot be made.
PANEL_ID = 'claudeDesignGraphProgress'
PANEL_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
:root{--bg:#f7f7f5;--fg:#1f1f1d;--mut:#6b6a64;--bar:#e4e2da;--fill:#185fa5;--ok:#1f8a4c;--err:#c0392b;--line:#d9d7cf}
@media (prefers-color-scheme:dark){:root{--bg:#232322;--fg:#ecebe6;--mut:#a3a29c;--bar:#3a3a38;--fill:#6aa8e8;--ok:#5fcf8f;--err:#ff7b6b;--line:#3a3a38}}
body{margin:0;padding:10px 12px;font:12px -apple-system,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
.top{display:flex;align-items:center;gap:8px;margin-bottom:6px}.top b{font-size:13px;flex:1}
button{font:inherit;padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer}
.bar{height:6px;border-radius:3px;background:var(--bar);overflow:hidden}.bar i{display:block;height:100%;width:0;background:var(--fill);transition:width .2s}
#eta{color:var(--mut);margin:4px 0 10px}
.row{padding:6px 0;border-top:1px solid var(--line)}.row .h{display:flex;gap:8px;align-items:baseline}
.row .n{font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row .s{color:var(--mut);font-size:11px;margin:2px 0 4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.segs{display:flex;gap:4px}.sg{flex:1;min-width:0}.sl{font-size:10px;color:var(--mut);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row.done .bar i{background:var(--ok)}.row.fail .bar i{background:var(--err)}.row.fail .s{color:var(--err)}.row.wait{opacity:.55}
</style></head><body>
<div class="top"><b>Dependencies graph</b><button id="cx">Cancel</button></div>
<div class="bar"><i id="all"></i></div><div id="eta">Starting...</div><div id="rows"></div>
<script>
const rows={};let seq=0;
// one bar per step, side by side, each with its name under it
function segs(r,x){const box=r.querySelector('.segs');const L=(x.l&&x.l.length)?x.l:[''];const P=(x.p&&x.p.length)?x.p:[x.f];
  if(box.childElementCount!==L.length||box.dataset.l!==L.join('|')){box.innerHTML='';L.forEach(l=>{const d=document.createElement('div');d.className='sg';d.innerHTML='<div class="bar"><i></i></div><div class="sl"></div>';d.querySelector('.sl').textContent=l;d.title=l;box.appendChild(d);});box.dataset.l=L.join('|');}
  [...box.children].forEach((d,j)=>{d.querySelector('i').style.width=(100*(P[j]||0))+'%';});}
// in progress on top, then waiting, then finished (done or failed); each group keeps the order the designs appeared in
function sortRows(){const box=document.getElementById('rows');const rank=c=>c==='wait'?1:(c==='done'||c==='fail')?2:0;
  Object.values(rows).sort((a,b)=>rank(a.dataset.c)-rank(b.dataset.c)||a.dataset.o-b.dataset.o).forEach(r=>box.appendChild(r));}
function row(k){let r=rows[k];if(!r){r=document.createElement('div');r.className='row wait';r.innerHTML='<div class="h"><span class="n"></span></div><div class="s"></div><div class="segs"></div>';r.dataset.o=seq++;document.getElementById('rows').appendChild(r);rows[k]=r;}return r;}
window.fusionJavaScriptHandler={handle:function(action,data){try{const d=JSON.parse(data);
  if(action==='all'){document.getElementById('all').style.width=(100*d.f)+'%';document.getElementById('eta').textContent=d.t;}
  if(action==='rows'){d.forEach(x=>{const r=row(x.k);r.querySelector('.n').textContent=x.n;r.querySelector('.s').textContent=x.s;segs(r,x);r.className='row '+(x.c||'');r.dataset.c=x.c||'';});sortRows();}
  if(action==='end'){document.getElementById('cx').disabled=true;}
}catch(e){}return 'ok';}};
document.getElementById('cx').onclick=()=>{document.getElementById('cx').textContent='Stopping...';document.getElementById('cx').disabled=true;adsk.fusionSendData('cancel','{}');};
</script></body></html>"""


class _PanelCancelHandler(adsk.core.HTMLEventHandler):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def notify(self, args):
        if _safe(lambda: args.action) == 'cancel':
            self.panel.wasCancelled = True


class _ProgressPanel:
    def __init__(self):
        self.wasCancelled = False
        self._rows = {}
        self._dirty = set()
        self._last = 0.0
        self._all = (0.0, 'Starting...')
        path = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', '_progress.html')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(PANEL_HTML)
        old = _ui.palettes.itemById(PANEL_ID)
        if old:
            _safe(old.deleteMe)
        self.pal = _ui.palettes.add(PANEL_ID, 'Dependencies graph', pathlib.Path(path).as_uri(), True, True, True, 380, 460)
        _safe(lambda: setattr(self.pal, 'dockingState', adsk.core.PaletteDockingStates.PaletteDockStateRight))
        self._h = _PanelCancelHandler(self)
        self.pal.incomingFromHTML.add(self._h)
        _handlers.append(self._h)
        adsk.doEvents()

    # the progress dialog's interface
    @property
    def message(self):
        return self._all[1]

    @message.setter
    def message(self, text):
        self._all = (self._all[0], text)
        self._flush()

    @property
    def progressValue(self):
        return int(self._all[0] * 1000)

    @progressValue.setter
    def progressValue(self, v):
        self._all = (max(0.0, min(1.0, v / 1000.0)), self._all[1])
        self._flush()

    def row(self, key, name, status, frac=None, state='', labels=None, idx=None):
        """One design's line: state '' (working), 'wait', 'done' or 'fail'. With `labels` (its steps) the bar is
        split into one part per step: steps before `idx` full, step `idx` at `frac`, later ones empty."""
        r = self._rows.get(key) or {'k': key, 'n': name, 's': '', 'f': 0.0, 'c': 'wait', 'l': [], 'p': []}
        r.update({'n': name or r['n'], 's': status, 'c': state})
        if labels is not None:
            r['l'] = list(labels)
        if frac is not None:
            r['f'] = max(0.0, min(1.0, frac))
        L = len(r['l'])
        if L:
            if state == 'done':
                r['p'] = [1.0] * L
            elif idx is not None:
                r['p'] = [1.0 if j < idx else (r['f'] if j == idx else 0.0) for j in range(L)]
            elif not r['p'] or len(r['p']) != L:
                r['p'] = [0.0] * L
        self._rows[key] = r
        self._dirty.add(key)
        self._flush()

    def _flush(self, force=False):
        # the panel is redrawn at most a few times a second: sending is not free
        now = time.time()
        if not force and now - self._last < 0.15:
            return
        self._last = now
        f, t = self._all
        _safe(lambda: self.pal.sendInfoToHTML('all', json.dumps({'f': f, 't': t.split('\n')[0]})))
        if self._dirty:
            _safe(lambda: self.pal.sendInfoToHTML('rows', json.dumps([self._rows[k] for k in self._dirty])))
            self._dirty = set()

    def hide(self):
        """End of the run: the panel stays open with the final state of every line (closed with its X)."""
        self._all = (1.0 if not self.wasCancelled else self._all[0],
                     'Cancelled.' if self.wasCancelled else 'Finished: the graph opened in your browser.')
        self._flush(True)
        _safe(lambda: self.pal.sendInfoToHTML('end', '{}'))


_panel = None


def _prow(key, name, status, frac=None, state='', labels=None, idx=None):
    """A design's line in the progress panel (no-op without one)."""
    if _panel is not None:
        _safe(lambda: _panel.row(key, name, status, frac, state, labels, idx))


# ------------------------------------------------------------------- run ---

def generate(mode='both', thumbs=True, derived=False):
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
        chosen = _settings().get('savePath')
        if chosen:
            # a file chosen in the dialog (replaced by each run); the temporary folder when it cannot be written
            try:
                os.makedirs(os.path.dirname(chosen) or '.', exist_ok=True)
                path = chosen
            except Exception:
                pass

        # the same saved version generated before with the same options: the result is reused as it is
        mdf = _safe(lambda: _app.activeDocument.dataFile)
        m_id, m_ver = _safe(lambda: mdf.id), _safe(lambda: mdf.versionNumber)
        m_kind = 'main_%s%s%s%s' % (int(exact), int(groups_test), int(bool(thumbs)), int(bool(derived)))
        cached = None
        if not _safe(lambda: _app.activeDocument.isModified, True):
            # a Full analysis result also answers a Quick estimate (it is exact)
            for kind in (m_kind, 'main_11%s%s' % (int(bool(thumbs)), int(bool(derived)))):
                cached = _cache_load(kind, m_id, m_ver)
                if cached:
                    break
        if cached and cached.get('data'):
            data = cached['data']
            data['meta']['warnings'] = list(data['meta'].get('warnings') or []) + [
                'Reused the result generated for this saved version on %s.' % data['meta'].get('date', '?')]
            return _write_page(data, path, out_dir)

        global _panel
        try:
            progress_dlg = _panel = _ProgressPanel()
        except Exception:
            _panel = None
            progress_dlg = _ui.createProgressDialog()
            progress_dlg.isCancelButtonShown = True
            progress_dlg.show('Dependencies graph', 'Starting...', 0, 1000)
        _prow('main', doc_name + ' (this design)', 'Starting...', 0, '')

        # One bar for the whole run: every step gets a share of it sized by how long it usually takes.
        steps = []                      # [name, weight]
        cur = {'k': 0, 'base': 0.0}

        def set_step(k):
            total = sum(w for _, w in steps) or 1.0
            cur['k'] = k
            cur['base'] = sum(w for _, w in steps[:k]) / total
            cur['span'] = steps[k][1] / total

        stopped = {'v': False}
        run_t0 = time.time()
        eta = {'v': None}

        def time_left(frac):
            """Estimated time left, from the time taken so far and the part of the run done (smoothed, so it does
            not jump with every item)."""
            spent = time.time() - run_t0
            if frac < 0.02 or spent < 5:
                return 'Estimating time left...'
            left = spent * (1 - frac) / frac
            eta['v'] = left if eta['v'] is None else 0.8 * eta['v'] + 0.2 * left
            left = eta['v']
            if left < 45:
                return 'Less than a minute left'
            if left < 90:
                return 'About a minute left'
            if left < 3600:
                return 'About %d min left' % round(left / 60)
            return 'About %d h %02d min left' % (left // 3600, round(left % 3600 / 60))

        def progress(msg, i, n):
            # after Cancel nothing updates the window any more (an update shows it again)
            if stopped['v'] or _safe(lambda: progress_dlg.wasCancelled, False):
                stopped['v'] = True
                return
            frac = cur['base'] + cur.get('span', 0) * min(1.0, i / max(1, n))
            progress_dlg.message = ('%s  ·  step %d of %d: %s  (%d/%d)\n%s' % (
                time_left(frac), cur['k'] + 1, len(steps), steps[cur['k']][0] if steps else '', min(i + 1, n), n,
                msg.replace('%', ' percent')))[:200]
            progress_dlg.progressValue = int(1000 * frac)
            if not cur.get('linked'):
                _prow('main', None, '%s: %s (%d/%d)' % (steps[cur['k']][0] if steps else '', msg, min(i + 1, n), n),
                      min(1.0, (i + 1) / max(1, n)), '', [x[0] for x in steps[:len(steps) - (1 if derived else 0)]], cur['k'])
            adsk.doEvents()

        def cancelled():
            # Cancel stops the whole run: the current test and every step after it
            if not stopped['v']:
                adsk.doEvents()
                stopped['v'] = bool(_safe(lambda: progress_dlg.wasCancelled, False))
            return stopped['v']

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
        if derived:
            # Placeholder until the read-only derived pre-scan has found the exact number of source items/groups.
            steps.append(['Linked designs' + (' (read and tested)' if (exact or groups_test) else ''), 20])
        t0 = time.time()
        try:
            try:
                open(os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', 'run_log.txt'), 'w').close()
            except Exception:
                pass
            _mem_log('start: %s' % doc_name)
            set_step(0)
            col.build_nodes()
            col.scan()
            if thumbs and _has_geometry(col.des):
                col.part_pic = _part_picture()
            _safe(col.scan_components)
            col.scan_parameters()
            _mem_log('read done')
            k = 1
            if groups_test and not cancelled():
                set_step(k); k += 1
                col.group_suppression_test(progress, cancelled)
                _mem_log('group test done')
            if exact and not cancelled():
                set_step(k); k += 1
                col.suppression_test(progress, cancelled)
                _mem_log('item test done')
            _prow('main', None, 'Cancelled' if cancelled() else 'Done', 1, 'fail' if cancelled() else 'done')
            if derived and not cancelled():
                # while the groups are still expanded: the derive features' timeline indexes are read from them
                cur['linked'] = True
                set_step(k)
                _safe(lambda: col.tl.moveToEnd())
                try:
                    def _derived_plan(di, dg, nd):
                        if exact or groups_test:
                            steps[k][1] = max(1.0, di * 1.7 + dg * 2.5 + max(1, nd) * 2.0)
                        else:
                            steps[k][1] = max(1.0, di * 0.05 + dg * 0.1 + max(1, nd) * 1.0)
                        set_step(k)
                    _collect_derived(col, progress, cancelled, exact, groups_test, thumbs, plan=_derived_plan)
                except Exception as ex:
                    col.warnings.append('Could not read the derived designs: %s' % ex)
                    col.derived_failed = True
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
        if stopped['v']:
            # cancelled: the design is put back (above, and by reopening the saved version), no page is made
            _safe(lambda: progress_dlg.hide())
            progress_dlg = None
            return []
        data = col.result(doc_name, exact, time.time() - t0)
        progress_dlg.hide()
        progress_dlg = None
        if not getattr(col, 'recovered', 0) and not getattr(col, 'derived_failed', False):
            _cache_save(m_kind, m_id, m_ver, {'data': data, 'exact': exact, 'groups': groups_test, 'pics': bool(thumbs)})
        return _write_page(data, path, out_dir)
    except Exception:
        if progress_dlg:
            _safe(lambda: progress_dlg.hide())
        if _ui:
            _ui.messageBox('Dependencies graph failed:\n{}'.format(traceback.format_exc()))



def _write_page(data, path, out_dir):
    data = dict(data)
    data['meta'] = dict(data['meta'])
    if _sel_info.get('port'):
        data['meta']['sel'] = {'port': _sel_info['port'], 'token': _sel_info['token']}
    html = TEMPLATE.replace('/*__DATA__*/null', json.dumps(data).replace('</', '<\\/'))
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
    except Exception as ex:
        fallback = os.path.join(out_dir, os.path.basename(path))
        data['meta']['warnings'] = list(data['meta']['warnings']) + [
            'Could not save to %s (%s); saved to %s instead.' % (path, ex, fallback)]
        path = fallback
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
    webbrowser.open(pathlib.Path(path).as_uri())
    return data['meta']['warnings']


# ---------------------------------------------------- select in Fusion ---
# The graph page (a local file in the browser) asks the add-in to select items in Fusion through a small
# HTTP server on 127.0.0.1. Every generated page carries the port and a secret token; requests without the
# token are refused. The server thread only queues the request; the selection itself runs on Fusion's main
# thread through a custom event.
SEL_EVENT_ID = 'claudeDesignGraphSelect'
SEL_PORT = 47391                 # preferred port, so pages made earlier keep working after a restart
_sel_info = {'port': None, 'token': None}
_sel_server = None
_sel_event = None
_sel_jobs = []
_sel_lock = threading.Lock()


def _sel_token():
    """A secret that stays the same between Fusion sessions (so older pages still work)."""
    d = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph')
    f = os.path.join(d, '.select_token')
    try:
        with open(f, encoding='utf-8') as h:
            t = h.read().strip()
        if len(t) >= 20:
            return t
    except Exception:
        pass
    t = secrets.token_urlsafe(24)
    try:
        os.makedirs(d, exist_ok=True)
        with open(f, 'w', encoding='utf-8') as h:
            h.write(t)
    except Exception:
        pass
    return t


class _SelRequest(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _head(self, code, body=b''):
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._head(204)

    def do_GET(self):
        ok = self.path.startswith('/ping') and ('token=' + (_sel_info['token'] or '-')) in self.path
        self._head(200 if ok else 403, json.dumps({'ok': ok}).encode())

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(min(n, 2000000)).decode('utf-8') or '{}')
        except Exception:
            self._head(400, b'{"ok":false,"error":"bad request"}')
            return
        if self.path != '/select' or req.get('token') != _sel_info['token']:
            self._head(403, b'{"ok":false,"error":"not allowed"}')
            return
        job = {'req': req, 'done': threading.Event(), 'res': None}
        with _sel_lock:
            _sel_jobs.append(job)
        _safe(lambda: _app.fireCustomEvent(SEL_EVENT_ID, ''))
        if not job['done'].wait(15):
            self._head(200, b'{"ok":false,"error":"Fusion did not answer (busy?). Try again."}')
            return
        self._head(200, json.dumps(job['res']).encode())


def _sel_start():
    global _sel_server
    if _sel_server is not None:
        return
    _sel_info['token'] = _sel_token()
    for port in (SEL_PORT, 0):
        try:
            srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), _SelRequest)
            break
        except Exception:
            srv = None
    if srv is None:
        return
    srv.daemon_threads = True
    _sel_server = srv
    _sel_info['port'] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def _sel_stop():
    global _sel_server
    srv, _sel_server = _sel_server, None
    if srv is not None:
        _safe(srv.shutdown)
        _safe(srv.server_close)
    _sel_info['port'] = None


def _flat_timeline(tl):
    """Timeline objects in the order the scan numbered them (every group opened), without changing the timeline."""
    out = []
    def walk(coll, depth=0):
        for k in range(_safe(lambda: coll.count, 0) or 0):
            it = _safe(lambda: coll.item(k))
            if it is None:
                continue
            if _safe(lambda: it.isGroup) and depth < 20:
                walk(adsk.fusion.TimelineGroup.cast(it), depth + 1)
            else:
                out.append(it)
    walk(tl)
    return out


def _do_select(req):
    des = adsk.fusion.Design.cast(_safe(lambda: _app.activeProduct))
    doc = _safe(lambda: _app.activeDocument)
    want = re.sub(r'\s+v\d+$', '', req.get('doc') or '')
    have = re.sub(r'\s+v\d+$', '', _safe(lambda: doc.name, '') or '')
    if des is None or (want and want != have):
        return {'ok': False, 'error': 'Open "%s" in Fusion (Design workspace) first.' % (want or 'the design')}
    flat = _flat_timeline(des.timeline)
    root = des.rootComponent
    occs = None
    ents, missing = [], 0
    for it in req.get('items') or []:
        e = None
        if it.get('tl') is not None:
            i = it['tl']
            if 0 <= i < len(flat) and _safe(lambda: flat[i].name) == it.get('name'):
                e = _safe(lambda: flat[i].entity)
            if e is None and it.get('tok'):
                found = _safe(lambda: des.findEntityByToken(it['tok'])) or []
                e = found[0] if len(found) else None
        elif it.get('occ'):
            if occs is None:
                occs = _safe(lambda: list(root.allOccurrences)) or []
            e = next((o for o in occs if _safe(lambda: o.fullPathName) == it['occ']), None)
        if e is None:
            missing += 1
        else:
            ents.append(e)
    sel = _ui.activeSelections
    if not req.get('add'):
        _safe(sel.clear)
    n = 0
    for e in ents:
        try:
            sel.add(e)
            n += 1
        except Exception:
            missing += 1
    _safe(lambda: _app.activeViewport.refresh())
    return {'ok': True, 'selected': n, 'missing': missing}


class _SelHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        while True:
            with _sel_lock:
                job = _sel_jobs.pop(0) if _sel_jobs else None
            if job is None:
                break
            try:
                job['res'] = _do_select(job['req'])
            except Exception as ex:
                job['res'] = {'ok': False, 'error': 'Selecting failed: %s' % ex}
            job['done'].set()


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
            cmd.okButtonText = 'Full analysis'      # the blue default button; 'Quick estimate' is a button in the dialog
            _run_mode.clear()
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
            # less common options, folded away
            # a group of its own at the top level: Fusion cannot fold a group nested inside another
            ag = inputs.addGroupCommandInput('hgAdvanced', 'Advanced options')
            _safe(lambda: setattr(ag, 'isExpanded', False))
            dv = ag.children.addBoolValueInput('hgDerived', 'Include linked designs', True, '', False)
            dv.tooltip = 'Also map the designs this one derives from or inserts'
            dv.tooltipDescription = ('Each design brought in with Derive or inserted as a linked component is read as well '
                                     '(and the designs those link, at any depth). Each is shown in a frame of its own, '
                                     'connected to the Derive feature or insert that uses it. With Full analysis they are '
                                     'suppression-tested too, each in a hidden copy.')
            ru = oc.addBoolValueInput('hgReuse', 'Reuse earlier results', True, '', bool(_settings().get('reuse', True)))
            ru.tooltip = 'Take results of saved versions analysed before instead of opening and testing them again'
            ru.tooltipDescription = ('A saved version never changes, so its results stay valid. Applies to linked designs and '
                                     'to this design when it has not changed since it was last analysed. Untick to analyse '
                                     'everything again.')
            # where the page is saved: the temporary folder, or a file chosen here (remembered for next time)
            sv = oc.addTextBoxCommandInput('hgSavePath', 'Save to', _save_label(), 1, True)
            sv.tooltip = 'Where the page is saved (a single self-contained .html file: opens in any browser)'
            bt = oc.addBoolValueInput('hgSaveChoose', 'Choose file...', False, '', False)
            bt.tooltip = 'Choose where to save the page'
            bt2 = oc.addBoolValueInput('hgSaveTemp', 'Use temporary folder', False, '', False)
            bt2.tooltip = 'Save to the temporary folder again (a new file every time)'

            # --- two ways to generate: Full analysis (the dialog's OK button) or Quick estimate (a button here)
            mins = max(1, int(round((n_items * 1.7 + n_groups * 2.5) / 60.0)))
            inputs.addTextBoxCommandInput('hgModes', '', _modes_info(mins), 6, True)
            qb = inputs.addBoolValueInput('hgQuick', 'Quick estimate', False, '', False)
            qb.text = 'Quick estimate'
            _safe(lambda: setattr(qb, 'isFullWidth', True))
            qb.tooltip = 'Generate from references only (seconds)'
            qb.tooltipDescription = ('Uses only what each feature references (sketches, planes, bodies, faces, parameters). '
                                     'Fast, but some real dependencies are missing and a few links may be wrong.')
            qb.isEnabled = _unsaved_reason() is None

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


_run_mode = {}     # set to {'mode': 'off'} when Quick estimate was pressed


def _modes_info(mins):
    return ('<b>Full analysis</b> <span style="color:#6b6a64">(recommended, about %d min): suppresses every item and every '
            'timeline group in turn, so every link is a real dependency and the page can preview suppressions.</span><br><br>'
            '<b>Quick estimate</b> <span style="color:#6b6a64">(seconds): uses only what each feature references. Some real '
            'dependencies are missing and a few links may be wrong.</span>' % mins)


_SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')


def _settings():
    try:
        with open(_SETTINGS, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(d):
    try:
        with open(_SETTINGS, 'w', encoding='utf-8') as f:
            json.dump(d, f)
    except Exception:
        pass


def _save_label():
    p = _settings().get('savePath')
    return p if p else 'Temporary folder (a new file each time)'


def _choose_save_path():
    """System Save dialog; the chosen file is remembered. Returns the path or None."""
    dlg = _ui.createFileDialog()
    dlg.title = 'Save the dependencies graph as'
    dlg.filter = 'Web page (*.html)'
    cur = _settings().get('savePath')
    doc = _safe(lambda: _app.activeDocument.name, 'design') or 'design'
    dlg.initialFilename = os.path.basename(cur) if cur else re.sub(r'[^\w\- ]+', '_', doc).strip() + '_dependencies_graph.html'
    if cur:
        dlg.initialDirectory = os.path.dirname(cur)
    if dlg.showSave() != adsk.core.DialogResults.DialogOK:
        return None
    path = dlg.filename
    if not path.lower().endswith(('.html', '.htm')):
        path += '.html'
    st = _settings()
    st['savePath'] = path
    _save_settings(st)
    return path


def _options(inputs):
    mode = _run_mode.pop('mode', None) or 'both'
    ti = inputs.itemById('hgThumbs')
    dv = inputs.itemById('hgDerived')
    return mode, (bool(ti.value) if ti else True), (bool(dv.value) if dv else False)


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
        if args.input.id == 'hgReuse':
            st = _settings()
            st['reuse'] = bool(args.input.value)
            _save_settings(st)
            return
        if args.input.id in ('hgSaveChoose', 'hgSaveTemp'):
            if args.input.id == 'hgSaveChoose':
                _choose_save_path()
            else:
                st = _settings()
                st.pop('savePath', None)
                _save_settings(st)
            cmd = _safe(lambda: args.firingEvent.sender)
            box = _safe(lambda: cmd.commandInputs.itemById('hgSavePath'))
            if box is not None:
                box.formattedText = _save_label()
            return
        if args.input.id == 'hgQuick':
            if _unsaved_reason():
                return
            # Fusion does not let a command end itself from its own events, so ask for the run through the
            # custom event: its handler closes this dialog first, then runs with references only
            # the options sit inside the Options group: read them from the whole dialog, like Full analysis does
            # (args.inputs holds only the inputs next to this button)
            cmd = _safe(lambda: args.firingEvent.sender)
            _, thumbs, derived = _options(cmd.commandInputs if cmd else args.inputs)
            _app.fireCustomEvent(EVENT_ID, json.dumps({'mode': 'off', 'thumbs': thumbs, 'derived': derived,
                                                       'closeDialog': True}))


class _ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        # Generate is only allowed on a saved design (the run is undone by reopening the saved version)
        args.areInputsValid = _unsaved_reason() is None


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        mode, thumbs, derived = _options(args.command.commandInputs)
        # run after the dialog has closed, outside the command
        _app.fireCustomEvent(EVENT_ID, json.dumps({'mode': mode, 'thumbs': thumbs, 'derived': derived}))


class _RunHandler(adsk.core.CustomEventHandler):
    """Runs the scan, opens the page, then reopens the saved version so the design shows no changes."""
    def notify(self, args):
        try:
            opts = json.loads(args.additionalInfo or '{}')
        except Exception:
            opts = {}
        try:
            if opts.get('closeDialog'):
                # started from the Quick estimate button: close the still open dialog (discarding its preview)
                _safe(lambda: _ui.terminateActiveCommand(), False)
                if _safe(lambda: _ui.activeCommand, '') == CMD_ID:
                    _ui.messageBox('Could not close the dialog to start the quick estimate. Close it and try again.',
                                   'Dependencies graph')
                    return
                r = _safe(lambda: _ui.messageBox(
                    'Quick estimate uses only what each feature references (sketches, planes, bodies, faces, '
                    'parameters). The dependencies it finds might not be very precise: some real dependencies can be '
                    'missing and a few links may be wrong.\n\nUse Full analysis for exact results.\n\nContinue with the quick estimate?',
                    'Dependencies graph - Quick estimate', adsk.core.MessageBoxButtonTypes.OKCancelButtonType,
                    adsk.core.MessageBoxIconTypes.WarningIconType))
                if r == adsk.core.DialogResults.DialogCancel:
                    return
            if _unsaved_reason():
                return
            doc = _app.activeDocument
            w = list(generate(opts.get('mode', 'both'), bool(opts.get('thumbs', True)),
                              bool(opts.get('derived', False))) or [])
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
    global _sel_event
    _safe(lambda: _app.unregisterCustomEvent(SEL_EVENT_ID))
    _sel_event = _app.registerCustomEvent(SEL_EVENT_ID)
    on_sel = _SelHandler()
    _sel_event.add(on_sel)
    _handlers.append(on_sel)
    _safe(_sel_start)
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
        _safe(_sel_stop)
        _safe(lambda: _app.unregisterCustomEvent(SEL_EVENT_ID))
        _safe(lambda: _ui.palettes.itemById(PANEL_ID).deleteMe())
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
  --up:#185fa5;--down:#1f8a4c;--sel:#d19a00;--hov:#c9950c;--route:#d9480f;--err:#c0392b;--warn:#b7791f;--edge:#b4b2a9;
  --c-sketch:#0f6e56;--c-sketch-bg:#e1f5ee;--c-construct:#5f5e5a;--c-construct-bg:#f1efe8;
  --c-solid:#185fa5;--c-solid-bg:#e6f1fb;--c-finish:#854f0b;--c-finish-bg:#faeeda;
  --c-offset:#534ab7;--c-offset-bg:#eeedfe;--c-hole:#993c1d;--c-hole-bg:#faece7;
  --c-body:#993556;--c-body-bg:#fbeaf0;--c-param:#3b6d11;--c-param-bg:#eaf3de;
  --c-other:#444441;--c-other-bg:#ecebe6;--c-group:#444441;--c-group-bg:#e4e2da;
  --supp:#8a8a86;--supp-bg:#dcdcd8;
  --c-joint:#c2410c;--c-joint-bg:#fdebe0;--c-insert:#be185d;--c-insert-bg:#fce6f0;--c-newcomp:#5b6b0c;--c-newcomp-bg:#eef3d2;--c-surface:#0e7490;--c-surface-bg:#dff3f7;
  --c-sheet:#475569;--c-sheet-bg:#e6eaef;--c-component:#9a6700;--c-component-bg:#fdf3d7;--c-form:#a21caf;--c-form-bg:#f8e5fa;--c-mesh:#65730f;--c-mesh-bg:#eff3d8;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#1b1b1a;--panel:#242422;--panel2:#2d2d2a;--text:#ecebe6;--muted:#a3a19a;--border:#3d3c38;--accent:#85b7eb;
  --up:#85b7eb;--down:#6fd39a;--sel:#ffc83d;--hov:#f2c14e;--route:#ff8f5a;--err:#f09595;--warn:#ef9f27;--edge:#5f5e5a;
  --c-sketch:#9fe1cb;--c-sketch-bg:#08352c;--c-construct:#d3d1c7;--c-construct-bg:#34332f;
  --c-solid:#b5d4f4;--c-solid-bg:#0c2f52;--c-finish:#fac775;--c-finish-bg:#3d2a07;
  --c-offset:#cecbf6;--c-offset-bg:#26215c;--c-hole:#f5c4b3;--c-hole-bg:#4a1b0c;
  --c-body:#f4c0d1;--c-body-bg:#4b1528;--c-param:#c0dd97;--c-param-bg:#173404;
  --c-other:#d3d1c7;--c-other-bg:#2c2c2a;--c-group:#ecebe6;--c-group-bg:#3a3935;
  --supp:#7c7b77;--supp-bg:#3a3a38;
  --c-joint:#fdba8c;--c-joint-bg:#4a1c06;--c-insert:#f9a8d4;--c-insert-bg:#4a0a2a;--c-newcomp:#d4e46a;--c-newcomp-bg:#2b320a;--c-surface:#86d8ea;--c-surface-bg:#07313b;
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
.popbox label.chk.sub{padding-left:22px}.popbox label.chk.off{opacity:.45;pointer-events:none}
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
#details .rsec{margin-top:10px;padding:2px 10px 10px;border-left:3px solid var(--route);background:var(--panel2);border-radius:0 6px 6px 0}
#details .rsec h3{color:var(--route)}
#details .rpath{font-size:12.5px;line-height:1.6;margin-top:4px}#details .rpath .rno{color:var(--muted);margin-right:4px}
#details .rpath .name{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;overflow-wrap:normal}
#details .rpath .rar{color:var(--route);margin:0 4px}#details .rpath .rsel,#details .rpath .rend{font-weight:700;color:var(--sel)}#details .rpath .rsel{cursor:default;text-decoration:none}
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
/* hovered box: its direct parents and children stay bright, everything else fades */
svg.nhov .nd.nhc,svg.nhov .nd.nhr{opacity:1!important}
svg .nhov-ov{pointer-events:none}
/* while a box is hovered, the other links step back a little (their layers at 80%) */
svg .elayer{transition:opacity .2s ease}svg.nhov .elayer{opacity:.8}
svg .edge.nhsrc{visibility:hidden}   /* a link shown in the hover / route-preview layer is hidden underneath */
/* All links off (Display menu): plain grey links are hidden, except during playback */
svg.quiet:not(.playing) .edge.plain:not(.hov),svg.quiet:not(.playing) .ehit.plain,svg.quiet:not(.playing) .ecount.plain{display:none}
/* routes between the selection and another item */
svg .edge.route{stroke:var(--route);stroke-width:2.6;stroke-opacity:1}
svg .rbtn{cursor:pointer}svg .rbtn circle{fill:var(--panel);stroke:var(--route);stroke-width:1.2}
svg .rbtn path{fill:none;stroke:var(--route);stroke-width:1.6;stroke-linecap:round}svg .rbtn .rdot{fill:var(--route);stroke:none}
svg .rbtn:hover circle{stroke-width:2}svg .rbtn.on circle{fill:var(--route)}svg .rbtn.on path{stroke:var(--panel)}svg .rbtn.on .rdot{fill:var(--panel)}
svg .edge.rpv{stroke:var(--route);stroke-opacity:1;opacity:1}svg .edge.rpv.band{stroke-width:6;stroke-opacity:.3}
svg .edge.rpv.flow{stroke-width:2.2;stroke-dasharray:7 5;animation:nhflow .7s linear infinite}
@media (prefers-reduced-motion:reduce){svg .edge.rpv.flow{animation:none}}
/* while a route preview is shown, everything off the routes is slightly dimmed (opacity: Safari ignores CSS filters on SVG) */
svg.rpvon .nd:not(.rpin):not(.dim){opacity:.5}svg.rpvon .edge:not(.rpv):not(.dim),svg.rpvon .ecount:not(.dim){opacity:.4}
svg .rpring{fill:none;stroke:var(--route);stroke-width:2.5;stroke-dasharray:6 4;pointer-events:none}svg .rpring.end{stroke-width:4;stroke-dasharray:none}
svg .rbtn .rcount{font-size:11px;font-weight:700;fill:var(--route);stroke:none}
svg.playing .rbtn{display:none}
/* number of items a joined link leads to inside a collapsed box */
svg .ecount rect{fill:var(--panel);stroke:var(--edge);stroke-width:1.2}
svg .ecount text{font-size:10.5px;font-weight:700;fill:var(--muted);text-anchor:middle}
svg .ecount.up rect{stroke:var(--up)}svg .ecount.up text{fill:var(--up)}
svg .ecount.down rect{stroke:var(--down)}svg .ecount.down text{fill:var(--down)}
svg .ecount.dim{opacity:.18}svg .ecount{transition:opacity .2s ease}
svg .ecount.nhl rect{stroke:var(--hov);stroke-width:1.8}svg .ecount.nhl text{fill:var(--hov)}
svg.playing .ecount{display:none}
/* hovered box: gold. Its links get a soft gold band with gold dashes flowing in the link's direction;
   the boxes it uses have a dashed ring, the boxes that use it a dotted ring. Nothing else changes. */
svg .edge.nhl{stroke:var(--hov);stroke-opacity:1;opacity:1}
svg .edge.nhl.band{stroke-width:6;stroke-opacity:.28}
svg .edge.nhl.flow{stroke-width:2.2;stroke-dasharray:7 5;animation:nhflow .7s linear infinite}
@keyframes nhflow{to{stroke-dashoffset:-12}}
@media (prefers-reduced-motion:reduce){svg .edge.nhl.flow{animation:none}}
svg .hovring.up{stroke-width:3;stroke-dasharray:8 4;stroke:var(--up)}svg .hovring.down{stroke-width:3;stroke-dasharray:2 3;stroke:var(--down)}
/* links to the boxes it uses: blue; to the boxes that use it: green (same colours as the selection) */
svg .edge.nhl.up{stroke:var(--up)}svg .edge.nhl.down{stroke:var(--down)}
svg .ecount.nhl.up rect{stroke:var(--up)}svg .ecount.nhl.up text{fill:var(--up)}svg .ecount.nhl.down rect{stroke:var(--down)}svg .ecount.nhl.down text{fill:var(--down)}
svg .hovring{fill:none;stroke:var(--hov);stroke-width:5;pointer-events:none;transition:opacity .2s ease}
svg .edge{transition:stroke .2s ease,stroke-width .2s ease,stroke-opacity .2s ease,opacity .2s ease}
svg .nd{transition:opacity .2s ease}
/* selection: a gold frame with dashes running round it and a softly pulsing glow */
svg .selglow{fill:var(--sel);fill-opacity:.12;stroke:var(--sel);stroke-opacity:1;stroke-width:3;stroke-dasharray:10 6;pointer-events:none;animation:selMarch 1.1s linear infinite,selPulse 1.8s ease-in-out infinite}
@keyframes selMarch{to{stroke-dashoffset:-16}}@keyframes selPulse{50%{fill-opacity:.3}}
@media (prefers-reduced-motion:reduce){svg .selglow{animation:none}}
svg.playing .selglow{animation:none}
svg .ctog{cursor:pointer}svg .ctog.off{opacity:.3;cursor:not-allowed}
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
  <div class="seg" id="layoutSeg"><span class="cap">Layout</span><button id="layDeps" title="Boxes arranged by dependency depth">Depth</button><button id="laneBtn" class="on" title="Boxes arranged by timeline group: one block per group, in a grid">Groups</button><button id="compBtn" title="Boxes arranged by component: one block per component, in a grid">Components</button><button id="layTime" title="Every item in timeline order, left to right; open timeline groups appear as events just before their items">Timeline</button></div>
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
      <div class="popbox" id="dispBox"><div class="ph">Boxes</div><label class="chk" id="thumbCtrl" style="display:none"><input type="checkbox" id="showThumbs" checked> Thumbnails</label><label class="chk"><input type="checkbox" id="focus" checked> Only the selected branch</label><label class="chk" title="When something is selected, the boxes related to it move next to it (inside their block in the Groups and Components layouts)"><input type="checkbox" id="pullTog" checked> Move related boxes closer to the selection</label><div class="chk" style="display:flex;gap:10px;align-items:center;padding:3px 0;font-size:13px" title="Where the view goes when you select something">Zoom to<label style="display:inline-flex;gap:4px;align-items:center;cursor:pointer"><input type="radio" name="zoomTo" id="zoomObj"> selected object</label><label style="display:inline-flex;gap:4px;align-items:center;cursor:pointer"><input type="radio" name="zoomTo" id="zoomTree" checked> selected tree</label></div><label class="chk" title="Hovering a box rings it in gold, with its direct parents and children and the links between them"><input type="checkbox" id="hovRel" checked> Highlight parents and children on hover</label><label class="chk" title="Timeline layout: moving the mouse close to the left or right edge of the graph scrolls the row that way (faster the closer you get)"><input type="checkbox" id="edgeScroll" checked> Scroll at the left / right edge (Timeline)</label><div class="ph">Around the selection</div><label class="chk" title="Highlight (blue) the items the selection uses directly"><input type="checkbox" id="relUp" checked> What it depends on</label><label class="chk sub" id="relUpAllL" title="Also highlight what those items depend on, all the way back"><input type="checkbox" id="relUpAll" checked> The whole chain back</label><label class="chk" title="Highlight (green) the items that use the selection directly"><input type="checkbox" id="relDn"> What uses it</label><label class="chk sub off" id="relDnAllL" title="Also highlight what uses those items, all the way forward"><input type="checkbox" id="relDnAll" checked disabled> The whole chain forward</label><div class="ph">Lines</div><label class="chk" title="Earlier features that changed the same body before this one. Timeline order, not a dependency: suppressing them does not suppress this."><input type="checkbox" id="showOrder"> “Same body, later” links (dashed)</label><label class="chk" title="Show every link all the time. When off, only the links of the selection, of the hovered box, and the lines joining a group box to its items are drawn."><input type="checkbox" id="allLinks"> All links (grey)</label></div></div>
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
      <div id="pbCard"><img alt=""><div class="cap"></div></div><div id="pbBar"><button id="pbPrev" title="Back one step (←)">⏮</button><button id="pbPlay" title="Pause (Space)">❚❚</button><button id="pbNext" title="Next step (→)">⏭</button><button id="pbSpeed" title="Playback speed">1×</button><span id="pbInfo"></span><button id="pbStop" title="Stop (Esc)">■ Stop</button><div id="pbTrack"><div id="pbProg"></div></div></div>
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
const CAT={sketch:'Sketch',construct:'Construction',solid:'Solid feature',finish:'Chamfer / fillet',offset:'Offset / face',hole:'Hole / thread',body:'Body operation',param:'Parameter',component:'Component',surface:'Surface',sheet:'Sheet metal',form:'Form / base',mesh:'Mesh / volumetric',insert:'Insert / derive',newcomp:'New component',joint:'Joint / motion',other:'Other'};
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
  Occurrence:'insert',DeriveFeature:'derive',DerivedDesign:'derive',Joint:'joint',AsBuiltJoint:'joint',JointOrigin:'jointorigin',RigidGroup:'rigid',
  MotionLink:'motion',ContactSet:'contact',Snapshot:'snapshot',ArrangeFeature:'arrange',
  PatchFeature:'patch',StitchFeature:'stitch',UnstitchFeature:'stitch',TrimFeature:'trim',UntrimFeature:'trim',ExtendFeature:'extend',
  OffsetFeature:'surface',RuledSurfaceFeature:'surface',ReverseNormalFeature:'surface',SurfaceDeleteFaceFeature:'deleteface',
  FlangeFeature:'sheet',HemFeature:'sheet',RipFeature:'sheet',CornerClosureFeature:'sheet',FoldFeature:'sheet',UnfoldFeature:'flat',
  RefoldFeature:'sheet',JoinByBendFeature:'sheet',LoftedFlangeFeature:'sheet',SheetMetalChamferFeature:'chamfer',SheetMetalFilletFeature:'fillet',
  FlatPattern:'flat',FormFeature:'form',BaseFeature:'base',Canvas:'canvas',Decal:'canvas',CustomFeature:'custom'};
const ICON_BY_CAT={newcomp:'component',sketch:'sketch',construct:'plane',solid:'extrude',finish:'fillet',offset:'offsetface',hole:'hole',body:'combine',
  param:'param',surface:'surface',sheet:'sheet',form:'form',mesh:'mesh',component:'component',insert:'insert',joint:'joint',group:'group',other:'other'};
const ICON_CATS={construct:'plane axis point',solid:'extrude revolve sweep loft rib box cylinder sphere torus coil pipe emboss thicken fill',finish:'fillet chamfer',
      offset:'offsetface shell draft deleteface replaceface splitface',hole:'hole thread',body:'scale combine mirror rectpattern circpattern pathpattern move splitbody copy remove',
      param:'param dparam params',component:'component',insert:'insert derive',joint:'joint jointorigin rigid motion contact snapshot arrange',surface:'patch stitch trim extend surface',
      sheet:'sheet flat',form:'form base',mesh:'mesh',sketch:'sketch'};
function catOfIcon(k){return Object.keys(ICON_CATS).find(c=>ICON_CATS[c].split(' ').includes(k))||'other';}
function iconName(n){if(!n)return 'other';if(n.cat==='newcomp')return 'component';if(ICON_BY_TYPE[n.type])return ICON_BY_TYPE[n.type];if(/^Mesh|Tessellate|Volumetric/.test(n.type))return 'mesh';return ICON_BY_CAT[n.cat]||'other';}
// one hidden sprite with every icon; everything else refers to it with <use>
(function(){const NS='http://www.w3.org/2000/svg';const sp=document.createElementNS(NS,'svg');sp.setAttribute('width','0');sp.setAttribute('height','0');sp.style.position='absolute';
  sp.innerHTML='<defs>'+Object.keys(ICONS).map(k=>'<symbol id="ic-'+k+'" viewBox="0 0 16 16"><g fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round">'+ICONS[k]+'</g></symbol>').join('')+'</defs>';
  document.body.insertBefore(sp,document.body.firstChild);})();
function iconHTML(name,cat,size){size=size||14;return '<svg class="ico" width="'+size+'" height="'+size+'" style="color:var(--c-'+(cat||'other')+')" aria-hidden="true"><use href="#ic-'+name+'"/></svg>';}
function iconEl(n,size){const s=document.createElement('span');s.className='icw';s.innerHTML=iconHTML(iconName(n),n?n.cat:'other',size);return s;}
function iconUse(name,x,y,size,color){const NS='http://www.w3.org/2000/svg';const u=document.createElementNS(NS,'use');u.setAttribute('href','#ic-'+name);u.setAttribute('x',x);u.setAttribute('y',y);u.setAttribute('width',size);u.setAttribute('height',size);u.setAttribute('class','nico');u.style.color=color;return u;}
const KIND={sketch:'Sketch',profile:'Profile',plane:'Plane / axis / point',geometry:'Faces / edges',body:'Body',feature:'Feature',param:'Parameter',component:'Component',incomp:'In component',joint:'Joint / motion',suppress:'Suppression test',order:'Same body, later',derive:'Derived design'};
const nodes=D.nodes, byId={}; nodes.forEach(n=>byId[n.id]=n);
// graphs made before the split had one "assembly" category
nodes.forEach(n=>{if(n.cat==='assembly'||(n.cat==='other'&&ICON_BY_TYPE[n.type]&&catOfIcon(ICON_BY_TYPE[n.type])!=='other'))n.cat=catOfIcon(ICON_BY_TYPE[n.type]||'other')==='other'?((n.type==='Occurrence'||n.type==='DeriveFeature')?'insert':'joint'):catOfIcon(ICON_BY_TYPE[n.type]);});
// a component made in the design itself is not an insert: its own colour (inserts of other files stay pink)
nodes.forEach(n=>{if(n.type==='Occurrence'&&n.local)n.cat='newcomp';});
// one common parent for all user parameters (derived parameters stay under their Derive feature)
{const ups=nodes.filter(n=>n.type==='UserParameter'&&!n.dsg);
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
    const extG=mem.length&&mem.every(n=>n.dsg);   // a derived design (or a group in it): not in this timeline, nothing to simulate
    if(simOn&&!extG){r.appendChild(simButton(sim.groups.has(g.id),'Switch this whole group off/on in the simulation',()=>simToggleGroup(g.id)));}
    else if(simOn){const sp=document.createElement('span');sp.style.cssText='width:30px;flex:none';r.appendChild(sp);}
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
let showThumbs=true,pullTogether=true;const catOn={};nodes.forEach(n=>{catOn[n.cat]=true;});let view='tree', selected=null, selGroup=null, focus=true, search='';
const expanded=new Set(Object.keys(groups).filter(g=>!groups[g].design)); // graph starts with every group expanded, derived designs folded into one box each
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
// what the selection highlights around itself (Display menu): what it depends on / what uses it, each either
// only the direct neighbours or the whole chain. The whole chain needs the direct level switched on.
const relShow={up:true,upAll:true,dn:false,dnAll:true};   // "What uses it" is off by default
function relOf(id,dir){const on=dir==='up'?relShow.up:relShow.dn,all=dir==='up'?relShow.upAll:relShow.dnAll;
  if(!on)return new Set();if(all)return closure(id,dir);return new Set((dir==='up'?preds[id]:succs[id]).map(e=>dir==='up'?e.s:e.t));}

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
function snap(){return {sel:selected,grp:selGroup,ms:selGroup==='_sel'?multi.slice():null,rt:(route&&route.sel===selected)?[...route.items].sort():null,items:[...sim.items].sort(),groups:[...sim.groups].sort(),un:[...sim.un].sort()};}
// each history step also remembers the graph's zoom and position while it was the current one
const histKey=o=>JSON.stringify({sel:o.sel,grp:o.grp,ms:o.ms||null,rt:o.rt||null,items:o.items,groups:o.groups,un:o.un});
function saveView(){if(hMute||hIdx<0||!hist[hIdx]||view!=='graph'||!Object.keys(pos).length)return;hist[hIdx].view={x:T.x,y:T.y,k:T.k,layout:layoutMode};}
function pushHist(){if(hMute)return;const st=snap();if(hIdx>=0&&histKey(hist[hIdx])===histKey(st))return;
  hist.splice(hIdx+1);hist.push(st);if(hist.length>300)hist.shift();hIdx=hist.length-1;updHistBtns();}
function viewTo(v,dur){if(!v)return;if(anim)cancelAnimationFrame(anim);const s0={x:T.x,y:T.y,k:T.k},t0=performance.now();dur=dur||450;
  const step=now=>{const a=Math.min(1,(now-t0)/dur),e=a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;T.k=s0.k+(v.k-s0.k)*e;T.x=s0.x+(v.x-s0.x)*e;T.y=s0.y+(v.y-s0.y)*e;applyT();if(a<1)anim=requestAnimationFrame(step);else anim=null;};
  anim=requestAnimationFrame(step);}
function updHistBtns(){const b=$('hBack'),n=$('hNext');if(b)b.disabled=hIdx<=0;if(n)n.disabled=hIdx>=hist.length-1;}
function goHist(d){const j=hIdx+d;if(j<0||j>=hist.length)return;saveView();hIdx=j;const st=hist[j];hMute=true;
  try{sim.items=new Set(st.items);sim.groups=new Set(st.groups);sim.un=new Set(st.un);simCompute();renderSimBar();paintTree();renderGroupPanel();
    route=(st.sel&&st.rt)?{sel:st.sel,items:st.rt}:null;
    if(st.grp==='_sel'&&st.ms){multi=st.ms.slice();groups._sel={id:'_sel',name:multi.length+' items selected',parent:null,multi:true};}
    if(st.sel&&byId[st.sel])select(st.sel);else if(st.grp&&groups[st.grp])selectGroup(st.grp);else clearSel();}
  finally{hMute=false;}updHistBtns();
  // back where we were: the zoom and position from then (after the selection's own zoom has started)
  const v=st.view;if(v&&view==='graph'&&v.layout===layoutMode)requestAnimationFrame(()=>requestAnimationFrame(()=>viewTo(v,480)));}
// Multiple selection: Cmd+click (Mac) / Ctrl+click adds or removes items. Several selected items act as an ad-hoc
// group '_sel' (highlight, only-the-branch, playback work as for a timeline group); one item is a normal selection.
let multi=[],clickMod=false;
window.addEventListener('mousedown',e=>{clickMod=!!(e.metaKey||e.ctrlKey);},true);
window.addEventListener('click',e=>{clickMod=!!(e.metaKey||e.ctrlKey);setTimeout(()=>{clickMod=false;},0);},true);
function curSelItems(){if(selGroup==='_sel')return multi.slice();if(selected)return [selected];return [];}
function setMulti(list){clickMod=false;list=[...new Set(list)].filter(i=>byId[i]);
  if(!list.length){clearSel();return;}if(list.length===1){select(list[0]);return;}
  multi=list;groups._sel={id:'_sel',name:list.length+' items selected',parent:null,multi:true};selectGroup('_sel');}
function toggleMulti(ids){const cur=curSelItems();const all=ids.length&&ids.every(i=>cur.includes(i));setMulti(all?cur.filter(i=>!ids.includes(i)):[...cur,...ids]);}
function select(id){if(clickMod&&!hMute&&byId[id]){toggleMulti([id]);return;}if(!hMute)route=null;saveView();setTimeout(pushHist,0);peekHide();setTimeout(renderGroupPanel,0);selected=id;selGroup=null;if(byId[id])(byId[id].g||[]).forEach(g=>{if(groups[g]&&groups[g].design)expanded.add(g);});   // an item inside a folded linked design: open that design
  renderDetails();if(view==='tree')document.querySelectorAll('#tree .row').forEach(r=>r.classList.toggle('sel',!!id&&r.dataset.id===id));else{animatedRerender(()=>{},{});if(id&&byId[id])requestAnimationFrame(()=>focusOn(zoomTree&&selRelated.length?selRelated:[rep(byId[id])]));}}
function selectGroup(gid){if(clickMod&&!hMute&&gid!=='_sel'&&groups[gid]){toggleMulti(groupMembers(gid).filter(visibleNode).map(n=>n.id));return;}if(!hMute)route=null;saveView();setTimeout(pushHist,0);peekHide();selected=null;setTimeout(renderGroupPanel,0);selGroup=gid;renderDetails();if(view==='tree')document.querySelectorAll('#tree .row').forEach(r=>r.classList.toggle('sel',!!gid&&(r.dataset.gid===gid||(gid==='_sel'&&multi.includes(r.dataset.id)))));else{animatedRerender(()=>{},{});if(gid)requestAnimationFrame(()=>focusOn(zoomTree&&selRelated.length?selRelated:[...new Set(groupMembers(gid).filter(visibleNode).map(rep))]));}}
function clearSel(){if(!hMute)route=null;saveView();setTimeout(pushHist,0);setTimeout(renderGroupPanel,0);selected=null;selGroup=null;renderDetails();if(view==='graph'){if(Object.keys(pos).length)animatedRerender(()=>{},{});else renderGraph(false);}else document.querySelectorAll('#tree .row.sel').forEach(r=>r.classList.remove('sel'));}
function groupUpIds(gid){const mem=groupMembers(gid);const mset=new Set(mem.map(n=>n.id));
  const links=mem.flatMap(n=>[...closure(n.id,'up')]);
  const test=D.meta.gtest?D.groups.filter(o=>o.id!==gid&&o.dsupp&&o.dsupp.some(id=>mset.has(id))).flatMap(o=>groupMembers(o.id).map(n=>n.id)):[];
  return [...new Set([...links,...test])].filter(i=>!mset.has(i)&&byId[i]&&visibleNode(byId[i]));}
function groupMembers(gid){if(gid==='_sel')return multi.map(i=>byId[i]).filter(Boolean);if(groups[gid]&&groups[gid].pseudo)return nodes.filter(n=>pseudoOf(n)===gid);return nodes.filter(n=>(n.g||[]).includes(gid));}
function groupPath(gid){const out=[];let g=groups[gid];let guard=0;while(g&&guard++<20){out.unshift(g.name);g=groups[g.parent];}return out;}
function groupsSuppressing(id){return D.groups.filter(g=>g.dsupp&&g.dsupp.includes(id));}
function itemList(ids){const ul=document.createElement('ul');if(!ids.length){ul.innerHTML='<li class="kv">none</li>';return ul;}
  ids.forEach(id=>{const x=byId[id];if(!x)return;const li=document.createElement('li');const ti=thumbImg(x.id);if(ti)li.appendChild(ti);li.appendChild(pill(x));const a=document.createElement('span');a.className='name '+stateCls(x);a.textContent=x.name;a.onclick=()=>select(id);li.appendChild(a);
    ul.appendChild(li);});return ul;}
function groupLinks(list){const ul=document.createElement('ul');if(!list.length){ul.innerHTML='<li class="kv">none</li>';return ul;}
  list.forEach(([gid,cnt])=>{const g=groups[gid];const li=document.createElement('li');const a=document.createElement('span');{const gm=g?groupMembers(gid):[];a.className='name grp'+(gm.length&&gm.every(isSupp)?' st-sup':'');}a.textContent=g?g.name:gid;a.onclick=()=>selectGroup(gid);li.appendChild(a);if(cnt!=null){const k=document.createElement('span');k.className='k';k.textContent=cnt+' item'+(cnt===1?'':'s');li.appendChild(k);}ul.appendChild(li);});return ul;}
function renderGroupDetails(d){if(selGroup==='_sel'){renderMultiDetails(d);return;}
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
  }else{const x=document.createElement('div');x.className='hint';x.textContent=g.empty?'This group is empty.':(D.meta.gtest?'This group was not tested (it could not be suppressed, or everything in it was already suppressed).':'Run Dependencies Graph with Full analysis to see what suppressing this group does.');d.appendChild(x);}
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
// side panel for several selected items
function renderMultiDetails(d){const mem=groupMembers('_sel');const mset=new Set(mem.map(n=>n.id));
  // hovering an item in these lists highlights its box in the graph (with its parents and children)
  // the usual hover highlight, limited to boxes of the selected tree (what is highlighted around the selection)
  const hov=(el,id)=>{el.addEventListener('mouseenter',()=>{if(view==='graph'&&byId[id]){const r=lastVisRep(rep(byId[id]));const tree=new Set(selRelated);tree.delete(r);nodeHover(r,true,tree);}});el.addEventListener('mouseleave',()=>nodeHoverClear());};
  const h=document.createElement('h2');h.textContent=mem.length+' items selected';d.appendChild(h);
  const kv=document.createElement('div');kv.className='kv';kv.textContent=(/Mac/.test(navigator.platform)?'Cmd':'Ctrl')+'+click items to add or remove them.';d.appendChild(kv);
  const ul=document.createElement('ul');ul.style.marginTop='8px';
  [...mem].sort((a,b)=>a.o-b.o).forEach(n=>{const li=document.createElement('li');li.appendChild(pill(n));const a=document.createElement('span');a.className='name '+stateCls(n);a.textContent=n.name;a.title='Select only this';a.onclick=()=>{clickMod=false;select(n.id);};li.appendChild(a);
    const x=document.createElement('span');x.className='k';x.textContent='✕';x.title='Remove from the selection';x.style.cursor='pointer';x.style.marginLeft='auto';x.onclick=()=>setMulti(multi.filter(i=>i!==n.id));li.appendChild(x);hov(li,n.id);ul.appendChild(li);});
  d.appendChild(ul);
  // two items where one depends on the other: their routes
  if(mem.length===2){const [p,q]=mem;const up=closure(q.id,'up');const a=up.has(p.id)?p:(closure(p.id,'up').has(q.id)?q:null);
    const w=document.createElement('div');w.className='kv';w.style.marginTop='10px';
    if(a){const b=a===p?q:p;const bt=document.createElement('button');bt.textContent='Show the routes from '+a.name+' to '+b.name;bt.onclick=()=>{clickMod=false;select(a.id);setRoute([b.id]);};d.appendChild(bt);bt.style.marginTop='10px';}
    else{w.textContent='Neither depends on the other, so there is no route between them.';d.appendChild(w);}}
  const upIds=groupUpIds('_sel'),dnIds=[...new Set(mem.flatMap(n=>[...closure(n.id,'down')]))].filter(i=>!mset.has(i)&&byId[i]&&visibleNode(byId[i]));
  const sec=(t,ids)=>{const det=document.createElement('details');det.style.marginTop='10px';const sm=document.createElement('summary');sm.textContent=t+' ('+ids.length+')';const sorted=[...ids].filter(i=>byId[i]).sort((a,b)=>byId[a].o-byId[b].o);const il=itemList(sorted);[...il.children].forEach((li,k)=>{if(sorted[k])hov(li,sorted[k]);});det.appendChild(sm);det.appendChild(il);d.appendChild(det);};
  sec('Together they depend on',upIds);sec('Used by any of them',dnIds);
  const b=document.createElement('div');b.style.cssText='margin-top:12px;display:flex;gap:6px;flex-wrap:wrap';
  const bg=document.createElement('button');bg.textContent='Show in graph';bg.onclick=()=>showInGraph(()=>groupMembers('_sel').filter(visibleNode).map(rep));b.appendChild(bg);
  const bp=document.createElement('button');bp.textContent='▶ Play history';bp.title='Animate how these items were built from their dependencies (P)';bp.onclick=()=>playHistory();b.appendChild(bp);
  const bx=document.createElement('button');bx.textContent='Clear selection';bx.onclick=clearSel;b.appendChild(bx);d.appendChild(b);}
function linkList(list,dir){const ul=document.createElement('ul');if(!list.length){ul.innerHTML='<li class="kv">none</li>';return ul;}
  list.forEach(e=>{const n=byId[dir==='up'?e.s:e.t];const li=document.createElement('li');li.appendChild(pill(n));const a=document.createElement('span');a.className='name '+stateCls(n);a.textContent=n.name;a.onclick=()=>select(n.id);li.appendChild(a);const k=document.createElement('span');k.className='k';k.textContent=e.k.map(x=>KIND[x]||x).join(', ');li.appendChild(k);li.addEventListener('mouseenter',()=>panelLinkHover(e.s,e.t,true));li.addEventListener('mouseleave',()=>panelLinkHover(e.s,e.t,false));ul.appendChild(li);});return ul;}
function setInfo(open){if(open&&document.body.classList.contains('legendopen'))setLegend(false);document.body.classList.toggle('infoopen',!!open);$('infoBtn').classList.toggle('on',!!open);}
function renderInfo(){const b=$('infoBody');b.innerHTML='';const cols=document.createElement('div');cols.className='cols';
  const c1=document.createElement('div');c1.innerHTML='<h2>Dependencies graph</h2><div class="kv">Click an item or group in the tree, or a box in the graph, to see its details; Cmd+click (Mac) or Ctrl+click adds or removes items to select several. Click empty space in the graph to deselect.</div>';
  const gen=document.createElement('div');gen.className='hint';
  {const sec=D.meta.generationSeconds;const dur=sec==null?'unknown':(sec<60?(sec.toFixed(1)+' s'):(Math.floor(sec/60)+' min '+Math.round(sec%60)+' s'));
   gen.innerHTML='<b>Generated:</b> '+(D.meta.date||'unknown')+' · <b>Generation time:</b> '+dur;}
  c1.appendChild(gen);
  const lg=document.createElement('div');lg.className='legend';Object.keys(CAT).forEach(c=>{if(!nodes.some(n=>n.cat===c))return;const s=document.createElement('span');s.className='pill';s.textContent=CAT[c];s.style.color='var(--c-'+c+')';s.style.background='var(--c-'+c+'-bg)';lg.appendChild(s);});c1.appendChild(lg);
  const h=document.createElement('div');h.className='hint';h.innerHTML='Graph: drag to pan, scroll to zoom, click a grey group box to expand it. Blue links lead to what the selection depends on, green links to what depends on it. <b>▶ Play</b> (or P) animates how the selected item was built from its dependencies, or the whole history when nothing is selected: Space pauses, → goes one step forward and ← one step back (while paused, one step at a time), Esc stops.'+(D.meta.exact||D.meta.gtest?'':'<br>Links come from references the add-in could read (sketches, profiles, planes, faces/edges, bodies, parameters). Use <b>Full analysis</b> in the add-in to get Fusion\'s real dependencies and the suppression preview.');c1.appendChild(h);
  if(canGroups){const x=document.createElement('div');x.className='hint';x.innerHTML='<b>Suppression preview:</b> use the on/off buttons (groups panel, tree, details) or Shift+click a box in the graph'+(canItems?'':' (switches its whole group - this page has the group test only)')+'. The preview bar appears as soon as something is switched off.'+
      '<br><b>How exact it is:</b> one item switched off'+(D.meta.gtest?', or one whole timeline group,':'')+' shows exactly what Fusion did in the suppression test. '+
      'With several things switched off at once, the preview adds up their single results. Features that fail or switch off only when those things are off <i>together</i> are not shown, so treat that result as an estimate.';c1.appendChild(x);}
  cols.appendChild(c1);
  if(D.meta.warnings&&D.meta.warnings.length){const c2=document.createElement('div');c2.innerHTML='<h2>Warnings</h2>';const w=document.createElement('div');w.className='msg';w.textContent=D.meta.warnings.join('\n');c2.appendChild(w);cols.appendChild(c2);}
  b.appendChild(cols);}
// ---------- select in Fusion ----------
// The add-in listens on 127.0.0.1 (port and secret token are written into the page when it is generated).
// Timeline items are selected through their timeline entry, components through their occurrence; parameters
// cannot be selected in Fusion.
const FSEL=(D.meta&&D.meta.sel)||null;
function fusionSpec(id){const n=byId[id];if(!n)return null;if(n.tl!=null)return {tl:n.tl,name:n.name,tok:n.tok||''};if(n.occ)return {occ:n.occ};return null;}
// the selection with what it highlights around itself (Display > Around the selection)
function branchIds(base){const out=new Set(base);base.forEach(id=>{relOf(id,'up').forEach(i=>out.add(i));relOf(id,'down').forEach(i=>out.add(i));});return [...out].filter(i=>byId[i]&&byId[i].type!=='UserParameters');}
function selectInFusion(ids,st){const say=(t,bad)=>{st.textContent=t;st.style.color=bad?'var(--err)':'';};
  if(!FSEL){say('This page was made by an older add-in. Generate it again to select in Fusion.',true);return;}
  const items=[];let skipped=0;[...new Set(ids)].forEach(i=>{const x=fusionSpec(i);if(x)items.push(x);else if(byId[i])skipped++;});
  const sk=skipped?' · '+skipped+' parameter'+(skipped===1?'':'s')+' cannot be selected':'';
  if(!items.length){say('Nothing here can be selected in Fusion'+(skipped?' (parameters cannot be selected)':'')+'.',true);return;}
  say('Selecting '+items.length+' in Fusion…');
  const ctl=typeof AbortController!=='undefined'?new AbortController():null;if(ctl)setTimeout(()=>ctl.abort(),20000);
  fetch('http://127.0.0.1:'+FSEL.port+'/select',{method:'POST',headers:{'Content-Type':'text/plain'},body:JSON.stringify({token:FSEL.token,doc:D.meta.doc,items}),signal:ctl?ctl.signal:undefined})
    .then(r=>r.json()).then(r=>{if(!r.ok)say(r.error||'Fusion refused the selection.',true);else say('Selected '+r.selected+' in Fusion'+(r.missing?' · '+r.missing+' not found in the design':'')+sk+'.');})
    .catch(()=>say('Could not reach Fusion. Is Fusion running with the Dependencies Graph add-in?',true));}
function fusionRow(d,ids){const w=document.createElement('div');w.style.cssText='margin-top:8px;display:flex;gap:6px;flex-wrap:wrap';
  const st=document.createElement('div');st.className='kv';st.style.marginTop='4px';
  const b1=document.createElement('button');b1.textContent='Select in Fusion';b1.title='Select '+(ids.length===1?'this item':'these '+ids.length+' items')+' in Fusion (timeline or browser)';b1.onclick=()=>selectInFusion(ids,st);
  const b2=document.createElement('button');b2.textContent='Select branch in Fusion';b2.title='Also select what is highlighted around the selection (Display > Around the selection)';b2.onclick=()=>selectInFusion(branchIds(ids),st);
  w.append(b1,b2);d.append(w,st);}
function renderDetails(){
  pbSync();const d=$('details');d.innerHTML='';
  if(selGroup&&groups[selGroup]){document.body.classList.remove('nosel');setInfo(false);renderGroupDetails(d);fusionRow(d,groupMembers(selGroup).filter(visibleNode).map(x=>x.id));return;}
  document.body.classList.toggle('nosel',!selected);
  if(!selected){renderInfo();return;}
  setInfo(false);
  const n=byId[selected];
  const h=document.createElement('h2');h.textContent=n.name;h.className=stateCls(n);h.prepend(iconEl(n,18));d.appendChild(h);
  if(TH[n.id]&&showThumbs){const im=document.createElement('img');im.className='big';im.src=TH[n.id];im.alt='Model after '+n.name;d.appendChild(im);}
  const kv=document.createElement('div');kv.className='kv';
  [n.type+(n.info?' · '+n.info:''),n.tl!=null?'Timeline position '+(n.tl+1):n.stl!=null?'Timeline position '+(n.stl+1)+' in '+n.dsg:'',isSupp(n)?'Suppressed':(isBroken(n)?brokenText(n):warnKind(n)?warnText(n):'OK')].filter(Boolean).forEach(t=>{const x=document.createElement('div');x.textContent=t;kv.appendChild(x);});
  if(!(n.g&&n.g.length)){const gl=document.createElement('div');gl.textContent='Not in a timeline group';kv.insertBefore(gl,kv.children[1]||null);}
  d.appendChild(kv);
  // routes to the other end (route button in the graph)
  {const RT=routeCompute();if(RT){const sec=document.createElement('div');sec.className='rsec';
    const ends=route.items.map(i=>byId[i]).filter(Boolean);const endName=ends.length===1?ends[0].name:(ends.length+' items: '+ends.slice(0,3).map(x=>x.name).join(', ')+(ends.length>3?'…':''));
    // routes run the way the links do: from the other end when the selection depends on it, else to it
    const fromEnd=route.items.some(i=>RT.up.has(i));
    const h3=document.createElement('h3');h3.textContent=(fromEnd?'Routes from ':'Routes to ')+endName;sec.appendChild(h3);
    const k=document.createElement('div');k.className='kv';const np=RT.paths>=1e6?'over a million':String(RT.paths);
    const mid=RT.items.size-1-ends.length,usesIt=fromEnd;
    k.textContent=np+' route'+(RT.paths===1?'':'s')+' · '+mid+' item'+(mid===1?'':'s')+' in between · '+(usesIt?'the selection depends on it':'it depends on the selection');sec.appendChild(k);
    const P=routePaths(RT,12);
    if(!P.more){P.paths.forEach((pth,i)=>{const row=document.createElement('div');row.className='rpath';
      const no=document.createElement('span');no.className='rno';no.textContent=(i+1)+'.';row.appendChild(no);
      pth.forEach((id,j)=>{const x=byId[id];if(!x)return;if(j){const ar=document.createElement('span');ar.className='rar';ar.textContent='→';row.appendChild(ar);}
        const a=document.createElement('span');a.className='name '+stateCls(x)+(id===selected?' rsel':route.items.includes(id)?' rend':'');a.textContent=x.name;
        if(id!==selected)a.onclick=()=>select(id);row.appendChild(a);});sec.appendChild(row);});}
    else{const w=document.createElement('div');w.className='kv';w.style.marginTop='6px';w.textContent='Too many to list one by one. Items on the routes:';sec.appendChild(w);
      sec.appendChild(itemList([...RT.items].filter(i=>i!==selected&&byId[i]).sort((a,b)=>byId[a].o-byId[b].o)));}
    const bb=document.createElement('div');bb.style.cssText='margin-top:8px;display:flex;gap:6px;flex-wrap:wrap';
    const b1=document.createElement('button');b1.textContent='Show routes in graph';b1.onclick=()=>showInGraph(()=>[...new Set([...RT.items].filter(i=>byId[i]).map(i=>rep(byId[i])))]);
    const b2=document.createElement('button');b2.textContent='Hide routes';b2.title='Hide the routes (Esc). Back returns to them.';b2.onclick=()=>setRoute(null);
    bb.append(b1,b2);sec.appendChild(bb);d.appendChild(sec);}}
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
  d.appendChild(b);fusionRow(d,[n.id]);
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
// hovering a box highlights it (gold ring), its direct parents (blue) and children (green) and the links
// between them. The links are copied into a layer above all other links, just under the boxes.
let nhState=null,nhAnchor=null,hoverRel=true;
function nodeHoverClear(){const h=nhState;nhState=null;svg.classList.remove('nhov');if(!h)return;
  h.ov.remove();h.rings.forEach(r=>r.remove());h.nodes.forEach(g=>g.classList.remove('nhc','nhr'));h.srcs.forEach(el=>el.classList.remove('nhsrc'));(h.badgeCls||[]).forEach(([b,c])=>b.setAttribute('class',c));}
// only: when given, a set of boxes; just the links between the hovered box and those boxes are shown
function nodeHover(id,on,only){
  nodeHoverClear();if(!on||(!hoverRel&&!only)||PB||drag||hovPin||!nodeEls[id])return;
  const NS='http://www.w3.org/2000/svg';const ov=document.createElementNS(NS,'g');ov.setAttribute('class','nhov-ov');
  const st={ov,rings:[],nodes:[],badgeCls:[],srcs:[]};const kin={};
  edgeEls.forEach(x=>{if(x.s===x.t)return;let dir=null,o=null;if(x.t===id){dir='up';o=x.s;}else if(x.s===id){dir='down';o=x.t;}if(!dir||!nodeEls[o]||(only&&!only.has(o)))return;
    const c=x.el.cloneNode(true);c.querySelectorAll('title').forEach(t=>t.remove());c.removeAttribute('style');
    c.setAttribute('class','edge nhl band '+dir);const f=c.cloneNode(true);f.setAttribute('class','edge nhl flow '+dir);ov.append(c,f);
    x.el.classList.add('nhsrc');st.srcs.push(x.el);   // the link itself is hidden meanwhile, so the two do not show through each other
    if(x.badge){st.badgeCls.push([x.badge,x.badge.getAttribute('class')]);x.badge.setAttribute('class','ecount nhl '+dir);}
    if(!kin[o])kin[o]=dir;else if(kin[o]!==dir)kin[o]='both';});
  const ring=(g,cls)=>{const r=document.createElementNS(NS,'rect');r.setAttribute('class','hovring'+(cls?' '+cls:''));r.setAttribute('x',-6);r.setAttribute('y',-6);r.setAttribute('width',NW+12);r.setAttribute('height',NH+12);r.setAttribute('rx',9);g.appendChild(r);st.rings.push(r);};
  const g0=nodeEls[id];g0.classList.add('nhc');st.nodes.push(g0);ring(g0,'');
  Object.keys(kin).forEach(o=>{const g=nodeEls[o];g.classList.add('nhr');st.nodes.push(g);ring(g,kin[o]==='up'?'up':kin[o]==='down'?'down':'');});
  const vpEl=$('vp');if(nhAnchor&&nhAnchor.parentNode===vpEl)vpEl.insertBefore(ov,nhAnchor);else vpEl.appendChild(ov);
  svg.classList.add('nhov');nhState=st;}
// hovering a link in the side panel highlights the same link in the graph
function panelLinkHover(s,t,on){if(view!=='graph'||!byId[s]||!byId[t])return;
  if(!on){edgeHover(null,null,null,false);return;}
  const a=rep(byId[s]),b=rep(byId[t]);const x=edgeEls.find(x=>x.s===a&&x.t===b);edgeHover(x?x.el:null,a,b,true);}
// Links: the curve itself (edgeCurve) plus an arrowhead drawn as part of the same path (edgeD), so the arrow
// takes the line's colour, width, hover and animation in every browser (Safari ignores context-stroke markers).
const AH=6;
function arrowAt(x,y,dx,dy){const px=-dy,py=dx,w=AH*0.7;return ' M'+(x-dx*AH+px*w)+','+(y-dy*AH+py*w)+' L'+x+','+y+' L'+(x-dx*AH-px*w)+','+(y-dy*AH-py*w);}
// Timeline layout: a link between boxes more than one place apart arcs above the row (top of one box to the
// top of the other); longer links arc higher, so nested links do not lie on top of each other
let tlArcTop=0;   // how far the Timeline arcs reach above the row (for fitting the view)
function tArc(a,b){if(layoutMode!=='time'||Math.abs(a.y-b.y)>1||Math.abs(b.x-a.x)<=NW+XG+2)return null;
  const dir=b.x>a.x?1:-1,x1=a.x+NW/2+dir*NW*0.18,x2=b.x+NW/2-dir*NW*0.18,h=26+Math.sqrt(Math.abs(x2-x1))*8;
  return {x1,y1:a.y,x2,y2:b.y,h};}
function edgeEnd(a,b,o2){{const t=tArc(a,b);if(t)return [t.x2,t.y2,0,1];}const x2=b.x+NW/2+(o2||0),y2=b.y;if(y2>a.y+NH)return [x2,y2,0,1];const by=b.y+NH/2;
  if(b.x>=a.x+NW)return [b.x,by,1,0];if(b.x+NW<=a.x)return [b.x+NW,by,-1,0];return [b.x+NW,by,-1,0];}
function edgeD(a,b,o1,o2,bow){const e=edgeEnd(a,b,o2);return edgeCurve(a,b,o1,o2,bow)+arrowAt(e[0],e[1],e[2],e[3]);}
// the middle of a link (where its item-count badge sits)
function edgeMid(a,b,o1,o2,bow){const sg=edgeSegs(a,b,o1,o2,bow);if(sg.length>1)return sg[0][3];const q=sg[0];
  return [0,1].map(i=>0.125*q[0][i]+0.375*q[1][i]+0.375*q[2][i]+0.125*q[3][i]);}
function placeBadge(x,a,b){if(!x.badge)return;const m=edgeMid(a,b,x.o1,x.o2,x.bow);x.badge.setAttribute('transform','translate('+m[0]+','+m[1]+')');}
// A link as a list of cubic Bezier segments [p0,c1,c2,p3]; used both to draw it and to sample it
// for the crossing checks (sampling in plain maths is much faster than asking the browser).
function edgeSegs(a,b,o1,o2,bow){{const t=tArc(a,b);if(t)return [[[t.x1,t.y1],[t.x1,t.y1-t.h],[t.x2,t.y2-t.h],[t.x2,t.y2]]];}
  const x1=a.x+NW/2+(o1||0),y1=a.y+NH,x2=b.x+NW/2+(o2||0),y2=b.y;
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
    edgeEls.forEach(x=>{if(cur[x.s]&&cur[x.t]){{const dd=edgeD(cur[x.s],cur[x.t],x.o1,x.o2,x.bow);x.el.setAttribute('d',dd);if(x.hit)x.hit.setAttribute('d',dd);placeBadge(x,cur[x.s],cur[x.t]);}if(fresh.has(x.s)||fresh.has(x.t)||freshE.has(x)){x.el.style.opacity=a<1?String(e):'';if(x.badge)x.badge.style.opacity=a<1?String(e):'';}}});
    gEdges.forEach(x=>{x.el.style.opacity=String(1-e);});
    if(newLanes)newLanes.style.opacity=a<1?String(e):'';if(oldLanes&&oldLanes!==newLanes){oldLanes.style.opacity=String(1-e);if(a>=1)oldLanes.remove();}
    ghosts.forEach(g=>{const c={x:g.from.x+(g.to.x-g.from.x)*e,y:g.from.y+(g.to.y-g.from.y)*e};g.el.setAttribute('transform','translate('+c.x+','+c.y+')');g.el.style.opacity=String(1-e);});
    if(a<1)graphAnim=requestAnimationFrame(step);else{graphAnim=null;gl.remove();}};
  graphAnim=requestAnimationFrame(step);}
// Routes: with an item selected, every other item of its tree (what it depends on, or what uses it, at any
// distance) gets a small button; clicking it highlights every route between the two. route = {sel, items} (items: the other end).
// Showing or hiding routes is a step in the Back / Forward history; the other end looks selected too.
let route=null;
function routeCompute(){if(!route||!selected||route.sel!==selected){route=null;return null;}
  const RT=routeFor(selected,route.items);if(!RT)route=null;return RT;}
function routeFor(S,items){
  const aS=closure(S,'up'),dS=closure(S,'down'),ri=new Set([S]);let nPaths=0;
  const count=(from,to,memo)=>{if(from===to)return 1;if(memo.has(from))return memo.get(from);let n=0;
    for(const e of succs[from]){if(ri.has(e.t))n+=count(e.t,to,memo);if(n>1e6)break;}memo.set(from,n);return n;};
  const pairs=[];
  items.forEach(t=>{if(aS.has(t)){const dT=closure(t,'down');aS.forEach(x=>{if(dT.has(x))ri.add(x);});ri.add(t);pairs.push([t,S]);}
    else if(dS.has(t)){const aT=closure(t,'up');dS.forEach(x=>{if(aT.has(x))ri.add(x);});ri.add(t);pairs.push([S,t]);}});
  if(ri.size<2)return null;
  pairs.forEach(([a,b])=>{nPaths+=count(a,b,new Map());});
  return {pairs,items:ri,up:new Set([...ri].filter(i=>aS.has(i))),down:new Set([...ri].filter(i=>dS.has(i))),paths:nPaths};}
// the routes themselves, as lists of item ids (at most `limit`; more=true when there are more)
function routePaths(RT,limit){const out=[];let more=false;
  const walk=(x,to,path)=>{if(out.length>=limit){more=true;return;}if(x===to){out.push(path.slice());return;}
    for(const e of succs[x]){if(!RT.items.has(e.t))continue;path.push(e.t);walk(e.t,to,path);path.pop();if(more)return;}};
  RT.pairs.forEach(([a,b])=>{if(!more)walk(a,b,[a]);});return {paths:out,more};}
// hovering a route button previews its routes: orange flowing links and rings, nothing else changes
let rpState=null,lastVisRep=x=>x;
function routePreviewClear(){const h=rpState;rpState=null;svg.classList.remove('rpvon');if(!h)return;h.ov.remove();h.srcs.forEach(el=>el.classList.remove('nhsrc'));h.rings.forEach(r=>r.remove());h.nodes.forEach(g=>g.classList.remove('rpin'));}
function routePreview(items,on,tt){routePreviewClear();if(!on||PB||drag||!selected)return null;nodeHoverClear();
  const RT=routeFor(selected,items);if(!RT)return null;const NS='http://www.w3.org/2000/svg';
  const repsOf=ids=>new Set([...ids].filter(i=>byId[i]).map(i=>lastVisRep(rep(byId[i]))));const rr=repsOf(RT.items),tr=repsOf(items);
  const ov=document.createElementNS(NS,'g');ov.setAttribute('class','nhov-ov');const st={ov,rings:[],nodes:[],srcs:[]};
  edgeEls.forEach(x=>{if(x.s===x.t||!rr.has(x.s)||!rr.has(x.t)||!(x.src||[]).some(q=>RT.items.has(q.s)&&RT.items.has(q.t)))return;
    const c=x.el.cloneNode(true);c.querySelectorAll('title').forEach(t=>t.remove());c.removeAttribute('style');c.setAttribute('class','edge rpv band');
    const f=c.cloneNode(true);f.setAttribute('class','edge rpv flow');ov.append(c,f);x.el.classList.add('nhsrc');st.srcs.push(x.el);});
  const selR=lastVisRep(rep(byId[selected]));[selR,...rr].forEach(id=>{const g=nodeEls[id];if(g&&!g.classList.contains('rpin')){g.classList.add('rpin');st.nodes.push(g);}});
  rr.forEach(id=>{const g=nodeEls[id];if(!g||id===selR)return;const r=document.createElementNS(NS,'rect');r.setAttribute('class','rpring'+(tr.has(id)?' end':''));r.setAttribute('x',-6);r.setAttribute('y',-6);r.setAttribute('width',NW+12);r.setAttribute('height',NH+12);r.setAttribute('rx',9);g.appendChild(r);st.rings.push(r);});
  const vpEl=$('vp');if(nhAnchor&&nhAnchor.parentNode===vpEl)vpEl.insertBefore(ov,nhAnchor);else vpEl.appendChild(ov);rpState=st;svg.classList.add('rpvon');
  if(tt)tt.textContent='Show the '+(RT.paths>=1e6?'over a million':RT.paths)+' route'+(RT.paths===1?'':'s')+' ('+(RT.items.size-1-items.length)+' items in between) between the selection and this';
  return RT;}
function setRoute(items){routePreviewClear();saveView();route=items?{sel:selected,items}:null;setTimeout(pushHist,0);renderDetails();
  if(view==='graph'&&Object.keys(pos).length)animatedRerender(()=>{},focus?{fit:'fit'}:{dur:450});}
let searchOpen=new Set();let layoutMode='lanes',lanes=[],laneEls={},designFrames=[];const collapsedNodes=new Set();let collapseEverything=false;
// block layouts: 'lanes' = one block per top-level timeline group, 'comps' = one block per component
const isLanes=()=>layoutMode==='lanes'||layoutMode==='comps';
const compOf={};D.edges.forEach(e=>{if(e.k.includes('incomp')&&byId[e.s]&&byId[e.s].cat==='component')compOf[e.t]=e.s;});
nodes.forEach(n=>{if(n.cat==='component')compOf[n.id]=n.id;});
const cColor={};nodes.filter(n=>n.cat==='component').sort((a,b)=>a.o-b.o).forEach((n,i)=>cColor[n.id]=GCOL[(i+3)%GCOL.length]);
// user parameters get a block of their own; a derived parameter stays with its Derive feature
const dparamOf={};D.edges.forEach(e=>{if(byId[e.t]&&byId[e.t].type==='DerivedParameter'&&byId[e.s]&&byId[e.s].tl!=null)dparamOf[e.t]=e.s;});
// With derived designs in the graph, each design is a frame of its own, laid out inside like the main design:
// its timeline groups, its user parameters and its items outside groups get blocks keyed 'X2|_params' etc.
const hasDesigns=nodes.some(n=>n.dsg);
const MAIN_DSG='_main';
function dsgOfNode(n){return n&&n.dsg&&n.g&&n.g.length?n.g[0]:MAIN_DSG;}
function laneBase(id){return id&&id.includes('|')?id.split('|')[1]:id;}
function laneDesign(id){if(!id)return MAIN_DSG;if(id.includes('|'))return id.split('|')[0];let g=groups[id],guard=0;while(g&&guard++<30){if(g.design)return g.id;g=g.parent?groups[g.parent]:null;}return MAIN_DSG;}
function laneKey(n){if(!n)return null;
  if(n.dsg&&n.g&&n.g.length){const X=n.g[0];if(layoutMode==='comps')return compOf[n.id]||X+'|_root';
    if(n.port)return X+'|_port';if(n.type==='UserParameter')return X+'|_params';return n.g.length>1?n.g[1]:X+'|_none';}
  if(n.type==='UserParameter'||n.type==='UserParameters')return '_params';
  if(n.type==='DerivedParameter'&&dparamOf[n.id])return laneKey(byId[dparamOf[n.id]]);
  if(layoutMode==='comps')return compOf[n.id]||'_root';return topGroup(n)||'_none';}
// while searching, the fold (−) buttons are switched off; unfolding (+) still works
function offFold(bt,tt){bt.classList.add('off');tt.textContent='Clear the search to fold';bt.addEventListener('click',ev=>{ev.stopImmediatePropagation();ev.stopPropagation();});}
function rep(n){const gp=n.g||[];for(const g of gp){if(!expanded.has(g)&&!searchOpen.has(g))return 'g'+g;}
  const ps=pseudoOf(n);if(ps&&groups[ps]&&!expanded.has(ps)&&!searchOpen.has(ps))return 'g'+ps;return n.id;}
let NW=210,NH=26;const XG=18,YG=64;// top-down: XG = gap between boxes in a row, YG = gap between rows
function renderGraph(fitAfter,centerId){
  if(PB)stopPlay();   // a re-render replaces the boxes the playback is animating
  nodeHoverClear();routePreviewClear();const RT=routeCompute();tlArcTop=0;
  const gth=hasThumbs&&showThumbs;NW=gth?240:210;NH=gth?50:26;
  searchOpen=new Set();let hitReps=null;
  // searching does not open folded groups: a folded group (or collapsed box) holding matches is itself marked as a hit
  // the Groups and Components layouts give user parameters a block of their own, so the common
  // "User Parameters" parent box is left out there
  const vis=nodes.filter(n=>visibleNode(n)&&!((isLanes()||layoutMode==='time')&&n.id==='up:all'));
  let keep=null;
  if(focus&&selected&&RT){keep=new Set(RT.items);}
  else if(focus&&selected){keep=new Set([selected,...relOf(selected,'up'),...relOf(selected,'down')]);}
  else if(focus&&selGroup&&groups[selGroup]){const mem=groupMembers(selGroup).map(n=>n.id);const g=groups[selGroup];
    keep=new Set([...mem,...(relShow.dn&&relShow.dnAll&&g.dsupp?g.dsupp:mem.flatMap(i=>[...relOf(i,'down')])),...mem.flatMap(i=>[...relOf(i,'up')])]);}
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
  // a box folded away by a collapse is represented by the collapsed box that hides it (the joined line ends there)
  const visRep=x=>{if(!hiddenBy[x])return x;const c=hiddenBy[x].find(c=>!hiddenBy[c]);return c||x;};lastVisRep=visRep;
  const hitCount={};
  if(hitReps){hitReps=new Set([...hitReps].map(visRep));nodes.forEach(n=>{if(!visibleNode(n)||!matches(n))return;const x=visRep(rep(n));hitCount[x]=(hitCount[x]||0)+1;});
    const ordR={};reps.forEach(r=>ordR[r.id]=r.o);graphHits=[...hitReps].filter(id=>ordR[id]!=null).sort((a,b)=>ordR[a]-ordR[b]);if(hitIdx>=graphHits.length)hitIdx=0;}
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
      R[h.id]={id:h.id,o:Math.min(...h.mem.map(n=>n.o))-0.5,members:h.mem,isGroup:true,header:true,depth:h.depth};reps.push(R[h.id]);pr[h.id]=[];
      layer[h.id]=Math.min(...h.targets.map(t=>layer[t]!=null?layer[t]:0))-1;});}
  const cols={};reps.forEach(r=>{(cols[layer[r.id]]=cols[layer[r.id]]||[]).push(r);});
  pos={};lanes=[];designFrames=[];const Ls=Object.keys(cols).map(Number).sort((a,b)=>a-b);
  const laneLevels=[];
  if(layoutMode==='time'){
    // Timeline layout: one row, every box in timeline order; an open group's box is an event just before its items
    const tk=r=>r.header?Math.min(...r.members.map(n=>n.o))-1e-4*(20-(r.depth||0)):r.o;   // a group event sits right before its first item (outer groups first)
    const seq=reps.slice().sort((a,b)=>tk(a)-tk(b));
    seq.forEach((r,i)=>{pos[r.id]={x:i*(NW+XG),y:0};});
  }else if(isLanes()){
    // one column per top-level timeline group, in timeline order; rows inside a lane follow the
    // dependencies between that lane's own boxes, so each lane starts at the top
    const laneOf=r=>{if(r.isGroup){const gid=r.id.slice(1);if(groups[gid]&&groups[gid].design)return gid+'|_folded';}return laneKey(r.members[0]);};const LN={};   // a folded derived design keeps its frame
    const ports=[];reps.forEach(r=>{const k=laneOf(r);if(k&&k.endsWith('|_port')){ports.push(r);return;}(LN[k]=LN[k]||{id:k,o:1e9,items:[]});LN[k].o=Math.min(LN[k].o,r.o);LN[k].items.push(r);});
    // a connector sits on its design's frame; when nothing else of that design is on show (e.g. only the selected
    // branch), it becomes a block of its own so that design still gets a frame
    for(let i=ports.length-1;i>=0;i--){const r=ports[i];const d=laneKey(r.members[0]).split('|')[0];
      if(!Object.keys(LN).some(k=>laneDesign(k)===d)){const k=d+'|_none';LN[k]={id:k,o:r.o,items:[r]};ports.splice(i,1);}}
    const order=Object.values(LN).sort((a,b)=>a.o-b.o);const LG=46,TOP=44,SUBG=YG*0.45;
    // 1) each lane on its own: rows by dependency depth, long rows wrap into a small grid
    const built=order.map(l=>{const inL=new Set(l.items.map(r=>r.id));const ll={};l.items.sort((a,b)=>a.o-b.o).forEach(r=>{let d=0;pr[r.id].forEach(s=>{if(inL.has(s)&&ll[s]!=null&&R[s].o<r.o)d=Math.max(d,ll[s]+1);});ll[r.id]=d;});
      const rows={};l.items.forEach(r=>{(rows[ll[r.id]]=rows[ll[r.id]]||[]).push(r);});
      const maxc=Math.max(2,Math.min(l.items.length>40?14:5,Math.ceil(Math.sqrt(l.items.length*(l.items.length>40?1.8:1)))));   // big blocks (e.g. hundreds of parameters) grow wider, not only taller
      const loc={};let y=TOP,w=1;const levels=[];
      Object.keys(rows).map(Number).sort((a,b)=>a-b).forEach((L,li)=>{const a=rows[L];if(li)y+=YG;const lv={ids:a.map(r=>r.id),ys:[],home:{}};levels.push(lv);
        for(let i=0;i<a.length;i+=maxc){if(i)y+=SUBG;const ch=a.slice(i,i+maxc);lv.ys.push(y);ch.forEach((r,k)=>{loc[r.id]={x:k*(NW+XG),y:y};lv.home[r.id]=(lv.ys.length-1)*1000+k;});w=Math.max(w,ch.length);y+=NH;}});
      // the places a row may use when the selection pulls boxes together: every column of the block, on the row's own lines
      levels.forEach(lv=>{lv.slots=[];lv.ys.forEach((yy,ri)=>{for(let k=0;k<w;k++)lv.slots.push({x:k*(NW+XG),y:yy,key:ri*1000+k});});});
      return {l,loc,levels,w:w*(NW+XG)-XG,h:y+18};});
    // 2) lanes packed left to right into rows of lanes, aiming at a roughly 16:10 overall shape; with derived designs,
    //    each design's lanes are packed on their own, inside a frame, and the frames are packed the same way
    const pack=(list,ratio=1.6)=>{const area=list.reduce((a,b)=>a+(b.w+LG)*(b.h+LG),0);const target=Math.max(Math.max(...list.map(b=>b.w)),Math.sqrt(area*ratio));
      let lx=0,ly=0,rowH=0,W=0;const placed=[];
      list.forEach(b=>{if(lx>0&&lx+b.w>target){lx=0;ly+=rowH+LG;rowH=0;}placed.push({b,x:lx,y:ly});W=Math.max(W,lx+b.w);lx+=b.w+LG;rowH=Math.max(rowH,b.h);});
      return {placed,w:W,h:ly+rowH};};
    const putLane=(b,lx,ly)=>{Object.keys(b.loc).forEach(id=>{pos[id]={x:lx+b.loc[id].x,y:ly+b.loc[id].y};});
      b.levels.forEach(lv=>laneLevels.push({ids:lv.ids,home:lv.home,slots:lv.slots.map(q=>({x:lx+q.x,y:ly+q.y,key:q.key}))}));
      lanes.push({id:b.l.id,x:lx-14,w:b.w+28,y0:ly,y1:ly+b.h});};
    designFrames=[];
    {const byD={};built.forEach(b=>{const d=laneDesign(b.l.id);(byD[d]=byD[d]||[]).push(b);});
      const PADX=40,PADB=40,PIC_W=150,PIC_H=100;const picOf=d=>d===MAIN_DSG?D.meta.pic:(groups[d]&&groups[d].pic);const padT=d=>74;
      const frameTitle=d=>{if(d===MAIN_DSG)return 'This design · '+(D.meta.doc||'');const g=groups[d]||{};const v=g.via||[];return (v.includes('derive')&&!v.includes('insert')?'Derived design · ':v.includes('insert')&&!v.includes('derive')?'Inserted design · ':'Linked design · ')+(g.name||d);};
      // the part's picture is a tile of its own, packed with the blocks (it fills space beside them instead of
      // adding a header strip); a folded design gets a smaller one
      const frames=Object.keys(byD).map(d=>{const folded=byD[d].every(b=>laneBase(b.l.id)==='_folded');
        const list=byD[d].slice();if(picOf(d))list.unshift(folded?{w:210,h:140,picTile:true}:{w:330,h:230,picTile:true});
        const pk=pack(list);return {d,pk,w:Math.max(pk.w+2*PADX,frameTitle(d).length*15.5+60),h:pk.h+padT(d)+PADB,o:Math.min(...byD[d].map(b=>b.l.o))};}).sort((a,b)=>a.o-b.o);
      const fr=pack(frames.map(f=>({w:f.w,h:f.h,f})),2.4);
      fr.placed.forEach(q=>{const f=q.b.f;let pr=null;
        f.pk.placed.forEach(p=>{const x=q.x+PADX+p.x,y=q.y+padT(f.d)+p.y;if(p.b.picTile)pr={x,y,w:p.b.w,h:p.b.h};else putLane(p.b,x,y);});
        designFrames.push({d:f.d,title:frameTitle(f.d),pic:picOf(f.d),picRect:pr,x:q.x-14,y:q.y,w:f.w+28,h:f.h});});
      // a derived design's connector sits on the middle of its frame's bottom edge
      ports.forEach(r=>{const d=laneKey(r.members[0]).split('|')[0];const f=designFrames.find(x=>x.d===d);if(f)pos[r.id]={x:f.x+f.w/2-NW/2,y:f.y+f.h-NH/2};});}
  }else
  Ls.forEach(L=>{const c=cols[L];c.forEach(r=>{const xs=pr[r.id].map(s=>pos[s]?pos[s].x+NW/2:null).filter(v=>v!=null);r.bc=xs.length?xs.reduce((a,b)=>a+b,0)/xs.length:null;});
    const withBc=c.filter(r=>r.bc!=null);const avg=withBc.length?withBc.reduce((a,r)=>a+r.bc,0)/withBc.length:0;
    c.forEach(r=>{if(r.bc==null)r.bc=avg+r.o*0.001;});
    c.sort((a,b)=>a.bc-b.bc||a.o-b.o);let x=-1e9;const y=L*(NH+YG);
    c.forEach(r=>{x=Math.max(x,r.bc-NW/2);pos[r.id]={x:x,y:y};x+=NW+XG;});
    // shift the row so on average boxes sit under their parents
    const sh=c.reduce((a,r)=>a+(r.bc-(pos[r.id].x+NW/2)),0)/c.length;c.forEach(r=>{pos[r.id].x+=sh;});});
  if(headers.length){if(layoutMode!=='time'){headers.forEach(h=>{const xs=h.targets.map(t=>pos[t]?pos[t].x:null).filter(v=>v!=null);if(xs.length&&pos[h.id])pos[h.id].x=xs.reduce((a,b)=>a+b,0)/xs.length;});
    const rows={};Object.keys(pos).forEach(id=>{(rows[pos[id].y]=rows[pos[id].y]||[]).push(id);});
    Object.values(rows).forEach(ids=>{ids.sort((a,b)=>pos[a].x-pos[b].x);for(let i=1;i<ids.length;i++){const m=pos[ids[i-1]].x+NW+XG;if(pos[ids[i]].x<m)pos[ids[i]].x=m;}});}
    // Timeline layout: the group bar under the row shows what belongs to the group, so only the line to the item right after the event is kept
    headers.forEach(h=>h.targets.forEach(t=>{if(pos[t]&&(layoutMode!=='time'||Math.abs(pos[t].x-pos[h.id].x)<=NW+XG+2))redges.push({s:h.id,t:t,n:0,src:[],contain:true});}));}
  // relation sets for highlight
  let up=null,down=null,selRep=null,selSet=null;selRelated=[];
  if(selected&&byId[selected]){const s=byId[selected];selRep=rep(s);selSet=new Set([selRep]);up=new Set([...relOf(selected,'up')].map(i=>rep(byId[i])));down=new Set([...relOf(selected,'down')].map(i=>rep(byId[i])));}
  else if(selGroup&&groups[selGroup]){const mem=groupMembers(selGroup);const mset=new Set(mem.map(n=>n.id));selSet=new Set(mem.map(n=>rep(n)));if(R['h'+selGroup])selSet.add('h'+selGroup);selRep='__group__';
    // a group depends on everything any of its items depends on (links, incl. user parameters and items outside
    // groups), plus, with the group test, the groups whose suppression takes items of this group with them
    const g=groups[selGroup];const dnLinks=[...new Set(mem.flatMap(n=>[...relOf(n.id,'down')]))];
    // the group test results are whole chains: only used when the whole chain forward is shown
    const dn=[...new Set([...(g.dsupp&&relShow.dn&&relShow.dnAll?[...g.dsupp,...(g.dbreak||[])]:[]),...dnLinks])].filter(i=>!mset.has(i));
    down=new Set(dn.filter(i=>byId[i]&&visibleNode(byId[i])).map(i=>rep(byId[i])));
    const upIds=!relShow.up?[]:relShow.upAll?groupUpIds(selGroup):[...new Set(mem.flatMap(n=>[...relOf(n.id,'up')]))].filter(i=>!mset.has(i)&&byId[i]&&visibleNode(byId[i]));
    up=new Set(upIds.filter(i=>byId[i]).map(i=>rep(byId[i])));selSet.forEach(r=>{up.delete(r);down.delete(r);});}
  // direct parents / children folded into a collapsed box: the collapsed box counts as the direct parent / child
  if(selSet){if(up)up=new Set([...up].map(visRep));if(down)down=new Set([...down].map(visRep));selSet.forEach(r=>{if(up)up.delete(r);if(down)down.delete(r);});}
  let routeReps=null,treeIds=null;
  if(selected&&byId[selected])treeIds=new Set([...closure(selected,'up'),...closure(selected,'down')]);
  let tReps=new Set();if(RT)tReps=new Set(route.items.filter(i=>byId[i]).map(i=>visRep(rep(byId[i]))));
  if(RT&&selSet){routeReps=new Set([...RT.items].filter(i=>byId[i]).map(i=>visRep(rep(byId[i]))));
    up=new Set([...RT.up].map(i=>visRep(rep(byId[i]))));down=new Set([...RT.down].map(i=>visRep(rep(byId[i]))));selSet.forEach(r=>{up.delete(r);down.delete(r);});}
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
  if(selSet&&!isLanes()&&layoutMode!=='time'&&!focus&&pullTogether){const rel=new Set([...selSet,...(up||[]),...(down||[])]);
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
  const addBtn=(id,bt)=>{let w=nodeBtnEls[id];if(!w){w=document.createElementNS(NS,'g');w.setAttribute('transform','translate('+pos[id].x+','+pos[id].y+')');nodeBtnEls[id]=w;btnLayer.appendChild(w);
    // the box's +/- button counts as part of the box for the hover highlight
    w.addEventListener('mouseenter',()=>nodeHover(id,true));w.addEventListener('mouseleave',()=>{if(nhState)nodeHover(id,false);});}w.appendChild(bt);};
  laneEls={};
  // Timeline layout: a thin bar in the group's colour under each open group, from its event box to its last item
  if(layoutMode==='time'&&headers.length){const gb=document.createElementNS(NS,'g');gb.setAttribute('class','tbands');vp.appendChild(gb);
    headers.forEach(h=>{const ids=[h.id,...h.mem.map(n=>visRep(rep(n)))].filter(i=>pos[i]);if(!ids.length)return;const xs=ids.map(i=>pos[i].x);
      const x0=Math.min(...xs),x1=Math.max(...xs)+NW,y=NH+16+(h.depth-1)*7,col=colorOfGroup(h.gid)||'var(--muted)';
      const rc=document.createElementNS(NS,'rect');rc.setAttribute('x',x0);rc.setAttribute('y',y);rc.setAttribute('width',x1-x0);rc.setAttribute('height',4);rc.setAttribute('rx',2);rc.setAttribute('style','fill:'+col+';fill-opacity:.75');
      const tt=document.createElementNS(NS,'title');tt.textContent='Timeline group: '+(groups[h.gid]?groups[h.gid].name:h.gid);rc.appendChild(tt);gb.appendChild(rc);});}
  if(designFrames.length){const fl=document.createElementNS(NS,'g');fl.setAttribute('class','dframes');vp.appendChild(fl);
    designFrames.forEach(f=>{const isMain=f.d===MAIN_DSG;const col=isMain?'var(--accent)':(gColor[f.d]||'var(--muted)');
      const bg=document.createElementNS(NS,'rect');bg.setAttribute('x',f.x);bg.setAttribute('y',f.y);bg.setAttribute('width',f.w);bg.setAttribute('height',f.h);bg.setAttribute('rx',18);
      bg.setAttribute('style','fill:'+col+';fill-opacity:.035;stroke:'+col+';stroke-opacity:.85;stroke-width:3.5;stroke-dasharray:'+(isMain?'none':'14 7'));
      const tx=document.createElementNS(NS,'text');tx.setAttribute('x',f.x+26);tx.setAttribute('y',f.y+46);tx.setAttribute('style','font-size:26px;font-weight:800;fill:'+col);
      tx.textContent=f.title;
      fl.append(bg,tx);
      if(f.pic&&f.picRect){const pw=f.picRect.w,ph=f.picRect.h;const px=f.picRect.x-14,py=f.picRect.y;
        const pb=document.createElementNS(NS,'rect');pb.setAttribute('x',px);pb.setAttribute('y',py);pb.setAttribute('width',pw);pb.setAttribute('height',ph);pb.setAttribute('rx',10);pb.setAttribute('style','fill:var(--panel);stroke:'+col+';stroke-opacity:.5;stroke-width:1.5');
        const im=document.createElementNS(NS,'image');im.setAttribute('x',px+3);im.setAttribute('y',py+3);im.setAttribute('width',pw-6);im.setAttribute('height',ph-6);im.setAttribute('preserveAspectRatio','xMidYMid meet');im.setAttribute('href',f.pic);im.style.cursor='zoom-in';
        // hovering the small picture shows it large
        im.addEventListener('mouseenter',ev=>picPopShow(f.pic,f.title,ev));im.addEventListener('mousemove',ev=>picPopMove(ev));im.addEventListener('mouseleave',picPopHide);
        fl.append(pb,im);}});}
  if(lanes.length){const gl=document.createElementNS(NS,'g');vp.appendChild(gl);
    const CM=layoutMode==='comps';const noLane=id=>{if(id.includes('|'))return true;return id==='_none'||id==='_root'||(id==='_params'&&!groups['_params']);};
    lanes.forEach(l=>{const col=laneBase(l.id)==='_params'?'var(--c-param)':noLane(l.id)?'var(--muted)':((CM?cColor[l.id]:gColor[l.id])||'var(--muted)');const bg=document.createElementNS(NS,'rect');bg.setAttribute('x',l.x);bg.setAttribute('y',l.y0);bg.setAttribute('width',l.w);bg.setAttribute('height',l.y1-l.y0);bg.setAttribute('rx',10);
      bg.setAttribute('style','fill:'+col+';fill-opacity:.07;stroke:'+col+';stroke-opacity:.45;stroke-width:1.5');
      const tx=document.createElementNS(NS,'text');tx.setAttribute('x',l.x+12);tx.setAttribute('y',l.y0+24);tx.setAttribute('style','font-size:15px;font-weight:700;fill:'+col);{const DSG=groups[l.id]&&groups[l.id].design;if(DSG){bg.setAttribute('style','fill:'+col+';fill-opacity:.05;stroke:'+col+';stroke-opacity:.8;stroke-width:3;stroke-dasharray:10 5');tx.setAttribute('style','font-size:17px;font-weight:800;fill:'+col);}
      const LB=laneBase(l.id);const full=DSG?'Design · '+groups[l.id].name:LB==='_folded'?'Whole design (folded)':LB==='_params'?'User parameters':LB==='_none'?'Not in a group':LB==='_root'?'Root component':CM?(byId[l.id]?byId[l.id].name:l.id):(groups[l.id]?groups[l.id].name:l.id);const mc=Math.max(4,Math.floor((l.w-24)/9));tx.textContent=full.length>mc?full.slice(0,mc-1)+'…':full;const tt=document.createElementNS(NS,'title');tt.textContent=full;tx.appendChild(tt);}
      // fold button on the block title: a whole timeline group (Groups layout) or a whole component (Components layout)
      {const gidL=CM?null:l.id,cidL=CM&&byId[l.id]?l.id:null,psL=(l.id==='_params'||(l.id==='_none'&&!CM))?l.id:null;const gk=psL||gidL;
        const can=(gk&&groups[gk])||(cidL&&canCollapse(cidL))||(cidL&&collapsedNodes.has(cidL));
        if(can){const open=cidL?!collapsedNodes.has(cidL):(expanded.has(gk)||searchOpen.has(gk));const bt=document.createElementNS(NS,'g');bt.setAttribute('class','ctog');bt.setAttribute('transform','translate('+(l.x+l.w/2)+','+(l.y1+1)+')');   // bottom centre, like the boxes' buttons
          const c=document.createElementNS(NS,'circle');c.setAttribute('r',open?8:9);c.setAttribute('class','ctogc'+(open?'':' col'));const t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','middle');t.setAttribute('y',4);t.setAttribute('class','ctogt');t.textContent=open?'−':'+';
          const tt=document.createElementNS(NS,'title');tt.textContent=open?(cidL?'Fold this component into its box':'Fold this whole group into one box'):(cidL?'Show what is in this component':'Show the items of this group');bt.append(c,t,tt);if(search&&open)offFold(bt,tt);
          bt.addEventListener('mousedown',ev=>ev.stopPropagation());
          bt.addEventListener('click',ev=>{ev.stopPropagation();animatedRerender(()=>{if(cidL){if(open)collapsedNodes.add(cidL);else collapsedNodes.delete(cidL);}else if(open){expanded.delete(gk);Object.keys(groups).forEach(x=>{let q=groups[x].parent,gd=0;while(q&&gd++<20){if(q===gk){expanded.delete(x);break;}q=groups[q]?groups[q].parent:null;}});}else expanded.add(gk);},{});});
          l.btn=bt;}}
      const lg=document.createElementNS(NS,'g');lg.style.cursor=noLane(l.id)?'default':'pointer';lg.append(bg,tx);laneEls[l.id]={g:lg,l,col};if(l.btn)btnLayer.appendChild(l.btn);if(!noLane(l.id))lg.addEventListener('click',ev=>{ev.stopPropagation();if(moved)return;if(l.id==='_params')selectGroup('_params');else if(CM&&!(groups[l.id]&&groups[l.id].design))select(l.id);else selectGroup(l.id);});gl.appendChild(lg);});}
  const ge=document.createElementNS(NS,'g');ge.setAttribute('class','elayer');vp.appendChild(ge);const geHi=document.createElementNS(NS,'g');geHi.setAttribute('class','elayer');const gBadge=document.createElementNS(NS,'g');
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
    {const ta=tArc(a,b);if(ta)tlArcTop=Math.max(tlArcTop,ta.h*0.75);}
    const d=edgeD(a,b,pm.o1,pm.o2,bow);edgeEls.push({el:p,s:e.s,t:e.t,o1:pm.o1,o2:pm.o2,bow,src:e.src});
    p.setAttribute('d',d);let cls=e.contain?'edge contain':'edge'+(e.src.every(x=>x.k.every(k=>k==='order'||(k!=='suppress'&&!kindOn[k])))?' order':'')+(!e.contain&&(e.via||e.src.length&&e.src.every(x=>x.via))?' bridge':'');
    if(e.contain){const gc=colorOfGroup(e.s.slice(1));if(gc)p.setAttribute('style','stroke:'+gc);}
    if(selRep&&e.contain){if(!(selSet.has(e.s)||selSet.has(e.t)||up.has(e.t)||down.has(e.t)))cls+=' dim';}
    else if(selRep){
      if(selSet.has(e.s)&&selSet.has(e.t))cls+=' insel';   // a link inside the selection (between items of a selected group)
      else if(selSet.has(e.t)&&up.has(e.s))cls+=' up';else if(selSet.has(e.s)&&down.has(e.t))cls+=' down';else if(up.has(e.s)&&up.has(e.t))cls+=' up ind';else if(down.has(e.s)&&down.has(e.t))cls+=' down ind';else cls+=' dim';}
    if(!selSet&&hitReps&&!hitReps.has(e.s)&&!hitReps.has(e.t))cls+=' dim';
    if(routeReps){if(!e.contain&&routeReps.has(e.s)&&routeReps.has(e.t)&&e.src.some(x=>RT.items.has(x.s)&&RT.items.has(x.t)))cls=cls.replace(/ (dim|up|down|ind|insel)\b/g,'')+' route';
      else if(!/\bdim\b/.test(cls))cls+=' dim';}
    p.setAttribute('class',cls);const lift=/ (up|down|insel|route)\b/.test(cls);
    const t=document.createElementNS(NS,'title');t.textContent=e.contain?'Part of timeline group '+(groups[e.s.slice(1)]?groups[e.s.slice(1)].name:''):[...new Set(e.src.flatMap(x=>x.k))].map(x=>KIND[x]||x).join(', ')+(e.n>1?' ('+e.n+' links)':'');if(!e.contain&&e.via)t.textContent+=' (through collapsed items)';else if(!e.contain&&/ bridge/.test(cls))t.textContent+=' (through hidden items)';p.appendChild(t);const layer=lift?geHi:ge;layer.appendChild(p);
    // a link joined to a collapsed box (a folded group or block, or a box that hides what depends on it), at
    // either end: show how many of the items folded into that box the link comes from / leads to
    {const folded=(id,ids)=>{const r=R[id];return !!r&&ids.length>0&&((r.isGroup&&!r.header)||(e.via&&collapsedNodes.has(id)&&ids.some(i=>i!==id)));};
      const sIds=e.contain?[]:[...new Set(e.src.map(x=>x.s).filter(i=>byId[i]))],tIds=e.contain?[]:[...new Set(e.src.map(x=>x.t).filter(i=>byId[i]))];
      const fromF=!e.contain&&folded(e.s,sIds),intoF=!e.contain&&folded(e.t,tIds);
      // not a link to the whole box but to some of the items folded into it: drawn dotted, like other indirect links
      if(fromF||intoF){if(!p.classList.contains('bridge')){p.classList.add('bridge');const pt=p.querySelector('title');if(pt&&!/\(through /.test(pt.textContent))pt.textContent+=' (to or from items inside a collapsed box)';}}
      if(fromF||intoF){const bg=document.createElementNS(NS,'g');bg.setAttribute('class','ecount'+(/ up\b/.test(cls)?' up':/ down\b/.test(cls)?' down':'')+(/\bdim\b/.test(cls)?' dim':''));
        const txt=fromF&&intoF?sIds.length+'→'+tIds.length:String(fromF?sIds.length:tIds.length),w=Math.max(18,8+txt.length*7);const rc=document.createElementNS(NS,'rect');rc.setAttribute('x',-w/2);rc.setAttribute('y',-8);rc.setAttribute('width',w);rc.setAttribute('height',16);rc.setAttribute('rx',8);
        const tx=document.createElementNS(NS,'text');tx.setAttribute('y',3.8);tx.textContent=txt;
        const lst=ids=>ids.slice(0,20).map(i=>'• '+byId[i].name).join('\n')+(ids.length>20?'\n…':'');
        const tt=document.createElementNS(NS,'title');tt.textContent=[fromF?'Comes from '+sIds.length+' item'+(sIds.length===1?'':'s')+' inside the collapsed box:\n'+lst(sIds):'',intoF?'Leads to '+tIds.length+' item'+(tIds.length===1?'':'s')+' inside the collapsed box:\n'+lst(tIds):''].filter(Boolean).join('\n\n');
        bg.append(rc,tx,tt);gBadge.appendChild(bg);const x=edgeEls[edgeEls.length-1];x.badge=bg;p.badge=bg;placeBadge(x,a,b);}}
    // wide invisible twin that catches the mouse; hovering shows the link bold and rings both ends
    const plain=!e.contain&&!/ (up|down|insel|route)\b/.test(cls);if(plain)p.classList.add('plain');if(p.badge)p.badge.classList.add('plain');
    const h=document.createElementNS(NS,'path');h.setAttribute('class','ehit'+(/\bdim\b/.test(cls)?' off':'')+(plain?' plain':''));h.setAttribute('d',d);h.appendChild(t.cloneNode(true));layer.appendChild(h);
    edgeEls[edgeEls.length-1].hit=h;const es=e.s,et=e.t;
    h.addEventListener('mouseenter',()=>{if(drag||hovPin)return;edgeHover(p,es,et,true);});
    // click: zoom to show both boxes, keep the link highlighted until the mouse moves
    h.addEventListener('click',ev=>{ev.stopPropagation();if(moved)return;hovPin=null;edgeHover(p,es,et,true);hovPin={x:ev.clientX,y:ev.clientY};focusOn([es,et],450);});
    h.addEventListener('mouseleave',()=>edgeHover(p,es,et,false));});
  // layers, bottom to top: other links, dimmed boxes, links of the selection, boxes of the selection.
  // Links of the selected item run above boxes that are not part of its history.
  const gnDim=document.createElementNS(NS,'g'),gn=document.createElementNS(NS,'g');vp.append(gnDim,geHi,gBadge,gn);nhAnchor=gBadge;   // hover links: above every other link (and faded boxes), below the boxes
  reps.forEach(r=>{const p=pos[r.id];if(!p)return;const g=document.createElementNS(NS,'g');nodeEls[r.id]=g;g.setAttribute('class','nd');g.setAttribute('transform','translate('+p.x+','+p.y+')');
    const rect=document.createElementNS(NS,'rect');rect.setAttribute('width',NW);rect.setAttribute('height',NH);rect.setAttribute('rx',5);
    let label,cat;
    // Groups layout, whole block folded: the block itself stands for the group; inside it only a short note
    const laneFold=r.isGroup&&!r.header&&layoutMode==='lanes'&&laneKey(r.members[0])===r.id.slice(1);
    if(r.isGroup){const gid=r.id.slice(1);label=laneFold?(r.members.length+' item'+(r.members.length===1?'':'s')+' folded'):(r.header?'▾ ':'▸ ')+(groups[gid]?groups[gid].name:'group')+'  ('+r.members.length+')';cat='group';rect.setAttribute('stroke-dasharray','4 3');}
    else{const n=r.members[0];label=n.name;cat=n.cat;}
    const gsupp=!r.isGroup?isSupp(r.members[0]):!!(simState&&r.members.length&&r.members.every(isSupp));
    if(r.isGroup&&simState){const k=r.members.filter(isSupp).length;if(k){label+=' · '+k+' off';}}
    if(r.isGroup&&!r.header&&hitCount[r.id])label=label.startsWith('▸ ')?'▸ '+hitCount[r.id]+' found · '+label.slice(2):label+' · '+hitCount[r.id]+' found';   // first, so it is not cut off
    if(gsupp)cat='supp_';rect.setAttribute('fill',gsupp?'var(--supp-bg)':'var(--c-'+cat+'-bg)');if(gsupp)rect.setAttribute('stroke-dasharray','5 3');rect.setAttribute('stroke',gsupp?'var(--supp)':(!r.isGroup&&r.members[0].health===2)?'var(--err)':(!r.isGroup&&r.members[0].health===1)?'var(--warn)':'var(--c-'+cat+')');
    if(!r.isGroup&&r.members[0].port){rect.setAttribute('rx',NH/2);rect.setAttribute('stroke-width','3');}
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
      if(selRep!=='__group__'||r.header||selGroup==='_sel'){const gl=document.createElementNS(NS,'rect');gl.setAttribute('class','selglow');gl.setAttribute('x',-9);gl.setAttribute('y',-9);gl.setAttribute('width',NW+18);gl.setAttribute('height',NH+18);gl.setAttribute('rx',12);g.appendChild(gl);}}
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
      const tt=document.createElementNS(NS,'title');tt.textContent=open?'Collapse this timeline group into one box':'Expand this timeline group to show its items';bt.append(c,t,tt);if(search&&open)offFold(bt,tt);
      bt.addEventListener('mousedown',ev=>ev.stopPropagation());
      bt.addEventListener('click',ev=>{ev.stopPropagation();const newId=(open?'g':'h')+gid;
        animatedRerender(()=>{if(open){expanded.delete(gid);Object.keys(groups).forEach(x=>{if(groupPath(x).length>1&&groups[x].parent&&(function up(y){let q=groups[y].parent,gd=0;while(q&&gd++<20){if(q===gid)return true;q=groups[q]?groups[q].parent:null;}return false;})(x))expanded.delete(x);});}else expanded.add(gid);},{keepOld:r.id,keepNew:newId});});addBtn(r.id,bt);}
    else if(canCollapse(r.id)){const col=collapsedNodes.has(r.id);const bt=document.createElementNS(NS,'g');bt.setAttribute('class','ctog');bt.setAttribute('transform','translate('+(NW/2)+','+(NH+1)+')');
      const c=document.createElementNS(NS,'circle');c.setAttribute('r',col?9:7);c.setAttribute('class','ctogc'+(col?' col':''));
      const t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','middle');t.setAttribute('y',4);t.setAttribute('class','ctogt');t.textContent=col?'+':'−';
      const tt=document.createElementNS(NS,'title');tt.textContent=col?('Expand: show the '+(hiddenCount[r.id]||0)+' hidden items that depend on this'):(isLanes()?'Collapse: hide what depends on this in the same timeline group':'Collapse: hide everything that depends on this');bt.append(c,t,tt);if(search&&!col)offFold(bt,tt);
      if(col&&hiddenCount[r.id]){const cn=document.createElementNS(NS,'text');cn.setAttribute('x',13);cn.setAttribute('y',4);cn.setAttribute('class','ctogn');const inner=(hitCount[r.id]||0)-(matches(r.members[0])?1:0);cn.textContent=hiddenCount[r.id]+' hidden'+(search&&inner>0?' · '+inner+' found':'');bt.appendChild(cn);}
      bt.addEventListener('mousedown',ev=>ev.stopPropagation());
      bt.addEventListener('click',ev=>{ev.stopPropagation();const rid=r.id;animatedRerender(()=>{if(col)collapsedNodes.delete(rid);else collapsedNodes.add(rid);},{keepOld:rid,keepNew:rid});});addBtn(r.id,bt);}
    if(selSet&&!selSet.has(r.id)&&!up.has(r.id)&&!down.has(r.id)&&!(r.header&&hdrRelated(r)))g.classList.add('dim');
    if(routeReps){if(routeReps.has(r.id)&&!r.header){g.classList.remove('dim');if(tReps.has(r.id)){rect.setAttribute('stroke','var(--sel)');rect.setAttribute('stroke-width','3.5');
        const gl=document.createElementNS(NS,'rect');gl.setAttribute('class','selglow');gl.setAttribute('x',-9);gl.setAttribute('y',-9);gl.setAttribute('width',NW+18);gl.setAttribute('height',NH+18);gl.setAttribute('rx',12);g.insertBefore(gl,g.firstChild);}
      else if(!selSet.has(r.id)){rect.setAttribute('stroke','var(--route)');rect.setAttribute('stroke-width','2.2');}}else g.classList.add('dim');}
    // route button: on every other box of the selected item's tree
    if(treeIds&&!laneFold&&!r.header&&!selSet.has(r.id)){const tItems=r.members.filter(m=>treeIds.has(m.id)).map(m=>m.id);
      if(tItems.length){const on=!!(RT&&tReps.has(r.id));const bt=document.createElementNS(NS,'g');bt.setAttribute('class','rbtn'+(on?' on':''));bt.setAttribute('transform','translate('+(NW-14)+','+(NH+1)+')');
        const c=document.createElementNS(NS,'circle');c.setAttribute('r',8);
        const pa=document.createElementNS(NS,'path');pa.setAttribute('d','M-4,3.5 C-4,-1 4,1 4,-3.5');
        const d1=document.createElementNS(NS,'circle');d1.setAttribute('class','rdot');d1.setAttribute('cx',-4);d1.setAttribute('cy',3.5);d1.setAttribute('r',1.8);
        const d2=document.createElementNS(NS,'circle');d2.setAttribute('class','rdot');d2.setAttribute('cx',4);d2.setAttribute('cy',-3.5);d2.setAttribute('r',1.8);
        const tt=document.createElementNS(NS,'title');tt.textContent=on?('Showing '+(RT.paths>=1e6?'over a million':RT.paths)+' route'+(RT.paths===1?'':'s')+' ('+RT.items.size+' items) between the selection and this. Click to hide them.'):'Show every route between the selection and this';
        bt.append(c,pa,d1,d2,tt);
        if(on){const n=document.createElementNS(NS,'text');n.setAttribute('class','rcount');n.setAttribute('x',-12);n.setAttribute('y',4);n.setAttribute('text-anchor','end');n.textContent=(RT.paths>=1e6?'1M+':RT.paths)+' route'+(RT.paths===1?'':'s');bt.appendChild(n);}
        ['mousedown','dblclick'].forEach(ev=>bt.addEventListener(ev,e=>e.stopPropagation()));
        bt.addEventListener('click',ev=>{ev.stopPropagation();if(moved)return;setRoute(on?null:tItems);});
        if(!on){bt.addEventListener('mouseenter',()=>routePreview(tItems,true,tt));bt.addEventListener('mouseleave',()=>routePreviewClear());}
        addBtn(r.id,bt);}}
    if(hitReps){if(hitReps.has(r.id)){rect.setAttribute('stroke','var(--sel)');rect.setAttribute('stroke-width','3');if(r.id===graphHits[hitIdx]){rect.setAttribute('stroke','var(--down)');rect.setAttribute('stroke-width','4');}}else if(!selSet)g.classList.add('dim');}

    {const rid=r.id;g.addEventListener('mouseenter',()=>nodeHover(rid,true));g.addEventListener('mouseleave',()=>{if(nhState&&nodeEls[rid]===g)nodeHover(rid,false);});}
    g.addEventListener('click',ev=>{ev.stopPropagation();if(moved)return;if(simOn&&(ev.shiftKey||ev.altKey)){if(r.isGroup)simToggleGroup(r.id.slice(1));else if(canItems)simToggleItem(r.members[0].id);else{const gg=(r.members[0].g||[]);if(gg.length)simToggleGroup(gg[gg.length-1]);}return;}if(r.isGroup){selectGroup(r.id.slice(1));}else select(r.members[0].id);});
    (selSet&&g.classList.contains('dim')?gnDim:gn).appendChild(g);if(nodeBtnEls[r.id]&&g.classList.contains('dim'))nodeBtnEls[r.id].classList.add('dim');});
  vp.appendChild(btnLayer);
  if(fitAfter)fit(centerId?rep(byId[centerId]):null);
}
function fit(centerRep,only){const ids=(only&&only.length?only:Object.keys(pos)).filter(i=>pos[i]);if(!ids.length)return;const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;const W=(svg.clientWidth||800)-LP,H=svg.clientHeight||600;
  if(centerRep&&pos[centerRep]){T.k=1;T.x=LP+W/2-(pos[centerRep].x+NW/2);T.y=H/2-(pos[centerRep].y+NH/2);applyT();return;}
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;ids.forEach(i=>{const p=pos[i];x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});if(layoutMode==='time')y0-=tlArcTop;
  if(!(only&&only.length))designFrames.forEach(f=>{x0=Math.min(x0,f.x);y0=Math.min(y0,f.y);x1=Math.max(x1,f.x+f.w);y1=Math.max(y1,f.y+f.h);});   // the design frames' titles and pictures too
  const k=Math.min(1.2,Math.min((W-40)/(x1-x0||1),(H-80)/(y1-y0||1)));T.k=Math.max(0.03,k);const cw=(x1-x0)*T.k,ch=(y1-y0)*T.k;T.x=LP+(cw<W-40?(W-cw)/2-x0*T.k:20-x0*T.k);T.y=only&&ch<H-100?50+(H-50-ch)/2-y0*T.k:60-y0*T.k;applyT();}
// zoom and centre on the given boxes (animated); a single item is shown at a readable size
let anim=null;
function focusOn(repIds,dur0,alignTop){const ps=repIds.map(r=>pos[r]).filter(Boolean);if(!ps.length)return;
  const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;
  const W=(svg.clientWidth||800)-LP,H=svg.clientHeight||600;
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;ps.forEach(p=>{x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});
  // the design frames around these boxes (titles, pictures) are part of what is shown
  if(ps.length>1)designFrames.forEach(f=>{if(ps.some(p=>p.x>=f.x&&p.x<=f.x+f.w&&p.y>=f.y&&p.y<=f.y+f.h)){x0=Math.min(x0,f.x);y0=Math.min(y0,f.y);x1=Math.max(x1,f.x+f.w);y1=Math.max(y1,f.y+f.h);}});
  let k=Math.min(Math.max(T.k,1.2),1.6,(W-80)/(x1-x0||1),(H-140)/(y1-y0||1));k=Math.max(k,0.05);
  const cx=(x0+x1)/2,cy=(y0+y1)/2;const tx=LP+W/2-cx*k,ty=alignTop?70-y0*k:30+H/2-cy*k;
  const s0={x:T.x,y:T.y,k:T.k},t0=performance.now(),dur=dur0||280;if(anim)cancelAnimationFrame(anim);
  const step=now=>{const a=Math.min(1,(now-t0)/dur),e=a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;
    T.k=s0.k+(k-s0.k)*e;T.x=s0.x+(tx-s0.x)*e;T.y=s0.y+(ty-s0.y)*e;applyT();if(a<1)anim=requestAnimationFrame(step);else anim=null;};
  anim=requestAnimationFrame(step);}
// the large picture shown while hovering a design frame's small one
let picPop=null;
function picPopShow(src,title,ev){if(!picPop){picPop=document.createElement('div');picPop.id='picPop';
    picPop.style.cssText='position:fixed;z-index:60;pointer-events:none;background:var(--panel);border:1px solid var(--border);border-radius:12px;box-shadow:0 12px 36px rgba(0,0,0,.28);padding:10px;display:none';
    picPop.innerHTML='<div style="font-weight:700;font-size:13px;margin:0 2px 8px;color:var(--text)"></div><img style="display:block;width:560px;max-width:70vw;max-height:60vh;object-fit:contain;border-radius:8px;background:#fff">';document.body.appendChild(picPop);}
  picPop.firstChild.textContent=title||'';picPop.lastChild.src=src;picPop.style.display='block';picPopMove(ev);}
function picPopMove(ev){if(!picPop||picPop.style.display==='none')return;const r=picPop.getBoundingClientRect(),W=innerWidth,H=innerHeight;
  let x=ev.clientX+18,y=ev.clientY+18;if(x+r.width>W-8)x=ev.clientX-r.width-18;if(y+r.height>H-8)y=Math.max(8,H-r.height-8);picPop.style.left=Math.max(8,x)+'px';picPop.style.top=y+'px';}
function picPopHide(){if(picPop)picPop.style.display='none';}
// animated zoom to a rectangle of the graph (in graph coordinates)
function zoomToRect(x0,y0,x1,y1,dur){const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;
  const W=(svg.clientWidth||800)-LP,H=svg.clientHeight||600;const k=Math.max(0.03,Math.min(1.2,(W-40)/(x1-x0||1),(H-80)/(y1-y0||1)));
  const tx=LP+(W-(x1-x0)*k)/2-x0*k,ty=50+Math.max(0,(H-60-(y1-y0)*k)/2)-y0*k;
  const s0={x:T.x,y:T.y,k:T.k},t0=performance.now();if(anim)cancelAnimationFrame(anim);
  const step=now=>{const a=Math.min(1,(now-t0)/dur),e=a<.5?2*a*a:1-Math.pow(-2*a+2,2)/2;
    T.k=s0.k+(k-s0.k)*e;T.x=s0.x+(tx-s0.x)*e;T.y=s0.y+(ty-s0.y)*e;applyT();if(a<1)anim=requestAnimationFrame(step);else anim=null;};
  anim=requestAnimationFrame(step);}
// first view: like Fit, but never zoomed out further than MIN_START_ZOOM (top of the graph, centred)
const MIN_START_ZOOM=0.55;
function fitInitial(){const ids=Object.keys(pos);if(!ids.length)return;const gp=$('gpanel');const LP=(gp&&gp.style.display!=='none'&&!gp.classList.contains('min'))?320:0;
  const W=(svg.clientWidth||800)-LP,H=svg.clientHeight||600;let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;ids.forEach(i=>{const p=pos[i];x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});if(layoutMode==='time')y0-=tlArcTop;
  designFrames.forEach(f=>{x0=Math.min(x0,f.x);y0=Math.min(y0,f.y);x1=Math.max(x1,f.x+f.w);y1=Math.max(y1,f.y+f.h);});
  const k=Math.max(MIN_START_ZOOM,Math.min(1.2,(W-40)/(x1-x0||1),(H-80)/(y1-y0||1)));T.k=k;T.x=LP+(W-(x1-x0)*k)/2-x0*k;T.y=60-y0*k;
  // Timeline layout: start at the beginning of the row, with room above it for the arcs
  if(layoutMode==='time'){T.k=Math.max(k,0.5);T.x=LP+30-x0*T.k;T.y=Math.min(H-120,Math.max(90+tlArcTop*T.k,H*0.45));}   // the row (y=0) sits low enough for the arcs above it
  applyT();}
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
  if(!ok&&selGroup&&groups[selGroup]){b.title=selGroup==='_sel'?'Play how the selected items were built from their dependencies (P)':'Play how this timeline group was built from its dependencies (P)';return;}
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
  // open timeline groups (their header boxes in the Depth layout) are the parents of their items: each header
  // appears right before the first of its items, outer groups before inner ones
  {const anc=r=>{if(r[0]==='g'||r[0]==='h'){const out=[];let q=groups[r.slice(1)]?groups[r.slice(1)].parent:null,gd=0;while(q&&gd++<20){out.unshift(q);q=groups[q]?groups[q].parent:null;}return out;}
      const n=byId[r];if(!n)return [];if(n.g&&n.g.length)return n.g;const ps=pseudoOf(n);return ps?[ps]:[];};
    const seen=new Set(order),out=[];order.forEach(r=>{anc(r).forEach(gid=>{const h='h'+gid;if(pos[h]&&!seen.has(h)){seen.add(h);out.push(h);}});out.push(r);});order=out;}
  // Timeline layout: play exactly in the order of the row, left to right (boxes with the same timeline place, like
  // user parameters, keep the order they have on screen); a selected item still comes last
  if(layoutMode==='time'){const last=(!whole&&!grp)?order[order.length-1]:null;order=order.filter(r=>pos[r]).sort((a,b)=>pos[a].x-pos[b].x);
    if(last&&order.includes(last)){order=order.filter(r=>r!==last);order.push(last);}}
  const S=new Set(order);const idx={};order.forEach((r,i)=>idx[r]=i);
  const edges=edgeEls.filter(x=>S.has(x.s)&&S.has(x.t)&&x.s!==x.t);
  const steps=order.map((r,i)=>({id:r,inc:edges.filter(x=>x.t===r&&idx[x.s]<i)}));
  const ov=document.createElementNS('http://www.w3.org/2000/svg','g');ov.setAttribute('class','pbov');vp.appendChild(ov);
  const lits=document.createElementNS('http://www.w3.org/2000/svg','g');ov.appendChild(lits);
  edgeHover(null,null,null,false);hovPin=null;if(hovState)edgeHover(null,null,null,false);nodeHoverClear();
  // Groups / Components layouts: a block appears (with a ring around it) when its first item is built
  const laneAt=r=>{const q=pos[r];if(!q)return null;const l=lanes.find(l=>q.x>=l.x-1&&q.x+NW<=l.x+l.w+1&&q.y>=l.y0-1&&q.y+NH<=l.y1+1);return l?l.id:null;};
  const laneFirst={};if(isLanes())order.forEach((r,i)=>{const l=laneAt(r);if(l!=null&&laneEls[l]&&laneFirst[l]==null)laneFirst[l]=i;});
  PB={laneFirst,laneShown:new Set(),lalpha:{},whole,grp,order,S,idx,edges,steps,k:-1,phase:'intro',vt:0,ph0:0,dur:1800,speed:PB_speed,paused:false,
    alpha:{},ealpha:new Map(),ov,lits,fading:[],dots:[],pulses:[],userCam:false,last:performance.now(),raf:0,shown:new Set()};
  Object.keys(nodeEls).forEach(id=>PB.alpha[id]=1);edgeEls.forEach(x=>PB.ealpha.set(x,1));
  svg.classList.add('playing');document.body.classList.add('pbon');document.body.classList.toggle('pbthumbs',hasThumbs&&showThumbs);pbUI();
  PB.raf=requestAnimationFrame(pbFrame);}
let PB_speed=1;
function stopPlay(){if(!PB)return;cancelAnimationFrame(PB.raf);PB.ov.remove();
  Object.values(nodeEls).forEach(g=>{g.style.opacity='';});edgeEls.forEach(x=>{x.el.style.opacity='';});Object.values(laneEls).forEach(L=>{L.g.style.opacity='';});
  PB=null;svg.classList.remove('playing');document.body.classList.remove('pbon','pbthumbs');$('pbCard').dataset.id='';pbUI();}
function pbUI(){const bar=$('pbBar');if(!bar)return;bar.style.display=PB?'flex':'none';pbSync();if(!PB)return;
  $('pbPlay').textContent=PB.phase==='done'?'↺':(PB.paused?'▶':'❚❚');
  $('pbPlay').title=PB.phase==='done'?'Replay':(PB.paused?'Resume (Space)':'Pause (Space)');
  $('pbNext').disabled=PB.phase==='done';$('pbPrev').disabled=PB.phase==='intro'||pbLastShown()<0;$('pbSpeed').textContent=PB.speed+'×';
  const n=PB.order.length;let t;
  if(PB.phase==='intro')t=PB.whole?'Whole history · <b>'+n+'</b> step'+(n===1?'':'s'):PB.grp==='_sel'?'<b>'+n+'</b> step'+(n===1?'':'s')+' to build the <b>'+multi.length+' selected items</b>':PB.grp?'<b>'+n+'</b> step'+(n===1?'':'s')+' to build group <b>'+esc(groups[PB.grp]?groups[PB.grp].name:'')+'</b>':'<b>'+n+'</b> step'+(n===1?'':'s')+' to build <b>'+esc(byId[selected]?byId[selected].name:'')+'</b>';
  else if(PB.phase==='done')t='Done · '+n+' step'+(n===1?'':'s');
  else{t='Step <b>'+(PB.k+1)+'</b> / '+n;}
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
// a travelling dot and a lit line for each incoming link of a step; returns the longest link length
function pbMakeDots(st){let mx=0;st.inc.forEach(x=>{const pp=pbPath(x);mx=Math.max(mx,pp.len);
      const NS2='http://www.w3.org/2000/svg';const d0=edgeCurve(pos[x.s],pos[x.t],x.o1,x.o2,x.bow);
      const lit=document.createElementNS(NS2,'g');const mk=c=>{const q=document.createElementNS(NS2,'path');q.setAttribute('class','pblit '+c);q.setAttribute('d',d0);lit.appendChild(q);return q;};
      const lb=mk('base'),lg=mk('glow'),lt=mk('trail');PB.lits.appendChild(lit);const ll=lt.getTotalLength()||pp.len;
      const g=document.createElementNS('http://www.w3.org/2000/svg','g');g.setAttribute('class','pbdot');
      const h=document.createElementNS('http://www.w3.org/2000/svg','circle');h.setAttribute('class','pbhalo');
      const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('class','pbcore');g.append(h,c);PB.ov.appendChild(g);
      PB.dots.push({x,pp,el:g,h,c,lit,lb,lg,lt,ll,dotted:x.el.classList.contains('bridge')});});return mx;}
// place the dots at fraction f of their links (the lit trail runs from the source to the dot)
function pbDrawDots(f,p,ea,camPts){const rad=Math.min(40,Math.max(5,7/T.k));
  PB.dots.forEach(d=>{const q=pbAt(d.pp,f);camPts.push(q);d.c.setAttribute('cx',q[0]);d.c.setAttribute('cy',q[1]);d.c.setAttribute('r',rad);
    d.h.setAttribute('cx',q[0]);d.h.setAttribute('cy',q[1]);d.h.setAttribute('r',rad*(2.1+0.35*Math.sin(PB.vt/140)));
    ea.set(d.x,PB_A.edgePre+(1-PB_A.edgePre)*f);
    const px=1/T.k;d.lb.setAttribute('stroke-width',3*px);d.lg.setAttribute('stroke-width',13*px);d.lt.setAttribute('stroke-width',4.5*px);
    const L=d.ll,on=f*L;d.lt.setAttribute('stroke-dasharray',pbDash(d,on,4.5*px));d.lg.setAttribute('stroke-dasharray',pbDash(d,on,4.5*px));
    if(d.dotted)d.lb.setAttribute('stroke-dasharray',pbDash(d,L,4.5*px));
    d.lb.style.opacity=String(Math.min(1,p*4));});}
function pbKillDots(){PB.dots.forEach(d=>{d.el.remove();d.lit.remove();});PB.dots=[];}
function pbStep(k){PB.k=k;PB.userCam=false;pbDropDots();if(PB.steps[k])PB.steps[k].pulsed=false;
  if(k>=PB.order.length){pbSetPhase('done',1600);pbUI();return;}   // shows the whole result briefly, then closes
  Object.keys(PB.laneFirst).forEach(l=>{if(PB.laneFirst[l]===k&&!PB.laneShown.has(l)){PB.laneShown.add(l);pbLanePulse(l);}});
  const st=PB.steps[k];
  if(st.inc.length){const mx=pbMakeDots(st);pbSetPhase('fly',Math.max(1400,Math.min(3400,1100+mx*1.0)));}
  else pbSetPhase('fade',k===0?1400:1100);
  pbUI();}
// dots are removed at once; their lit links stay fully drawn and fade out
// the lit part of a link, from its start up to `on`: a solid line, or a dotted one for a dotted (indirect) link
function pbDash(d,on,w){const L=d.ll;if(!d.dotted)return on+' '+(L+10);const step=w*2.4;const n=Math.floor(on/step);
  let a=[];for(let i=0;i<n;i++)a.push('0.01',String(step-0.01));const r=on-n*step;if(r>0)a.push('0.01',String(r));a.push('0',String(L+10));return a.join(' ');}
function pbDropDots(){PB.dots.forEach(d=>{d.el.remove();const L=d.ll,w=4.5/T.k;d.lt.setAttribute('stroke-dasharray',pbDash(d,L,w));d.lg.setAttribute('stroke-dasharray',pbDash(d,L,w));PB.fading.push({el:d.lit,t0:PB.vt});});PB.dots=[];}
function pbLanePulse(lid){const L=laneEls[lid];if(!L)return;const r=document.createElementNS('http://www.w3.org/2000/svg','rect');r.setAttribute('class','pbpulse');r.setAttribute('rx',14);
  r.style.stroke=L.col;PB.ov.appendChild(r);PB.pulses.push({el:r,lane:L.l,t0:PB.vt});}
function pbPulse(id){const p=pos[id];if(!p)return;const r=document.createElementNS('http://www.w3.org/2000/svg','rect');r.setAttribute('class','pbpulse');r.setAttribute('rx',9);PB.ov.appendChild(r);PB.pulses.push({el:r,id,t0:PB.vt});}
// finish the running step at once (Next)
// the last step whose item is built (-1: none yet)
function pbLastShown(){if(!PB)return -1;const k=Math.min(PB.k,PB.order.length-1);if(k>=0&&PB.shown.has(PB.order[k]))return k;return k-1;}
// Next: while playing, finish the running step at once and go on; while paused, animate exactly one step and stay paused
// finish a running step back at once (its item is no longer built)
function pbFinishBack(){pbKillDots();const j=PB.backK,st=PB.steps[j];PB.shown.delete(st.id);Object.keys(PB.laneFirst).forEach(l=>{if(PB.laneFirst[l]===j)PB.laneShown.delete(l);});PB.k=j;}
// finish the running step at once (its item is built), without starting the next one
function pbFinishStep(){const k=PB.k;if(k<0||k>=PB.order.length)return;const r=PB.order[k];pbDropDots();
  if(!PB.shown.has(r)){PB.shown.add(r);pbPulse(r);}}   // the next frame sets the opacities from PB.shown
// Next: always skips whatever is animating and starts the next step's animation right away.
// While paused it animates that one step and stays paused.
function pbNextStep(){if(!PB||PB.phase==='done')return;PB.userCam=false;
  if(PB.phase==='back'){pbFinishBack();pbStep(PB.k);}              // the step taken back comes straight back
  else if(PB.phase==='intro')pbStep(0);
  else if(PB.paused&&!PB.stepOnce&&!PB.shown.has(PB.order[PB.k])){} // paused before a step: just play that step
  else{pbFinishStep();pbStep(PB.k+1);}
  PB.stepOnce=PB.paused&&PB.phase!=='done';}
// Back: take the last built step back (animated); a running animation (forward or back) is cut short.
// Playing goes on from there, paused stays paused.
function pbBackStep(){if(!PB||PB.phase==='intro')return;
  if(PB.phase==='back')pbFinishBack();
  const j=pbLastShown();if(j<0){if(PB.phase!=='back')return;pbStep(PB.k);PB.stepOnce=false;pbUI();return;}
  pbDropDots();   // a step still running (item not built yet) is dropped
  PB.k=j;PB.backK=j;PB.stepOnce=false;PB.userCam=false;const st=PB.steps[j];const mx=st.inc.length?pbMakeDots(st):0;
  pbSetPhase('back',mx?Math.max(900,Math.min(2200,700+mx*0.6)):700);pbUI();}
function pbSkip(){if(!PB||PB.phase==='done')return;
  if(PB.phase==='intro'){pbStep(0);return;}
  const r=PB.order[PB.k];PB.shown.add(r);pbPulse(r);pbStep(PB.k+1);}
function pbFrame(now){if(!PB)return;const dt=Math.min(80,now-PB.last);PB.last=now;
  if(!PB.paused||PB.stepOnce||PB.phase==='back')PB.vt+=dt*PB.speed;
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
    pbDrawDots(f,p,ea,camPts);
    camIds=[st.id];if(p>=1){pbDropDots();pbPulse(st.id);pbSetPhase('fade',1000);}}
  else if(PB.phase==='fade'){const st=PB.steps[PB.k];na[st.id]=PB_A.pre+(1-PB_A.pre)*e;st.inc.forEach(x=>ea.set(x,1));
    camIds=[st.id];kmax=1.3;if(!st.inc.length&&!st.pulsed&&p>0.15){st.pulsed=true;pbPulse(st.id);}
    if(p>=1){PB.shown.add(st.id);pbSetPhase('hold',st.inc.length?500:650);pbUI();}}
  else if(PB.phase==='hold'){camIds=[PB.order[PB.k]];kmax=1.3;if(p>=1){if(!PB.paused)pbStep(PB.k+1);else PB.stepOnce=false;}}
  else if(PB.phase==='back'){// a step taken back: its box and links fade back, the camera returns to the step before
    // the box fades out first, then a dot runs back along each incoming link, taking the lit line back with it
    const j=PB.backK,st=PB.steps[j];const pb=Math.min(1,p/0.3),pf=Math.max(0,(p-0.3)/0.7);na[st.id]=1-(1-PB_A.pre)*pbEase(pb);
    if(PB.dots.length){camPts=[];pbDrawDots(1-pbEase(pf),1,ea,camPts);camIds=[st.id];}
    else{st.inc.forEach(x=>ea.set(x,1-(1-PB_A.edgePre)*e));camIds=[j>0?PB.order[j-1]:st.id];}
    kmax=1.3;
    if(p>=1){pbKillDots();PB.shown.delete(st.id);Object.keys(PB.laneFirst).forEach(l=>{if(PB.laneFirst[l]===j)PB.laneShown.delete(l);});PB.stepOnce=false;pbStep(j);}}
  else if(PB.phase==='done'){camIds=PB.order;kmin=0.02;kmax=1.1;pad=60;if(p>=1){stopPlay();return;}}
  // blocks (Groups / Components layouts): dim until their first item is built, then fade in
  {const k=Math.min(1,dt*PB.speed/Math.max(200,tau));Object.keys(laneEls).forEach(l=>{const L=laneEls[l];
    let v=PB.laneFirst[l]==null?0.15:(PB.laneShown.has(l)||PB.phase==='done'?1:0.35);
    if(PB.phase==='intro')v=1+(v-1)*e;const cur=PB.lalpha[l]==null?1:PB.lalpha[l];let nv=PB.phase==='intro'?v:cur+(v-cur)*k;if(Math.abs(v-nv)<0.03)nv=v;
    if(Math.abs(nv-cur)>0.002||PB.lalpha[l]==null){PB.lalpha[l]=nv;L.g.style.opacity=String(nv);}});}
  // apply opacities
  Object.keys(nodeEls).forEach(id=>{const g=nodeEls[id];const v=na[id];if(PB.alpha[id]!==v){PB.alpha[id]=v;g.style.opacity=String(v);}});
  edgeEls.forEach(x=>{const v=ea.get(x);if(PB.ealpha.get(x)!==v){PB.ealpha.set(x,v);x.el.style.opacity=String(v);}});
  PB.fading=PB.fading.filter(q=>{const a=(PB.vt-q.t0)/900;if(a>=1){q.el.remove();return false;}q.el.style.opacity=String(1-pbEase(a));return true;});
  // arrival pulses: a ring that grows out of the box and fades
  PB.pulses=PB.pulses.filter(q=>{if(q.lane){const a=(PB.vt-q.t0)/1400,L=q.lane;if(a>=1){q.el.remove();return false;}
      const g=4+30*pbEase(a);q.el.setAttribute('x',L.x-g);q.el.setAttribute('y',L.y0-g);q.el.setAttribute('width',L.w+2*g);q.el.setAttribute('height',L.y1-L.y0+2*g);q.el.style.opacity=String(1-a);return true;}
    const a=(PB.vt-q.t0)/1100;const pp=pos[q.id];if(a>=1||!pp){q.el.remove();return false;}
    const g=6+22*pbEase(a);q.el.setAttribute('x',pp.x-g);q.el.setAttribute('y',pp.y-g);q.el.setAttribute('width',NW+2*g);q.el.setAttribute('height',NH+2*g);q.el.style.opacity=String(1-a);return true;});
  // camera: follow the dots and the box being built
  if(!PB.userCam&&(!PB.paused||PB.stepOnce||PB.phase==='back')){const b=pbBox(camIds,camPts);if(b)pbCam(pbView(b,kmin,kmax,pad),dt,PB.phase==='intro'||PB.phase==='done'?tau*1.6:tau);}
  PB.raf=requestAnimationFrame(pbFrame);}
svg.addEventListener('mousedown',()=>{if(PB)PB.userCam=true;},true);
svg.addEventListener('click',e=>{if(PB){e.stopImmediatePropagation();}},true);
svg.addEventListener('wheel',()=>{if(PB)PB.userCam=true;},{capture:true,passive:true});
$('pbStart').onclick=()=>playHistory();
$('pbStop').onclick=()=>stopPlay();
$('pbNext').onclick=()=>{if(PB){pbNextStep();pbUI();}};
$('pbPrev').onclick=()=>{if(PB){pbBackStep();pbUI();}};
$('pbPlay').onclick=()=>{if(!PB)return;if(PB.phase==='done'){playHistory();return;}PB.paused=!PB.paused;PB.stepOnce=false;PB.userCam=false;pbUI();};
$('pbSpeed').onclick=()=>{PB_speed=PBS[(PBS.indexOf(PB_speed)+1)%PBS.length];if(PB)PB.speed=PB_speed;pbUI();};
window.addEventListener('keydown',e=>{if(e.target&&(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||e.target.tagName==='TEXTAREA'))return;
  if(!PB){if((e.key==='p'||e.key==='P')&&!e.metaKey&&!e.ctrlKey&&!e.altKey&&view==='graph'){e.preventDefault();playHistory();}return;}
  if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();stopPlay();}
  else if(e.key===' '){e.preventDefault();$('pbPlay').onclick();}
  else if(e.key==='ArrowRight'&&!e.altKey){e.preventDefault();$('pbNext').onclick();}
  else if(e.key==='ArrowLeft'&&!e.altKey){e.preventDefault();$('pbPrev').onclick();}},true);
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
  insert:'Items that bring another file into the design: linked component inserts and Derive features.',
  newcomp:'Components made in the design itself (New Component, or a feature set to create a new component). Nothing outside the design leads into them.',
  joint:'Joints, as-built joints, joint origins, rigid groups, motion links, contact sets, Arrange and captured positions (snapshots).',
  other:'Anything else (canvases, decals, add-in features, items the Fusion API does not describe).'};
const KIND_DESC={sketch:'uses a sketch (or geometry projected/included into it)',profile:'uses a sketch profile',plane:'built on or references a construction plane, axis or point',
  derive:'comes from the design it derives from (what the Derive feature brings in)',
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
    row(box('var(--c-solid-bg)','var(--sel)',{w:3,extra:'<rect x="-1" y="0" width="72" height="30" rx="8" class="selglow" style="stroke-width:2"/>'}),'Selected','Gold outline with a glow and dashes running round it. With a timeline group selected, all its boxes get the purple outline.'),
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
    row(line(''),'Grey line','A link: the lower item uses the upper one. Arrows point from what is used to what uses it. Grey links are hidden unless Display > All links is on; the links of the selection and of the hovered box always show.'),
    row(line('up'),'Blue, thick','A direct parent of the selection: the selected item uses it directly.'),
    row(line('up ind'),'Blue, faint','A further ancestor: a link between two items the selection depends on.'),
    row(line('down'),'Green, thick','A direct child: it uses the selected item directly.'),
    row(line('down ind'),'Green, faint','A further descendant: a link between two items that depend on the selection.'),
    row(line('insel'),'Gold','A link inside the selection (between items of the selected timeline group).'),
    row(line('dim'),'Very faint','Not related to the selection.'),
    row(line('hov'),'Gold, thick','The link under the mouse, or the one you clicked (stays until the mouse moves). Both its ends get a gold ring.'),
    row(sv('<path d="M4,15 L64,15" fill="none" style="stroke:var(--hov);stroke-width:6;stroke-opacity:.28"/><path d="M4,15 L64,15" fill="none" style="stroke:var(--hov);stroke-width:2.2;stroke-dasharray:7 5"/>'),'Gold, dashed (hover)','Hovering a box rings it in gold and marks its links with moving dashes (they move in the link\'s direction): blue to the boxes it uses (dashed blue ring), green to the boxes that use it (dotted green ring). Nothing else changes. Can be switched off in the Display menu.'),
    row(line('order'),'Dashed','"Same body, later": only timeline order on the same body, not a real reference. Off by default (Display menu).'),
    row(sv('<path d="M4,15 L60,15" fill="none" stroke="'+G+'" stroke-width="2.4" stroke-opacity=".85" stroke-linecap="round"/>'),'Thick coloured line','Joins a group header to the group\'s items (Depth layout).'),
    row(line('bridge'),'Dotted','Not a direct link: it goes through items that are hidden by the Filter menu or folded into a collapsed box ("N hidden"), or it starts or ends at a folded group or block and so really links only some of the items inside it (see the number on the line).'),
    row(sv('<path class="edge" d="M4,15 L64,15"/><rect x="24" y="7" width="22" height="16" rx="8" style="fill:var(--panel);stroke:var(--edge);stroke-width:1.2"/><text x="35" y="19" style="font-size:10.5px;font-weight:700;fill:var(--muted);text-anchor:middle">3</text>'),'Number on a line','The line starts or ends at a collapsed box (a folded group or block, or a box hiding what depends on it): the number is how many of the items folded into that box it comes from or leads to ("3→2" when both ends are collapsed). Hover it to see their names.'),
    row(sv('<path d="M4,15 L64,15" fill="none" style="stroke:var(--route);stroke-width:2.6"/><g transform="translate(34,15)"><circle r="8" style="fill:var(--panel);stroke:var(--route);stroke-width:1.2"/><path d="M-4,3.5 C-4,-1 4,1 4,-3.5" style="fill:none;stroke:var(--route);stroke-width:1.6"/></g>'),'Orange / route button','With an item selected, every other box of its tree (what it depends on or what uses it, at any distance) gets a small route button at its lower right. Hover it to preview the routes (orange moving dashes), without changing anything. Click it to show, in orange, every route between the selection and that box; the box then looks selected too and the side panel lists the routes. Everything else fades. Click it again, press Esc or Hide routes to hide them; Back returns to the previous state.'),
    row(sv('<path class="edge" d="M10,26 C10,4 58,4 58,26"/><rect x="2" y="24" width="64" height="3" rx="1.5" style="fill:'+G+';fill-opacity:.75"/>'),'Timeline layout','Every item in one row in timeline order; an open timeline group is an event box just before its items, with a bar in its colour under them. Links between neighbours run straight; longer links arc above the row (longer ones higher).'),
    row(sv('<path class="edge" d="M4,24 C22,24 20,6 38,6 S60,6 64,6"/>'),'Curves and bends','Only routing: long links bend sideways so they do not lie on top of each other. The shape means nothing.'),
  ],'Each line may stand for several references between the same two items; hover it to see which kinds.');
  h+=sec('Link kinds',Object.keys(KIND).filter(k=>KIND_DESC[k]).map(k=>row('<span class="pill lgk">'+KIND[k]+'</span>',KIND[k],KIND_DESC[k])),
    'Why the lower item uses the upper one. All kinds are always shown, except "Same body, later" (Display menu).');
  h+=sec('History playback',[
    row(sv('<path class="pblit base" d="M4,15 L60,15" style="stroke-width:3"/><path class="pblit trail" d="M4,15 L36,15" style="stroke-width:4.5"/><circle cx="36" cy="15" r="9" class="pbhalo"/><circle cx="36" cy="15" r="5" class="pbcore"/>'),'Blue dot and gold line','Travels from items already built to the next one; the link it follows lights up in gold.'),
    row(box('var(--c-solid-bg)','var(--c-solid)',{extra:'<rect x="0" y="1" width="70" height="28" rx="8" class="pbpulse" style="stroke-width:2.5"/>'}),'Blue ring','An item appears (fades in to full colour). An open timeline group box appears just before its first item. In the Groups and Components layouts a whole block fades in, with a ring in its colour around it, when its first item is built.'),
    row(sv('<rect x="10" y="2" width="50" height="26" rx="4" fill="var(--panel)" stroke="var(--border)"/><rect x="14" y="5" width="42" height="14" rx="2" fill="var(--panel2)"/><rect x="14" y="21" width="16" height="5" rx="2" fill="var(--c-solid-bg)"/>'),'Card (bottom right)','Thumbnail, type and name of the item being built.'),
  ],'▶ Play (or P) with an item selected plays how it was built; with a group selected, how the group was built; with nothing selected, the whole history. Space pauses, → / ← step forward / back, Esc stops.');
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
if(!kindsPresent.includes('order')){const l=$('showOrder').parentNode;l.style.display='none';}
if(hasThumbs)$('thumbCtrl').style.display='';
$('infoBtn').onclick=()=>{const open=!document.body.classList.contains('infoopen');if(open){renderInfo();}setInfo(open);};
$('infoClose').onclick=()=>setInfo(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(document.body.classList.contains('infoopen'))setInfo(false);else if(route&&route.sel===selected)setRoute(null);else if(selected||selGroup)clearSel();}});
$('gpToggle').onclick=()=>{const m=$('gpanel').classList.toggle('min');$('gpToggle').textContent=m?'+':'–';};
$('showThumbs').onchange=e=>{const v=e.target.checked;const ch=()=>{showThumbs=v;document.body.classList.toggle('nothumbs',!showThumbs);peekHide();renderDetails();};
  if(view==='graph'&&Object.keys(pos).length)animatedRerender(ch,{fit:'fit',dur:480});else{ch();if(view==='graph')renderGraph(true);}};
$('pullTog').onchange=e=>{const v=e.target.checked;if(view==='graph'&&Object.keys(pos).length)animatedRerender(()=>{pullTogether=v;},{dur:450});else pullTogether=v;};
[['relUp','up'],['relUpAll','upAll'],['relDn','dn'],['relDnAll','dnAll']].forEach(([el,k])=>{$(el).onchange=e=>{const v=e.target.checked;
  const set=()=>{relShow[k]=v;$('relUpAllL').classList.toggle('off',!relShow.up);$('relDnAllL').classList.toggle('off',!relShow.dn);
    $('relUpAll').disabled=!relShow.up;$('relDnAll').disabled=!relShow.dn;};
  if(view==='graph'&&Object.keys(pos).length&&(selected||selGroup))animatedRerender(set,focus?{fit:'fit'}:{dur:450});else{set();if(view==='graph')renderGraph(false);}};});
let showAllLinks=false;svg.classList.toggle('quiet',!showAllLinks);
$('allLinks').onchange=e=>{showAllLinks=e.target.checked;svg.classList.toggle('quiet',!showAllLinks);};
// Timeline layout: scroll the row while the mouse is near the left or right edge of the graph
let edgeScroll=true,asDir=0,asF=0,asRaf=0,asLast=0;
function asStop(){asDir=0;}
function asFrame(now){if(!asDir||layoutMode!=='time'||drag||PB){asRaf=0;return;}const dt=Math.min(50,now-asLast);asLast=now;
  const ids=Object.keys(pos);if(!ids.length){asRaf=0;return;}let x0=1e9,x1=-1e9;ids.forEach(i=>{x0=Math.min(x0,pos[i].x);x1=Math.max(x1,pos[i].x+NW);});
  const LP=pbLP(),W=svg.clientWidth||800;const v=(180+1100*asF*asF)*dt/1000;
  if(asDir>0){const lim=W-60-x1*T.k;if(T.x>lim)T.x=Math.max(lim,T.x-v);}   // right edge: bring what is to the right into view
  else{const lim=LP+60-x0*T.k;if(T.x<lim)T.x=Math.min(lim,T.x+v);}
  applyT();asRaf=requestAnimationFrame(asFrame);}
svg.addEventListener('mousemove',ev=>{if(!edgeScroll||layoutMode!=='time'||view!=='graph'||drag||PB){asStop();return;}
  const r=svg.getBoundingClientRect(),LP=pbLP(),Z=70;const xl=ev.clientX-r.left-LP,xr=r.right-ev.clientX;
  if(xr<Z){asDir=1;asF=(Z-xr)/Z;}else if(xl>=0&&xl<Z){asDir=-1;asF=(Z-xl)/Z;}else asDir=0;
  if(asDir&&!asRaf){asLast=performance.now();asRaf=requestAnimationFrame(asFrame);}});
svg.addEventListener('mouseleave',asStop);
$('edgeScroll').onchange=e=>{edgeScroll=e.target.checked;if(!edgeScroll)asStop();};
// when selecting: zoom to the selected object or to its whole tree (default) (what is highlighted around it)
let zoomTree=true;$('zoomObj').onchange=()=>{zoomTree=false;};$('zoomTree').onchange=()=>{zoomTree=true;};
$('hovRel').onchange=e=>{hoverRel=e.target.checked;if(!hoverRel)nodeHoverClear();};
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
function setLayout(m){layoutMode=m;$('laneBtn').classList.toggle('on',m==='lanes');$('compBtn').classList.toggle('on',m==='comps');$('layDeps').classList.toggle('on',m==='deps');$('layTime').classList.toggle('on',m==='time');renderGraph(false);fitInitial();}
$('layDeps').onclick=()=>setLayout('deps');
$('layTime').onclick=()=>setLayout('time');
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
let st;$('search').oninput=e=>{clearTimeout(st);st=setTimeout(()=>{search=e.target.value.trim().toLowerCase();hitIdx=0;$('colAll').disabled=!!search;$('colAll').title=search?'Clear the search to fold':'';if(view==='tree')renderTree();else{renderGraph(false);if(graphHits.length)goHit(0);}updateSearchNav();},150);};
$('search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();goHit(e.shiftKey?-1:1);}});
$('sPrev').onclick=()=>goHit(-1);$('sNext').onclick=()=>goHit(1);
if(canGroups){simOn=true;simCompute();renderSimBar();}
renderHealth();updateLinksBtn();hist.push(snap());hIdx=0;updHistBtns();
applyT();buildAdj();renderGroupPanel();setView('graph');renderGraph(false);fit();setInfo(true);
// opening: the whole picture first (every design), then a glide in to the design the analysis started from
{const mf=designFrames.find(f=>f.d===MAIN_DSG);if(mf&&designFrames.length>1)setTimeout(()=>{if(view==='graph'&&!selected&&!selGroup)zoomToRect(mf.x,mf.y,mf.x+mf.w,mf.y+mf.h,1400);},900);}
// opening: show the whole graph first, then glide in to the top row, at its middle (nothing gets selected)
{// the box in the top row that is closest to the horizontal middle of the graph
  const ids=Object.keys(pos);let first=null;
  if(ids.length){const top=Math.min(...ids.map(i=>pos[i].y));const x0=Math.min(...ids.map(i=>pos[i].x)),x1=Math.max(...ids.map(i=>pos[i].x+NW));const mid=(x0+x1)/2;
    first=ids.filter(i=>Math.abs(pos[i].y-top)<1).sort((a,b)=>Math.abs(pos[a].x+NW/2-mid)-Math.abs(pos[b].x+NW/2-mid))[0];}
  if(first&&!designFrames.length)setTimeout(()=>{if(!selected&&!selGroup)focusOn([first],1100,true);},650);}
})();
</script>
</body>
</html>
'''
