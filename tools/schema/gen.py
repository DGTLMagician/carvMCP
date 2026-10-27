"""Compact schema.json for the package from crawl.py, sertypes.py and enums.py output."""
import json
sch=json.load(open('schema.json')); ser=json.load(open('sertypes.json'))
ONEOF_NAMES={'proto::data::section::Sections':{1:'run',2:'lift',7:'segment'}}
out={}
def short(t): return t.replace('proto::','') if t else t
for t,v in sch.items():
    if 'fields' not in v: continue
    st=ser.get(t)
    fs={}
    for tag,f in v['fields'].items():
        k=f['kind']
        if k=='?': continue
        name=f['name'] or ONEOF_NAMES.get(t,{}).get(int(tag)) or f'field_{tag}'
        if st is not None and name in st:
            rep = st[name].startswith('alloc::vec::Vec')
        else:  # no serde info: prost repeated helper, or a plural message field name
            rep = f['repeated'] or (k=='message' and name.endswith('s') and not name.endswith('ss'))
        if k in ('varint','int32','int64','uint32','uint64'): k='int'
        if k in ('hash_map','map'): k='map'; rep=True
        if k=='packed': k='int'; rep=True
        e=[name,k]
        if k in('message','oneof'): e.append(short(f['type']))
        if rep: e+=([None] if len(e)==2 else [])+[1]
        fs[int(tag)]=e
    out[short(t)]=fs
enums=json.load(open('enums.json'))
KEEP={'proto::data::TurnType2507':'TurnType','proto::data::LiftType':'LiftType','proto::data::PisteDifficulty':'PisteDifficulty','proto::data::Activity':'Activity','proto::data::UnifiedCoachingMetric':'CoachingMetric'}
out['__enums__']={KEEP[k]:v for k,v in enums.items() if k in KEEP}
json.dump(out,open('../../src/carv_mcp/schema.json','w'),separators=(',',':'),sort_keys=True)
print(len(out), sum(len(x) for x in out.values()))
