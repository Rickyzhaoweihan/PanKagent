"""Explicit, additive public metadata index provisioning; never runs on API startup."""
import argparse
import json
from .entity_lookup import INDEX_NAME, IDENTITY
LABELS = tuple(IDENTITY['public_labels'])

PROPERTIES = tuple(dict.fromkeys([*IDENTITY['name_fields'], IDENTITY['synonym_field']]))


def ensure_index(session, create=False):
    rows = session.run('SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties RETURN name,type,state,labelsOrTypes,properties').data()
    matching = [r for r in rows if r['name'] == INDEX_NAME]
    if matching:
        index = matching[0]
        if index['type'] != 'FULLTEXT' or set(index['labelsOrTypes']) != set(LABELS) or set(index['properties']) != set(PROPERTIES):
            raise ValueError('existing_entity_index_incompatible')
        return {'created': False, 'index': index}
    if not create: return {'created': False, 'missing': INDEX_NAME, 'indexes': rows}
    labels = '|'.join('`'+label+'`' for label in LABELS)
    properties = ', '.join('n.'+field for field in PROPERTIES)
    session.run(f'CREATE FULLTEXT INDEX `{INDEX_NAME}` IF NOT EXISTS FOR (n:{labels}) ON EACH [{properties}] '
                "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-no-stop-words', `fulltext.eventually_consistent`: false}}").consume()
    session.run('CALL db.awaitIndex($name, 60)', {'name': INDEX_NAME}).consume()
    return {'created': True, 'name': INDEX_NAME, 'state': 'ONLINE'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--create', action='store_true')
    args = parser.parse_args()
    from .config import Settings
    from neo4j import GraphDatabase
    settings = Settings()
    with GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)) as driver:
        with driver.session(database=settings.neo4j_database) as session:
            print(json.dumps(ensure_index(session, create=args.create)))

if __name__ == '__main__': main()
