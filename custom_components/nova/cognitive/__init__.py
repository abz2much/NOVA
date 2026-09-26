"""Nova's cognitive architecture: how a household event becomes a decision.

The package separates the stages a proactive decision passes through:

  models        typed, immutable evidence, snapshots, decisions and provider
                outcomes (stdlib only)
  evaluators    pure deterministic rules: critical hazards, security and
                entry, presence, repetition, relevance templates, the Local
                Mind core and delivery rules
  arbitration   explicit priority order; critical safety always wins
  provider      validation of a provider's reply into a decision or a failure
  cache_policy  which decisions may be learned into the reasoning cache
  patterns      deterministic routine evidence and scoring
  presentation  the words a person hears or reads
  coordinator   the one place that collects a snapshot, runs the stages in
                order and applies permitted effects

Everything but the coordinator is pure: no Home Assistant access, no
database, no provider, no service call, no speech, no scheduling, no cache
write and no module-level mutation. tests/unit/test_cognitive_structure.py
enforces that. reasoning_loop and local_mind remain the public entry points
and delegate here.
"""
