"""
Prueba de la capa social sin red ni API: Bluesky, YouTube y Claude simulados.

    cd analysis && python3 test_social.py
"""
import sys, json, re
from collections import Counter
import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os; os.chdir(Path(__file__).resolve().parents[1])
import pipeline_social as S
from ingest import bluesky, youtube
end=datetime(2026,11,20,13,45,tzinfo=timezone.utc); start=end-timedelta(hours=24)
rng=np.random.default_rng(0)
def fake_bsky(method, params):
    if method=='app.bsky.feed.getAuthorFeed':
        h=params['actor']; side='izq' if hash(h)%2 else 'der'
        feed=[{"post":{"uri":f"at://{h}/post/{i}","author":{"handle":h,"did":"did:"+h},"record":{"text":f"{side} opinión sobre el debate electoral número {i} de hoy","createdAt":(end-timedelta(hours=i+1)).isoformat()},"replyCount":3,"likeCount":i}} for i in range(4)]
        feed.append({"reason":{"$type":"repost"},"post":feed[0]["post"]})
        return {"feed":feed}
    if method=='app.bsky.feed.getPostThread':
        return {"thread":{"replies":[{"post":{"author":{"did":f"did:u{j}"},"record":{"text":f"respuesta izq {j} sobre el debate y la vivienda","langs":["es"]},"likeCount":j}} for j in range(3)]}}
    if method=='app.bsky.actor.getProfile':
        return {"followersCount":10,"postsCount":5} if params['actor'].endswith('.es') else None
    if method=='app.bsky.actor.searchActors':
        return {"actors":[{"handle":"cand.bsky.social","followersCount":3}]}
bluesky._get=fake_bsky; bluesky.PAUSE=0
def fake_yt(path, params):
    if path=='channels': return {"items":[{"id":"C1","snippet":{"title":params['forHandle']},"contentDetails":{"relatedPlaylists":{"uploads":"U1"}}}]}
    if path=='playlistItems': return {"items":[{"contentDetails":{"videoId":"v1","videoPublishedAt":(end-timedelta(hours=2)).isoformat()},"snippet":{"title":"vídeo"}}]}
    if path=='commentThreads': return {"items":[{"snippet":{"topLevelComment":{"snippet":{"textOriginal":f"der comentario {k} contra el gobierno\nsegunda línea","authorChannelId":{"value":f"a{k}"},"likeCount":k}}}} for k in range(5)]}
youtube._get=fake_yt
def fake_run(bodies, stage, mode):
    out=[]
    for b in bodies:
        txt=b['messages'][0]['content']; assert txt.startswith('PUBLICACIONES'), txt[:60]
        items=re.findall(r'^(\d+)\. (.*)$',txt,re.M)
        if stage.startswith('asignacion'):
            out.append(json.dumps([{"id":int(i),"topic":1} for i,_ in items]))
        else:
            out.append(json.dumps([{"id":int(i),"why":"w","stance":-0.6 if 'izq' in t else 0.6,"hostility":0.3,"conflict":0.5,"us_them":"political","bloc":0.5 if 'der' in t else -0.5,"confidence":.8,"off_topic":False} for i,t in items]))
    return out
S.runner.run=fake_run
acc=json.load(open('data/social_accounts.json'))
def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond: raise SystemExit(1)
print("social")
texts=S.collect(acc,start,end,{"bluesky","replies","youtube"},20,3000,100,3000)
layers=Counter(t.layer for t in texts)
check("tres capas recogidas", set(layers)=={"bluesky_cuentas","bluesky_respuestas","youtube_comentarios"})
check("sin reposts ni duplicados", len({(t.layer,t.text) for t in texts})==len(texts))
check("respuestas con seudónimo, no con usuario", all(t.source.startswith("u_") for t in texts if t.layer!="bluesky_cuentas"))
check("fuera de ventana no entra", all(start.isoformat()<=t.created<=end.isoformat() for t in texts if t.layer=="bluesky_cuentas"))
debates=[{"label":"Debate electoral 29N","pole_a":"PP","pole_b":"Gob"}]
recs=S.classify(texts,debates,'sync')
check("todos asignados y puntuados", all(r.get("stance") is not None for r in recs))
check("bloc guardado", all(r.get("bloc") is not None for r in recs))
press=[{"layer":"prensa","country":"ES","domain":f"m{i%6}.es","url":"https://x.es/a","topic":"Debate electoral 29N","stance":(-0.5 if i%2 else 0.5),"hostility":0.1} for i in range(40)]
rep=S.layers_report(press,recs,acc)
check("informe con prensa y redes", set(rep["debates"]["Debate electoral 29N"])=={"prensa","bluesky_cuentas","bluesky_respuestas","youtube_comentarios"})
check("cobertura de cuentas por grupo", "bluesky_coverage" in rep)
print("\nRESULTADO: TODO PASA")
