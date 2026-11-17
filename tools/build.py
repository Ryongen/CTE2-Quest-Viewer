"""Turn source/ (SNBT quests + lang + names) into site/data/quests.json.

Needs only the files in this repo, so it also runs in CI:

    python tools/build.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import snbt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'source')
OUT = os.path.join(ROOT, 'site', 'data', 'quests.json')

KEY_RE = re.compile(r'^\{([a-z0-9_.\-]+)\}$', re.I)


def load_json(path, default):
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default


lang = load_json(os.path.join(SOURCE, 'lang', 'extra_en_us.json'), {})
lang.update(load_json(os.path.join(SOURCE, 'lang', 'en_us.json'), {}))
names = load_json(os.path.join(SOURCE, 'names.json'), {})
ICONS = set(names.get('icons', []))
TEXTURES = set(names.get('textures', []))


def t(s):
    """Resolve a '{translation.key}' string through the quest lang file."""
    if not isinstance(s, str):
        return s
    m = KEY_RE.match(s.strip())
    if m and m.group(1) in lang:
        # lang values are Java format strings in-game, so '%%' shows as '%'
        return lang[m.group(1)].replace('%%', '%')
    return s


def tlist(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    return [t(x) for x in v]


def item_ref(v, count=None):
    """Normalise an item (string or {id, Count, tag}) to {id, count, name}."""
    if v is None:
        return None
    tag = None
    if isinstance(v, dict):
        iid = v.get('id')
        c = v.get('Count', v.get('count', 1))
        tag = v.get('tag')
    else:
        iid, c = v, 1
    if not iid:
        return None
    if count is not None:
        c = count
    out = {'id': iid, 'count': c, 'name': names.get('items', {}).get(iid) or pretty(iid)}
    if isinstance(tag, dict):
        disp = tag.get('display') or {}
        nm = disp.get('Name')
        if isinstance(nm, str):
            try:
                j = json.loads(nm)
                out['name'] = j.get('text') if isinstance(j, dict) else str(j)
            except Exception:
                out['name'] = nm
        if 'Potion' in tag:
            out['extra'] = pretty(tag['Potion'])
        if 'gateway' in tag:
            out['extra'] = pretty(tag['gateway'])
    return out


def pretty(rid):
    if not isinstance(rid, str):
        return ''
    p = rid.lstrip('#').split(':')[-1].split('/')[-1]
    return ' '.join(w.capitalize() for w in p.replace('-', '_').split('_') if w)


def icon_ref(v):
    """An icon is either an item id or a texture path. Returns {'item': id} / {'texture': path} / None."""
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get('id')
    if not isinstance(v, str) or not v:
        return None
    if ':textures/' in v or v.endswith('.png'):
        return {'texture': v} if v in TEXTURES else None
    return {'item': v}


def task_out(task, tables):
    ty = task.get('type')
    o = {'id': task.get('id'), 'type': ty}
    title = t(task.get('title'))
    icon = icon_ref(task.get('icon'))
    if ty == 'item':
        it = item_ref(task.get('item'), task.get('count'))
        o['item'] = it
        icon = icon or (it and {'item': it['id']})
        o['name'] = title or (it and it['name'])
        if task.get('consume_items'):
            o['consume'] = True
    elif ty == 'kill':
        ent = task.get('entity', '')
        en = names.get('entities', {}).get(ent) or pretty(ent)
        o['name'] = title or f'Kill {en}'
        o['count'] = task.get('value', 1)
        o['entity'] = en
        icon = icon or {'item': 'minecraft:diamond_sword'}
    elif ty == 'checkmark':
        o['name'] = title or 'Checkmark'
        icon = icon or {'gui': 'accept_gray'}
    elif ty == 'advancement':
        a = names.get('advancements', {}).get(task.get('advancement', ''), {})
        o['name'] = title or a.get('title') or pretty(task.get('advancement'))
        o['detail'] = a.get('description') or ''
        icon = icon or (a.get('icon') and {'item': a['icon']})
    elif ty == 'biome':
        b = task.get('biome', '')
        o['name'] = title or 'Visit ' + (names.get('biomes', {}).get(b) or pretty(b))
    elif ty == 'dimension':
        d = task.get('dimension', '')
        o['name'] = title or 'Visit ' + (names.get('dimensions', {}).get(d) or pretty(d))
    elif ty == 'structure':
        icon = icon or {'item': 'minecraft:compass'}
        s = task.get('structure', '')
        o['name'] = title or 'Find ' + (names.get('structures', {}).get(s) or pretty(s))
    elif ty == 'stat':
        o['name'] = title or pretty(task.get('stat'))
        icon = icon or {'item': 'minecraft:paper'}
        o['count'] = task.get('value', 1)
    else:
        o['name'] = title or pretty(ty)
    o['icon'] = icon
    return o


def reward_out(r, tables):
    ty = r.get('type', 'item')
    o = {'id': r.get('id'), 'type': ty}
    title = t(r.get('title'))
    icon = icon_ref(r.get('icon'))
    if ty == 'item':
        it = item_ref(r.get('item'), r.get('count'))
        o['item'] = it
        icon = icon or (it and {'item': it['id']})
        o['name'] = title or (it and it['name'])
    elif ty in ('choice', 'random', 'loot', 'all_table'):
        tid = r.get('table_id')
        tid = format(tid & 0xFFFFFFFFFFFFFFFF, '016X') if isinstance(tid, int) else str(tid)
        o['table'] = tid
        tab = tables.get(tid)
        label = {'choice': 'Choose one: ', 'random': 'Random: ', 'loot': 'Loot crate: ', 'all_table': 'All of: '}[ty]
        o['name'] = title or label + (tab['title'] if tab else 'Reward table')
        icon = icon or (tab and tab.get('icon'))
    elif ty in ('xp', 'xp_levels'):
        o['name'] = title or f"{r.get('xp', r.get('xp_levels', 0))} {'XP' if ty == 'xp' else 'Levels'}"
        icon = icon or {'item': 'minecraft:experience_bottle'}
    elif ty == 'advancement':
        a = names.get('advancements', {}).get(r.get('advancement', ''), {})
        o['name'] = title or a.get('title') or 'Advancement'
    else:  # command, custom, ...
        o['name'] = title or pretty(ty)
    if r.get('auto') == 'enabled' or r.get('auto') == 'invisible':
        o['auto'] = True
    o['icon'] = icon
    return o


def main():
    qdir = os.path.join(SOURCE, 'quests')
    data = snbt.load(os.path.join(qdir, 'data.snbt'))
    groups_raw = snbt.load(os.path.join(qdir, 'chapter_groups.snbt')).get('chapter_groups', [])

    # reward tables
    tables = {}
    tdir = os.path.join(qdir, 'reward_tables')
    if os.path.isdir(tdir):
        for fn in sorted(os.listdir(tdir)):
            if not fn.endswith('.snbt'):
                continue
            rt = snbt.load(os.path.join(tdir, fn))
            tid = rt.get('id')
            entries = []
            for e in rt.get('rewards', []):
                ty = e.get('type', 'item')
                ent = {'weight': e.get('weight', 1), 'type': ty}
                if ty == 'item':
                    it = item_ref(e.get('item'), e.get('count'))
                    ent['item'] = it
                    ent['name'] = t(e.get('title')) or (it and it['name'])
                    ent['icon'] = it and {'item': it['id']}
                else:
                    ent['name'] = t(e.get('title')) or pretty(ty)
                    ent['icon'] = icon_ref(e.get('icon'))
                entries.append(ent)
            tables[tid] = {
                'id': tid,
                'title': t(rt.get('title')) or pretty(fn[:-5]),
                'icon': icon_ref(rt.get('icon')) or (entries and entries[0].get('icon')) or None,
                'lootSize': rt.get('loot_size', 1),
                'order': rt.get('order_index', 0),
                'rewards': entries,
            }

    chapters = []
    cdir = os.path.join(qdir, 'chapters')
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith('.snbt'):
            continue
        ch = snbt.load(os.path.join(cdir, fn))
        default_shape = ch.get('default_quest_shape') or data.get('default_quest_shape') or 'circle'
        quests = []
        for q in ch.get('quests', []):
            tasks = [task_out(x, tables) for x in q.get('tasks', [])]
            rewards = [reward_out(x, tables) for x in q.get('rewards', [])]
            title = t(q.get('title'))
            if not title:
                title = tasks[0]['name'] if tasks else (rewards[0]['name'] if rewards else 'Quest')
            icon = icon_ref(q.get('icon'))
            if not icon:
                icon = next((x['icon'] for x in tasks if x.get('icon')), None) \
                    or next((x['icon'] for x in rewards if x.get('icon')), None)
            quests.append({
                'id': q['id'],
                'x': q.get('x', 0), 'y': q.get('y', 0),
                'size': q.get('size', 1),
                'shape': q.get('shape') or default_shape,
                'title': title,
                'subtitle': t(q.get('subtitle')) or '',
                'description': tlist(q.get('description')),
                'icon': icon,
                'tasks': tasks,
                'rewards': rewards,
                'deps': q.get('dependencies', []),
                'hideLines': q.get('hide_dependency_lines', ch.get('default_hide_dependency_lines', False)),
                'depMode': q.get('dependency_requirement', 'all_completed'),
                'minDeps': q.get('min_required_dependencies', 0),
                'repeatable': bool(q.get('can_repeat')),
                'hideUntilDeps': bool(q.get('hide_until_deps_complete')),
                'optional': bool(q.get('optional')),
            })
        images = []
        for im in ch.get('images', []):
            path = im.get('image')
            if path not in TEXTURES:
                continue
            images.append({
                'texture': path, 'x': im.get('x', 0), 'y': im.get('y', 0),
                'w': im.get('width', 1), 'h': im.get('height', 1),
                'rotation': im.get('rotation', 0),
                'color': im.get('color'), 'alpha': im.get('alpha', 255),
                'order': im.get('order', 0),
                'hover': tlist(im.get('hover')),
            })
        links = [{'id': l.get('id'), 'quest': l.get('linked_quest'), 'x': l.get('x', 0), 'y': l.get('y', 0),
                  'size': l.get('size', 1), 'shape': l.get('shape')} for l in ch.get('quest_links', [])]
        chapters.append({
            'id': ch['id'],
            'filename': ch.get('filename', fn[:-5]),
            'group': ch.get('group', ''),
            'title': t(ch.get('title')) or pretty(fn[:-5]),
            'subtitle': tlist(ch.get('subtitle')),
            'icon': icon_ref(ch.get('icon')) or (quests and quests[0]['icon']) or None,
            'order': ch.get('order_index', 0),
            'progression': ch.get('progression_mode', data.get('progression_mode', 'default')),
            'images': images,
            'links': links,
            'quests': quests,
        })

    cfg = load_json(os.path.join(ROOT, 'config.json'), {})
    hidden = set(cfg.get('hiddenChapters', []))
    chapters = [c for c in chapters if c['filename'] not in hidden and c['id'] not in hidden]
    group_order = {g['id']: i for i, g in enumerate(groups_raw)}
    chapters.sort(key=lambda c: (group_order.get(c['group'], -1), c['order']))

    out = {
        'siteTitle': cfg.get('siteTitle') or 'Quests',
        'packName': cfg.get('packName') or '',
        'title': t(data.get('title')) or 'Quests',
        'icon': icon_ref(data.get('icon')),
        'groups': [{'id': g['id'], 'title': t(g.get('title'))} for g in groups_raw],
        'chapters': chapters,
        'tables': tables,
        'icons': sorted(ICONS),
        'stats': {
            'chapters': len(chapters),
            'quests': sum(len(c['quests']) for c in chapters),
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print(f"wrote {os.path.relpath(OUT, ROOT)}: {out['stats']['chapters']} chapters, "
          f"{out['stats']['quests']} quests, {len(tables)} reward tables")


if __name__ == '__main__':
    main()
