"""Extract the actual landing examples. Never calls a model or a database."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def freeze(frontend):
    root = Path(frontend)
    names = ('landing_sample_questions.json', 'landing_page_schema.json')
    raw = {name: (root / 'src/schema' / name).read_bytes() for name in names}
    catalog, choices = (json.loads(raw[name]) for name in names)
    entries = [('search_examples', i, e) for i, e in enumerate(catalog['search_examples']['entries'])]
    for group in catalog['example_buttons']:
        entries.extend((group['title'], i, e) for i, e in enumerate(group['entries']))
    cases = {}
    for group, index, entry in entries:
        question = ' '.join(entry['question'].split())
        variants = [(question, {})]
        if '[GENE]' in question or '[SNP]' in question:
            qid = int(re.search(r'qid=(\d+)', entry['link']).group(1))
            kind = 'gene' if '[GENE]' in question else 'snp'
            variants = [(question.replace('[' + kind.upper() + ']', value.split('(')[0]), {kind: value})
                        for value in choices[qid]['default_terms_list'][kind]]
        for text, slots in variants:
            case = cases.setdefault(text, {'id': 'landing-' + hashlib.sha256(text.encode()).hexdigest()[:12],
                                           'question': text, 'sources': [], 'substitutions': slots})
            case['sources'].append({'group': group, 'entry': index, 'original_question': entry['question'],
                                    'link': entry['link']})
    return {'version': 1, 'source_repository': 'https://github.com/wangyiqunumich/pank_frontend',
            'source_branch': 'xuteng/react',
            'source_commit': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
            'files': {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
            'displayed_entries': len(entries), 'cases': list(cases.values())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('frontend'); parser.add_argument('output')
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(freeze(args.frontend), indent=2, ensure_ascii=False) + '\n')
