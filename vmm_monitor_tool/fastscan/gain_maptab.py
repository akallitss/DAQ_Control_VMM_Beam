import json, statistics as S, sys
G = ['4.5', '6.0', '9.0', '12.0', '16.0']
M = {g: json.load(open('out/gain_20261118/maps/map_g%s.json' % g)) for g in G}
ref = M['4.5']
print('chip | baseline median (mV) per gain 4.5/6/9/12/16 | dB vs 4.5 | odd channels (|dB|>30) per gain | thr-base median')
for c in [str(i) for i in range(2, 20)]:
    row, db, odd, tb = [], [], [], []
    for g in G:
        b = M[g]['baseline'].get(c, {}); t = M[g]['threshold'].get(c, {})
        rb = ref['baseline'].get(c, {})
        d = [b[k] - rb[k] for k in b if k in rb]
        row.append('%5.0f' % S.median(b.values()) if b else '  -  ')
        db.append('%+5.1f' % S.median(d) if d else '  -  ')
        odd.append('%2d' % sum(1 for x in d if abs(x) > 30))
        x = [t[k] - b[k] for k in b if k in t]
        tb.append('%5.1f' % S.median(x) if x else '  -  ')
    print('%3s | %s | %s | %s | %s' % (c, ' '.join(row), ' '.join(db), ' '.join(odd), ' '.join(tb)))
