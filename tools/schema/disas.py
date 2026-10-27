# usage: dis.py <so> <start_hex> <len_hex>  -- disassemble and annotate adrp/add string refs
import sys,subprocess,re
so,start,ln=sys.argv[1],int(sys.argv[2],16),int(sys.argv[3],16)
data=open(so,"rb").read()
# map vaddr->file offset via program headers
import struct
e_phoff=struct.unpack_from("<Q",data,0x20)[0]; n=struct.unpack_from("<H",data,0x38)[0]
segs=[]
for i in range(n):
    p=e_phoff+i*56
    t,fl,off,va,pa,fs,ms,al=struct.unpack_from("<IIQQQQQQ",data,p)
    if t==1: segs.append((va,off,fs))
def rd(va,k=120):
    for v,o,s in segs:
        if v<=va<v+s:
            b=data[o+va-v:o+va-v+k]; return b
    return b""
out=subprocess.run(["objdump","-d","-C","--no-show-raw-insn",f"--start-address={start:#x}",f"--stop-address={start+ln:#x}",so],capture_output=True,text=True).stdout
regs={}
for line in out.splitlines():
    m=re.match(r"\s*([0-9a-f]+):\s+(\S+)\s+(.*)",line)
    ann=""
    if m:
        op,args=m.group(2),m.group(3)
        if op=="adrp":
            r,imm=args.split(",")[0],re.search(r"0x([0-9a-f]+)",args)
            if imm: regs[r.strip()]=int(imm.group(1),16)
        elif op=="add":
            a=[x.strip() for x in args.split(",")]
            if len(a)==3 and a[1] in regs and a[2].startswith("#"):
                va=regs[a[1]]+int(a[2][1:],0); b=rd(va,80)
                ann=f"   ; {va:#x} {b[:80]!r}"
    print(line+ann)
