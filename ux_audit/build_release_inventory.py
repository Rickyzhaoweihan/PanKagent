"""Read-only full-release metadata inventory. Load protected settings externally.

Run as a module: python -m ux_audit.build_release_inventory --output inventory.json
This is offline build tooling, never a request-path or reference-query fallback.
"""
import argparse
import asyncio
import json
import re
from pathlib import Path

from pankagent_vnext.config import Settings
from pankagent_vnext.graph import GraphAdapter
async def collect():
 s=Settings();s.graph_timeout=60;g=GraphAdapter(s)
 try:
  await g._ensure_identity()
  out={'version':'release-schema-v1','release':s.graph_version,'identity_strength':'schema_and_anchors','nodes':{},'relations':{},'categories':{},'aliases':{'anatomical_structure':{'pancreatic islet':'pancreatic islet (islet of Langerhans)','islet':'pancreatic islet (islet of Langerhans)','active pancreatic stellate cell':'pancreatic stellate cell active state','activated pancreatic stellate cell':'pancreatic stellate cell active state'}}}
  rows=await g._small_query('CALL db.schema.nodeTypeProperties() YIELD nodeLabels,propertyName RETURN nodeLabels,propertyName')
  for row in rows:
   for label in row['nodeLabels']:
    if row['propertyName']:out['nodes'].setdefault(label,[]).append(row['propertyName'])
  out['nodes']={k:sorted(set(v)) for k,v in out['nodes'].items()}
  for rel in sorted(g.release_relations):
   if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',rel): raise ValueError('invalid_relationship_identifier')
   rows=await g._small_query('MATCH (a)-[r:`'+rel+'`]->(b) RETURN labels(a) AS a,labels(b) AS b,collect(DISTINCT keys(r)) AS property_sets,count(*) AS records')
   out['relations'][rel]={'paths':[{'source':r['a'],'target':r['b'],'records':r['records']} for r in rows], 'properties':sorted({k for r in rows for ps in r['property_sets'] for k in ps})}
  for rel,prop in [('PART_OF_QTL_SIGNAL','tissue_name'),('PART_OF_QTL_SIGNAL','tissue_id'),('GENE_ENRICHED_IN','condition'),('GENE_ENRICHED_IN','comparison')]:
   rows=await g._small_query('MATCH ()-[r:`'+rel+'`]->() RETURN collect(DISTINCT r.`'+prop+'`) AS values');out['categories'][rel+'.'+prop]=rows[0]['values']
  return out
 finally:await g.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('Refusing to overwrite an existing reviewed inventory.')
    inventory=asyncio.run(collect())
    with args.output.open('x') as stream:
        json.dump(inventory,stream,indent=2)
    print(json.dumps({'labels':len(inventory['nodes']),'relations':len(inventory['relations']), 'release':inventory['release']}))
