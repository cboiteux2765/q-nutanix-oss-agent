"""Small read-only product tools, shared by local and Prime RLM specialists."""
import copy


CATALOG = {
    'evidence_search': ('Find pinned source excerpts. query is a literal substring; empty lists sources.', {'research', 'scenarios', 'baseline', 'candidate', 'critic'}),
    'feedback_memory': ('Read recent human feedback as suggestions, never product evidence. query must be empty.', {'prompt'}),
    'decision_register': ('Read unresolved product decisions. query must be empty.', {'baseline', 'candidate', 'critic'}),
    'requirement_trace': ('Map requirements to source IDs and acceptance tests for both drafts. query must be empty.', {'critic'}),
}


class SpecTools:
    def __init__(self, role, context, record):
        self.role, self.context, self.record = role, copy.deepcopy(context), record

    def catalog(self):
        return [{'name': name, 'description': description, 'arguments': {'query': 'string'}}
                for name, (description, roles) in CATALOG.items() if self.role in roles]

    def call(self, name, query=''):
        if name not in CATALOG or self.role not in CATALOG[name][1]:
            raise ValueError('Tool is not available to this specialist.')
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError('Tool query must be a string of at most 200 characters.')
        if name != 'evidence_search' and query:
            raise ValueError('This tool requires an empty query.')
        if name == 'evidence_search':
            matches = [s for s in self.context.get('sources', []) if query.casefold() in s['text'].casefold()]
            result = {'sources': [{**s, 'text': s['text'][:2000], 'truncated': len(s['text']) > 2000} for s in matches[:8]],
                      'total_matches': len(matches), 'scope': 'Pinned snapshot only; not a repository-wide search.'}
        elif name == 'feedback_memory':
            result = {'suggestions': self.context.get('feedback', [])[-20:], 'authority': 'Unverified reviewer suggestions'}
        elif name == 'decision_register':
            result = {'open_decisions': self.context.get('product_analysis', {}).get('open_decisions', [])}
        else:
            result = {}
            for label in ('baseline', 'candidate'):
                draft = self.context.get(label, {})
                tests = {t['id']: t['expected_behavior'] for t in draft.get('acceptance_tests', [])}
                result[label] = [{'requirement': r['id'], 'source_ids': r['source_ids'],
                                  'tests': {t: tests.get(t) for t in r['test_ids']}}
                                 for r in draft.get('requirements', [])]
        self.record({'role': self.role, 'tool': name, 'query': query, 'result': result})
        return copy.deepcopy(result)
