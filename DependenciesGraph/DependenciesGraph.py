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
            # the same faces come up again and again (every curve a sketch projects from a gear): the token is
            # slow to read (~3 ms), so it is looked up once per face while one item is read
            memo = self.__dict__.setdefault('_face_memo', {})
            fk = (_safe(lambda: obj.body.name), _safe(lambda: obj.tempId))
            if fk[1] is not None and fk in memo:
                tok = memo[fk]
            else:
                tok = _safe(lambda: obj.entityToken)
                if fk[1] is not None:
                    memo[fk] = tok
            n = self.face_owner.get(tok) if tok else None
            if n:
                out.append((n, 'geometry'))
            else:
                self.resolve(_safe(lambda: obj.body), out, 'body', depth + 1)
            return
        if t == 'BRepEdge':
            memo = self.__dict__.setdefault('_edge_memo', {})
            ek = (_safe(lambda: obj.body.name), _safe(lambda: obj.tempId))
            if ek[1] is not None and ek in memo:
                out.extend(memo[ek])
                return
            sub = []
            for f in (_safe(lambda: list(obj.faces)) or []):
                self.resolve(f, sub, 'geometry', depth + 1)
            if ek[1] is not None:
                memo[ek] = sub
            out.extend(sub)
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

    def _diff_outputs(self, it):
        """The faces an item made or changed, found by comparing each of its bodies with the last time an item
        changed that body (face centre and area). Read through the bodies: a feature's own face list is very
        slow to walk in Fusion (about 2.5 ms per face, seconds for a gear), a body's is not. Updates the record."""
        e = _safe(lambda: it.entity)
        if e is None or _safe(lambda: it.isSuppressed, False) or not _has(e, 'bodies'):
            return []
        bs = self.__dict__.setdefault('_body_sigs', {})
        new = []
        for b in (_safe(lambda: list(e.bodies)) or []):
            key = self._body_key(b)
            prev = bs.get(key)
            cur = {}
            for f in (_safe(lambda: list(b.faces)) or []):
                cur.setdefault(self._face_sig(f), f)
            new.extend(f for sg, f in cur.items() if prev is None or sg not in prev)
            bs[key] = set(cur)
        return new

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
        for f in (getattr(self, '_cur_new', None) or []):
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
        self._face_memo = {}         # face and edge ids are only valid until the next compute
        self._edge_memo = {}
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

    @staticmethod
    def _body_key(b):
        return (_safe(lambda: b.parentComponent.name, ''), _safe(lambda: b.name, ''))

    def _remember_bodies(self, e):
        """After an item: the face signatures of the bodies it made or changed, for the next items. Only those
        bodies are read (reading every face of every body after every item took over a minute on an assembly)."""
        if e is None or not _has(e, 'faces'):
            return
        bs = self.__dict__.setdefault('_body_sigs', {})
        for b in (_safe(lambda: list(e.bodies)) or []):
            bs[self._body_key(b)] = set(self._face_sig(f) for f in (_safe(lambda: list(b.faces)) or []))

    def _new_faces(self, e):
        """Faces of the feature that did not exist before it. Fusion hands faces that a later feature
        only touched (for example a second cut through the same slot) over to that feature, so
        e.faces alone can show geometry an earlier feature made. Compared with the faces its bodies had the
        last time an item changed them."""
        return list(getattr(self, '_cur_new', None) or [])

    def capture(self, it, nid):
        # Called with the marker right after `it`.
        if not self.want_thumbs:
            return
        try:
            if nid is not None:
                self._capture(it, nid)
        finally:
            pass

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
        n_items = self.tl.count
        stop = getattr(self, 'cancelled', None)
        tm = self.scan_times = {'roll': 0.0, 'thumbs': 0.0, 'outputs': 0.0, 'references': 0.0}
        clk = time.perf_counter
        # no pictures in this walk: nothing needs to be seen, so nothing is meshed for display at every step (a
        # design the user has open elsewhere is left as it is)
        hide = not self.want_thumbs and not getattr(self, 'no_roll', False)
        if hide:
            self._hide_display()
        try:
            self._scan(n_items, stop, tm, clk)
        finally:
            if hide:
                self._show_display()
        _mem_log('read times: ' + ', '.join('%s %.1f s' % (k, v) for k, v in tm.items()))

    def _scan(self, n_items, stop, tm, clk):
        tl = self.tl
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
            _mem_tick(lambda: 'reading %s: item %d of %d' % (
                _safe(lambda: self.des.parentDocument.name, '?'), i + 1, n_items))
            t0 = clk()
            if not getattr(self, 'no_roll', False):
                _safe(lambda: it.rollTo(True))
            self.keep_active()
            t1 = clk()
            tm['roll'] += t1 - t0
            if prev is not None:
                self._cur_new = _safe(lambda: self._diff_outputs(prev[0]), [])
                self.capture(*prev)
                t2 = clk()
                tm['thumbs'] += t2 - t1
                try:
                    self.record_outputs(*prev)
                except Exception as ex:
                    self.warnings.append('Could not read outputs of %s: %s' % (_safe(lambda: prev[0].name, '?'), ex))
                t1 = clk()
                tm['outputs'] += t1 - t2
            try:
                links = self.inputs_of(it)
            except Exception as ex:
                links = []
                self.warnings.append('Could not read references of %s: %s' % (_safe(lambda: it.name, '?'), ex))
            tm['references'] += clk() - t1
            for src, kind in links:
                self.add_edge(src, nid, kind)
            prev = (it, nid)
        if not getattr(self, 'no_roll', False):
            _safe(lambda: tl.moveToEnd())
        if prev is not None:
            self._cur_new = _safe(lambda: self._diff_outputs(prev[0]), [])
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
        t0 = time.perf_counter()
        ok = False
        try:
            r = self._set_suppressed_now(es, value)
            ok = True
            return r
        finally:
            # Undo bookkeeping: suppressions since the design was last known to be in its original state, each
            # one undo step; anything else (switching back on) makes Undo unusable until the next clean state
            n = getattr(self, '_undo_n', 0)
            self._undo_n = (n + 1) if (value and ok and n is not None) else None
            self.t_compute = getattr(self, 't_compute', 0.0) + time.perf_counter() - t0
            self.n_compute = getattr(self, 'n_compute', 0) + 1
            _mem_tick(lambda: '%s: %d suppress/restore calls' % (
                _safe(lambda: self.des.parentDocument.name, '?'), self.n_compute))
            _breathe(self.UI_PAUSE)

    def _set_suppressed_now(self, es, value):
        fn = getattr(self.des, 'setSuppressed', None)
        if fn is not None:
            try:
                result = fn(es, bool(value))
                # Autodesk documents this call as all-or-none.  Do not silently
                # fall back to per-item suppression when the bulk operation returns
                # False: doing so would change the semantics and introduce extra
                # recomputes.  Let the caller's existing recovery path handle it.
                if result is False:
                    raise RuntimeError('Design.setSuppressed returned False')
                self.n_bulk_suppressed = getattr(self, 'n_bulk_suppressed', 0) + 1
                return True
            except Exception as ex:
                # Fusion can report a downstream compute failure after changing the
                # requested suppression state.  Preserve the existing behavior: if
                # the requested states actually landed, propagate the exception so
                # callers can inspect the resulting health state; otherwise retain
                # the exception and use the legacy fallback only for older/unsupported
                # builds.
                if all(_safe(lambda: e.isSuppressed, None) == bool(value) for e in es):
                    raise
                # A real Design.setSuppressed failure is not equivalent to an API
                # missing on an older Fusion build.  Only use the per-item fallback
                # when the method itself was unavailable.
                raise
        else:
            self.n_bulk_suppressed_fallback = getattr(self, 'n_bulk_suppressed_fallback', 0) + 1
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

    def _static_dependency_profile(self, items):
        """Classify timeline items from the reference graph without treating it as ground truth.

        ``explained`` means the scan found at least one explicit/reference edge leaving the
        item.  ``ambiguous`` means no such edge was found.  The exact suppression pass still
        tests both classes because Fusion can have native/implicit dependencies that are not
        exposed through the public input properties.  The profile is therefore used only to
        prioritize work and for diagnostics, never to discard a test.
        """
        explained = set()
        for (src, _dst), kinds in self.edges.items():
            if kinds - {'order'}:
                explained.add(src)
        out = {}
        for i in items:
            nid = self.tl2node.get(i)
            out[i] = 'explained' if nid in explained else 'ambiguous'
        return out

    DISPLAY_FOLDERS = ('isBodiesFolderLightBulbOn', 'isSketchFolderLightBulbOn', 'isConstructionFolderLightBulbOn',
                       'isJointsFolderLightBulbOn')

    def _hide_display(self):
        """Hide bodies, sketches and construction geometry of every component while the tests run: each recompute
        also builds the display meshes of what is visible (dense for gears), and Fusion keeps them. Hidden
        geometry is still computed, so suppression, errors and warnings are the same."""
        self._hiding = True
        saved = []
        for c in (_safe(lambda: list(self.des.allComponents)) or []):
            for a in self.DISPLAY_FOLDERS:
                if _safe(lambda: getattr(c, a), False):
                    try:
                        setattr(c, a, False)
                        saved.append((c, a))
                    except Exception:
                        pass
        self._display_saved = saved

    def _show_display(self):
        self._hiding = False
        for c, a in getattr(self, '_display_saved', None) or []:
            _safe(lambda: setattr(c, a, True))
        self._display_saved = []

    def suppression_test(self, progress, cancelled):
        self._hide_display()
        self._mr_base = _process_memory()
        exp = _experiments_begin()
        self.t_compute = self.t_state = 0.0
        self.n_compute = 0
        self.n_state_items = 0
        self.n_state_skipped = 0
        t0 = time.perf_counter()
        try:
            self._undo_n = 0
            return (yield from self._suppression_test(progress, cancelled))
        finally:
            _experiments_end(exp)
            self._show_display()
            _mem_log('item test times: total %.1f s, %d suppress/restore calls %.1f s, reading states %.1f s, '
                     'state items %d (skipped %d), combined steps %d, undo restores %d (failed %d)' %
                     (time.perf_counter() - t0, self.n_compute, self.t_compute, self.t_state,
                      getattr(self, 'n_state_items', 0), getattr(self, 'n_state_skipped', 0),
                      getattr(self, 'swapped', 0), getattr(self, 'n_undo', 0), getattr(self, 'n_undo_failed', 0)))

    def group_suppression_test(self, progress, cancelled):
        self._hide_display()
        self._mr_base = _process_memory()
        exp = _experiments_begin()
        try:
            self._undo_n = 0
            return (yield from self._group_suppression_test(progress, cancelled))
        finally:
            _experiments_end(exp)
            self._show_display()

    # ------------------------------------------- proven-tail timeline frontier ---
    SUPPRESSION_FRONTIER_ENABLED = True
    # Timeline walk in the item and group tests: suppress with the marker right after the tested item(s), move it
    # forward until the rest is proven (or the end), put back through the marker. False: the plain way - suppress
    # and put back with the marker at the end of the timeline (slower, same results).
    TIMELINE_WALK = True
    UPPER_BOUNDS = True     # test blocks of neighbouring items first; their result bounds each item's walk
    BLOCK_SIZE = 4          # items per block
    MARKER_PAUSE = 0.02     # seconds Fusion gets to redraw after every marker move during the tests
    UI_PAUSE = 0.03         # seconds Fusion gets for clicks and redraws after every suppress / switch back on

    def _set_test_marker(self, marker):
        """Put the timeline marker so that items [0, marker) are computed; returns the marker Fusion reports."""
        tl = self.tl
        n = _safe(lambda: tl.count, 0) or 0
        if marker >= n:
            _safe(lambda: tl.moveToEnd())
        else:
            if not _safe(lambda: setattr(tl, 'markerPosition', int(marker)) or True, False):
                _safe(lambda: tl.item(int(marker)).rollTo(True))
        _breathe(self.MARKER_PAUSE)      # let Fusion redraw the timeline, so the marker is seen moving
        return _safe(lambda: tl.markerPosition)

    def _frontier_items(self, marker, orig):
        """The active items at or after `marker`: the tail a certificate says is suppressed anyway."""
        return set(j for j in orig if j >= marker and not orig[j])

    def _record_frontier_certificate(self, i, casc, certificates, active_indices):
        """If suppressing item i left every active item from some index M to the end suppressed, remember M as
        i's certificate: whenever i is suppressed, that whole tail is off. Only a contiguous run up to the end
        counts; one active item left on stops it. Returns M, or None when there is no such tail."""
        cs = set(casc)
        m = None
        for j in reversed(active_indices):
            if j <= i:
                break
            if j in cs:
                m = j
            else:
                break
        if m is None:
            certificates.pop(i, None)
            return None
        certificates[i] = m
        return m

    def _frontier_for(self, i, certificates, observed_by_index):
        """Where the marker may go for testing item i. A certified item A (tested earlier, after i) whose whole
        tail from cert[A] is suppressed with it can cut the test short at cert[A] - but only if this test then
        shows A itself suppressed (checked by the caller; otherwise the test is repeated at the end). A is only
        tried when the reference scan or earlier results link i to A, so the guess is usually right; a wrong guess
        costs a repeat, never a wrong result. Returns the marker (timeline count = no shortening)."""
        self._frontier_src = getattr(self, '_frontier_src', {})
        self._frontier_src.pop(i, None)
        n = self.tl.count
        if not certificates or not getattr(self, 'SUPPRESSION_FRONTIER_ENABLED', True):
            return n
        if getattr(self, '_kids_cache', None) is None:
            n2i = {v: k for k, v in self.tl2node.items()}
            kids = {}
            for (a, b), k in self.edges.items():
                if k - {'order'} and a in n2i and b in n2i and n2i[b] > n2i[a]:
                    kids.setdefault(n2i[a], set()).add(n2i[b])
            self._kids_cache = kids
        kids = self._kids_cache
        seen, stack = set(), [i]
        while stack:
            x = stack.pop()
            for y in set(kids.get(x, ())) | set(observed_by_index.get(x, ())):
                if y not in seen and y > i:
                    seen.add(y)
                    stack.append(y)
        best, src = n, None
        for a in seen:
            m = certificates.get(a)
            if m is not None and a < m < best:
                best, src = m, a
        if src is None:
            return n
        self._frontier_src[i] = src
        return best


    def _suppression_test(self, progress, cancelled):
        """Suppress each item and record what Fusion suppresses, breaks or warns about with it - exactly what a
        test with the marker at the end shows, computed with less work:

        - The marker is put right after the tested item(s) before suppressing, so the suppression itself costs
          nothing; then the marker moves forward a few items at a time and Fusion computes only those.
        - Items are tested from the back of the timeline. Every finished test is a proof of which items a
          suppressed item takes down with it. When the items found switched off so far are known (from their own
          tests) to take down every remaining active item after the marker, the rest is proven and the marker
          does not go further. A proof that does not hold in the part computed (an item that should be off is
          on) switches this shortcut off for that test, which then runs to the end.
        - Putting an item back: the marker goes back right after it, the item is switched on, and the marker
          goes to the end - the design is then the original one again and Fusion reuses the result it already
          has instead of recomputing (measured: ~0.1 s instead of ~11 s).
"""
        tl = self.tl
        orig = {}
        for i in range(tl.count):
            it = tl.item(i)
            if not it.isGroup:
                orig[i] = it.isSuppressed
        vol0 = self.body_signature()
        desc, brk, fails, wrn = {}, {}, {}, {}
        ERR = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
        WARN = adsk.fusion.FeatureHealthStates.WarningFeatureHealthState
        st0 = self._state(orig)
        err0 = set(i for i in orig if st0[i][1] == ERR)
        warn0 = set(i for i in orig if st0[i][1] == WARN)
        items = [i for i in orig if not orig[i]]
        active = sorted(items)
        total = len(items)
        done = [0]
        known = {}              # tested item -> every active item suppressed together with it (complete, proven)
        self._heavy = set()
        stats = {'tests': 0, 'stopped_early': 0, 'items_not_computed': 0, 'proof_mismatch': 0, 'blocks': 0,
                 'bound_mismatch': 0, 'computed': 0,
                 'secs_marker0': 0.0, 'secs_suppress': 0.0, 'secs_walk': 0.0, 'secs_back': 0.0,
                 'undo_tries': 0, 'undo_ok': 0, 'secs_back_undo': 0.0}
        self._defer_on = bool(_settings().get('experimentDeferCompute'))
        self._undo_put_back = bool(_settings().get('experimentUndoPutBack'))
        last = {'t0': time.perf_counter()}      # when the latest test started

        def marker_to(m):
            self._set_test_marker(m)

        def probe(S, bound=None):
            """Suppress the items S, move forward until the rest is proven or the end is reached.
            `bound`: the items that can react at all (from a test of a block containing S); the others are proven
            unaffected and never need computing. Returns (suppressed ok, fail message, casc, broke, warned)."""
            S = sorted(S)
            Sset = set(S)
            free = (lambda j: False) if bound is None else (lambda j: j not in bound)
            # the marker goes straight to the first place the walk could stop (worked out from the known tails),
            # not right after the item: Fusion then computes up to there in one go
            pos = (self._next_marker(S[-1] + 1, Sset, active, known, tl.count, free=free) if self.TIMELINE_WALK
                   else tl.count)
            ts = time.perf_counter()
            last['t0'] = ts
            deferred = self._defer_begin()
            marker_to(pos)
            fail_msg = None
            t0 = time.perf_counter()
            stats['secs_marker0'] += t0 - ts
            try:
                self._set_suppressed([tl.item(i) for i in S], True)
            except Exception as ex:
                fail_msg = str(ex)
            fail_msg = self._defer_end(deferred) or fail_msg
            t1 = time.perf_counter()
            stats['secs_suppress'] += t1 - t0
            self._note_cost(S[-1] + 1, pos, t1 - (ts if deferred else t0))
            st = self._state(S)
            if not all(st[i][0] for i in S):
                return False, fail_msg, [], [], []
            stats['tests'] += 1
            casc, broke, warned = [], [], []
            covered = set()
            use_proof = True
            # items between the first and last of S are already computed
            start = S[0] + 1
            n = tl.count
            # an item no finished test ever took down can never be proven off, so the walk cannot stop before it:
            # go straight past the last such item in one move instead of stepping to it
            if self.TIMELINE_WALK:
                pos = self._jump_past_unprovable(pos, Sset, active, known, free)
            while True:
                seg = [j for j in active if start <= j < pos and j not in Sset]
                if seg:
                    sts = self._state(seg)
                    for j in seg:
                        sup, h = sts[j]
                        if bound is not None and free(j) and (sup or (h == ERR and j not in err0) or
                                                              (h == WARN and j not in warn0)):
                            # an item the block test proved unaffected reacts: do not rely on the bound
                            bound = None
                            free = lambda j: False
                            stats['bound_mismatch'] += 1
                        if sup:
                            casc.append(j)
                            if j in known:
                                covered |= known[j]
                        elif use_proof and j in covered:
                            # a proven item is not off: do not rely on proofs in this test
                            use_proof = False
                            stats['proof_mismatch'] += 1
                        elif h == ERR and j not in err0:
                            broke.append(j)
                        elif h == WARN and j not in warn0:
                            warned.append(j)
                rest = [j for j in active if j >= pos and j not in Sset]
                if not rest:
                    break
                if (use_proof or bound is not None) and all((use_proof and j in covered) or free(j) for j in rest):
                    casc.extend(j for j in rest if use_proof and j in covered)
                    stats['stopped_early'] += 1
                    stats['items_not_computed'] += len(rest)
                    break
                start = pos
                pos = self._next_marker(pos, Sset, active, known, n, covered, use_proof, free)
                t0 = time.perf_counter()
                marker_to(pos)
                t1 = time.perf_counter()
                stats['secs_walk'] += t1 - t0
                self._note_cost(start, pos, t1 - t0)
            stats['computed'] += self._work(S[0], min(pos, n))
            return True, fail_msg, casc, broke, warned

        def put_back(S, what):
            """Marker right after the first item of S, switch S back on, marker to the end: the original design
            again, which Fusion does not recompute. Checked; the usual restore if anything differs. Then, when
            Fusion has grown too much, a hidden copy is reopened (see _memory_refresh)."""
            nonlocal tl
            ok = False
            if self._undo_put_back and time.perf_counter() - last['t0'] >= self.UNDO_PUT_BACK_MIN:
                t0 = time.perf_counter()
                stats['undo_tries'] += 1
                ok = yield from self._undo_back(orig, err0)
                if ok:
                    stats['undo_ok'] += 1
                    stats['secs_back_undo'] += time.perf_counter() - t0
                    self._undo_n = 0
            if not ok:
                ok = put_back_now(S, what)
            if ok and self._memory_refresh(orig, err0):
                tl = self.tl
            return ok

        def put_back_now(S, what):
            S = sorted(S)
            ts = time.perf_counter()
            deferred = self._defer_begin()
            # a test that ran to the end: switched back on right there (Fusion recognises the original design
            # and reuses its result, measured). One that stopped early: through the marker right after S.
            at_end = (_safe(lambda: tl.markerPosition, -1) or 0) >= tl.count
            if self.TIMELINE_WALK and not at_end:
                marker_to(S[0] + 1)
            try:
                self._set_suppressed([tl.item(i) for i in S], False)
            except Exception:
                pass
            _safe(lambda: tl.moveToEnd())
            self._defer_end(deferred)
            stats['secs_back'] += time.perf_counter() - ts
            if self._clean(orig, err0):
                self._undo_n = 0
                return True
            return self._restore_checked(orig, err0, None, what)

        def record(i, casc, broke, warned):
            known[i] = set(casc)
            nid = self.tl2node.get(i)
            if nid:
                desc[nid] = [self.tl2node[j] for j in casc if j in self.tl2node]
                brk[nid] = [self.tl2node[j] for j in broke if j in self.tl2node]
                wrn[nid] = [self.tl2node[j] for j in warned if j in self.tl2node]

        batch_hits = 0
        # from the back: later items are tested first, so their results are there for the earlier ones
        rest = sorted(items, reverse=True)

        # --- blocks: a run of neighbouring items suppressed together first. Whatever one item makes react is
        # always within what its whole block makes react (suppressing less cannot take more down), so the items
        # outside the block's reaction are proven unaffected for every item of the block and are never computed
        # in their tests - a heavy feature the block does not reach is computed once, not once per item.
        bound_of = {}
        if self.UPPER_BOUNDS and self.TIMELINE_WALK and len(rest) > 2:
            blocks, cur = [], []
            for i in rest:              # rest is back to front
                cur.append(i)
                if len(cur) >= self.BLOCK_SIZE:
                    blocks.append(cur)
                    cur = []
            if len(cur) > 1:
                blocks.append(cur)
            block_of = {}
            for b in blocks:
                for i in b:
                    block_of[i] = b
            self._block_of = block_of
        else:
            self._block_of = {}

        useless = [0]

        def block_bound(i):
            if False:
                yield
            b = self._block_of.get(i)
            if b is None:
                return None
            key = b[0]
            # only worth a test of its own when something slow to compute comes after the block that, going by
            # the references read, the block does not lead to (the test itself decides; this only picks blocks)
            if key not in bound_of and useless[0] >= 3:
                return None
            if key not in bound_of:
                reach = self._static_reach(b)
                if not any(h > max(b) and h not in reach for h in self._heavy):
                    return None
            if key not in bound_of:
                bound_of[key] = None
                if not cancelled():
                    progress('%d items at once' % len(b), done[0], total)
                    ok, _m, bc, bb, bw = probe(b)
                    if ok:
                        stats['blocks'] += 1
                        bound_of[key] = set(b) | set(bc) | set(bb) | set(bw)
                        # useful only if it keeps some slow item out; after a few that do not, stop trying
                        if any(h > max(b) and h not in bound_of[key] for h in self._heavy):
                            useless[0] = 0
                        else:
                            useless[0] += 1
                    yield from put_back(b, '%d items' % len(b))
            return bound_of[key]

        # --- every item on its own
        for i in rest:
            if cancelled():
                self.warnings.append('Suppression test was cancelled; results are partial.')
                break
            bnd = yield from block_bound(i)
            it = tl.item(i)
            progress(it.name, done[0], total)
            done[0] += 1
            ok, fail_msg, casc, broke, warned = probe([i], bnd)
            if not ok:
                nid = self.tl2node.get(i)
                if nid:
                    fails[nid] = self._parse_fail(fail_msg)
                yield from put_back([i], it.name)
                continue
            record(i, casc, broke, warned)
            yield from put_back([i], _safe(lambda: it.name, ''))
        _safe(lambda: tl.moveToEnd())
        self.batched = batch_hits
        vol1 = self.body_signature()
        if vol0 != vol1:
            self.warnings.append('Warning: after the suppression test the bodies differ from before '
                                 '(%s vs %s). Check the design, or revert to the saved version.' % (vol0, vol1))
        _mem_log('item test: %d runs, %d stopped early (proven), %d items not computed, %d proof mismatches, '
                 '%d direct jumps past unprovable items, %d stops chosen from known tails, %d blocks tested '
                 '(%d bound mismatches)' %
                 (stats['tests'], stats['stopped_early'], stats['items_not_computed'], stats['proof_mismatch'],
                  getattr(self, 'n_jumps', 0), getattr(self, 'n_cert_jumps', 0), stats['blocks'], stats['bound_mismatch']))
        _mem_log('item test time: first marker move %.1f s, suppress %.1f s, walk forward %.1f s, put back %.1f s%s%s; '
                 'slowest items: %s' % (stats['secs_marker0'], stats['secs_suppress'], stats['secs_walk'],
                                        stats['secs_back'], ' (deferred compute)' if self._defer_on else '',
                                        (', Undo put back %d of %d tries, %.1f s' % (
                                            stats['undo_ok'], stats['undo_tries'], stats['secs_back_undo']))
                                        if self._undo_put_back else '',
                                        self._top_costs()))
        self.test_stats = stats
        self.item_proofs = known
        if False:
            yield           # a generator like the other steps of the run

        anc = {}
        for s_, ds in desc.items():
            for d in ds:
                anc.setdefault(d, set()).add(s_)
        for d, ancs in anc.items():
            for s_ in ancs:
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
        t0 = time.perf_counter()
        try:
            return self._state_now(idxs)
        finally:
            self.t_state = getattr(self, 't_state', 0.0) + time.perf_counter() - t0

    def _state_now(self, idxs):
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

    UNDO_RESTORE = False      # superseded by the marker put-back (faster, no waiting for Fusion)
    UNDO_MAX_STEPS = 12

    def _undo_enabled(self):
        return self.UNDO_RESTORE and not getattr(self, '_undo_off', False)

    def _flags_back(self, orig):
        st = self._state(orig)
        return all(st[i][0] == orig[i] for i in orig)

    def _undo_restore(self, orig, err0, tested=None, what=''):
        """Put the design back with Fusion's Undo instead of switching items back on: Undo brings back the model
        Fusion kept from before the change instead of recomputing it (measured ~2 s instead of ~12 s). Undo runs
        only after the add-in hands control back to Fusion, so this is a generator: it yields until Fusion has
        run it. The result is checked item by item; if Undo did not bring back exactly the original state (or
        cannot be used), the usual restore runs, and after two failed Undos it is not tried again in this design."""
        n = getattr(self, '_undo_n', None)
        if self._undo_enabled() and isinstance(n, int) and 0 < n <= self.UNDO_MAX_STEPS:
            app = adsk.core.Application.get()
            # Undo works on the active tab: make sure it is this design's
            mydoc = _safe(lambda: self.des.parentDocument)
            if mydoc is not None and _safe(lambda: app.activeDocument != mydoc, False):
                _safe(mydoc.activate)
            cd = _safe(lambda: app.userInterface.commandDefinitions.itemById('UndoCommand'))
            if mydoc is not None and _safe(lambda: app.activeDocument != mydoc, False):
                cd = None
            queued = 0
            for _ in range(n):
                if cd is not None and _safe(lambda: cd.execute() or True, False):
                    queued += 1
            if queued == n:
                for _ in range(60):
                    yield 'undo'
                    if self._flags_back(orig):
                        break
                if self._flags_back(orig):
                    _safe(lambda: self.tl.moveToEnd())
                    if self._clean(orig, err0):
                        self.n_undo = getattr(self, 'n_undo', 0) + 1
                        self._undo_n = 0
                        return True
            self.n_undo_failed = getattr(self, 'n_undo_failed', 0) + 1
            if self.n_undo_failed >= 2:
                self._undo_off = True
        ok = self._restore_checked(orig, err0, tested, what)
        return ok

    MEM_REFRESH_GB = 4      # a hidden copy of a linked design is reopened when Fusion has grown by this much

    def _memory_refresh(self, orig, err0):
        """Between two tests (the design is in its original state): when Fusion has grown by MEM_REFRESH_GB
        since this design's tests started, close the hidden copy of the linked design and open the same saved
        version again. Fusion keeps every test step's model data as undo history until the document is closed
        (measured ~110 MB per recompute of a heavy part); closing gives it back. Only for copies the add-in
        opened itself (closed without saving anyway), never the design you have open. Returns True when the
        design was reopened: self.des / self.tl are then new objects."""
        hd = getattr(self, 'hidden_doc', None)
        limit = _settings().get('memoryRefreshGB', self.MEM_REFRESH_GB)
        if hd is None or not limit:
            return False
        now = time.time()
        if now - getattr(self, '_mr_t', 0) < 5:
            return False
        self._mr_t = now
        m = _process_memory()
        if m is None:
            return False
        # freeing after a close goes on for a while: the lowest reading is the baseline
        base = getattr(self, '_mr_base', None)
        self._mr_base = m if base is None else min(base, m)
        if base is None or m - self._mr_base < limit * 1024 ** 3:
            return False
        app = adsk.core.Application.get()
        name = _safe(lambda: hd.name, '?')
        df = _safe(lambda: hd.dataFile)
        fid, ver = _safe(lambda: df.id), _safe(lambda: df.versionNumber)
        if df is None or not fid:
            return False
        try:
            hd.close(False)
        except Exception:
            return False
        self.hidden_doc = None
        gc.collect()
        try:
            nd = app.documents.open(df, True)
        except Exception as ex:
            raise RuntimeError('could not reopen %s to free memory: %s' % (name, ex))
        _safe(nd.activate)
        adsk.doEvents()
        got = _safe(lambda: nd.dataFile)
        if _safe(lambda: got.id) != fid or (ver is not None and _safe(lambda: got.versionNumber) != ver):
            _safe(lambda: nd.close(False))
            raise RuntimeError('reopening %s to free memory opened a different file or version' % name)
        des = adsk.fusion.Design.cast(nd.products.itemByProductType('DesignProductType'))
        self.hidden_doc = nd
        self.des, self.root, self.tl = des, des.rootComponent, des.timeline
        self.expand_groups()
        self._display_saved = []
        self._hide_display()
        _safe(lambda: self.tl.moveToEnd())
        if not self._clean(orig, err0):
            raise RuntimeError('%s reopened to free memory is not in the state it was tested in' % name)
        self.n_refresh = getattr(self, 'n_refresh', 0) + 1
        after = _process_memory()
        _mem_log('reopened %s to free memory: %.1f GB -> %s' % (
            name, m / 1024 ** 3, '%.1f GB' % (after / 1024 ** 3) if after else '?'))
        self._mr_base = after or m
        return True

    UNDO_PUT_BACK_MIN = 3.0     # seconds a test took before its put-back is tried with Undo
    UNDO_PUT_BACK_STEPS = 12    # Undo steps tried at most

    def _undo_back(self, orig, err0):
        """Experiment (setting experimentUndoPutBack): put the design back with Fusion's Undo, one step at a time,
        until every item has its original suppression state, then the marker to the end. Undo brings back the
        model Fusion kept from before the change, so heavy features the test suppressed or recomputed need not be
        computed again. Undo runs only after the add-in hands control back to Fusion, so this is a generator.
        Returns True when the design is back in its original state; otherwise the caller puts it back the usual
        way from wherever Undo left it."""
        app = adsk.core.Application.get()
        mydoc = _safe(lambda: self.des.parentDocument)
        if mydoc is not None and not _safe(lambda: app.activeDocument == mydoc, False):
            _safe(mydoc.activate)
            adsk.doEvents()
        if mydoc is None or not _safe(lambda: app.activeDocument == mydoc, False):
            return False            # Undo works on the active tab only
        cd = _safe(lambda: app.userInterface.commandDefinitions.itemById('UndoCommand'))
        if cd is None:
            return False
        tl = self.tl
        idx = sorted(orig)

        def snap():
            st = self._state(idx)
            return _safe(lambda: tl.markerPosition), tuple(st[i][0] for i in idx)

        for _ in range(self.UNDO_PUT_BACK_STEPS):
            before = snap()
            if not _safe(lambda: cd.execute() or True, False):
                return False
            for _w in range(20):
                yield 'undo'
                if snap() != before:
                    break
            else:
                return False        # nothing changed: nothing left to undo, or Undo did not run
            if self._flags_back(orig):
                break
        else:
            return False
        _safe(lambda: tl.moveToEnd())
        return self._clean(orig, err0)

    def _restore_checked(self, orig, err0, tested=None, what=''):
        r = self._restore_checked_now(orig, err0, tested, what)
        self._undo_n = 0 if r else None
        return r

    def _restore_checked_now(self, orig, err0, tested=None, what=''):
        self._restore(orig, tested)
        if self._clean(orig, err0):
            return True
        self._restore(orig)
        if self._clean(orig, err0):
            return True
        # the design is never closed and reopened while it is worked on: a third, slower try item by item
        tl = self.tl
        for i in sorted(orig):
            it = _safe(lambda: tl.item(i))
            if it is not None and _safe(lambda: it.isSuppressed, None) != orig[i]:
                _safe(lambda: self._set_suppressed([it], orig[i]))
        _safe(lambda: tl.moveToEnd())
        if self._clean(orig, err0):
            return True
        self.recovered = getattr(self, 'recovered', 0) + 1
        self.warnings.append('Could not put the design back after testing %s; later results may be wrong.' % what)
        return False


    def _static_reach(self, idxs):
        """Timeline items the reference scan links (directly or through others) from the given items."""
        if getattr(self, '_kids_idx', None) is None:
            n2i = {v: k for k, v in self.tl2node.items()}
            kids = {}
            for (x, y), k in self.edges.items():
                if k - {'order'} and x in n2i and y in n2i:
                    kids.setdefault(n2i[x], set()).add(n2i[y])
            self._kids_idx = kids
        seen, stack = set(), list(idxs)
        while stack:
            x = stack.pop()
            for y in self._kids_idx.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen

    HEAVY_SECONDS = 2.0     # a stretch of the timeline that takes this long to compute counts as heavy

    def _defer_begin(self):
        """Experiment (setting experimentDeferCompute): the steps of one test move (marker, then suppress; or marker
        back, switch on, marker to the end) are made with Design.isComputeDeferred on, so Fusion computes once
        when it is switched off again instead of after every step. Returns True when deferred."""
        if not getattr(self, '_defer_on', False):
            return False
        return _safe(lambda: setattr(self.des, 'isComputeDeferred', True) or True, False)

    def _defer_end(self, deferred):
        """Computes what was deferred; returns the error Fusion reports (a later feature failing), or None."""
        if not deferred:
            return None
        try:
            self.des.isComputeDeferred = False
        except Exception as ex:
            _safe(lambda: setattr(self.des, 'isComputeDeferred', False))
            return str(ex)
        return None

    def _top_costs(self, k=5):
        """The slowest items to compute seen in the tests, for the run log."""
        est = getattr(self, '_cost_est', None) or {}
        top = sorted(est.items(), key=lambda x: -x[1])[:k]
        return ', '.join('%s %.1f s' % (_safe(lambda: self.tl.item(j).name, '#%d' % j), v) for j, v in top) or 'none'

    def _note_cost(self, a, b, seconds):
        """Remember the items of a stretch [a, b) Fusion took long to compute (a block test is only done in
        front of such items)."""
        seconds = self._measure(a, b, seconds)
        items = [j for j in range(a, b) if j in self.tl2node]
        if not items:
            return
        # the stretch's time is split over its items; an item's estimate is the smallest seen (a stretch that
        # holds one slow item makes all of them look slow once, a later shorter stretch corrects it)
        est = self.__dict__.setdefault('_cost_est', {})
        per = seconds / len(items)
        for j in items:
            est[j] = min(est.get(j, per), per) if len(items) > 1 else per
        self._heavy = set(j for j, v in est.items() if v >= self.HEAVY_SECONDS)

    def _measure(self, a, b, seconds):
        return seconds

    def _work(self, a, b):
        """Diagnostic: how much was computed between two timeline positions (items after a, before b)."""
        return max(0, b - a - 1)

    def _jump_past_unprovable(self, pos, S, active, known, free=None):
        """The walk forward can only stop where every remaining item is covered by some finished test. An item
        that is in no test's results can never be covered, so stepping before it is wasted: the marker goes
        straight past the last such item (to the end when it is the last item). Returns the new marker."""
        union = set()
        for v in known.values():
            union |= v
        free = free or (lambda j: False)
        blockers = [j for j in active if j >= pos and j not in S and j not in union and not free(j)]
        if not blockers:
            return pos
        target = max(blockers) + 1
        if target > pos:
            self.n_jumps = getattr(self, 'n_jumps', 0) + 1
            self._set_test_marker(target)
            return target
        return pos

    def _next_marker(self, pos, S, active, known, n, covered=frozenset(), use_proof=True, free=None):
        """Where the marker goes next in the walk: the next position where stopping is still possible, using
        every finished test (all known tails). Stopping right after position p needs every active item from p on
        to be proven off - by the items already found off (`covered`) or by items before p whose finished tests
        take it down (they may still turn out off when computed). Positions where that cannot hold even in the
        best case are skipped, so the marker moves from one possible stop to the next and computes nothing a
        finer step would have avoided. Without any possible stop it goes straight to the end."""
        free = free or (lambda j: False)
        rest = [j for j in active if j >= pos and j not in S and not free(j)]
        if not rest:
            return pos if pos >= n else min(n, pos)
        if not use_proof:
            return min(n, rest[-1] + 1)
        u = set(covered)
        for k, c in enumerate(rest):
            if c in known:
                u |= known[c]
            if all(j in u for j in rest[k + 1:]):
                p = min(n, c + 1)
                if pos < p < n:
                    self.n_cert_jumps = getattr(self, 'n_cert_jumps', 0) + 1
                return p
        return min(n, rest[-1] + 1)

    def _walk_forward(self, orig, S, err0, warn0, start, pos):
        """With the items S suppressed and the marker at `pos`, read what Fusion computed and move the marker
        forward from one possible stop to the next (see _next_marker), until the end or until every remaining active item is proven off by the
        item test's results for the items found off (self.item_proofs). Returns (casc, broke, warned)."""
        tl = self.tl
        ERR = adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
        WARN = adsk.fusion.FeatureHealthStates.WarningFeatureHealthState
        known = getattr(self, 'item_proofs', None) or {}
        active = sorted(i for i in orig if not orig[i])
        casc, broke, warned = [], [], []
        covered, use_proof = set(), True
        n = tl.count
        if self.TIMELINE_WALK:
            pos = self._jump_past_unprovable(pos, S, active, known)
        while True:
            seg = [j for j in active if start <= j < pos and j not in S]
            if seg:
                sts = self._state(seg)
                for j in seg:
                    sup, h = sts[j]
                    if sup:
                        casc.append(j)
                        if j in known:
                            covered |= known[j]
                    elif use_proof and j in covered:
                        use_proof = False
                    elif h == ERR and j not in err0:
                        broke.append(j)
                    elif h == WARN and j not in warn0:
                        warned.append(j)
            rest = [j for j in active if j >= pos and j not in S]
            if not rest:
                break
            if use_proof and all(j in covered for j in rest):
                casc.extend(rest)
                self.g_stopped_early = getattr(self, 'g_stopped_early', 0) + 1
                break
            start = pos
            pos = self._next_marker(pos, S, active, known, n, covered, use_proof)
            self._set_test_marker(pos)
        return casc, broke, warned

    def _group_suppression_test(self, progress, cancelled):
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
            # the marker at the first place the walk could stop (known tails of the item test), then forward
            active_g = sorted(i for i in orig if not orig[i])
            gpos = (self._next_marker(max(inside) + 1, set(inside), active_g, getattr(self, 'item_proofs', None) or {},
                                      tl.count) if self.TIMELINE_WALK else tl.count)
            gt0 = time.perf_counter()
            self._set_test_marker(gpos)
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
            casc, broke, warned = self._walk_forward(orig, set(inside), err0, warn0, min(inside) + 1, gpos)
            if gid in byg:
                byg[gid]['dsupp'] = [self.tl2node[i] for i in casc if i in self.tl2node]
                byg[gid]['dbreak'] = [self.tl2node[i] for i in broke if i in self.tl2node]
                if warned:
                    byg[gid]['dwarn'] = [self.tl2node[i] for i in warned if i in self.tl2node]
            gname = _safe(lambda: g.name, gid)
            if (_settings().get('experimentUndoPutBack') and
                    time.perf_counter() - gt0 >= self.UNDO_PUT_BACK_MIN):
                t0u = time.perf_counter()
                if (yield from self._undo_back(orig, err0)):
                    self.g_undo = getattr(self, 'g_undo', 0) + 1
                    self.g_undo_secs = getattr(self, 'g_undo_secs', 0.0) + time.perf_counter() - t0u
                    self._memory_refresh(orig, err0)
                    if self.tl is not tl:
                        tl = self.tl
                        tgroups = _safe(lambda: list(tl.timelineGroups)) or tgroups
                    continue
            # put back with the marker right after the group's first item: the design is the original one again
            # and Fusion reuses its result instead of recomputing everything after the group
            first = min(inside) if inside else None
            if first is not None and self.TIMELINE_WALK and (_safe(lambda: tl.markerPosition, -1) or 0) < tl.count:
                self._set_test_marker(first + 1)
            _safe(lambda: self._set_suppressed([g], False))
            _safe(lambda: tl.moveToEnd())
            if self._clean(orig, err0) or self._restore_checked(orig, err0, None, gname):
                self._memory_refresh(orig, err0)
            if self.tl is not tl:
                tl = self.tl
                tgroups = _safe(lambda: list(tl.timelineGroups)) or tgroups
        self.gtested = True
        if getattr(self, 'g_undo', 0):
            _mem_log('group test: Undo put back %d groups, %.1f s' % (self.g_undo, getattr(self, 'g_undo_secs', 0.0)))
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


def _file_location(dfile):
    """'Project / folder / subfolder' of a cloud file, to tell apart files with the same name."""
    parts = []
    f = _safe(lambda: dfile.parentFolder)
    while f is not None:
        nm = _safe(lambda: f.name)
        if nm and not _safe(lambda: f.isRoot, False):
            parts.append(nm)
        f = _safe(lambda: f.parentFolder)
    proj = _safe(lambda: dfile.parentProject.name)
    return ' / '.join(([proj] if proj else []) + list(reversed(parts)))


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
    """Memory Fusion uses now (footprint / private bytes); None when it cannot be read."""
    try:
        if sys.platform == 'darwin':
            # the footprint (what Activity Monitor shows): the resident set leaves out memory macOS has
            # compressed or swapped out, which is most of it once Fusion grows large
            try:
                import ctypes

                class RI(ctypes.Structure):
                    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
                        'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins', 'wired_size',
                        'resident_size', 'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime')]
                ri = RI()
                if ctypes.CDLL('/usr/lib/libproc.dylib').proc_pid_rusage(os.getpid(), 0, ctypes.byref(ri)) == 0:
                    return int(ri.phys_footprint)
            except Exception:
                pass
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
                return int(c.PagefileUsage)      # private memory, also what is paged out
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


_mem_last = [0.0]


def _mem_tick(what, every=30.0):
    """A memory line in the run log at most every `every` seconds during the long steps, to see which of them
    makes Fusion grow. `what` is a function giving the text, only called when a line is written."""
    now = time.time()
    if now - _mem_last[0] >= every:
        _mem_last[0] = now
        _mem_log(_safe(what, '?'))


# ------------------------------------------------------------ result cache ---
# A saved version of a design never changes, so what was read and tested in it can be kept and reused: a later run
# (or another assembly using the same part) takes it from here instead of opening and testing the design again.
CACHE_VERSION = 4      # 4: drops results tested with Fusion's transactions off (they found nothing)


def _cache_dir():
    """Kept outside the temporary folder (macOS clears files there it has not touched for a few days) and
    outside the add-in folder (replaced when a new version is copied in)."""
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    elif sys.platform.startswith('win'):
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    d = os.path.join(base, 'FusionDependenciesGraph', 'cache')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
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


def _breathe(seconds=0.15):
    """Let Fusion handle clicks, scrolling and redraws for a moment between two recomputes; a recompute itself
    cannot be interrupted, so this is where the UI gets its turn."""
    end = time.perf_counter() + seconds
    while True:
        adsk.doEvents()
        if time.perf_counter() >= end:
            break
        time.sleep(0.01)


def _probe_text_commands():
    """Once per Fusion session: note Fusion's text commands about memory, caches or undo in the run log (to find
    one that gives memory back)."""
    if globals().get('_probed'):
        return
    globals()['_probed'] = True
    try:
        out = adsk.core.Application.get().executeTextCommand('TextCommands.List /hidden') or ''
    except Exception as ex:
        out = ''
        _mem_log('text commands: could not list (%s)' % ex)
    hits = [l.strip() for l in out.splitlines() if re.search(r'mem|cache|purge|undo|flush|garbage|release|trim', l, re.I)]
    try:
        with open(os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', 'text_commands.txt'), 'w') as f:
            f.write(out)
    except Exception:
        pass
    _mem_log('text commands about memory: ' + ('; '.join(hits[:60]) if hits else 'none found'))


_TX_FLAG = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', 'transactions_off.flag')


def _tx_command(arg=''):
    """Fusion's text command for its transaction (undo) system; None when it cannot be run."""
    try:
        return adsk.core.Application.get().executeTextCommand(('Options.Transactions ' + arg).strip()) or ''
    except Exception as ex:
        _mem_log('Options.Transactions %s failed: %s' % (arg, ex))
        return None


def _tx_on(force=False):
    """Switches Fusion's transaction system back on. An earlier version of the add-in switched it off during the
    tests (to stop the undo history growing) - with it off, suppressing an item takes nothing down with it, so
    every test found nothing. force: only when that version left the flag file behind (stopped mid-test)."""
    if force and not os.path.exists(_TX_FLAG):
        return
    _tx_command('/on')
    _safe(lambda: os.remove(_TX_FLAG))
    _mem_log('undo recording switched back on (%s)' % ((_tx_command() or '?').strip()[:80]))


# Experiments (settings.json, off by default): Fusion background work switched off while the suppression tests
# run, and back on right after. Both are hidden Fusion text commands; keep one only if tools/compare_pages.py
# shows the same results with and without it (switching Options.Transactions off made every test find nothing).
EXPERIMENTS = (
    # setting key,              text command,                        off,     on
    ('experimentNoCrashRecovery', 'Options.CrashRecovery',           '/off',  '/on'),   # periodic crash-recovery autosave
    ('experimentNoBodyCache',     'DebugCommands.BodyCacheUpdateMgr', '/Off',  '/On'),   # background mass properties
)
_EXP_FLAG = os.path.join(tempfile.gettempdir(), 'FusionDependenciesGraph', 'experiments_on.json')


def _text_command(cmd):
    try:
        return adsk.core.Application.get().executeTextCommand(cmd) or ''
    except Exception as ex:
        _mem_log('%s failed: %s' % (cmd, ex))
        return None


def _experiments_begin():
    """Switches off what the experiment settings ask for; returns what to switch back on (see _experiments_end).
    Something Fusion already reports as off is left alone."""
    st = _settings()
    undo = []
    for key, cmd, off, on in EXPERIMENTS:
        if not st.get(key):
            continue
        before = _text_command(cmd)
        if before is None:
            continue
        if re.search(r'\boff\b', before, re.I) and not re.search(r'\bon\b', before, re.I):
            _mem_log('%s already off (%s): left as it is' % (cmd, before.strip()[:80]))
            continue
        if _text_command('%s %s' % (cmd, off)) is None:
            continue
        undo.append('%s %s' % (cmd, on))
        _mem_log('experiment: %s %s (was: %s)' % (cmd, off, before.strip()[:80] or '?'))
    if undo:
        # written down, so an interrupted run (Fusion or the add-in stopped mid-test) is undone at the next start
        try:
            with open(_EXP_FLAG, 'w', encoding='utf-8') as f:
                json.dump(undo, f)
        except Exception:
            pass
    return undo


def _experiments_end(undo=None):
    """Switches back on what _experiments_begin switched off; undo=None: what an interrupted run left behind."""
    if undo is None:
        try:
            with open(_EXP_FLAG, 'r', encoding='utf-8') as f:
                undo = json.load(f)
        except Exception:
            return
    for c in undo or []:
        _text_command(c)
        _mem_log('experiment ended: %s' % c)
    _safe(lambda: os.remove(_EXP_FLAG))


def _cache_save(kind, file_id, ver, d):
    if not file_id or ver is None:
        return
    st = _settings()
    if any(st.get(k) for k, *_ in EXPERIMENTS) or st.get('experimentDeferCompute') or st.get('experimentUndoPutBack'):
        return          # an experiment is on: its results are not trusted until compared, so never reused
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


def _collect_derived(main, progress, cancelled, exact=False, groups_test=False, pictures=False, max_designs=80, plan=None,
                     skipped_groups=False):
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
        return ['Reading'] + (['Item test'] if exact and testing else []) + (['Group test'] if groups_test and testing else [])

    def entry_for(sd, depth):
        """The queue entry for a linked design (one per file), created when first seen."""
        ref_doc = _safe(lambda: sd.parentDocument)
        name = _safe(lambda: ref_doc.name) or 'Linked design'
        dfile = _safe(lambda: ref_doc.dataFile)
        fid = _safe(lambda: dfile.id)
        if not fid:
            # identified only by its file id: names repeat across folders and projects
            main.warnings.append('%s: its cloud file could not be identified, so it was left out.' % name)
            return None
        return add_entry(fid, name, dfile, _safe(lambda: dfile.versionNumber), depth)

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
        if not str(rec.get('key') or '').startswith('urn:'):
            return          # an old cache record keyed by name: never trusted (names repeat across folders)
        e = add_entry(rec['key'], rec['name'], rec.get('dfile'), rec['ver'], depth)
        if e is None:
            return
        if rec.get('loc'):
            e['loc'] = rec['loc']
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
            fid = _safe(lambda: dfile.id)
            if not fid:
                # identified only by its file id: names repeat across folders and projects
                main.warnings.append('%s: its cloud file could not be identified, so it was left out.' % name)
                return
            rec = {'key': fid, 'name': name, 'ver': _safe(lambda: dfile.versionNumber), 'loc': _file_location(dfile),
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
        """Opens the version the link uses in its own tab and switches to it, so you can see what is being worked
        on (the progress panel stays on top); the tab is closed when the design is done. mine=False: Fusion handed
        back a document the user has open."""
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
            doc = app.documents.open(target, True)
        except Exception:
            return None, False, None
        _safe(doc.activate)
        adsk.doEvents()
        mine = not any(_safe(lambda: d == doc, False) for d in before)
        # make sure Fusion handed back exactly that file and version (never another file with the same name)
        got = _safe(lambda: doc.dataFile)
        got_id, got_ver = _safe(lambda: got.id), _safe(lambda: got.versionNumber)
        if got_id != e['key'] or (e['read_ver'] is not None and got_ver is not None and got_ver != e['read_ver']
                                  and mine):
            if mine:
                _safe(lambda: doc.close(False))
            main.warnings.append('%s: Fusion opened a different file (%s v%s in %s) instead of the linked one '
                                 '(%s v%s in %s), so it was left out.' % (
                                     e['name'], _safe(lambda: got.name, '?'), got_ver, _file_location(got) or '?',
                                     e['name'], e['read_ver'], e.get('loc') or _file_location(dfile) or '?'))
            return None, False, None
        e['loc'] = e.get('loc') or _file_location(got)
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
        if back is not None and not _safe(lambda: back == doc, False):
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
        log('opened %s from %s (%s)' % (name, e.get('loc') or '?', e['key']))
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
                        _prow(e['key'], name, '%s: %s (%d/%d)' % ('Item test' if (exact and stage['k'] == 0) else 'Group test', msg, min(i + 1, n), n),
                              min(1.0, (i + 1) / max(1, n)), '', step_labels(True), 1 + stage['k'])
                        if progress:
                            progress('%s: %s' % (name, msg), i, n)
                    _safe(lambda: sc.tl.moveToEnd())
                    # items first: their proofs let the group test stop early
                    failed = False
                    if exact and not cancelled():
                        try:
                            yield from sc.suppression_test(prog, cancelled)
                        except Exception as ex:
                            failed = True
                            main.warnings.append('%s: the item test failed: %s' % (name, ex))
                    if exact:
                        stage['k'] = 1
                    if groups_test and not cancelled():
                        try:
                            yield from sc.group_suppression_test(prog, cancelled)
                        except Exception as ex:
                            failed = True
                            main.warnings.append('%s: the whole groups test failed: %s' % (name, ex))
                    tested = not cancelled() and not failed
            # kept for later runs: only a complete result from a hidden copy of the saved version
            if mine and not cancelled() and not (testing and not tested):
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
            yield from process(e, n_done)
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

    # designs with the same name in different folders: their frames say where each one is
    base = lambda nm: re.sub(r'\s+v\d+$', '', nm or '')
    main_name = base(_safe(lambda: adsk.core.Application.get().activeDocument.name, ''))
    counts = {}
    for e in sources:
        counts[base(e['name'])] = counts.get(base(e['name']), 0) + 1
    for e in sources:
        e['show_loc'] = bool((counts[base(e['name'])] > 1 or base(e['name']) == main_name) and e.get('loc'))

    # deepest sources first, then the main design (its items have o >= 0 and user parameters o = -1..)
    order = sorted(sources, key=lambda s: -s['depth'])
    for rank, src in enumerate(order):
        sc, sp, gid = src['col'], src['prefix'], src['gid']
        base = -1e6 + rank * 1e4
        if len(src['versions']) > 1:
            base_name = re.sub(r'\s+v\d+$', '', src['name'])
            src['name'] = '%s (v%s; read v%s)' % (base_name, ', v'.join(str(v) for v in sorted(src['versions'])), src['read_ver'])
            main.warnings.append('%s is linked at more than one version; it is shown once, read at v%s.' % (base_name, src['read_ver']))
        if src.get('show_loc'):
            src['name'] = '%s (%s)' % (src['name'], src['loc'])
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
            if skipped_groups and not getattr(sc, 'gtested', False):
                ng['gskip'] = True       # not tested on its own: the page adds up its items' results
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
_HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_HTML_PATH = os.path.join(_HERE, 'progress_panel.html')      # the panel's page
TEMPLATE_PATH = os.path.join(_HERE, 'page_template.html')         # the generated page, without its data


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
        old = _ui.palettes.itemById(PANEL_ID)
        if old:
            _safe(old.deleteMe)
        self.pal = _ui.palettes.add(PANEL_ID, 'Dependencies graph', pathlib.Path(PANEL_HTML_PATH).as_uri(), True, True,
                                    True, 380, 460)
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
    # the whole groups test of linked designs is optional (it takes about as long as their item test); without it
    # the page adds up the items' results for their groups (estimated)
    linked_groups = groups_test and bool(_settings().get('linkedGroupTest', False))
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
        skip_lg = bool(derived and groups_test and not linked_groups)
        kinds = [m_kind + ('s' if skip_lg else ''), 'main_11%s%s' % (int(bool(thumbs)), int(bool(derived)))]
        if skip_lg:
            # a result with the linked groups tested (more complete) also answers one without
            kinds = [kinds[0], m_kind, kinds[1] + 's', kinds[1]]
        cached = None
        if not _safe(lambda: _app.activeDocument.isModified, True):
            # a Full analysis result also answers a Quick estimate (it is exact)
            for kind in kinds:
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
        if exact:
            steps.append(['Every item test', n_tl * 1.7])
        if groups_test:
            steps.append(['Whole groups test', n_groups * 2.5])
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
            _safe(_probe_text_commands)
            set_step(0)
            col.build_nodes()
            col.scan()
            if thumbs and _has_geometry(col.des):
                col.part_pic = _part_picture()
            _safe(col.scan_components)
            col.scan_parameters()
            _mem_log('read done')
            k = 1
            # items first: their proofs let the group test stop early
            if exact and not cancelled():
                set_step(k); k += 1
                yield from col.suppression_test(progress, cancelled)
                _mem_log('item test done')
            if groups_test and not cancelled():
                set_step(k); k += 1
                yield from col.group_suppression_test(progress, cancelled)
                _mem_log('group test done')
            _prow('main', None, 'Cancelled' if cancelled() else 'Done', 1, 'fail' if cancelled() else 'done')
            if derived and not cancelled():
                # while the groups are still expanded: the derive features' timeline indexes are read from them
                cur['linked'] = True
                set_step(k)
                _safe(lambda: col.tl.moveToEnd())
                try:
                    def _derived_plan(di, dg, nd):
                        if exact or groups_test:
                            steps[k][1] = max(1.0, (di * 1.7 if exact else 0) + (dg * 2.5 if linked_groups else 0)
                                              + max(1, nd) * 2.0)
                        else:
                            steps[k][1] = max(1.0, di * 0.05 + dg * 0.1 + max(1, nd) * 1.0)
                        set_step(k)
                    yield from _collect_derived(col, progress, cancelled, exact, linked_groups, thumbs,
                                                plan=_derived_plan, skipped_groups=skip_lg)
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
            _cache_save(kinds[0], m_id, m_ver, {'data': data, 'exact': exact, 'groups': groups_test, 'pics': bool(thumbs)})
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
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        html = f.read()
    html = html.replace('/*__DATA__*/null', json.dumps(data).replace('</', '<\\/'))
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
            lg = ag.children.addBoolValueInput('hgLinkedGroups', 'Group test for linked designs', True, '',
                                               bool(_settings().get('linkedGroupTest', False)))
            lg.tooltip = 'Full analysis: also run the Whole groups test on every linked design'
            lg.tooltipDescription = ('Takes about as long again as their item test. Off: their items are still tested '
                                     'exactly; what suppressing one of their timeline groups does is added up from its '
                                     'items\' results (shown as estimated). This design\'s groups are always tested.')
            # experiments: Fusion background work off during the tests (see EXPERIMENTS)
            for key, iid, label, tip, desc in (
                    ('experimentNoCrashRecovery', 'hgExpCrash', 'Experiment: no autosave during tests',
                     'Switch off Fusion\'s crash-recovery autosave while the suppression tests run',
                     'Fusion saves open designs for crash recovery every few minutes; that can take a while on a large '
                     'assembly. Switched back on right after each test. Experimental: results of such a run are not '
                     'reused later; compare them with a normal run (tools/compare_pages.py) before relying on it.'),
                    ('experimentNoBodyCache', 'hgExpBody', 'Experiment: no background mass properties',
                     'Switch off Fusion\'s background mass-property calculation while the suppression tests run',
                     'After every recompute Fusion works out mass properties of changed bodies in the background. '
                     'Switched back on right after each test. Experimental: results of such a run are not reused '
                     'later; compare them with a normal run (tools/compare_pages.py) before relying on it.'),
                    ('experimentDeferCompute', 'hgExpDefer', 'Experiment: one recompute per test step',
                     'Item test: move the marker and suppress (or put back) with Fusion\'s compute deferred',
                     'Each test moves the timeline marker and then suppresses the item, and puts it back in three '
                     'steps; each step can make Fusion recompute. With compute deferred Fusion computes once per step '
                     'group. The run log shows the time of each part either way. Experimental: results of such a run '
                     'are not reused later; compare them with a normal run (tools/compare_pages.py).'),
                    ('experimentUndoPutBack', 'hgExpUndo', 'Experiment: put back with Undo after slow tests',
                     'After a test that took more than a few seconds, put the design back with Fusion\'s Undo',
                     'Switching a feature back on makes Fusion compute again everything after it that the test '
                     'changed, including heavy features at the end. Undo brings back the model Fusion kept from '
                     'before the test instead. Checked item by item; when Undo does not bring back exactly the '
                     'original state, the usual way is used. The run log shows how often it worked and how long it '
                     'took. Experimental: results of such a run are not reused later; compare them with a normal run '
                     '(tools/compare_pages.py).')):
                x = ag.children.addBoolValueInput(iid, label, True, '', bool(_settings().get(key, False)))
                x.tooltip = tip
                x.tooltipDescription = desc
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
        keys = {'hgReuse': 'reuse', 'hgLinkedGroups': 'linkedGroupTest',
                'hgExpCrash': 'experimentNoCrashRecovery', 'hgExpBody': 'experimentNoBodyCache',
                'hgExpDefer': 'experimentDeferCompute', 'hgExpUndo': 'experimentUndoPutBack'}
        if args.input.id in keys:
            st = _settings()
            st[keys[args.input.id]] = bool(args.input.value)
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
            if _STEPPER.active:
                _ui.messageBox('A dependencies graph is already being generated. Wait for it to finish or cancel it '
                               'in the progress panel.', 'Dependencies graph')
                return
            doc = _app.activeDocument
            gen = generate(opts.get('mode', 'both'), bool(opts.get('thumbs', True)), bool(opts.get('derived', False)))
            _STEPPER.start(gen, lambda w: _run_finished(doc, list(w or [])))
        except Exception:
            _ui.messageBox('Dependencies graph failed:\n{}'.format(traceback.format_exc()))


STEP_EVENT_ID = 'claudeDesignGraphStep'
_step_event = None


class _Stepper:
    """Runs generate() in steps. Wherever the run needs Fusion to do something that only happens after the
    add-in hands control back (Fusion's Undo command), it yields; this fires a custom event and continues from
    there when Fusion delivers it, after the queued command has run."""
    def __init__(self):
        self.gen = None
        self.done = None

    @property
    def active(self):
        return self.gen is not None

    def start(self, gen, done):
        self.gen, self.done = gen, done
        self.step()

    def step(self):
        while self._step_once():
            # no custom event to continue with: go on right here (Undo then falls back to switching back on)
            pass

    def _step_once(self):
        gen = self.gen
        if gen is None:
            return False
        try:
            next(gen)
        except StopIteration as fin:
            self.gen = None
            done, self.done = self.done, None
            if done:
                try:
                    done(fin.value)
                except Exception:
                    _ui.messageBox('Dependencies graph failed:\n{}'.format(traceback.format_exc()))
            return False
        except Exception:
            self.gen = self.done = None
            _ui.messageBox('Dependencies graph failed:\n{}'.format(traceback.format_exc()))
            return False
        return not _safe(lambda: _app.fireCustomEvent(STEP_EVENT_ID, '') or True, False)


_STEPPER = _Stepper()


class _StepHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        _STEPPER.step()


def _run_finished(doc, w):
    try:
        if True:
            if _safe(lambda: doc.isModified):
                # The design was saved when the run started and nothing else could edit it during the run,
                # so everything that marks it modified came from the run itself (marker moves etc.).
                if not _revert(doc):
                    w.append('Fusion marks the design as modified because the timeline marker was moved, '
                             'and reopening the saved version did not work. Nothing in the design was '
                             'changed; you can close it without saving.')
            gc.collect()
            adsk.doEvents()
            _mem_log('finished (design put back, %d documents open)' % (_safe(lambda: _app.documents.count, -1)))
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
    global _step_event
    _safe(lambda: _app.unregisterCustomEvent(STEP_EVENT_ID))
    _step_event = _app.registerCustomEvent(STEP_EVENT_ID)
    on_step = _StepHandler()
    _step_event.add(on_step)
    _handlers.append(on_step)
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
    # a run stopped mid-test (Fusion or the add-in stopped) left the undo recording off: switch it back on
    _safe(lambda: _tx_on(force=True))
    _safe(lambda: _experiments_end())
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
        _safe(lambda: _app.unregisterCustomEvent(STEP_EVENT_ID))
        if _STEPPER.gen is not None:
            _safe(_STEPPER.gen.close)
            _STEPPER.gen = None
        _safe(lambda: _tx_on(force=True))
        _safe(lambda: _experiments_end())
        _safe(lambda: _ui.palettes.itemById(PANEL_ID).deleteMe())
        _handlers.clear()
    except Exception:
        pass
