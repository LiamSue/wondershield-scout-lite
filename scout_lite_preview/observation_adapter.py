"""Internal structured candidate: reject synthetic media observations."""
from .sources import Fault, need

MODE = 'synthetic_observations_disabled'


def validate_config(config):
    need(config.get('synthetic_observations') is None, 'INVALID_REQUEST', 'SYNTHETIC_OBSERVATIONS_NOT_INCLUDED')
    return None


def configured(profile, metadata):
    return False


def expected_scope(profile, request, systems, tenant):
    return {}


def units(raw, metadata, profile):
    raise Fault('UNSUPPORTED', 'SYNTHETIC_OBSERVATIONS_NOT_INCLUDED')


def annotate_evidence(evidence, unit):
    return evidence


def descriptor(profile, runtime):
    raise Fault('UNSUPPORTED', 'SYNTHETIC_OBSERVATIONS_NOT_INCLUDED')


def overlay(out, scope, good, observed, unresolved, has_gaps, enabled, mode, profile, runtime):
    need(not enabled and not mode, 'INVALID_REQUEST', 'SYNTHETIC_OBSERVATIONS_NOT_INCLUDED')
    return out
