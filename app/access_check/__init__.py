"""Effective-access snapshot and comparison, used to check a schema revision before it commits.

``snapshot.capture`` records who can reach what, as the running code computes it. ``compare`` lists every difference
between two snapshots. ``guard.run_guarded`` applies a revision in one transaction and refuses it, rolling back,
if access differs in a way the revision did not declare.
"""
