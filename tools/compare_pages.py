#!/usr/bin/env python3
"""Compare the results of two generated Dependencies Graph pages (e.g. the same design run with different
settings): what each suppression test found per item and group, and the links. Pictures, layout and dates are
ignored.

    python3 tools/compare_pages.py first.html second.html
"""
import json
import re
import sys

NODE_FIELDS = ('dsupp', 'dbreak', 'dwarn', 'fail', 'health', 'supp')
GROUP_FIELDS = ('dsupp', 'dbreak', 'dwarn', 'fail', 'empty')


def load(path):
    with open(path, 'r', encoding='utf-8') as f:
        m = re.search(r'^const D = (.*);\s*$', f.read(), re.M)
    if not m:
        sys.exit('%s: no data found (not a Dependencies Graph page?)' % path)
    return json.loads(m.group(1))


def norm(v):
    return sorted(v) if isinstance(v, list) else v


def compare(kind, a_list, b_list, fields, names):
    a = {x['id']: x for x in a_list}
    b = {x['id']: x for x in b_list}
    diffs = 0
    for i in sorted(set(a) | set(b), key=str):
        if i not in a or i not in b:
            print('  %s %s (%s): only in the %s page' % (kind, names.get(i, i), i, 'first' if i in a else 'second'))
            diffs += 1
            continue
        for f in fields:
            va, vb = norm(a[i].get(f)), norm(b[i].get(f))
            if (va or None) != (vb or None):
                diffs += 1
                if isinstance(va, list) or isinstance(vb, list):
                    sa, sb = set(va or []), set(vb or [])
                    print('  %s %s: %s  only first: %s  only second: %s' % (
                        kind, names.get(i, i), f, [names.get(x, x) for x in sorted(sa - sb)],
                        [names.get(x, x) for x in sorted(sb - sa)]))
                else:
                    print('  %s %s: %s  %r -> %r' % (kind, names.get(i, i), f, va, vb))
    return diffs


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    a, b = load(sys.argv[1]), load(sys.argv[2])
    names = {n['id']: n.get('name') or n['id'] for n in a['nodes'] + b['nodes']}
    names.update({g['id']: 'group ' + (g.get('name') or g['id']) for g in a['groups'] + b['groups']})
    for d, p in ((a, sys.argv[1]), (b, sys.argv[2])):
        m = d['meta']
        print('%s: %s, item test %s, group test %s, %s s, %d items, %d warnings' % (
            p, m.get('doc'), m.get('exact'), m.get('gtest'), m.get('generationSeconds'), len(d['nodes']),
            len(m.get('warnings') or [])))
    print()
    n = compare('item', a['nodes'], b['nodes'], NODE_FIELDS, names)
    n += compare('group', a['groups'], b['groups'], GROUP_FIELDS, names)
    ea = {(e['s'], e['t']): tuple(e['k']) for e in a['edges']}
    eb = {(e['s'], e['t']): tuple(e['k']) for e in b['edges']}
    for k in sorted(set(ea) | set(eb), key=str):
        if ea.get(k) != eb.get(k):
            n += 1
            print('  link %s -> %s: %s -> %s' % (names.get(k[0], k[0]), names.get(k[1], k[1]), ea.get(k), eb.get(k)))
    wa, wb = set(a['meta'].get('warnings') or []), set(b['meta'].get('warnings') or [])
    for w in sorted(wa ^ wb):
        print('  warning only in the %s page: %s' % ('first' if w in wa else 'second', w))
    print('\n%s' % ('Same results.' if not n else '%d differences.' % n))
    sys.exit(1 if n else 0)


if __name__ == '__main__':
    main()
