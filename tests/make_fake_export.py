"""Synthetic RE2-OB-shaped export for an offline self-test (no download, no API key).
Usage: python tests/make_fake_export.py <out_dir>"""
import sys
import os,json,random
import pandas as pd
random.seed(1)
svcs=["adservice","cartservice","checkoutservice","currencyservice","productcatalogservice"]
faults=["cpu","mem","disk","delay","loss","socket"]
base=sys.argv[1] if len(sys.argv)>1 else "selftest"
root=os.path.join(base,"agent_tasks","re2-ob"); os.makedirs(root+"/cases",exist_ok=True)
ids=[];ans=[];i=0
inj=1705312345
for s in svcs:
  for f in faults:
    for r in range(3):
      i+=1;cid=f"re2-ob-{i:04d}";d=f"{root}/cases/{cid}";os.makedirs(d)
      rows=[];sid=0
      for t in range(inj-600,inj+600,5):
        sid+=1;fs=f"f{sid}";rows.append(dict(time=t,traceID=f"t{sid}",spanID=fs,serviceName="frontend",methodName="",operationName="GET /",startTimeMillis=t*1000,startTime=t*1_000_000,duration=50000,statusCode=0,parentSpanID=""))
        for k,callee in enumerate(["cartservice","productcatalogservice","currencyservice"]):
          c=f"c{sid}{k}";sv=f"s{sid}{k}"
          rows.append(dict(time=t,traceID=f"t{sid}",spanID=c,serviceName="frontend",methodName=f"hipstershop.{callee}/Get",operationName="x",startTimeMillis=t*1000,startTime=t*1_000_000+100,duration=20000,statusCode=0,parentSpanID=fs))
          rows.append(dict(time=t,traceID=f"t{sid}",spanID=sv,serviceName=callee,methodName=f"hipstershop.{callee}/Get",operationName="x",startTimeMillis=t*1000,startTime=t*1_000_000+200,duration=15000+random.randint(0,2000),statusCode=0,parentSpanID=c))
      pd.DataFrame(rows).to_csv(d+"/traces.csv",index=False)
      lg=[dict(time=t,timestamp=t*1_000_000_000,container_name=c,message=f"request served {t}",level="info") for t in range(inj-600,inj+600,7) for c in ["cartservice","frontend","productcatalogservice"]]
      pd.DataFrame(lg).to_csv(d+"/logs.csv",index=False)
      pd.DataFrame({"time":range(inj-600,inj+600),**{x+"_cpu":0.1 for x in svcs}}).to_csv(d+"/metrics.csv",index=False)
      json.dump({"schema_version":1,"case_id":cid,"dataset":"re2-ob","system":"Online Boutique","inject_time":inj,"standard_window":{"start":inj-600,"end":inj+600},"files":{"metrics":"metrics.csv","logs":"logs.csv","traces":"traces.csv"},"candidate_services":svcs+["frontend"],"candidate_indicators":[x+"_cpu" for x in svcs],"answer_format":""},open(d+"/task.json","w"))
      ids.append(cid);ans.append(dict(case_id=cid,dataset="re2-ob",source=f"{s}_{f}/{r+1}",service=s,fault=f,indicator=s+"_cpu",answer_in_candidates=True,indicator_in_candidates=False,n_candidate_services=len(svcs)+1,n_candidate_indicators=0))
json.dump({"case_ids":ids},open(root+"/manifest.json","w"))
open(root+".answers.jsonl","w").write("".join(json.dumps(a)+"\n" for a in ans))
