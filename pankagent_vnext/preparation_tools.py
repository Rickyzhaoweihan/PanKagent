"""Registered preparation tools. Schema chooses tested operators, never code."""
from .agent_schemas import module


def prepare_scope(question, grounding, proposal):
    from .cohort_plan_scope import compile_scope as cohort
    from .planning_compile import compile_property_owners
    from .independent_checks import split
    from .coloc_tissue_scope import compile_scope as coloc
    from .planning_requirements import compile_requested_scope
    from .genomic_scope import compile_genomic_scope
    from .dependency_scope import compile_inputs

    def single(function):
        return lambda p: (function(p), None)
    operators = {
        'cohort_scope': lambda p: cohort(question, grounding, p),
        'property_owners': lambda p: compile_property_owners(p, grounding, question=question),
        'independent_measurements': single(lambda p: split(p, question)),
        'coloc_tissue': lambda p: coloc(question, grounding, p),
        'requested_scope': lambda p: compile_requested_scope(question, grounding, p),
        'genomic_scope': lambda p: compile_genomic_scope(question, grounding, p),
        'dependency_bindings': single(compile_inputs),
    }
    for rule in module('validation_repair')['scope_compilers']:
        try:
            proposal, issue = operators[rule](proposal)
        except ValueError as exc:
            issue = str(exc)
        if issue:
            return proposal, issue
        proposal.setdefault('selected_rule_ids', []).append(rule)
    return proposal, None
