"""Public, exact per-cell expression cache. No donor metadata or inference."""
import asyncio, base64, gzip, hashlib, json, os, re, struct, time
from pathlib import Path
import httpx
from fastapi import HTTPException
from fastapi.responses import Response

DATASET = 'islet_of_Langerhans_scRNA_v3-4'
ATLAS = 'map-c2f6511c24dca057e7d2287a3547c10122e70697bb03c902c62e6f9026e32138.json'
COUNT = 448935
ORIGIN = 'https://bioindex.pankbase.org'
VERSION = 1
TTL = 86400


def compact(payload, gene, count=COUNT, atlas=ATLAS):
    if payload.get('restricted') or payload.get('continuation'):
        raise ValueError('Incomplete source')
    rows = [r for r in payload.get('data', []) if r.get('dataset') == DATASET and r.get('gene') == gene]
    if len(rows) != 1: raise ValueError('Gene unavailable or ambiguous')
    vector = rows[0].get('expression')
    import math
    if not isinstance(vector, list) or len(vector) != count or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in vector):
        raise ValueError('Invalid source alignment')
    indices, values = bytearray(), bytearray()
    for i, value in enumerate(vector):
        if value != 0:
            indices.extend(struct.pack('<I', i)); values.extend(struct.pack('<d', value))
    return {'version':VERSION, 'datasetId':DATASET, 'atlas':atlas, 'gene':gene, 'count':count,
        'encoding':'sparse-u32-f64-le', 'indices':base64.b64encode(indices).decode(),
        'values':base64.b64encode(values).decode()}


class ExpressionCache:
    def __init__(self, directory, fetch=None):
        self.directory = Path(directory)
        self.fetch = fetch or self._fetch
        self.pending = {}
        self.semaphore = asyncio.Semaphore(2)

    async def _fetch(self, gene):
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            async with client.stream('GET', ORIGIN+'/api/bio/query/single-cell-lognorm', params={'q':DATASET+','+gene}) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 16*1024*1024: raise ValueError('Source exceeds limit')
                return json.loads(raw)

    def _path(self, gene):
        return self.directory / (hashlib.sha256((DATASET+ATLAS+gene).encode()).hexdigest()+'.json.gz')

    def _read(self, gene):
        path = self._path(gene)
        if not path.exists() or time.time()-path.stat().st_mtime > TTL: return None
        if path.stat().st_size > 16*1024*1024: return None
        raw = path.read_bytes()
        try:
            data = json.loads(gzip.decompress(raw))
            if data['atlas'] != ATLAS or data['gene'] != gene or data['datasetId'] != DATASET or data['version'] != VERSION or data['count'] != COUNT or data['encoding'] != 'sparse-u32-f64-le': return None
        except (ValueError, KeyError, OSError): return None
        return raw

    def _save(self, gene, raw):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(gene); temporary = path.with_suffix('.tmp')
        with temporary.open('wb') as file:
            os.chmod(temporary,0o600); file.write(raw)
        temporary.replace(path)
        files = sorted(self.directory.glob('*.json.gz'),key=lambda p:p.stat().st_mtime,reverse=True)
        total = 0
        for index, item in enumerate(files):
            total += item.stat().st_size
            if index >= 128 or total > 256*1024*1024: item.unlink(missing_ok=True)

    async def _fill(self, gene):
        async with self.semaphore:
            payload = await self.fetch(gene)
            data = await asyncio.to_thread(compact,payload,gene)
            source = json.dumps(payload,separators=(',',':')).encode()
            data['source_sha256'] = hashlib.sha256(source).hexdigest()
            data['retrieved_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
            raw = gzip.compress(json.dumps(data,separators=(',',':')).encode(),compresslevel=6)
            await asyncio.to_thread(self._save,gene,raw)
            return raw

    async def get(self, gene):
        cached = await asyncio.to_thread(self._read,gene)
        if cached: return cached,'hit'
        if gene not in self.pending:
            if len(self.pending) >= 16: raise HTTPException(503,'Expression cache is busy. Retry shortly.')
            task = asyncio.create_task(self._fill(gene)); self.pending[gene] = task
            def finished(task):
                self.pending.pop(gene,None)
                if not task.cancelled(): task.exception()
            task.add_done_callback(finished)
        try: return await asyncio.shield(self.pending[gene]),'miss'
        except HTTPException: raise
        except Exception: raise HTTPException(502,'Expression source unavailable or failed validation.') from None


def install(app, state_dir):
    cache = ExpressionCache(Path(state_dir)/'scbrowser-expression-v1')
    app.state.scbrowser_expression_cache = cache
    @app.get('/api/scbrowser/expression')
    async def expression(gene: str, dataset: str, atlas: str):
        if dataset != DATASET or atlas != ATLAS: raise HTTPException(409,'Unsupported atlas identity.')
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,79}',gene): raise HTTPException(422,'Invalid gene symbol.')
        raw,status = await cache.get(gene)
        return Response(raw,media_type='application/json',headers={'Content-Encoding':'gzip',
            'Cache-Control':'private, max-age=3600','X-Expression-Cache':status,
            'X-Content-Type-Options':'nosniff','ETag':'"'+hashlib.sha256(raw).hexdigest()+'"'})

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-manifest', type=Path)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--warm', nargs='*', default=[])
    args = parser.parse_args()
    if args.verify_manifest:
        manifest = json.loads(args.verify_manifest.read_text())
        if (manifest.get('datasetId'),manifest.get('asset'),manifest.get('count')) != (DATASET,ATLAS,COUNT):
            parser.error('Manifest does not match the registered dataset/atlas/count. Review and version the registry first.')
        print(json.dumps({'registry_verified':True,'dataset':DATASET,'atlas':ATLAS,'count':COUNT}))
    if args.warm:
        if not args.cache_dir or any(not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,79}',gene) for gene in args.warm):
            parser.error('Provide --cache-dir and valid gene symbols.')
        async def warm():
            cache = ExpressionCache(args.cache_dir)
            for gene in args.warm:
                started=time.monotonic(); raw,status=await cache.get(gene)
                print(json.dumps({'gene':gene,'cache':status,'seconds':round(time.monotonic()-started,4),'gzip_bytes':len(raw)}))
        asyncio.run(warm())
