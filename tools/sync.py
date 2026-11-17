"""Pull quest data + the assets it needs out of a local Minecraft instance.

Run this on a machine that has the modpack installed (it needs the mod jars):

    python tools/sync.py --instance "G:/PrismLauncher/instances/<pack>/minecraft"

It writes:
    source/quests/**          raw FTB Quests SNBT files (copied as-is)
    source/lang/en_us.json    quest text (from FTB Quest Localizer)
    source/names.json         display names for items/entities/biomes/... used by quests
    site/assets/icons/**      one PNG per item used by the quests
    site/assets/textures/**   textures referenced directly (chapter images, description images)
    site/assets/gui/**        FTB Quests shape textures + background used by the pack

After syncing, run tools/build.py (CI does this too) to produce site/data/quests.json.
"""
import argparse
import io
import json
import math
import os
import re
import shutil
import sys
import zipfile

from PIL import Image, ImageDraw, ImageEnhance

sys.path.insert(0, os.path.dirname(__file__))
import snbt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'source')
SITE = os.path.join(ROOT, 'site')


# ---------------------------------------------------------------- asset index

class Assets:
    """Overlay of every place the game loads assets/ and data/ from. Later sources win."""

    def __init__(self):
        self.files = {}  # 'assets/ns/...' -> (kind, ref, member)
        self.zips = {}
        self.lang = {}

    def add_zip(self, path, data=None, label=None):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data) if data is not None else path)
        except zipfile.BadZipFile:
            return
        key = label or path
        self.zips[key] = zf
        for n in zf.namelist():
            if n.startswith(('assets/', 'data/')) and not n.endswith('/'):
                self.files[n] = ('zip', key, n)
            elif n.startswith('META-INF/') and n.endswith('.jar'):
                self.add_zip(None, zf.read(n), key + '!' + n)

    def add_dir(self, base):
        for dp, _, fns in os.walk(base):
            for fn in fns:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, base).replace(os.sep, '/')
                if rel.startswith(('assets/', 'data/')):
                    self.files[rel] = ('dir', full, None)

    def has(self, p):
        return p in self.files

    def read(self, p):
        kind, ref, member = self.files[p]
        if kind == 'zip':
            return self.zips[ref].read(member)
        with open(ref, 'rb') as f:
            return f.read()

    def json(self, p):
        try:
            return json.loads(self.read(p).decode('utf-8-sig'))
        except Exception:
            return None

    def build_lang(self):
        # Collect en_us from every source in load order (dict insertion order = load order).
        for p in self.files:
            if p.startswith('assets/') and p.endswith('/lang/en_us.json'):
                d = self.json(p)
                if isinstance(d, dict):
                    self.lang.update({k: v for k, v in d.items() if isinstance(v, str)})


def rl(s, default_ns='minecraft'):
    if ':' in s:
        ns, p = s.split(':', 1)
    else:
        ns, p = default_ns, s
    return ns, p


# ---------------------------------------------------------------- textures

def load_texture(assets, ref):
    ns, p = rl(ref)
    path = f'assets/{ns}/textures/{p}.png'
    if not assets.has(path):
        return None
    im = Image.open(io.BytesIO(assets.read(path))).convert('RGBA')
    w, h = im.size
    if h > w and h % w == 0:  # animated strip: first frame
        im = im.crop((0, 0, w, w))
    return im


# ---------------------------------------------------------------- models

def load_model_chain(assets, ref):
    """Return (textures, elements, flags) after walking the parent chain."""
    textures, elements, flags = {}, None, set()
    seen = 0
    while ref and seen < 20:
        seen += 1
        ns, p = rl(ref)
        if ns == 'minecraft' and p in ('builtin/generated', 'item/generated'):
            flags.add('generated')
        if ns == 'minecraft' and p == 'builtin/entity':
            flags.add('entity')
        m = assets.json(f'assets/{ns}/models/{p}.json')
        if m is None:
            break
        for k, v in (m.get('textures') or {}).items():
            textures.setdefault(k, v)
        if elements is None and m.get('elements'):
            elements = m['elements']
        if m.get('loader') and 'composite' in str(m['loader']) and m.get('children'):
            # forge composite: use the first child that resolves
            for child in m['children'].values():
                if isinstance(child, dict) and child.get('parent'):
                    t2, e2, f2 = load_model_chain(assets, child['parent'])
                    for k, v in t2.items():
                        textures.setdefault(k, v)
                    elements = elements or e2
                    flags |= f2
                    break
        if m.get('loader') == 'forge:separate_transforms' and isinstance(m.get('base'), dict):
            base = m['base']
            for k, v in (base.get('textures') or {}).items():
                textures.setdefault(k, v)
            ref = base.get('parent')
            continue
        ref = m.get('parent')
    return textures, elements, flags


def resolve_tex(textures, v, depth=0):
    while isinstance(v, str) and v.startswith('#') and depth < 10:
        v = textures.get(v[1:])
        depth += 1
    return v if isinstance(v, str) and not v.startswith('#') else None


def render_flat(assets, textures):
    layers = []
    for i in range(8):
        t = resolve_tex(textures, textures.get(f'layer{i}'))
        if t:
            im = load_texture(assets, t)
            if im:
                layers.append(im)
    if not layers:
        return None
    size = max(l.size[0] for l in layers)
    out = Image.new('RGBA', (size, size))
    for l in layers:
        if l.size[0] != size:
            l = l.resize((size, size), Image.NEAREST)
        out.alpha_composite(l)
    return out


ISO_SIZE = 64
FOLIAGE = (0x77, 0xAB, 0x2F)


def _affine_from_points(src, dst):
    """Coefficients for PIL AFFINE mapping dst->src given 3 point pairs."""
    (x0, y0), (x1, y1), (x2, y2) = dst
    (u0, v0), (u1, v1), (u2, v2) = src
    det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    if abs(det) < 1e-9:
        return None
    a = ((u1 - u0) * (y2 - y0) - (u2 - u0) * (y1 - y0)) / det
    b = ((u2 - u0) * (x1 - x0) - (u1 - u0) * (x2 - x0)) / det
    c = u0 - a * x0 - b * y0
    d = ((v1 - v0) * (y2 - y0) - (v2 - v0) * (y1 - y0)) / det
    e = ((v2 - v0) * (x1 - x0) - (v1 - v0) * (x2 - x0)) / det
    f = v0 - d * x0 - e * y0
    return (a, b, c, d, e, f)


def render_block(assets, textures, elements, keep_order=False):
    """Tiny isometric renderer: draws the up/south/east faces of each element."""
    if not elements:
        elements = [{'from': [0, 0, 0], 'to': [16, 16, 16], 'faces': {
            'up': {'texture': '#top' if 'top' in textures else '#all'},
            'south': {'texture': '#side' if 'side' in textures else '#all'},
            'east': {'texture': '#side' if 'side' in textures else '#all'},
        }}]
    cos30, sin30 = math.cos(math.radians(30)), 0.5
    scale = ISO_SIZE / 33.0
    cx, cy = ISO_SIZE / 2, ISO_SIZE / 2

    def proj(x, y, z):
        # x/z in 0..16 centred on 8; y up
        x -= 8; y -= 8; z -= 8
        sx = (x - z) * cos30
        sy = (x + z) * sin30 - y
        return (cx + sx * scale, cy + sy * scale)

    out = Image.new('RGBA', (ISO_SIZE, ISO_SIZE))
    cache = {}
    order = elements if keep_order else sorted(elements, key=lambda e: (e['from'][1] + e['to'][1], e['from'][0] + e['from'][2]))
    drew = False
    for el in order:
        (x0, y0, z0), (x1, y1, z1) = el.get('from', [0, 0, 0]), el.get('to', [16, 16, 16])
        faces = el.get('faces') or {}
        # face: (corner points TL, TR, BL in 3D, default uv, shade)
        defs = {
            'east': ([(x1, y1, z1), (x1, y1, z0), (x1, y0, z1)], [16 - z1, 16 - y1, 16 - z0, 16 - y0], 0.6),
            'south': ([(x0, y1, z1), (x1, y1, z1), (x0, y0, z1)], [x0, 16 - y1, x1, 16 - y0], 0.8),
            'up': ([(x0, y1, z0), (x1, y1, z0), (x0, y1, z1)], [x0, z0, x1, z1], 1.0),
        }
        for name in ('east', 'south', 'up'):
            f = faces.get(name)
            if not f:
                continue
            tex_ref = resolve_tex(textures, f.get('texture'))
            if not tex_ref:
                continue
            if tex_ref not in cache:
                cache[tex_ref] = load_texture(assets, tex_ref)
            tex = cache[tex_ref]
            if tex is None:
                continue
            pts3, duv, shade = defs[name]
            uv = f.get('uv') or duv
            tw = tex.size[0] / 16.0
            u0, v0, u1, v1 = [c * tw for c in uv]
            face = tex
            if 'tintindex' in f:  # grass/leaves etc: multiply by a foliage green
                r, g, b, a = tex.split()
                face = Image.merge('RGBA', (
                    r.point(lambda v: v * FOLIAGE[0] // 255),
                    g.point(lambda v: v * FOLIAGE[1] // 255),
                    b.point(lambda v: v * FOLIAGE[2] // 255), a))
            if shade < 1:
                a = face.getchannel('A')
                face = ImageEnhance.Brightness(face.convert('RGB')).enhance(shade).convert('RGBA')
                face.putalpha(a)
            dst = [proj(*p) for p in pts3]
            src = [(u0, v0), (u1, v0), (u0, v1)]
            coeffs = _affine_from_points(src, dst)
            if not coeffs:
                continue
            layer = face.transform((ISO_SIZE, ISO_SIZE), Image.AFFINE, coeffs, resample=Image.NEAREST)
            # mask to the parallelogram so the texture does not repeat outside
            mask = Image.new('L', (ISO_SIZE, ISO_SIZE), 0)
            p0, p1, p2 = dst
            p3 = (p1[0] + p2[0] - p0[0], p1[1] + p2[1] - p0[1])
            ImageDraw.Draw(mask).polygon([p0, p1, p3, p2], fill=255)
            alpha = Image.composite(layer.getchannel('A'), Image.new('L', layer.size, 0), mask)
            layer.putalpha(alpha)
            out.alpha_composite(layer)
            drew = True
    return out if drew else None


# Mob heads and boss trophies are drawn by code in-game (builtin/entity); their item
# model only names soul sand as the break-particle texture. Build them as a cube cut
# from the mob texture instead. Values are texture pixels:
#   box: (u, v, w, h, d) in the usual entity cube layout, hat: (u, v) of the outer layer
#   faces: explicit (x0, y0, x1, y1) rects for models that don't fit that layout
#   parts: extra boxes on the front face, ((u, v, w, h, d), (x, y) offset from the face's top-left)
_SKULL = {'tex': 'minecraft:entity/skeleton/skeleton', 'box': (0, 0, 8, 8, 8)}
HEADS = {
    'minecraft:skeleton_skull': _SKULL,
    'minecraft:wither_skeleton_skull': {'tex': 'minecraft:entity/skeleton/wither_skeleton', 'box': (0, 0, 8, 8, 8)},
    'minecraft:zombie_head': {'tex': 'minecraft:entity/zombie/zombie', 'box': (0, 0, 8, 8, 8), 'hat': (32, 0)},
    'minecraft:player_head': {'tex': 'minecraft:entity/player/wide/steve', 'box': (0, 0, 8, 8, 8), 'hat': (32, 0)},
    'minecraft:creeper_head': {'tex': 'minecraft:entity/creeper/creeper', 'box': (0, 0, 8, 8, 8)},
    'minecraft:piglin_head': {'tex': 'minecraft:entity/piglin/piglin', 'box': (0, 0, 10, 8, 8),
                             'parts': [((31, 1, 4, 4, 1), (3, 4))]},  # snout
    'minecraft:dragon_head': {'tex': 'minecraft:entity/enderdragon/dragon', 'box': (112, 30, 16, 16, 16)},
    'mutantmonsters:mutant_skeleton_skull': {'tex': 'mutantmonsters:entity/mutant_skeleton', 'box': (0, 0, 8, 8, 8)},
    'twilightforest:skeleton_skull_candle': _SKULL,
    'twilightforest:naga_trophy': {'tex': 'twilightforest:model/nagahead', 'box': (0, 0, 16, 16, 16)},
    'twilightforest:lich_trophy': {'tex': 'twilightforest:model/twilightlich64', 'box': (0, 0, 8, 8, 8), 'hat': (32, 0)},
    'twilightforest:snow_queen_trophy': {'tex': 'twilightforest:model/snowqueen', 'box': (0, 0, 8, 8, 8)},
    'twilightforest:knight_phantom_trophy': {'tex': 'twilightforest:armor/phantom_1', 'box': (0, 0, 8, 8, 8), 'hat': (32, 0)},
    'twilightforest:minoshroom_trophy': {'tex': 'twilightforest:model/minoshroomtaur', 'box': (96, 16, 8, 8, 8)},
    'twilightforest:ur_ghast_trophy': {'tex': 'twilightforest:model/towerghast_fire', 'box': (0, 0, 16, 16, 16)},
    'twilightforest:hydra_trophy': {'tex': 'twilightforest:model/hydra4', 'size': (32, 16, 32), 'faces': {
        'up': (304, 0, 336, 32), 'south': (304, 32, 336, 48), 'east': (336, 32, 368, 48)}},
    'twilightforest:alpha_yeti_trophy': {'tex': 'twilightforest:model/yetialpha', 'size': (48, 48, 32), 'faces': {
        'up': (112, 0, 160, 32), 'south': (112, 32, 160, 80), 'east': (160, 32, 192, 80)}},
}


def render_head(assets, spec):
    tex = load_texture(assets, spec['tex'])
    if tex is None:
        return None
    k = 16.0 / tex.size[0]  # render_block's uv unit is 1/16 of the texture width

    def face_rects(u, v, w, h, d):
        return {'up': (u + d, v, u + d + w, v + d), 'south': (u + d, v + d, u + d + w, v + d + h),
                'east': (u + d + w, v + d, u + d + w + d, v + d + h)}

    if 'box' in spec:
        u, v, w, h, d = spec['box']
        layers = [face_rects(u, v, w, h, d)]
        if spec.get('hat'):
            layers.append(face_rects(*spec['hat'], w, h, d))
    else:
        (w, h, d), layers = spec['size'], [spec['faces']]
    s = 14.0 / max(w, h, d)
    elements = []
    for i, rects in enumerate(layers):
        g = 0.6 * i  # outer layer sits slightly outside the head
        lo = [8 - w * s / 2 - g, 8 - h * s / 2 - g, 8 - d * s / 2 - g]
        hi = [8 + w * s / 2 + g, 8 + h * s / 2 + g, 8 + d * s / 2 + g]
        elements.append({'from': lo, 'to': hi, 'faces': {
            n: {'texture': '#t', 'uv': [c * k for c in r]} for n, r in rects.items()}})
    x0, y1, z1 = 8 - w * s / 2, 8 + h * s / 2, 8 + d * s / 2
    for (u, v, pw, ph, pd), (ox, oy) in spec.get('parts', []):
        elements.append({'from': [x0 + ox * s, y1 - (oy + ph) * s, z1], 'to': [x0 + (ox + pw) * s, y1 - oy * s, z1 + pd * s],
                         'faces': {n: {'texture': '#t', 'uv': [c * k for c in r]}
                                   for n, r in face_rects(u, v, pw, ph, pd).items()}})
    return render_block(assets, {'t': spec['tex']}, elements, keep_order=True)


def guess_texture(assets, ns, p):
    """Last resort for items drawn by code (chests, backpacks...): find a texture named after the item."""
    base = p.split('/')[-1]
    prefix = f'assets/{ns}/textures/'
    best = None
    for f in assets.files:
        if not f.startswith(prefix) or not f.endswith('.png'):
            continue
        name = f.rsplit('/', 1)[1][:-4]
        if name == base or name.startswith(base + '_') or name.startswith(base):
            rank = (name != base, '/item' not in f, '/entity' in f, len(f))
            if best is None or rank < best[0]:
                best = (rank, f)
    if best:
        im = load_texture(assets, ns + ':' + best[1][len(prefix):-4])
        if im and im.size[0] == im.size[1]:  # skip entity atlases
            return im
    return None


def render_item(assets, item_id):
    im = render_head(assets, HEADS[item_id]) if item_id in HEADS else None
    im = im or _render_item(assets, item_id)
    if im is None:
        im = guess_texture(assets, *rl(item_id))
    return im


def _render_item(assets, item_id):
    ns, p = rl(item_id)
    model = f'{ns}:item/{p}'
    if not assets.has(f'assets/{ns}/models/item/{p}.json'):
        # fall back: a texture with the same name
        for guess in (f'{ns}:item/{p}', f'{ns}:block/{p}', f'{ns}:items/{p}', f'{ns}:{p}'):
            im = load_texture(assets, guess)
            if im:
                return im
        return None
    textures, elements, flags = load_model_chain(assets, model)
    if 'generated' in flags or any(k.startswith('layer') for k in textures):
        im = render_flat(assets, textures)
        if im:
            return im
    if elements or any(k in textures for k in ('all', 'top', 'side', 'end')):
        im = render_block(assets, textures, elements)
        if im:
            return im
    for k in ('particle', 'layer0', 'all', 'side', 'texture'):
        t = resolve_tex(textures, textures.get(k))
        if t:
            im = load_texture(assets, t)
            if im:
                return im
    for guess in (f'{ns}:item/{p}', f'{ns}:block/{p}'):
        im = load_texture(assets, guess)
        if im:
            return im
    return None


# ---------------------------------------------------------------- quest scan

TEXTURE_RE = re.compile(r'\{image:([a-z0-9_.\-]+:[^\s}]+)')


def collect_refs(quests_dir, lang):
    items, entities, biomes, dims, structures, advs, textures = set(), set(), set(), set(), set(), set(), set()

    def item_of(v):
        if isinstance(v, dict):
            v = v.get('id')
        return v if isinstance(v, str) and v else None

    def add_icon(v):
        v = item_of(v)
        if not v:
            return
        if '/textures/' in v or ':textures/' in v or v.endswith('.png'):
            textures.add(v)
        else:
            items.add(v)

    def walk(o, key=None):
        if isinstance(o, dict):
            t = o.get('type')
            if t == 'kill' and o.get('entity'):
                entities.add(o['entity'])
            if t == 'biome' and o.get('biome'):
                biomes.add(o['biome'])
            if t == 'dimension' and o.get('dimension'):
                dims.add(o['dimension'])
            if t == 'structure' and o.get('structure'):
                structures.add(o['structure'])
            if t == 'advancement' and o.get('advancement'):
                advs.add(o['advancement'])
            for k, v in o.items():
                if k in ('icon', 'item'):
                    add_icon(v)
                if k == 'image' and isinstance(v, str):
                    textures.add(v)
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)

    for dp, _, fns in os.walk(quests_dir):
        for fn in fns:
            if fn.endswith('.snbt'):
                walk(snbt.load(os.path.join(dp, fn)))
    for v in lang.values():
        for m in TEXTURE_RE.findall(v):
            textures.add(m)
    return dict(items=items, entities=entities, biomes=biomes, dims=dims,
                structures=structures, advs=advs, textures=textures)


def pretty(rid):
    ns, p = rl(rid)
    p = p.split('/')[-1]
    return ' '.join(w.capitalize() for w in p.replace('-', '_').split('_') if w)


def tr(assets, key):
    return assets.lang.get(key)


def name_for_item(assets, iid):
    ns, p = rl(iid)
    dotted = p.replace('/', '.')
    for k in (f'item.{ns}.{dotted}', f'block.{ns}.{dotted}'):
        v = tr(assets, k)
        if v:
            return v
    return pretty(iid)


def text_component(assets, c):
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        if 'translate' in c:
            return tr(assets, c['translate']) or c.get('fallback') or c['translate']
        return c.get('text', '')
    return ''


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--instance', required=True, help='path to the instance .minecraft folder')
    ap.add_argument('--client-jar', help='vanilla client jar (auto-detected for Prism Launcher)')
    args = ap.parse_args()

    inst = os.path.abspath(args.instance)
    quests_src = os.path.join(inst, 'config', 'ftbquests', 'quests')
    if not os.path.isdir(quests_src):
        sys.exit(f'No FTB Quests folder at {quests_src}')

    client = args.client_jar
    if not client:
        cands = [
            os.path.expandvars(r'%APPDATA%/PrismLauncher/libraries/com/mojang/minecraft/1.20.1/minecraft-1.20.1-client.jar'),
            os.path.join(inst, '..', '..', '..', 'libraries', 'com', 'mojang', 'minecraft', '1.20.1', 'minecraft-1.20.1-client.jar'),
        ]
        client = next((c for c in cands if os.path.isfile(c)), None)

    # 1. copy quest data
    dst_q = os.path.join(SOURCE, 'quests')
    shutil.rmtree(dst_q, ignore_errors=True)
    shutil.copytree(quests_src, dst_q)
    lang_src = os.path.join(inst, 'kubejs', 'assets', 'ftbquestlocalizer', 'lang', 'en_us.json')
    os.makedirs(os.path.join(SOURCE, 'lang'), exist_ok=True)
    if os.path.isfile(lang_src):
        shutil.copy(lang_src, os.path.join(SOURCE, 'lang', 'en_us.json'))
    else:
        print('warning: no FTB Quest Localizer lang file found; titles may show as keys')
    print('copied quest data')

    # 2. build the asset overlay (load order: vanilla < mods < kubejs < openloader packs)
    assets = Assets()
    if client:
        assets.add_zip(client)
        print('vanilla assets:', client)
    else:
        print('warning: vanilla client jar not found; vanilla item icons will be missing')
    mods = os.path.join(inst, 'mods')
    for fn in sorted(os.listdir(mods)):
        if fn.endswith('.jar'):
            assets.add_zip(os.path.join(mods, fn))
    kjs = os.path.join(inst, 'kubejs')
    if os.path.isdir(os.path.join(kjs, 'assets')):
        assets.add_dir(kjs)
    ol = os.path.join(inst, 'config', 'openloader', 'resources')
    if os.path.isdir(ol):
        for pack in sorted(os.listdir(ol)):
            full = os.path.join(ol, pack)
            if os.path.isdir(full):
                assets.add_dir(full)
            elif full.endswith('.zip'):
                assets.add_zip(full)
    assets.build_lang()
    print(f'indexed {len(assets.files)} asset files, {len(assets.lang)} lang keys')

    with open(os.path.join(SOURCE, 'lang', 'en_us.json'), encoding='utf-8') as f:
        quest_lang = json.load(f)
    # Quest text that isn't in the localizer file lives in the pack's normal lang files.
    used = set()
    for dp, _, fns in os.walk(dst_q):
        for fn in fns:
            with open(os.path.join(dp, fn), encoding='utf-8') as f:
                used.update(re.findall(r'"\{([A-Za-z0-9_.\-]+)\}"', f.read()))
    extra = {k: assets.lang[k] for k in sorted(used) if k not in quest_lang and k in assets.lang}
    # Last resort: en_us files inside any resource pack zip in the instance, even disabled ones.
    missing_keys = used - set(quest_lang) - set(extra)
    rp = os.path.join(inst, 'resourcepacks')
    if missing_keys and os.path.isdir(rp):
        for fn in sorted(os.listdir(rp)):
            if not fn.endswith('.zip'):
                continue
            try:
                zf = zipfile.ZipFile(os.path.join(rp, fn))
            except zipfile.BadZipFile:
                continue
            for n in zf.namelist():
                if n.endswith('/lang/en_us.json'):
                    try:
                        d = json.loads(zf.read(n).decode('utf-8-sig'))
                    except Exception:
                        continue
                    hit = {k: d[k] for k in missing_keys if isinstance(d.get(k), str)}
                    if hit:
                        print(f'  {len(hit)} keys taken from {fn}')
                        extra.update(hit)
                        missing_keys -= set(hit)
    with open(os.path.join(SOURCE, 'lang', 'extra_en_us.json'), 'w', encoding='utf-8') as f:
        json.dump(extra, f, indent=1, ensure_ascii=False)
    print(f'{len(extra)} extra lang keys from pack assets; {len(used - set(quest_lang) - set(extra))} unresolved')
    quest_lang.update(extra)
    refs = collect_refs(dst_q, quest_lang)
    # fallback icons build.py uses for tasks/rewards that don't set one
    refs['items'].update(['minecraft:compass', 'minecraft:diamond_sword', 'minecraft:paper', 'minecraft:experience_bottle'])

    advancements = {}
    for a in sorted(refs['advs']):
        ns, p = rl(a)
        d = assets.json(f'data/{ns}/advancements/{p}.json') or {}
        disp = d.get('display') or {}
        title = text_component(assets, disp.get('title'))
        if not title or title.startswith('advancements.'):
            title = pretty(a)
        desc = text_component(assets, disp.get('description'))
        if desc and desc.startswith('advancements.'):
            desc = ''
        icon = (disp.get('icon') or {}).get('item')
        if icon:
            refs['items'].add(icon)
        advancements[a] = {'title': title, 'description': desc, 'icon': icon}

    # 3. icons
    icon_dir = os.path.join(SITE, 'assets', 'icons')
    shutil.rmtree(icon_dir, ignore_errors=True)
    ok, missing = [], []
    for iid in sorted(refs['items']):
        try:
            im = render_item(assets, iid)
        except Exception as e:  # keep going; one broken model shouldn't stop the sync
            print('  error rendering', iid, e)
            im = None
        if im is None:
            missing.append(iid)
            continue
        ns, p = rl(iid)
        out = os.path.join(icon_dir, ns, p + '.png')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        im.save(out, optimize=True)
        ok.append(iid)
    print(f'icons: {len(ok)} rendered, {len(missing)} missing')
    if missing:
        print('  missing:', ', '.join(missing[:40]), '...' if len(missing) > 40 else '')

    # 4. raw textures (chapter images, description images, texture icons)
    tex_dir = os.path.join(SITE, 'assets', 'textures')
    shutil.rmtree(tex_dir, ignore_errors=True)
    tex_ok = []
    for t in sorted(refs['textures']):
        ns, p = rl(t)
        if not p.endswith('.png'):
            p_png = p + '.png'
        else:
            p_png = p
        if not p_png.startswith('textures/'):
            cand = [f'assets/{ns}/textures/{p_png}', f'assets/{ns}/{p_png}']
        else:
            cand = [f'assets/{ns}/{p_png}']
        src = next((c for c in cand if assets.has(c)), None)
        if not src:
            print('  texture not found:', t)
            continue
        out = os.path.join(tex_dir, ns, src.split(f'assets/{ns}/', 1)[1])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        im = Image.open(io.BytesIO(assets.read(src)))
        if im.size[1] > im.size[0] and im.size[1] % im.size[0] == 0 and assets.has(src + '.mcmeta'):
            im = im.crop((0, 0, im.size[0], im.size[0]))
        im.save(out)
        tex_ok.append(t)
    print(f'textures: {len(tex_ok)} copied')

    # 5. GUI textures (shapes + background) as the pack ships them
    gui = os.path.join(SITE, 'assets', 'gui')
    shutil.rmtree(gui, ignore_errors=True)
    os.makedirs(gui, exist_ok=True)
    for shape in ('circle', 'square', 'rsquare', 'diamond', 'pentagon', 'hexagon', 'octagon', 'heart', 'gear', 'none'):
        for part in ('background', 'outline', 'shape'):
            src = f'assets/ftbquests/textures/shapes/{shape}/{part}.png'
            if assets.has(src):
                os.makedirs(os.path.join(gui, 'shapes', shape), exist_ok=True)
                with open(os.path.join(gui, 'shapes', shape, part + '.png'), 'wb') as f:
                    f.write(assets.read(src))
    for src, name in (('assets/ftblibrary/textures/gui/background_squares.png', 'background.png'),
                      ('assets/ftbquests/textures/gui/dependency.png', 'dependency.png'),
                      ('assets/ftbquests/textures/item/book.png', 'book.png'),
                      ('assets/ftbquests/textures/gui/quest_locked.png', 'locked.png'),
                      ('assets/ftbquests/textures/gui/close.png', 'close.png'),
                      ('assets/ftblibrary/textures/icons/money_bag.png', 'money_bag.png'),
                      ('assets/ftblibrary/textures/icons/accept.png', 'accept.png'),
                      ('assets/ftblibrary/textures/icons/accept_gray.png', 'accept_gray.png'),
                      ('assets/ftbquests/ftb_quests_theme.txt', 'theme.txt')):
        if assets.has(src):
            with open(os.path.join(gui, name), 'wb') as f:
                f.write(assets.read(src))

    # 6. names
    names = {
        'items': {i: name_for_item(assets, i) for i in sorted(refs['items'])},
        'entities': {e: tr(assets, 'entity.{}.{}'.format(*rl(e))) or pretty(e) for e in sorted(refs['entities'])},
        'biomes': {b: tr(assets, 'biome.{}.{}'.format(*rl(b))) or pretty(b) for b in sorted(refs['biomes'])},
        'dimensions': {d: tr(assets, 'dimension.{}.{}'.format(*rl(d))) or pretty(d) for d in sorted(refs['dims'])},
        'structures': {s: tr(assets, 'structure.{}.{}'.format(*rl(s))) or pretty(s.lstrip('#')) for s in sorted(refs['structures'])},
        'advancements': advancements,
        'icons': sorted(ok),
        'textures': sorted(tex_ok),
    }
    with open(os.path.join(SOURCE, 'names.json'), 'w', encoding='utf-8') as f:
        json.dump(names, f, indent=1, ensure_ascii=False)
    print('wrote source/names.json')


if __name__ == '__main__':
    main()
