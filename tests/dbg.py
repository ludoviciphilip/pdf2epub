import pymupdf,sys
doc=pymupdf.open(sys.argv[1]); pno=int(sys.argv[2]); pat=sys.argv[3]
d=doc[pno].get_text("dict")
for b in d["blocks"]:
  if b["type"]!=0: print("IMG",b["bbox"]); continue
  for l in b["lines"]:
    t="".join(s["text"] for s in l["spans"])
    if pat in t:
      print([round(x) for x in l["bbox"]])
      for s in l["spans"]: print("   ",repr(s["text"][:40]),round(s["size"],1),s["flags"],s["font"],[round(v,1) for v in s["origin"]])
