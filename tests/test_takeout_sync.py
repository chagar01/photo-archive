"""End-to-end tests for takeout_sync (synthetic Takeout snapshots, real --apply on a temp
dir, Immich simulated by editing/deleting files) and unit tests for the date heuristics.
Run: python3 test_takeout_sync.py"""
import json, os, sys, zipfile, sqlite3, tempfile, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import takeout_sync as ts
import library, xmp

logging.disable(logging.WARNING)
root = Path(tempfile.mkdtemp(prefix='takeout_sync_test_'))
base = root/'base'; (base/'state').mkdir(parents=True)

def snapshot(name, files, parts=(1,)):
    d = root/name/'Takeout'; d.mkdir(parents=True)
    for n in parts:
        with zipfile.ZipFile(d/f'takeout-20261101T000000Z-1-{n:03d}.zip','w') as z:
            if n != parts[0]: z.writestr('Takeout/Google Photos/x/metadata.json','{}'); continue
            for path,(data,url,t) in files.items():
                z.writestr('Takeout/Google Photos/'+path, data)
                if url: z.writestr(f'Takeout/Google Photos/{path}.supplemental-metadata.json',
                                   json.dumps({'url':url,'photoTakenTime':{'timestamp':str(t)},
                                               'geoData':{'latitude':52.5,'longitude':-0.25}}))
    return root/name/'Takeout'

def apply(takeout, *extra):
    return ts.main(['--base', str(base), '--takeout', str(takeout), '--apply', '--no-probe',
                    '--workers', '1', '--immich-url', 'http://127.0.0.1:9', *extra])

def rows():
    return library.State(base/'state'/'library.sqlite', readonly=True)

def row_for(st, name):
    return next(r for r in st.rows.values() if (r['home'] or r['archive'] or '').endswith(name))

J = lambda tag: b'\xff\xd8\xff' + tag
A = {'Photos from 2020/keep.jpg': (J(b'keep'), 'u1', 1600000000),
     'Trip/keep.jpg':             (J(b'keep'), 'u1', 1600000000),   # album duplicate
     'Photos from 2020/gone.jpg': (J(b'gone'), 'u2', 1600000001),
     'Photos from 2020/edit.jpg': (J(b'v1'),   'u3', 1600000002),
     'Photos from 2020/del.jpg':  (J(b'del'),  'u4', 1600000003),
     'Photos from 1946/old.jpg':  (J(b'old'),  'u5', -757382400)}

# Month 1: everything added, deduplicated, dated, with sidecars.
assert apply(snapshot('A', A)) == 0
st = rows(); lib = base/'Library'
assert len(st.rows) == 5 and all(r['status'] == 'active' for r in st.rows.values())
k = row_for(st, 'keep.jpg'); kp = lib/k['home']
assert kp.read_bytes() == J(b'keep') and int(kp.stat().st_mtime) == 1600000000
assert xmp.read_date(xmp.sidecar_path(kp)) == 1600000000
assert '52,30.000000N' in xmp.sidecar_path(kp).read_text()
assert not list(lib.rglob('*.partial'))

# Between runs, in "Immich": edit keep.jpg's date, delete del.jpg; CSV override for old.jpg.
EDIT = 978350400   # 2001-01-01 12:00 UTC
xmp.sidecar_path(kp).write_bytes(xmp.build(EDIT, 0, None, None, 'my caption'))
d = row_for(st, 'del.jpg'); (lib/d['home']).unlink(); xmp.sidecar_path(lib/d['home']).unlink()
o = row_for(st, 'old.jpg')
(base/'state'/'date_overrides.csv').write_text(f"path,date\n{o['home']},1950-05-05 12:00\n")

# Month 2: gone.jpg removed from Google, edit.jpg re-exported with new content, a new photo.
B = dict(A); del B['Photos from 2020/gone.jpg']
B['Photos from 2020/edit.jpg'] = (J(b'v2-longer'), 'u3', 1600000002)
B['Photos from 2021/new.jpg'] = (J(b'new'), 'u6', 1610000000)
assert apply(snapshot('B', B)) == 0
st = rows()
k = row_for(st, 'keep.jpg')
assert k['user_xmp'] == 1 and k['taken'] == EDIT and st.manual[k['key']]['source'] == 'immich'
assert 'my caption' in xmp.sidecar_path(lib/k['home']).read_text()       # user's sidecar untouched
g = row_for(st, 'gone.jpg')
assert g['status'] == 'archived' and (base/'Archive'/g['archive']).exists() and not g['home']
e_old = [r for r in st.rows.values() if r['status'] == 'replaced']
assert len(e_old) == 1 and (base/'Archive'/e_old[0]['archive']).read_bytes() == J(b'v1')
e_new = next(r for r in st.rows.values() if r['status'] == 'active' and r['home'].endswith('edit.jpg'))
assert (lib/e_new['home']).read_bytes() == J(b'v2-longer')
assert e_new['home'] == e_old[0]['archive'].removeprefix('replaced/')    # same path: Immich keeps it
d = next(r for r in st.rows.values() if 'u4#' in r['item_ids'])
assert d['status'] == 'deleted' and not (lib/(d['archive'] or 'x')).exists()
assert not any(p.name == 'del.jpg' or p.name.endswith('_del.jpg') for p in lib.rglob('*.jpg'))
o = row_for(st, 'old.jpg')
assert dates_fmt(o['taken']).startswith('1950-05-05') if (dates_fmt := __import__('dates').fmt) else 0
assert xmp.read_date(xmp.sidecar_path(lib/o['home'])) == o['taken']
assert any(p.name.endswith('new.jpg') for p in lib.rglob('*.jpg'))

# Month 3: gone.jpg is back in Google -> restored to its old place; a re-run changes nothing.
C = dict(B); C['Photos from 2020/gone.jpg'] = A['Photos from 2020/gone.jpg']
assert apply(snapshot('C', C)) == 0
st = rows(); g = row_for(st, 'gone.jpg')
assert g['status'] == 'active' and (lib/g['home']).exists() and g['archive'] is None
before = {k: dict(r) for k, r in st.rows.items()}
assert apply(root/'C'/'Takeout') == 0
after = rows().rows
assert {k: (r['home'], r['status'], r['taken'], r['xmp_hash']) for k, r in after.items()} == \
       {k: (r['home'], r['status'], r['taken'], r['xmp_hash']) for k, r in before.items()}

# --delete-input removes the Takeout parts after a clean run.
assert apply(root/'C'/'Takeout', '--delete-input') == 0
assert not list((root/'C'/'Takeout').glob('*.zip'))

# Incomplete snapshot: gap in part numbers is flagged.
snap = ts.discover(snapshot('D', A, parts=(1, 3)))
assert not snap.complete and 'missing part(s) 002' in snap.warnings[0], snap.warnings
assert ts.check(root/'D'/'Takeout') == ['takeout-20261101T000000Z-1: missing part(s) 002']
assert ts.check(root/'C'/'Takeout') == []
(root/'E'/'Takeout').mkdir(parents=True)
(root/'E'/'Takeout'/'takeout-20261101T000000Z-1-001.zip').write_bytes(b'not a zip')
assert 'not a readable zip' in ts.check(root/'E'/'Takeout')[0]
# Two exports waiting: the oldest is processed first.
older = snapshot('F', A); newer = root/'F'/'Takeout'/'takeout-20261201T000000Z-1-001.zip'
(older/'takeout-20261101T000000Z-1-001.zip').rename(older/'takeout-20261001T000000Z-1-001.zip')
import shutil; shutil.copy(older/'takeout-20261001T000000Z-1-001.zip', newer)
assert ts.discover(older).export == 'takeout-20261001T000000Z-1'
print('all apply tests passed')

# --- Date heuristics ---
import datetime as dt
import dates
T = lambda *a: int(dt.datetime(*a).astimezone().timestamp())
EX = lambda s: {'EXIF:DateTimeOriginal': s}
D = lambda name, ext, g, tags, albums: dates.decide(dates.Item('k', name, ext, g, tags, albums))
# Verano 2009: Google says 1946, EXIF + filename year say 2009 -> EXIF wins.
d = D('Verano 2009 384.jpg', '.jpg', T(1946,10,17,6,2), EX('2009:07:14 18:02:11'), [])
assert d.how == 'override' and dates.fmt(d.ts).startswith('2009-07-14'), (d.how, d.describe())
# Digital camera name dated 1948 by Google, nothing else -> kept (maybe deliberate) but flagged.
d = D('DSCN0060.jpg', '.jpg', T(1948,5,1), {}, [])
assert d.how == 'implausible-kept' and dates.fmt(d.ts).startswith('1948'), d.describe()
# ... but with EXIF it is used.
d = D('DSCN0060.jpg', '.jpg', T(1948,5,1), EX('2006:08:01 10:00:00'), [])
assert d.how == 'google-implausible' and dates.fmt(d.ts).startswith('2006-08-01')
# Genuine scan: Google 1946 (user set), EXIF = scan date, no hints -> keep Google, flag.
d = D('grandma.jpg', '.jpg', T(1946,6,1), EX('2015:03:02 10:00:00'), [])
assert d.how == 'conflict-kept-google' and dates.fmt(d.ts).startswith('1946-06-01')
# Album title year breaks the tie.
d = D('grandma.jpg', '.jpg', T(2016,6,1), EX('1999:03:02 10:00:00'), ['Summer 1999'])
assert d.how == 'override' and dates.fmt(d.ts).startswith('1999-03-02')
# Timezone slop: local EXIF vs UTC Google 9h apart still agree; Google's exact instant used.
d = D('IMG_1.HEIC', '.heic', T(2023,1,1,20), EX('2023:01:02 05:00:00'), [])
assert d.how == 'agree' and d.ts == T(2023,1,1,20)
# HEIC before 2017 and camera placeholder dates are rejected.
d = D('IMG_2.HEIC', '.heic', T(2019,5,5), EX('2000:01:01 00:00:00'), [])
assert d.how == 'google-only' and 'placeholder' in d.describe(), d.describe()
d = D('x.heic', '.heic', T(2010,5,5), {}, [])
assert d.how == 'implausible-kept'
assert D('x.jpg', '.jpg', None, {}, []).how == 'none'
# Filename datetime counts as an independent source.
d = D('IMG_20180824_130518.jpg', '.jpg', T(2020,1,1), EX('2018:08:24 13:05:18'), [])
assert d.how == 'override' and dates.fmt(d.ts) == '2018-08-24 13:05'
# QuickTime CreateDate is UTC.
d = D('clip.mov', '.mov', None, {'QuickTime:CreateDate': '2020:04:03 21:08:43'}, [])
assert d.ts == int(dt.datetime(2020,4,3,21,8,43,tzinfo=dt.timezone.utc).timestamp())
# Verano 2009 with no EXIF: year from the name, Google's month/day/time kept.
d = D('Verano 2009 384.jpg', '.jpg', T(1946,10,17,8,17), {}, [])
assert d.how == 'year-from-name' and dates.fmt(d.ts) == '2009-10-17 08:17', (d.how, dates.fmt(d.ts))
# Camera frame numbers are not years.
d = D('IMG_2034.MOV', '.mov', T(2016,8,29), {}, [])
assert d.how == 'google-only' and not d.hints
# DDMMYYYY phone names.
d = D('01082009008.mp4', '.mp4', T(2009,8,1,12,57), {'QuickTime:CreateDate': '2016:04:12 21:05:00'}, [])
assert d.how == 'agree' and dates.fmt(d.ts) == '2009-08-01 12:57', (d.how, d.describe())

# Cross-file: batch re-encode date rejected; camera sequence neighbours bracket a bad date.
items = [dates.Item(f'v{i}', f'IMG_{i:04d}.MOV', '.mov', T(2015,1+i%7,1+i,10),
                    {'QuickTime:CreateDate': f'2015:08:27 03:{i:02d}:00'}, []) for i in range(12)]
items += [dates.Item('p1', 'P1020289.JPG', '.jpg', T(2011,3,4,12), EX('2011:03:04 12:00:00'), []),
          dates.Item('p2', 'P1020311.jpg', '.jpg', T(1948,4,29,7,44), {}, []),
          dates.Item('p3', 'P1020330.JPG', '.jpg', T(2011,3,20,12), EX('2011:03:20 12:00:00'), [])]
out = dates.decide_all(items)
assert all(out[f'v{i}'].how == 'google-only' for i in range(12)), [out[f'v{i}'].describe() for i in range(2)]
assert 'batch' in out['v0'].describe()
assert out['p2'].how == 'google-implausible' and dates.fmt(out['p2'].ts).startswith('2011-03-1'), out['p2'].describe()
# Phone-named file: Google's 1955 is impossible for a 2011 device name.
d = D('23072011206.jpg', '.jpg', T(1955,1,20,1,25), {}, [])
assert d.how == 'google-implausible' and dates.fmt(d.ts).startswith('2011-07-23'), d.describe()
# Years inside UUIDs are not hints.
assert D('432e2f9d-1959-4277-93cc-5fa10b0e28e3.jpg', '.jpg', T(2020,3,22), {}, []).how == 'google-only'
# Bulk upload minute: 12 uncorroborated files at the same Google minute.
items = [dates.Item(f'b{i}', f'{i+1:02d}072011{i:03d}.jpg', '.jpg', T(2011,12,29,1,22,i), {}, [])
         for i in range(12)]
items.append(dates.Item('bv', 'Verano 2009 382.jpg', '.jpg', T(2011,12,29,1,22,30), {}, []))
items.append(dates.Item('bx', 'photo.jpg', '.jpg', T(2011,12,29,1,22,40), {}, []))
out = dates.decide_all(items)
assert out['b0'].how == 'google-implausible' and dates.fmt(out['b0'].ts).startswith('2011-07-01'), out['b0'].describe()
assert out['bv'].how == 'year-from-name' and dates.fmt(out['bv'].ts).startswith('2009-12-29'), out['bv'].describe()
assert out['bx'].how == 'upload-time-kept' and 'bulk' in out['bx'].describe()
# Local timezone recovered from EXIF wall clock vs Google's UTC instant (Japan, +9h).
utc = int(dt.datetime(2023,4,1,6,0,tzinfo=dt.timezone.utc).timestamp())
d = D('IMG_9.HEIC', '.heic', utc, EX('2023:04:01 15:00:00'), [])
assert d.how == 'agree' and d.offset_min == 540, (d.how, d.offset_min)
print('all date tests passed')
